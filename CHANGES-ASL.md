# ASL bridge changes: USRP playout pacing and underrun handling

This branch fixes audio dropouts when Zello_Bridge drives an AllStarLink
`chan_usrp` channel (`Node 1001` in our setup). All numbers below come from
`tcpdump` captures on both ends of a real deployment.

## Symptoms

During a single 68 s Zello transmission:

| Metric | Before |
|---|---|
| Receiver carrier drops (ASL `RPT_RXKEYED` 1→0→1) | 41 (13.0 s of 67 s) |
| Audio discarded by `chan_usrp` queue flush | 22 % |
| Audio that reached the radio | 78 % |
| TX packet spacing | median **0.0 ms**, max **973 ms**, 164 gaps > 80 ms |
| Reference: ASL → Zello direction (`chan_usrp` writes) | median 20.00 ms, 0 gaps > 80 ms |

## Why

`chan_usrp.c` (AllStarLink/app_rpt) assumes a steady 20 ms voice stream:

```c
#define MAX_RXKEY_TIME 4                /* 4 x 20ms write cycles = 80ms */
#define QUEUE_OVERLOAD_THRESHOLD 25     /* frames */
```

* `rxkey` counts down once per write cycle; the channel **unkeys after ~80 ms
  without a 320-byte voice frame**.
* More than 25 queued frames → **the whole queue is flushed** (up to 500 ms lost).

Zello's Channel API delivers audio in **60 ms packets** (3 × 20 ms Opus frames).
`AsyncByteStream.read(n)` returns as soon as any data is available, so `run_tx()`
sent all three frames back-to-back and then blocked until the next message.

Separately, the Zello server itself delivers with 300–1600 ms holes. Verified at
the TCP layer: **0 retransmissions** and the socket `Recv-Q` stayed at 0
(771 samples, 2 non-zero) — i.e. server-side, not network and not client-side.
Those holes were previously passed straight through to `chan_usrp`.

## What this branch changes

1. `stream.py` — add `pending()` and `discard()` (additive; `read()`/`write()`
   behaviour unchanged).
2. `jitter.py` (new) — `PlayoutPolicy`: target depth, hard latency bound, tail
   drain budget, per-transmission statistics.
3. `usrp.py` `run_tx()` —
   * emit exactly one 320-byte frame per 20 ms on a monotonic clock;
   * on underrun emit a **silence frame** instead of stopping, so the receiver
     stays keyed for the whole transmission (this mirrors the official client,
     where the Web Audio timeline re-anchors instead of stopping —
     see `zelloptt/zello-channel-api`, `PCMPlayer.ts`);
   * drop the oldest audio only past `JITTER_MAX_LATENCY_MS`;
   * on PTT release, drain the remaining tail (bounded) before sending unkey,
     so the last word is not cut.

### Tuning

| Env var | Default | Notes |
|---|---|---|
| `JITTER_TARGET_MS` | 250 | audio kept when the latency bound is hit |
| `JITTER_MAX_LATENCY_MS` | 2000 | hard bound; measured max stall is 1184 ms |
| `JITTER_TAIL_DRAIN_MS` | 1200 | tail drained after PTT release |

A *smaller* bound makes things worse, which is worth knowing before tuning it
down. Offline replay of a real arrival timeline (3345 frames / 66.9 s):

| Latency bound | Audio played | Silence fill | Dropped | Final latency |
|---|---|---|---|---|
| 700 ms | 60.8 s | 6.5 s | **9 %** | 152 ms |
| 1200 ms | 66.9 s (all) | 0.9 s | 0 % | 595 ms |
| 3000 ms | 66.9 s (all) | 0.9 s | 0 % | 596 ms |

Dropping removes the very buffer that would absorb the next stall, so the
silence fill grew 7× when the bound was tightened to 700 ms.

## Result (same test rig, 71.9 s transmission)

| Metric | Before | After |
|---|---|---|
| Packet spacing median | 0.0 ms | **20.18 ms** (σ 0.85 ms) |
| Gaps > 80 ms | 164 | **0** |
| Receiver carrier drops | 41 | **0** |
| Audio delivered | 78 % | **100 %** (3528/3528 frames) |
| Silence fill / added latency | — | 1.5 s (1.9 %) / ~600 ms |

`RPT_RXKEYED` sampled at 1 Hz stayed at `1` for 72 consecutive seconds with no
mid-transmission drop, while the Zello side still showed 44 gaps > 300 ms
(max 1619 ms) during the same window.

## Testing

* `test_jitter_offline.py` (in the deployment workspace) drives the real
  `run_tx()` with a stub transport and a recorded arrival timeline; asserts
  0 gaps > 80 ms and 0 carrier drops.
* Live verification: `tcpdump` on both ends plus 1 Hz `rpt show variables`
  sampling on the AllStarLink node.

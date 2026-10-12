"""
Zello → AllStarLink direction playout policy.

Design basis: Official Zello desktop client (see zello-reverse/FINDINGS.md)
  * Audio is buffered in advance and then broadcast at a strict 20ms rhythm
  * **The playback timeline never stops**: When the buffer is empty, the mute box is sent to keep chan_usrp keyed.
  (chan_usrp: MAX_RXKEY_TIME=4 → 80ms, if the 320B voice box is not received, unkey;
    QUEUE_OVERLOAD_THRESHOLD=25 → The queue explodes and the entire batch is cleared)
  * Bounded latency: discard the **oldest** (replaced by silence) messages when MAX_LATENCY is exceeded

Actual measurement basis (2026-10-09, TCP 443 packet-by-packet analysis):
  The Zello server sends messages in 60ms intervals, but there will be a 300–1200ms window;
  TCP retransmission 0 times, socket Recv-Q is almost always 0 → the server did not send it.
  It's not a problem on our side or on the network. Therefore the bridge must absorb these empty windows rather than passing them to the radio.

Latency vs. completeness trade-off (chosen by this implementation):
  The radio is half-duplex and immediate, so it adopts "bounded delay":
  - Complement the silence during the pause → the carrier is not interrupted (the audience hears the brief silence instead of dropping the carrier)
  - Drop oldest messages when latency exceeds MAX_LATENCY → Latency does not accumulate with conversation length
  - Empty tail first when releasing PTT (TAIL_DRAIN) → will not cut off the last sentence
  All three parameters can be adjusted by environment variables without changing the program.
"""

import os
import time

FRAME_BYTES = 320  # 20ms slin 8kHz 16-bit mono
FRAME_INTERVAL_S = 0.02


def _env(name, default):
	try:
		return float(os.environ.get(name, default))
	except (TypeError, ValueError):
		return float(default)


def _ms_to_bytes(ms):
	return max(FRAME_BYTES, int(ms / 20.0) * FRAME_BYTES)


class PlayoutPolicy:
	"""Playback strategy and statistics during a single session (pure data, no I/O)."""

	def __init__(self, logger=None):
		self._logger = logger
		# 2026-10-09 Offline measurement (real pcap arrival timeline) determines these three default values:
		# Upper limit 700ms → discard 9% voice, mute 6.5s (discarding itself will create more starvation)
		# Upper limit 1200ms → discard 0%, mute 0.9s, final delay 596ms
		# Upper limit 3000ms → Same result as 1200ms
		# Therefore, 2000ms is taken (covering the measured maximum lag of 1184ms and leaving a margin). It will not be triggered under normal operation.
		self.target_bytes = _ms_to_bytes(_env('JITTER_TARGET_MS', 250))
		self.max_latency_bytes = _ms_to_bytes(_env('JITTER_MAX_LATENCY_MS', 2000))
		self.tail_drain_bytes = _ms_to_bytes(_env('JITTER_TAIL_DRAIN_MS', 1200))
		if self.max_latency_bytes < self.target_bytes:
			self.max_latency_bytes = self.target_bytes
		self.reset_message()

	# ------------------------------------------------------------------ life cycle
	def reset_message(self):
		self.played_frames = 0
		self.silence_frames = 0
		self.dropped_bytes = 0
		self.drop_events = 0
		self.tail_frames = 0
		self.starve_runs = 0
		self._in_starve = False

	# ------------------------------------------------------------------ event return
	def note_played(self, tail=False):
		self.played_frames += 1
		if tail:
			self.tail_frames += 1
		self._in_starve = False

	def note_starved(self):
		self.silence_frames += 1
		if not self._in_starve:
			self._in_starve = True
			self.starve_runs += 1
			if self._logger is not None:
				self._logger.info('Playout underrun #%d (silence fill)' % self.starve_runs)

	def note_dropped(self, nbytes):
		if nbytes:
			self.dropped_bytes += nbytes
			self.drop_events += 1

	# ------------------------------------------------------------------ statistics
	@property
	def latency_ms(self):
		"""The amount of latency currently represented by the buffer (for testing/logging purposes)."""
		return self.silence_frames * FRAME_INTERVAL_S * 1000.0

	def summary(self):
		return 'played=%d(%.1fs) tail=%d silence=%.1fs/%drun dropped=%dB/%devent' % (
			self.played_frames,
			self.played_frames * FRAME_INTERVAL_S,
			self.tail_frames,
			self.silence_frames * FRAME_INTERVAL_S,
			self.starve_runs,
			self.dropped_bytes,
			self.drop_events,
		)

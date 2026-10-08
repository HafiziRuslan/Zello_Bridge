"""
Zello → AllStarLink 方向的播放策略（playout policy）。

設計依據：官方 Zello 桌面版用戶端（見 zello-reverse/FINDINGS.md）
  * 音訊先進緩衝，再以嚴格的 20ms 節奏播出
  * **播放時間軸永不停止**：緩衝空時改送靜音框，讓 chan_usrp 保持 keyed
    （chan_usrp：MAX_RXKEY_TIME=4 → 80ms 沒收到 320B 語音框就 unkey；
      QUEUE_OVERLOAD_THRESHOLD=25 → 佇列爆掉整批清空）
  * 延遲有界：超過 MAX_LATENCY 時丟棄**最舊**（已被靜音取代）的音訊

實測依據（2026-10-09，TCP 443 逐包分析）：
  Zello 伺服器以 60ms 為單位送音訊，但會出現 300–1200ms 的空窗；
  TCP 重傳 0 次、socket Recv-Q 幾乎恆為 0 → 是伺服器端沒送，
  不是我方或網路問題。因此 bridge 必須吸收這些空窗，而不是把它們傳給無線電端。

延遲 vs 完整度的取捨（本實作選擇）：
  無線電是半雙工、即時性優先，因此採「有界延遲」：
    - 停頓期間補靜音 → carrier 不中斷（聽眾聽到短暫靜音，而非掉 carrier）
    - 延遲超過 MAX_LATENCY 時丟棄最舊音訊 → 延遲不會隨發話長度累積
    - 放開 PTT 時先排空尾巴（TAIL_DRAIN）→ 不會砍掉最後一句話
  三個參數皆可用環境變數調整，無需改程式。
"""

import os
import time

FRAME_BYTES = 320            # 20ms slin 8kHz 16-bit mono
FRAME_INTERVAL_S = 0.02


def _env(name, default):
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return float(default)


def _ms_to_bytes(ms):
    return max(FRAME_BYTES, int(ms / 20.0) * FRAME_BYTES)


class PlayoutPolicy:
    """單次發話期間的播放策略與統計（純資料，不碰 I/O）。"""

    def __init__(self, logger=None):
        self._logger = logger
        # 2026-10-09 離線實測（真實 pcap 到貨時間軸）決定這三個預設值：
        #   上限 700ms  → 丟棄 9% 語音、靜音 6.5s（丟棄本身會製造更多 starvation）
        #   上限 1200ms → 丟棄 0%、靜音 0.9s、最終延遲 596ms
        #   上限 3000ms → 與 1200ms 結果相同
        # 故取 2000ms（涵蓋實測最大卡頓 1184ms 並留餘裕），正常運作下不會觸發。
        self.target_bytes = _ms_to_bytes(_env('JITTER_TARGET_MS', 250))
        self.max_latency_bytes = _ms_to_bytes(_env('JITTER_MAX_LATENCY_MS', 2000))
        self.tail_drain_bytes = _ms_to_bytes(_env('JITTER_TAIL_DRAIN_MS', 1200))
        if self.max_latency_bytes < self.target_bytes:
            self.max_latency_bytes = self.target_bytes
        self.reset_message()

    # ------------------------------------------------------------------ 生命週期
    def reset_message(self):
        self.played_frames = 0
        self.silence_frames = 0
        self.dropped_bytes = 0
        self.drop_events = 0
        self.tail_frames = 0
        self.starve_runs = 0
        self._in_starve = False

    # ------------------------------------------------------------------ 事件回報
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

    # ------------------------------------------------------------------ 統計
    @property
    def latency_ms(self):
        """目前緩衝代表的延遲量（供測試/日誌用）。"""
        return self.silence_frames * FRAME_INTERVAL_S * 1000.0

    def summary(self):
        return ('played=%d(%.1fs) tail=%d silence=%.1fs/%drun dropped=%dB/%devent'
                % (self.played_frames, self.played_frames * FRAME_INTERVAL_S,
                   self.tail_frames, self.silence_frames * FRAME_INTERVAL_S,
                   self.starve_runs, self.dropped_bytes, self.drop_events))

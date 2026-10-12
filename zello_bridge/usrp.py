import asyncio
import logging
import math
import os
import struct
from .stream import AsyncByteStream
from .jitter import PlayoutPolicy

USRP_FRAME_SIZE = 352
USRP_HEADER_SIZE = 32
USRP_VOICE_SIZE = USRP_FRAME_SIZE - USRP_HEADER_SIZE
USRP_FRAME_INTERVAL = 0.02                 # 20ms slin frame (160 samples @ 8kHz)
SILENCE_PCM = bytes(USRP_VOICE_SIZE)   # 20ms of slin silence: keeps chan_usrp keyed on underrun
USRP_GAIN_RX_DB = int(os.environ.get('USRP_GAIN_RX_DB', 0))
USRP_GAIN_TX_DB = int(os.environ.get('USRP_GAIN_TX_DB', 0))
USRP_TYPE_VOICE = 0


def db_to_linear(db):
	return math.pow(10, db / 10)


def apply_gain(buf, gain):
	# USRP is PCM @ 8kHz, 16 bit signed. We should only ever get 20ms chunks,
	# but safer not to assume so.
	format = f'{int(len(buf) / 2)}h'
	pre_gain = struct.unpack(format, buf)
	return struct.pack(format, *[clamp_short(gain * b) for b in pre_gain])


def clamp_short(sh):
	return int(max(-32768, min(32767, sh)))


class USRPController(asyncio.DatagramProtocol):
	def __init__(self, stream_in: AsyncByteStream, stream_out: AsyncByteStream, usrp_ptt: asyncio.Event, zello_ptt: asyncio.Event):
		self._stream_in = stream_in
		self._stream_out = stream_out
		self._tx_seq = 0
		self._tx_seq_lock = asyncio.Lock()
		self._tx_address = os.environ.get('USRP_HOST')
		self._tx_port = int(os.environ.get('USRP_TXPORT', 7070))
		self._transport = None
		self._usrp_ptt = usrp_ptt
		self._zello_ptt = zello_ptt
		self._usrp_gain_rx = db_to_linear(USRP_GAIN_RX_DB)
		self._usrp_gain_tx = db_to_linear(USRP_GAIN_TX_DB)
		self._logger = logging.getLogger('USRPController')
		self._logger.info(f'USRP RX gain: {USRP_GAIN_RX_DB}dB = {self._usrp_gain_rx}')
		self._logger.info(f'USRP TX gain: {USRP_GAIN_TX_DB}dB = {self._usrp_gain_tx}')

	def connection_made(self, transport):
		self._transport = transport

	def datagram_received(self, data, addr):
		ptt = self._frame_ptt_state(data)
		if not ptt:
			self._usrp_ptt.clear()
			return
		self._usrp_ptt.set()
		loop = asyncio.get_running_loop()
		frame = data[USRP_HEADER_SIZE:]
		if self._usrp_gain_rx != 1:
			frame = apply_gain(frame, self._usrp_gain_rx)
		loop.create_task(self._stream_out.write(frame))

	async def run(self):
		# rx is handled by DatagramProtocol parent class
		await self.run_tx()

	async def _tx_encode_state(self, ptt=True):
		seq = await self._get_seq()
		return 'USRP'.encode('ascii') + struct.pack('>iiiiiii', seq, 0, ptt, 0, USRP_TYPE_VOICE, 0, 0)

	def _rx_decode_state(self, frame):
		header = frame[4:USRP_HEADER_SIZE]
		seq, mem, ptt, tg, type, mpx, res = struct.unpack('>iiiiiii', header)
		return (seq, mem, ptt, tg, type, mpx, res)

	def _frame_ptt_state(self, frame):
		state = self._rx_decode_state(frame)
		return state[2] == 1

	async def _get_seq(self):
		async with self._tx_seq_lock:
			self._tx_seq = self._tx_seq + 1
			return self._tx_seq

	async def _tx_frame(self, pcm):
		header = await self._tx_encode_state(ptt=True)
		if self._usrp_gain_tx != 1:
			pcm = apply_gain(pcm, self._usrp_gain_tx)
		frame = header + pcm
		self._tx(frame)

	async def _tx_off(self):
		frame = await self._tx_encode_state(ptt=False)
		self._tx(frame)

	def _tx(self, frame):
		if self._transport is not None:
			self._transport.sendto(frame, (self._tx_address, self._tx_port))

	async def _tx_silence(self):
		# 播放時間軸不中斷：緩衝空時補靜音，避免 chan_usrp 在發話中途 unkey
		await self._tx_frame(SILENCE_PCM)

	async def run_tx(self):
		loop = asyncio.get_running_loop()
		policy = PlayoutPolicy(self._logger)
		next_at = None
		started = False
		while True:
			if not self._zello_ptt.is_set():
				# 先排空尾巴，避免放開 PTT 時砍掉最後一句話
				if started:
					drained = 0
					while drained < policy.tail_drain_bytes:
						if await self._stream_in.pending() < USRP_VOICE_SIZE:
							break
						pcm = await self._stream_in.read(USRP_VOICE_SIZE)
						if len(pcm) != USRP_VOICE_SIZE:
							break
						policy.note_played(tail=True)
						await self._tx_frame(pcm)
						drained += USRP_VOICE_SIZE
						await asyncio.sleep(USRP_FRAME_INTERVAL)
					flushed = await self._stream_in.discard()
					self._logger.info(f'Playout done: {policy.summary()} flushed={flushed}B')
					next_at = None
					started = False
					policy.reset_message()
				await self._tx_off()
				await self._zello_ptt.wait()
				continue

			# 本輪第一幀音訊到達前不送任何東西
			if not started:
				if await self._stream_in.pending() < USRP_VOICE_SIZE:
					await asyncio.sleep(USRP_FRAME_INTERVAL)
					continue
				started = True
				next_at = loop.time()

			now = loop.time()
			if next_at > now:
				await asyncio.sleep(next_at - now)
			elif now - next_at > 0.5:
				next_at = now
			next_at += USRP_FRAME_INTERVAL

			pending = await self._stream_in.pending()
			if pending > policy.max_latency_bytes:
				dropped = await self._stream_in.discard(keep_bytes=policy.target_bytes)
				policy.note_dropped(dropped)
				self._logger.warning(
					f'Playout latency bound hit, dropped {dropped} bytes of stale audio')
				pending = await self._stream_in.pending()

			if pending >= USRP_VOICE_SIZE:
				pcm = await self._stream_in.read(USRP_VOICE_SIZE)
				if len(pcm) == USRP_VOICE_SIZE:
					policy.note_played()
					await self._tx_frame(pcm)
					continue

			policy.note_starved()
			await self._tx_silence()

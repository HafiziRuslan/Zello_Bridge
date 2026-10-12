import io
import asyncio


class AsyncByteStream:
	def __init__(self):
		self._buffer = io.BytesIO()
		self._data_available = asyncio.Event()
		self._lock = asyncio.Lock()

	async def write(self, data: bytes):
		async with self._lock:
			self._buffer.write(data)
			self._data_available.set()

	async def read(self, n: int = -1) -> bytes:
		while True:
			async with self._lock:
				self._buffer.seek(0)
				data = self._buffer.read(n)
				remaining_data = self._buffer.read()
				self._buffer = io.BytesIO()
				if len(remaining_data) > 0:
					self._buffer.write(remaining_data)
				else:
					self._data_available.clear()
				if data:
					return data
			await self._data_available.wait()

	async def pending(self) -> int:
		"""回傳緩衝區中尚未被讀取的位元組數。"""
		async with self._lock:
			return len(self._buffer.getvalue())

	async def discard(self, keep_bytes: int = 0) -> int:
		"""丟棄緩衝內容，只保留最後 keep_bytes 位元組；回傳被丟棄的位元組數。"""
		async with self._lock:
			self._buffer.seek(0)
			data = self._buffer.read()
			if keep_bytes > 0 and len(data) > keep_bytes:
				kept = data[-keep_bytes:]
			else:
				kept = data
			self._buffer = io.BytesIO()
			if kept:
				self._buffer.write(kept)
				self._data_available.set()
			else:
				self._data_available.clear()
			return len(data) - len(kept)

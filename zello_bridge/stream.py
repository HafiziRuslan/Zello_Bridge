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
		"""Returns the number of bytes in the buffer that have not yet been read."""
		async with self._lock:
			return len(self._buffer.getvalue())

	async def discard(self, keep_bytes: int = 0) -> int:
		"""Discard the buffer contents, retaining only the last keep_bytes bytes; return the number of discarded bytes."""
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

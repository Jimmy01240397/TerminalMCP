"""Byte-oriented ring buffer with absolute-offset addressing."""
from __future__ import annotations


class RingBuffer:
    """Append-only byte buffer that drops the oldest bytes when capacity is exceeded.

    Reads are addressed by *absolute* offsets — i.e. the number of bytes that
    have ever been written to the buffer over its lifetime. The buffer
    remembers the absolute offset of the oldest byte it still holds, so
    callers can paginate correctly even after drops.
    """

    __slots__ = ("capacity", "_data", "_total_written")

    def __init__(self, capacity: int):
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self.capacity = capacity
        self._data = bytearray()
        self._total_written = 0

    def write(self, data: bytes) -> None:
        if not data:
            return
        self._data.extend(data)
        self._total_written += len(data)
        overflow = len(self._data) - self.capacity
        if overflow > 0:
            del self._data[:overflow]

    @property
    def total_written(self) -> int:
        return self._total_written

    @property
    def available_start(self) -> int:
        """Absolute offset of the oldest byte still in the buffer."""
        return self._total_written - len(self._data)

    def read(self, offset: int = 0, length: int | None = None) -> tuple[bytes, int]:
        """Return ``(content, content_offset)``.

        ``offset`` is absolute. If it precedes the oldest still-buffered byte,
        the returned content begins at the buffer's start. ``content_offset``
        tells the caller where the returned slice actually begins.

        ``length`` ``None`` or negative means "read to the end".
        """
        if offset < 0:
            offset = max(0, self._total_written + offset)
        actual_start = max(offset, self.available_start)
        if length is None or length < 0:
            end = self._total_written
        else:
            end = min(actual_start + length, self._total_written)
        if actual_start >= end:
            return b"", actual_start
        lo = actual_start - self.available_start
        hi = end - self.available_start
        return bytes(self._data[lo:hi]), actual_start

"""PTY-backed session: fork+exec a command; read from the PTY on demand.

No reader thread. The PTY kernel buffer is the natural "pending" — when an
agent calls `output()` we drain everything currently buffered, commit it
to history, and return it. If an agent never calls `output()`, the child
eventually blocks on its own `write(stdout)` once the PTY buffer fills;
that's deliberate backpressure rather than a leak.
"""
from __future__ import annotations

import errno
import fcntl
import os
import signal as signal_mod
import struct
import sys
import termios
import threading
import time
import uuid

from .buffer import RingBuffer

if sys.platform == "win32":  # pragma: no cover
    raise RuntimeError(
        "terminal-mcp uses POSIX PTYs and does not run on Windows. "
        "Deploy on Linux or macOS (e.g. in a Docker container)."
    )

import pty  # noqa: E402  — must come after the Windows guard


class Session:
    """One PTY-backed child process. Output is drained on demand by `output()`."""

    def __init__(
        self,
        cmd: str,
        buffer_capacity: int,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        rows: int = 24,
        cols: int = 80,
    ) -> None:
        self.id = uuid.uuid4().hex[:12]
        self.cmd = cmd
        self.history = RingBuffer(buffer_capacity)
        self.lock = threading.Lock()
        self.eof = False
        self.exit_code: int | None = None
        self.exit_signal: int | None = None
        self._fd_closed = False

        pid, fd = pty.fork()
        if pid == 0:
            # Child. Anything we raise/print here only reaches the parent via
            # the PTY, so keep it minimal.
            try:
                if cwd:
                    os.chdir(cwd)
                shell_env = dict(os.environ)
                if env:
                    shell_env.update(env)
                shell_env.setdefault("TERM", "xterm-256color")
                os.execvpe("/bin/sh", ["/bin/sh", "-c", cmd], shell_env)
            except Exception as exc:  # noqa: BLE001
                try:
                    os.write(2, f"terminal-mcp: exec failed: {exc}\n".encode())
                except OSError:
                    pass
                os._exit(127)

        self.pid = pid
        self.fd = fd

        # Non-blocking master fd: reads return whatever's currently in the
        # kernel buffer (or BlockingIOError); writes return partial on a
        # full input buffer rather than blocking the request handler.
        flags = fcntl.fcntl(self.fd, fcntl.F_GETFL)
        fcntl.fcntl(self.fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

        self._set_winsize(rows, cols)

    # ---- internal helpers (assume lock held) --------------------------------

    def _set_winsize(self, rows: int, cols: int) -> None:
        if self._fd_closed:
            return
        try:
            winsize = struct.pack("HHHH", rows, cols, 0, 0)
            fcntl.ioctl(self.fd, termios.TIOCSWINSZ, winsize)
        except OSError:
            pass

    def _drain_fd_locked(self) -> bytes:
        """Non-blocking drain of everything currently in the PTY master."""
        if self._fd_closed:
            return b""
        chunks: list[bytes] = []
        while True:
            try:
                chunk = os.read(self.fd, 4096)
            except BlockingIOError:
                break  # nothing left right now
            except OSError as exc:
                # EIO = child closed its end (slave); EBADF = master closed
                if exc.errno in (errno.EIO, errno.EBADF):
                    break
                raise
            if not chunk:  # genuine EOF
                break
            chunks.append(chunk)
        return b"".join(chunks)

    def _maybe_reap_locked(self) -> None:
        """Non-blocking waitpid; updates eof / exit_code / exit_signal."""
        if self.eof:
            return
        try:
            wpid, status = os.waitpid(self.pid, os.WNOHANG)
        except ChildProcessError:
            self.eof = True
            return
        if wpid == 0:
            return  # still running
        self.eof = True
        if os.WIFEXITED(status):
            self.exit_code = os.WEXITSTATUS(status)
        elif os.WIFSIGNALED(status):
            self.exit_signal = os.WTERMSIG(status)

    def _close_fd_locked(self) -> None:
        if not self._fd_closed:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self._fd_closed = True

    # ---- public API ---------------------------------------------------------

    def resize(self, rows: int, cols: int) -> None:
        with self.lock:
            self._set_winsize(rows, cols)

    def write(self, data: bytes) -> int:
        """Write to the child's stdin via the PTY master."""
        with self.lock:
            self._maybe_reap_locked()
            if self.eof or self._fd_closed:
                raise RuntimeError("session has exited")
            total = 0
            view = memoryview(data)
            while total < len(view):
                try:
                    n = os.write(self.fd, view[total:])
                except BlockingIOError:
                    break  # input buffer full; return partial — agent can retry
                except OSError as exc:
                    raise RuntimeError(f"write failed: {exc}") from exc
                if n <= 0:
                    break
                total += n
            return total

    def drain_and_record(self) -> bytes:
        """Drain whatever's in the PTY → commit to history → return it."""
        with self.lock:
            data = self._drain_fd_locked()
            if data:
                self.history.write(data)
            self._maybe_reap_locked()
            return data

    def read_history(
        self, offset: int = 0, length: int | None = None
    ) -> tuple[bytes, int, int, int]:
        """Return ``(content, content_offset, total_length, buffer_start)``."""
        with self.lock:
            self._maybe_reap_locked()
            content, content_offset = self.history.read(offset, length)
            return (
                content,
                content_offset,
                self.history.total_written,
                self.history.available_start,
            )

    def refresh(self) -> None:
        """Just update eof / exit_code without draining."""
        with self.lock:
            self._maybe_reap_locked()

    def signal(self, sig: int) -> None:
        try:
            os.kill(self.pid, sig)
        except ProcessLookupError:
            pass

    def kill(self) -> None:
        self.signal(signal_mod.SIGKILL)

    def close(self) -> None:
        """Force-kill the child, reap it, and release the master fd."""
        with self.lock:
            already_reaped = self.eof
        if not already_reaped:
            self.kill()
            try:
                os.waitpid(self.pid, 0)  # SIGKILL is uncatchable → returns quickly
            except ChildProcessError:
                pass
        with self.lock:
            self.eof = True
            # Drain any final bytes the kernel still has buffered.
            tail = self._drain_fd_locked()
            if tail:
                self.history.write(tail)
            self._close_fd_locked()

    @property
    def is_alive(self) -> bool:
        # Reflects the last reap. Call refresh() / drain_and_record() / read_history()
        # to update before reading.
        return not self.eof

    def __del__(self) -> None:  # pragma: no cover — best-effort cleanup
        try:
            if not self._fd_closed:
                os.close(self.fd)
        except Exception:
            pass

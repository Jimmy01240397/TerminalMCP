"""PTY-backed session: fork+exec a command and stream output into a ring buffer."""
from __future__ import annotations

import errno
import fcntl
import os
import signal as signal_mod
import struct
import sys
import termios
import threading
import uuid

from .buffer import RingBuffer

if sys.platform == "win32":  # pragma: no cover
    raise RuntimeError(
        "terminal-mcp uses POSIX PTYs and does not run on Windows. "
        "Deploy on Linux or macOS (e.g. in a Docker container)."
    )

import pty  # noqa: E402  — must come after the Windows guard


class Session:
    """One PTY-backed child process and its merged stdout/stderr stream."""

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
        self.pending = bytearray()
        self.lock = threading.Lock()
        self.eof = False
        self.exit_code: int | None = None
        self.exit_signal: int | None = None

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
        self._set_winsize(rows, cols)

        self._reader = threading.Thread(
            target=self._read_loop, name=f"pty-reader-{self.id}", daemon=True
        )
        self._reader.start()

    def _set_winsize(self, rows: int, cols: int) -> None:
        try:
            winsize = struct.pack("HHHH", rows, cols, 0, 0)
            fcntl.ioctl(self.fd, termios.TIOCSWINSZ, winsize)
        except OSError:
            pass

    def resize(self, rows: int, cols: int) -> None:
        self._set_winsize(rows, cols)

    def _read_loop(self) -> None:
        try:
            while True:
                try:
                    chunk = os.read(self.fd, 4096)
                except OSError as exc:
                    # EIO on Linux when the child closes the slave end.
                    if exc.errno in (errno.EIO, errno.EBADF):
                        break
                    continue
                if not chunk:
                    break
                with self.lock:
                    self.history.write(chunk)
                    self.pending.extend(chunk)
        finally:
            self._reap()
            try:
                os.close(self.fd)
            except OSError:
                pass

    def _reap(self) -> None:
        try:
            _, status = os.waitpid(self.pid, 0)
        except ChildProcessError:
            with self.lock:
                self.eof = True
            return
        with self.lock:
            self.eof = True
            if os.WIFEXITED(status):
                self.exit_code = os.WEXITSTATUS(status)
            elif os.WIFSIGNALED(status):
                self.exit_signal = os.WTERMSIG(status)

    def write(self, data: bytes) -> int:
        if self.eof:
            raise RuntimeError("session has exited")
        total = 0
        view = memoryview(data)
        while total < len(view):
            try:
                n = os.write(self.fd, view[total:])
            except OSError as exc:
                raise RuntimeError(f"write failed: {exc}") from exc
            if n <= 0:
                break
            total += n
        return total

    def take_pending(self) -> bytes:
        with self.lock:
            data = bytes(self.pending)
            self.pending.clear()
            return data

    def read_history(
        self, offset: int = 0, length: int | None = None
    ) -> tuple[bytes, int, int, int]:
        """Return ``(content, content_offset, total_length, buffer_start)``."""
        with self.lock:
            content, content_offset = self.history.read(offset, length)
            return (
                content,
                content_offset,
                self.history.total_written,
                self.history.available_start,
            )

    def signal(self, sig: int) -> None:
        try:
            os.kill(self.pid, sig)
        except ProcessLookupError:
            pass

    def kill(self) -> None:
        self.signal(signal_mod.SIGKILL)

    @property
    def is_alive(self) -> bool:
        return not self.eof

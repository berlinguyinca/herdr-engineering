"""Terminal relay core: one child on a real pty, its output kept as numbered base64 chunks.

The app attaches by cursor: ``chunks_after(seq)`` returns everything newer than ``seq``, so a
reconnecting client repaints from the buffer instead of losing the screen. The buffer is capped;
a cursor that fell behind the cap gets its first chunk flagged ``reset`` so the app clears the
screen before painting. The child's argv always comes from :func:`attach_argv`, which validates
the session record through ``attach_commands`` and never builds a command from request text.
"""
from __future__ import annotations

import base64
import collections
import fcntl
import os
import select
import shutil
import signal
import struct
import termios
import threading

from .terminal_attach import attach_commands

_READ = 65536


def attach_argv(session, *, local_host, hosts=()):
    """argv that attaches to the session's pane: direct on its host, else ssh to it."""
    info = attach_commands(session, hosts=hosts)  # raises AttachRefused for dead/unsafe records
    host = info["host_id"]
    inner = ["herdr", "--session", info["herdr_session"], "agent", "attach", info["pane_id"]]
    if host == local_host:
        return inner
    # attach_commands already proved every field is quoting-free, so a plain join is exact.
    return ["ssh", "-tt", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", host,
            f"bash -lc '{' '.join(inner)}'"]


def _set_winsize(fd, rows, cols):
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def _check_size(rows, cols):
    for value, top in ((rows, 500), (cols, 1000)):
        if type(value) is not int or not 1 <= value <= top:
            raise ValueError("terminal size out of range")


class TerminalSession:
    """``buffer_bytes`` is a soft cap: the newest chunk is always kept, even if it alone exceeds it."""

    def __init__(self, argv, rows, cols, *, buffer_bytes=200_000):
        _check_size(rows, cols)
        self.argv = list(argv)
        self.rows, self.cols = rows, cols
        self._cap = buffer_bytes
        self._lock = threading.Lock()
        self._fdlock = threading.Lock()  # the reader closes the fd; writers must not race it
        self._chunks = collections.deque()  # (seq, raw bytes)
        self._size = 0
        self._seq = 0
        self._dropped = False  # something older than the first kept chunk was discarded
        self._ended = None  # {"seq", "ended", "exit"} once the child is gone
        self._pid = None
        self._fd = None
        self._wake = None  # (read, write) pipe that stops the reader on close()
        self._reader = None
        self._closed = False

    @property
    def alive(self):
        return self._pid is not None and self._ended is None and not self._closed

    def start(self):
        if self._pid is not None:
            raise RuntimeError("terminal session already started")
        exe = shutil.which(self.argv[0]) or self.argv[0]  # resolve in the parent
        wake_r, wake_w = os.pipe()
        # Size the pty before the fork so the program never starts at 0x0, and so a resize
        # that follows start() cannot be overwritten by the child's own setup.
        fd, slave = os.openpty()
        _set_winsize(slave, self.rows, self.cols)
        pid = os.fork()
        if pid == 0:  # child: login_tty, closerange, execv, _exit only; argv list, no shell
            try:
                os.login_tty(slave)  # setsid, controlling tty, stdio
                os.closerange(3, os.sysconf("SC_OPEN_MAX"))
                os.execv(exe, self.argv)
            finally:
                os._exit(127)
        os.close(slave)
        os.set_inheritable(fd, False)
        os.set_blocking(fd, False)
        self._pid, self._fd, self._wake = pid, fd, (wake_r, wake_w)
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _read_loop(self):
        fd, wake_r = self._fd, self._wake[0]
        while not self._closed:
            try:
                ready = select.select([fd, wake_r], [], [])[0]
            except (OSError, ValueError):
                break
            if wake_r in ready:
                break
            try:
                data = os.read(fd, _READ)
            except BlockingIOError:
                continue
            except OSError:  # EIO once the child closes its side of the pty
                break
            if not data:
                break
            with self._lock:
                self._seq += 1
                self._chunks.append((self._seq, data))
                self._size += len(data)
                while self._size > self._cap and len(self._chunks) > 1:
                    self._size -= len(self._chunks.popleft()[1])
                    self._dropped = True
        with self._fdlock:  # the reader owns the fd: nobody writes to it once it is closed
            self._fd = None
            os.close(fd)
        os.close(wake_r)  # close() owns the write end, so a late wake never hits a reused fd
        code = None
        try:
            _, status = os.waitpid(self._pid, 0)
            code = os.waitstatus_to_exitcode(status)
        except ChildProcessError:
            pass
        with self._lock:
            self._seq += 1
            self._ended = {"seq": self._seq, "ended": True, "exit": code}

    def write(self, data):
        while data and not self._closed:
            with self._fdlock:
                if self._fd is None:
                    return
                try:
                    data = data[os.write(self._fd, data):]
                    continue
                except BlockingIOError:
                    fd = self._fd
                except OSError:  # child went away mid-write; the ended chunk reports it
                    return
                select.select([], [fd], [], 0.05)  # pty full: wait briefly, holding the fd

    def resize(self, rows, cols):
        _check_size(rows, cols)
        self.rows, self.cols = rows, cols
        with self._fdlock:
            if self._fd is not None and not self._closed:
                try:
                    _set_winsize(self._fd, rows, cols)
                except OSError:
                    pass

    def snapshot_seq(self):
        with self._lock:
            return self._seq

    def chunks_after(self, seq):
        with self._lock:
            out = [{"seq": n, "data": base64.b64encode(raw).decode("ascii")}
                   for n, raw in self._chunks if n > seq]
            # Reset only when the cursor misses chunks that were dropped (seqs are contiguous).
            if out and self._dropped and self._chunks[0][0] > seq + 1:
                out[0]["reset"] = True
            if self._ended and self._ended["seq"] > seq:
                out.append(dict(self._ended))
            return out

    def close(self):
        if self._pid is None or self._closed:
            return
        self._closed = True
        try:
            os.killpg(self._pid, signal.SIGKILL)  # the child leads its own session and group
        except (ProcessLookupError, PermissionError):
            pass
        try:
            os.write(self._wake[1], b"x")
        except OSError:  # reader already finished and closed its end
            pass
        self._reader.join(timeout=2)
        os.close(self._wake[1])

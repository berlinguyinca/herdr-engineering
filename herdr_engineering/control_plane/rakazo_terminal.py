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
import pty
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


class TerminalSession:
    def __init__(self, argv, rows, cols, *, buffer_bytes=200_000):
        self.argv = list(argv)
        self.rows, self.cols = rows, cols
        self._cap = buffer_bytes
        self._lock = threading.Lock()
        self._chunks = collections.deque()  # (seq, raw bytes)
        self._size = 0
        self._seq = 0
        self._dropped = False  # something older than the first kept chunk was discarded
        self._ended = None  # {"seq", "ended", "exit"} once the child is gone
        self._pid = None
        self._fd = None
        self._reader = None
        self._closed = False

    @property
    def alive(self):
        return self._pid is not None and self._ended is None

    def start(self):
        pid, fd = pty.fork()
        if pid == 0:  # child: argv list only, no shell
            try:
                os.execvp(self.argv[0], self.argv)
            finally:
                os._exit(127)
        self._pid, self._fd = pid, fd
        _set_winsize(fd, self.rows, self.cols)
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _read_loop(self):
        while True:
            try:
                data = os.read(self._fd, _READ)
            except OSError:  # EIO once the child closes its side of the pty
                data = b""
            if not data:
                break
            with self._lock:
                self._seq += 1
                self._chunks.append((self._seq, data))
                self._size += len(data)
                while self._size > self._cap and len(self._chunks) > 1:
                    self._size -= len(self._chunks.popleft()[1])
                    self._dropped = True
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
        if not self.alive:
            return
        try:
            while data:
                data = data[os.write(self._fd, data):]
        except OSError:  # child went away mid-write; the ended chunk reports it
            pass

    def resize(self, rows, cols):
        self.rows, self.cols = rows, cols
        if self.alive:
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
            # Reset only when the cursor misses chunks that were dropped.
            if out and self._dropped and out[0]["seq"] > seq + 1 and self._chunks[0][0] > seq + 1:
                out[0]["reset"] = True
            if self._ended and self._ended["seq"] > seq:
                out.append(dict(self._ended))
            return out

    def close(self):
        if self._pid is None or self._closed:
            return
        self._closed = True
        try:
            os.kill(self._pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        if self._reader:
            self._reader.join(timeout=2)  # the reader reaps the child
        try:
            os.close(self._fd)
        except OSError:
            pass

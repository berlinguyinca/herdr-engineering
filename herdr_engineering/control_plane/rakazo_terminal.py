"""Terminal relay core: one child on a real pty, its output kept as numbered base64 chunks.

The app attaches by cursor: ``chunks_after(seq)`` returns everything newer than ``seq``, so a
reconnecting client repaints from the buffer instead of losing the screen. The buffer is capped;
a cursor that fell behind the cap gets its first chunk flagged ``reset`` so the app clears the
screen before painting. The child's argv always comes from :func:`attach_argv`, which validates
the session record through ``attach_commands`` and never builds a command from request text.
"""
from __future__ import annotations

import base64
import binascii
import collections
import fcntl
import os
import secrets
import select
import shutil
import signal
import socket
import struct
import termios
import threading
import time

from .terminal_attach import AttachRefused, attach_commands

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
        self._closelock = threading.Lock()

    @property
    def alive(self):
        return self._pid is not None and self._ended is None and not self._closed

    def start(self):
        if self._pid is not None:
            raise RuntimeError("terminal session already started")
        exe = shutil.which(self.argv[0])  # resolve in the parent, on PATH only
        if exe is None and "/" in self.argv[0]:
            exe = self.argv[0]
        if exe is None:  # never fall back to the bare name: execv would try it in the cwd
            raise FileNotFoundError(f"executable not found: {self.argv[0]!r}")
        # SC_OPEN_MAX can be unlimited or huge; a fixed ceiling keeps the child's closerange fast.
        limit = os.sysconf("SC_OPEN_MAX")
        limit = limit if 0 < limit <= 65536 else 65536
        opened, pid = [], None
        try:
            wake_r, wake_w = os.pipe()
            opened += [wake_r, wake_w]
            # Size the pty before the fork so the program never starts at 0x0, and so a resize
            # that follows start() cannot be overwritten by the child's own setup.
            fd, slave = os.openpty()
            opened += [fd, slave]
            _set_winsize(slave, self.rows, self.cols)
            pid = os.fork()
            if pid == 0:  # child: login_tty, closerange, execv, _exit only; argv list, no shell
                try:
                    signal.signal(signal.SIGPIPE, signal.SIG_DFL)  # Python ignores it; programs expect default
                    os.login_tty(slave)  # setsid, controlling tty, stdio
                    os.closerange(3, limit)
                    os.execv(exe, self.argv)
                finally:
                    os._exit(127)
            os.close(slave)
            opened.remove(slave)
            os.set_inheritable(fd, False)
            os.set_blocking(fd, False)
            self._pid, self._fd, self._wake = pid, fd, (wake_r, wake_w)
            self._reader = threading.Thread(target=self._read_loop, daemon=True)
            self._reader.start()
        except BaseException:
            self._pid = self._fd = self._wake = self._reader = None
            if pid:
                for kill, target in ((os.killpg, pid), (os.kill, pid)):  # child may not lead a group yet
                    try:
                        kill(target, signal.SIGKILL)
                    except OSError:
                        pass
                try:
                    os.waitpid(pid, 0)
                except OSError:
                    pass
            for f in opened:
                try:
                    os.close(f)
                except OSError:
                    pass
            raise

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
        with self._closelock:  # check-and-set and the final fd close are one step
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


_MAX_SESSIONS = 4
_MAX_INPUT = 64 * 1024     # bytes per terminal/input call, after base64 decoding
_KEEP_SECONDS = 300.0      # a session outlives its last stream by this long
_POLL_SECONDS = 0.03       # keystroke echo rides this stream
_NOT_READY = "The terminal is not ready."


class _Open:
    """One live terminal as the manager sees it."""

    def __init__(self, tid, agent, host, account, session, now):
        self.tid, self.agent, self.host, self.account, self.session = tid, agent, host, account, session
        self.lock = threading.Lock()   # orders input: n is checked and the bytes written as one step
        self.last_n = 0
        self.streams = 0
        self.idle_since = now          # set when the last stream closes (and at open)
        self.taken = False             # a newer open for the same agent replaced this one
        self.closed = False
        self.detached = False


class Terminal:
    """The ``terminal/*`` procedures: owner-only pty relays onto herdr agent panes. ``repo`` is set by the API glue."""

    def __init__(self, bridge, error, *, spawn=TerminalSession, local_host=None, ttl=_KEEP_SECONDS, clock=time.monotonic):
        self.b = bridge
        self.Error = error
        self.repo = None
        self.spawn = spawn
        self.local_host = (local_host or socket.gethostname()).lower()
        self.ttl = ttl
        self._clock = clock
        self._lock = threading.Lock()
        self._by_id: dict[str, _Open] = {}
        self._by_agent: dict[str, str] = {}   # agent id -> terminal id of its one live session

    def procs(self) -> dict:
        return {"terminal/open": self.open, "terminal/input": self.input, "terminal/resize": self.resize,
                "terminal/close": self.close, "terminal/subscribe": self.subscribe}

    # plumbing -----------------------------------------------------------------------------------------------
    def _owner(self, ctx) -> None:
        if ctx.account.role != "operator":
            raise self.Error(403, "FORBIDDEN", "Only the owner can use the terminal.")

    def _reap(self) -> None:
        with self._lock:
            gone = self._expired()
        for entry in gone:
            self._detach(entry)

    def _find(self, inp) -> _Open:
        self._reap()
        tid = inp.get("terminalId")
        with self._lock:
            entry = self._by_id.get(tid) if isinstance(tid, str) else None
        if entry is None:
            raise self.Error(404, "NOT_FOUND", "No such terminal.")
        return entry

    def _size(self, inp):
        rows, cols = inp.get("rows"), inp.get("cols")
        try:
            _check_size(rows, cols)
        except ValueError:
            raise self.Error(400, "BAD_REQUEST", "rows and cols are out of range.") from None
        return rows, cols

    def _detach(self, entry: _Open) -> None:
        """End the session and audit it once (ids and host only, never input or output)."""
        entry.closed = True
        entry.session.close()
        if not entry.detached:
            entry.detached = True
            self.b._audit(entry.account, "rakazo.bridge.terminal.detach", entry.agent, f"host={entry.host}")

    def _drop(self, entry: _Open) -> None:   # call with self._lock held
        self._by_id.pop(entry.tid, None)
        if self._by_agent.get(entry.agent) == entry.tid:
            del self._by_agent[entry.agent]

    def _expired(self) -> list[_Open]:   # call with self._lock held
        now = self._clock()
        gone = [e for e in self._by_id.values() if e.streams == 0 and now - e.idle_since >= self.ttl]
        for entry in gone:
            self._drop(entry)
        return gone

    def close_all(self) -> None:
        with self._lock:
            gone = list(self._by_id.values())
            self._by_id.clear()
            self._by_agent.clear()
        for entry in gone:
            self._detach(entry)

    # procedures ---------------------------------------------------------------------------------------------
    def open(self, ctx, inp):
        self._owner(ctx)
        if self.repo is None:
            raise self.Error(503, "UNAVAILABLE", _NOT_READY)
        rows, cols = self._size(inp)
        sid = inp.get("id")
        session = self.repo.get_session(sid) if isinstance(sid, str) else None
        if session is None:
            raise self.Error(404, "NOT_FOUND", "No such agent.")
        try:
            argv = attach_argv(session, local_host=self.local_host)
        except AttachRefused as exc:
            raise self.Error(exc.status, "CONFLICT", exc.body["error"]) from None
        host = session["host_id"]
        tid = secrets.token_urlsafe(16)
        self._reap()
        with self._lock:
            old = self._by_id.get(self._by_agent.get(sid))
            if old is not None:
                self._drop(old)
            try:
                if len(self._by_id) >= _MAX_SESSIONS:
                    raise self.Error(429, "TOO_MANY_REQUESTS", "Too many live terminals.", {"Retry-After": "10"})
                live = self.spawn(argv, rows, cols)
                try:
                    live.start()
                except OSError:
                    raise self.Error(503, "UNAVAILABLE", "Could not start the terminal.") from None
                entry = _Open(tid, sid, host, ctx.account, live, self._clock())
                self._by_id[tid], self._by_agent[sid] = entry, tid
            except BaseException:
                if old is not None:   # the takeover failed: the first client keeps its session
                    self._by_id[old.tid], self._by_agent[sid] = old, old.tid
                    old = None
                raise
        if old is not None:
            old.taken = True
            self._detach(old)
        self.b._audit(ctx.account, "rakazo.bridge.terminal.attach", sid, f"host={host}")
        return {"terminalId": tid, "seq": 0, "rows": rows, "cols": cols}

    def input(self, ctx, inp):
        self._owner(ctx)
        entry = self._find(inp)
        data, n = inp.get("data"), inp.get("n")
        if not isinstance(data, str) or isinstance(n, bool) or not isinstance(n, int):
            raise self.Error(400, "BAD_REQUEST", "data must be base64 text and n an integer.")
        if len(data) > _MAX_INPUT * 4 // 3 + 4:
            raise self.Error(400, "BAD_REQUEST", "Too much input in one call.")
        try:
            raw = base64.b64decode(data, validate=True)
        except (binascii.Error, ValueError):
            raise self.Error(400, "BAD_REQUEST", "data is not valid base64.") from None
        if len(raw) > _MAX_INPUT:
            raise self.Error(400, "BAD_REQUEST", "Too much input in one call.")
        with entry.lock:
            if n == entry.last_n + 1:   # anything else is a retry or a gap: answered ok, typed never
                entry.last_n = n
                entry.session.write(raw)
        return {"ok": True}

    def resize(self, ctx, inp):
        self._owner(ctx)
        entry = self._find(inp)
        rows, cols = self._size(inp)
        entry.session.resize(rows, cols)
        return {"ok": True}

    def close(self, ctx, inp):
        self._owner(ctx)
        self._reap()
        tid = inp.get("terminalId")
        with self._lock:
            entry = self._by_id.get(tid) if isinstance(tid, str) else None
            if entry is not None:
                self._drop(entry)
        if entry is not None:
            self._detach(entry)
        return {"ok": True}

    def subscribe(self, ctx, inp):
        self._owner(ctx)
        self.b.agents._check_stream(ctx, inp)
        entry = self._find(inp)
        done = threading.Event()

        def fetch(after: int) -> list[dict]:
            taken = entry.taken
            chunks = entry.session.chunks_after(after)
            if taken and entry.closed:   # the session was closed under this client: its exit is not news
                chunks = [c for c in chunks if not c.get("ended")]
                chunks.append({"seq": entry.session.snapshot_seq() + 1, "taken_over": True})
                done.set()
            elif any(c.get("ended") for c in chunks):
                done.set()
            return chunks

        def release():
            with self._lock:
                entry.streams -= 1
                if entry.streams == 0:
                    entry.idle_since = self._clock()

        with self._lock:
            entry.streams += 1
        try:
            return self.b.agents._stream(ctx, inp, fetch, stop=lambda: done.is_set() or (entry.closed and not entry.taken),
                                         on_close=release, poll_seconds=_POLL_SECONDS)
        except BaseException:
            release()
            raise

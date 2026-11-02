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
import logging
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
log = logging.getLogger(__name__)


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
_REASONS = {"not_live": "NOT_RUNNING", "unsafe_identity": "UNSAFE_IDENTITY"}


class _Pty:
    """One live pty, kept per agent. Clients (terminal ids) come and go; the pty and its buffer stay."""

    def __init__(self, agent, host, account, session, now):
        self.agent, self.host, self.account, self.session = agent, host, account, session
        self.client = None             # the one client that may type and stream
        self.streams = 0
        self.idle_since = now          # set when the last stream closes (and at every open)
        self.ended_seen = False        # a stream has delivered the child's ended frame
        self.closed = False
        self.detached = False


class _Client:
    """One terminal id: a numbered input sequence bound to the account that opened it."""

    def __init__(self, tid, pty, account_id):
        self.tid, self.pty, self.account_id = tid, pty, account_id
        self.lock = threading.Lock()   # orders input: n is checked and the bytes written as one step
        self.last_n = 0
        self.taken = False             # a newer open for the same agent replaced this client


class Terminal:
    """The ``terminal/*`` procedures: owner-only pty relays onto herdr agent panes. ``repo`` is set by the API glue.
    One daemon reaper thread (started on the first open, stopped by ``close_all``) enforces the idle expiry, because
    a timer per session would need cancelling on every stream open and close."""

    def __init__(self, bridge, error, *, spawn=TerminalSession, local_host=None, ttl=_KEEP_SECONDS, clock=time.monotonic):
        self.b = bridge
        self.Error = error
        self.repo = None
        self.spawn = spawn
        self.local_host = (local_host or socket.gethostname()).lower()
        self.ttl = ttl
        self._clock = clock
        self._lock = threading.Lock()
        self._open_lock = threading.Lock()
        self._clients: dict[str, _Client] = {}   # terminal id -> its client (current clients only)
        self._ptys: dict[str, _Pty] = {}         # agent id -> its pty
        self._stop = None
        self._reaper = None

    def procs(self) -> dict:
        return {"terminal/open": self.open, "terminal/input": self.input, "terminal/resize": self.resize,
                "terminal/close": self.close, "terminal/subscribe": self.subscribe}

    # plumbing -----------------------------------------------------------------------------------------------
    def _owner(self, ctx) -> None:
        if not self.b.is_owner(ctx.account):
            raise self.Error(403, "FORBIDDEN", "Only the owner can use the terminal.")

    def _missing(self):
        return self.Error(404, "NOT_FOUND", "No such terminal.")

    def _client(self, ctx, inp) -> _Client:   # call with self._lock held
        tid = inp.get("terminalId")
        client = self._clients.get(tid) if isinstance(tid, str) else None
        if client is None or client.account_id != ctx.account.account_id:
            raise self._missing()
        return client

    def _find(self, ctx, inp) -> _Client:
        self._reap()
        with self._lock:
            return self._client(ctx, inp)

    def _size(self, inp):
        rows, cols = inp.get("rows"), inp.get("cols")
        try:
            _check_size(rows, cols)
        except ValueError:
            raise self.Error(400, "BAD_REQUEST", "rows and cols are out of range.") from None
        return rows, cols

    def _detach(self, pty: _Pty) -> None:
        """End the pty and audit it once (ids and host only, never input or output)."""
        pty.closed = True
        try:
            pty.session.close()
        except Exception as exc:  # noqa: BLE001  one stuck close must not skip the audit or the rest of a batch
            log.warning("rakazo terminal: closing a session failed (%s)", type(exc).__name__)
        if not pty.detached:
            pty.detached = True
            self.b._audit(pty.account, "rakazo.bridge.terminal.detach", pty.agent, f"host={pty.host}")

    def _remove(self, pty: _Pty) -> None:   # call with self._lock held
        if self._ptys.get(pty.agent) is pty:
            del self._ptys[pty.agent]
        if pty.client is not None:
            self._clients.pop(pty.client.tid, None)

    def _reapable(self, pty: _Pty, now: float) -> bool:
        if pty.streams:
            return False
        # an ended child is dropped as soon as a stream has shown its exit; otherwise it waits out the ttl
        return now - pty.idle_since >= self.ttl or (not pty.session.alive and pty.ended_seen)

    def _reap(self) -> None:
        now = self._clock()
        with self._lock:
            gone = [p for p in self._ptys.values() if self._reapable(p, now)]
            for pty in gone:
                self._remove(pty)
        for pty in gone:
            self._detach(pty)

    def _reap_loop(self, stop) -> None:
        while not stop.wait(max(0.05, min(5.0, self.ttl / 4))):
            try:
                self._reap()
            except Exception as exc:  # noqa: BLE001  the reaper must outlive any one failure
                log.warning("rakazo terminal: reaping failed (%s)", type(exc).__name__)

    def close_all(self) -> None:
        with self._lock:
            stop, self._stop, self._reaper = self._stop, None, None
            gone = list(self._ptys.values())
            self._ptys.clear()
            self._clients.clear()
        if stop is not None:
            stop.set()
        for pty in gone:
            self._detach(pty)

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
            reason = exc.body["reason"]
            raise self.Error(exc.status, _REASONS.get(reason, reason.upper()), exc.body["error"]) from None
        host = session["host_id"]
        with self._open_lock:   # one open at a time; the manager lock is never held across pty work
            return self._open(ctx, sid, host, argv, rows, cols)

    def _open(self, ctx, sid, host, argv, rows, cols):
        while True:
            if self.b._closed.is_set():   # the bridge shut down while this open was retrying
                raise self.Error(503, "UNAVAILABLE", _NOT_READY)
            self._reap()
            tid = secrets.token_urlsafe(16)
            with self._lock:
                pty = self._ptys.get(sid)
                reuse = pty is not None and not pty.closed and pty.session.alive
                if reuse:
                    pty.idle_since = self._clock()
                elif len(self._ptys) - (pty is not None) >= _MAX_SESSIONS:
                    raise self.Error(429, "TOO_MANY_REQUESTS", "Too many live terminals.", {"Retry-After": "10"})
            if reuse:
                pty.session.resize(rows, cols)       # reattach: same pty and buffer, the new client's size
            else:
                live = self.spawn(argv, rows, cols)
                try:
                    live.start()
                except OSError:
                    raise self.Error(503, "UNAVAILABLE", "Could not start the terminal.") from None
            retired = None
            with self._lock:
                if reuse and (pty.closed or self._ptys.get(sid) is not pty):
                    continue                          # closed or reaped while resizing: decide again
                if not reuse:
                    retired, pty = self._ptys.get(sid), _Pty(sid, host, ctx.account, live, self._clock())
                    if retired is not None:           # an ended one is replaced
                        self._remove(retired)
                    self._ptys[sid] = pty
                previous = pty.client
                if previous is not None:
                    self._clients.pop(previous.tid, None)
                pty.client = self._clients[tid] = _Client(tid, pty, ctx.account.account_id)
                pty.idle_since = self._clock()
            break
        if previous is not None:
            with previous.lock:   # waits for an input mid-write; later ones see taken and type nothing
                previous.taken = True                # its stream ends with taken_over; the pty lives on
        if retired is not None:
            self._detach(retired)
        self._start_reaper()
        self.b._audit(ctx.account, "rakazo.bridge.terminal.attach", sid, f"host={host}")
        return {"terminalId": tid, "seq": 0, "rows": rows, "cols": cols}   # 0 replays the whole kept buffer

    def _start_reaper(self) -> None:
        with self._lock:
            if self._reaper is None and not self.b._closed.is_set():
                self._stop = threading.Event()
                self._reaper = threading.Thread(target=self._reap_loop, args=(self._stop,), daemon=True)
                self._reaper.start()

    def input(self, ctx, inp):
        self._owner(ctx)
        client = self._find(ctx, inp)
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
        with client.lock:
            # a retry, a gap or a retired client is answered ok and typed never
            if n == client.last_n + 1 and not client.taken:
                client.last_n = n
                client.pty.session.write(raw)
        return {"ok": True}

    def resize(self, ctx, inp):
        self._owner(ctx)
        client = self._find(ctx, inp)
        rows, cols = self._size(inp)
        client.pty.session.resize(rows, cols)
        return {"ok": True}

    def close(self, ctx, inp):
        self._owner(ctx)
        self._reap()
        with self._lock:
            try:
                pty = self._client(ctx, inp).pty
            except self.Error:
                return {"ok": True}
            self._remove(pty)
        self._detach(pty)
        return {"ok": True}

    def subscribe(self, ctx, inp):
        self._owner(ctx)
        self.b.agents._check_stream(ctx, inp)
        self._reap()
        with self._lock:
            client = self._client(ctx, inp)
            pty = client.pty
            pty.streams += 1
        done = threading.Event()

        def fetch(after: int) -> list[dict]:
            chunks = pty.session.chunks_after(after)
            if client.taken:   # a newer open owns the pty: this client just stops, the session goes on
                chunks = [c for c in chunks if not c.get("ended")]
                chunks.append({"seq": max(after, pty.session.snapshot_seq()) + 1, "taken_over": True})
                done.set()
            elif any(c.get("ended") for c in chunks) or pty.closed:
                if not any(c.get("ended") for c in chunks):   # closed under the client: say so
                    chunks.append({"seq": max(after, pty.session.snapshot_seq()) + 1, "ended": True, "exit": None})
                pty.ended_seen = True
                done.set()
            return chunks

        def release():
            with self._lock:
                pty.streams -= 1
                if pty.streams == 0:
                    pty.idle_since = self._clock()

        try:
            return self.b.agents._stream(ctx, inp, fetch, stop=done.is_set, on_close=release,
                                         poll_seconds=_POLL_SECONDS)
        except BaseException:
            release()
            raise

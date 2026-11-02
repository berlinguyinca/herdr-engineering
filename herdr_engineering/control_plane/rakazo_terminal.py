"""Terminal broker: the phone's ``terminal/*`` procedures on one side, host relays on the other.

The control plane never starts a process. A relay on each fleet host long-polls ``wait`` for commands (open, input,
resize, zoom, close) and pushes screen frames back with ``accept_frames``; the broker keeps a command queue per host
and, per agent, a numbered buffer of those frames. The app attaches by cursor: ``chunks_after(seq)`` returns everything
newer than ``seq``. A frame is a whole screen, so each one is stored as a ``reset`` chunk (the app clears, then paints).
The buffer is capped, and the newest frame is always kept.

Host protocol. Delivery is at-least-once. Every command has an id ``<epoch>-<counter>-<random>``: the epoch is random per
process start and the counter ascends within it. A command stays queued until the relay acknowledges it by passing the
last id it handled as ``wait(after=...)``; an unacknowledged command is delivered again (a relay whose response was lost,
or whose connection died, loses nothing) and expires after 60 s. A cursor from another epoch (a restarted control plane),
or with a counter beyond anything this process issued, is ignored: nothing is acknowledged and the whole queue comes back,
so a stale or forged ack can never cover commands the relay has not seen. The relay must dedupe by command id AND by
``(terminal, n)`` (a second relay polling the same host would dedupe separately, so only one relay runs per host), and must
refuse a gap in ``n``: when the queue is full the broker drops every queued input of the overflowing terminal and ends it
with ``reason: "input_overflow"`` rather than deliver inputs with a hole; an input, open or resize that expired unacknowledged
ends its terminal with ``reason: "stale"`` for the same reason. A terminal that ends, is closed or is retired
by a takeover loses its queued open/input/resize/zoom commands (its ``close`` stays, except when the host is gone).
A host counts as connected while a wait is blocked for it, or for 10 s after its last wait ended; a host silent for
those 10 s loses its open terminals (``reason: "host_offline"``). What a host may receive or push is decided by the
caller (``api.py`` requires its key to be explicitly bound to the host id); the broker keeps every queue and every
terminal id per host.
"""
from __future__ import annotations

import base64
import binascii
import collections
import logging
import secrets
import threading
import time

from .terminal_attach import AttachRefused, attach_commands

log = logging.getLogger(__name__)


def _check_size(rows, cols):
    for value, top in ((rows, 500), (cols, 1000)):
        if type(value) is not int or not 1 <= value <= top:
            raise ValueError("terminal size out of range")


class HostError(Exception):
    """A refused host call: the API glue turns it into a plain JSON error."""

    def __init__(self, status, message, headers=None):
        super().__init__(message)
        self.status, self.message, self.headers = status, message, headers or {}


_MAX_SESSIONS = 4
_MAX_INPUT = 64 * 1024     # bytes per terminal/input call, after base64 decoding
_KEEP_SECONDS = 300.0      # a terminal outlives its last stream by this long
_POLL_SECONDS = 0.03       # keystroke echo rides this stream
_BUFFER_BYTES = 200_000    # soft cap on the encoded frames kept per terminal
_MAX_FRAME = 160 * 1024    # encoded bytes per pushed frame
_MAX_FRAMES = 20           # frames per output POST
_QUEUE_CAP = 256           # queued commands per host
_SILENT_SECONDS = 10.0     # one rule: connected for this long after a wait ended, and silent this long = offline
_COMMAND_TTL = 60.0        # an unacknowledged command is dropped after this long
_FORGET_SECONDS = 600.0    # a host unseen this long loses its bookkeeping
_MAX_WAITERS = 64          # concurrent long-polls overall (one per host is live; the HTTP server holds a thread each)
_MAX_WAIT = 30.0
_WAIT_SLICE = 0.5          # a waiter re-checks shutdown this often
_NOT_READY = "The terminal is not ready."
_REASONS = {"not_live": "NOT_RUNNING", "unsafe_identity": "UNSAFE_IDENTITY"}


class _Live:
    """One live terminal, kept per agent: the pane's host, its frame buffer and its current client.
    Clients (terminal ids) come and go; the buffer stays. Mutated under the broker lock."""

    def __init__(self, agent, host, pane, account, now):
        self.agent, self.host, self.pane, self.account = agent, host, pane, account
        self.client = None             # the one client that may type and stream
        self.streams = 0
        self.opened_at = self.idle_since = now   # idle_since is reset when the last stream closes (and at every open)
        self.ended_seen = False        # a stream has delivered the ended frame
        self.closed = False
        self.detached = False
        self.host_knows_end = False    # the host itself ended it or is gone: no close needs sending
        self._chunks = collections.deque()       # (seq, base64 text, reset)
        self._size = 0
        self._seq = 0
        self._ended = None             # {"seq", "ended", "exit"[, "reason"]} once the pane is gone

    @property
    def alive(self):
        return self._ended is None and not self.closed

    @property
    def seq(self):
        return self._seq

    def add(self, data, reset):
        self._seq += 1
        self._chunks.append((self._seq, data, reset))
        self._size += len(data)
        while self._size > _BUFFER_BYTES and len(self._chunks) > 1:   # the newest frame stays, whatever its size
            self._size -= len(self._chunks.popleft()[1])

    def end(self, code, reason=None):
        if self._ended is None:
            self._seq += 1
            self._ended = {"seq": self._seq, "ended": True, "exit": code}
            if reason:
                self._ended["reason"] = reason

    def chunks_after(self, seq):
        out = []
        for n, data, reset in self._chunks:
            if n > seq:
                out.append({"seq": n, "data": data, **({"reset": True} if reset else {})})
        if self._ended and self._ended["seq"] > seq:
            out.append(dict(self._ended))
        return out


class _Client:
    """One terminal id: a numbered input sequence bound to the account that opened it."""

    def __init__(self, tid, live, account_id):
        self.tid, self.pty, self.account_id = tid, live, account_id
        self.lock = threading.Lock()   # orders input: n is checked and the command queued as one step
        self.last_n = 0
        self.taken = False             # a newer open for the same agent replaced this client


def _parse_frames(frames):
    """Validate a whole POST before anything is stored: ``[("data", b64, rows, cols) | ("ended", exit, reason)]``."""
    if not isinstance(frames, list):
        raise HostError(400, "frames must be a list.")
    if len(frames) > _MAX_FRAMES:
        raise HostError(413, "Too many frames in one call.")
    out = []
    for frame in frames:
        if not isinstance(frame, dict):
            raise HostError(400, "Each frame must be an object.")
        if frame.get("ended") is True:
            code, reason = frame.get("exit"), frame.get("reason")
            if code is not None and (isinstance(code, bool) or not isinstance(code, int)):
                raise HostError(400, "exit must be an integer or null.")
            if reason is not None and (not isinstance(reason, str) or len(reason) > 200):
                raise HostError(400, "reason must be short text.")
            out.append(("ended", code, reason))
            continue
        data = frame.get("data")
        if not isinstance(data, str):
            raise HostError(400, "data must be base64 text.")
        if len(data) > _MAX_FRAME:
            raise HostError(413, "A frame is too large.")
        try:
            base64.b64decode(data, validate=True)
            _check_size(frame.get("rows"), frame.get("cols"))
        except (binascii.Error, ValueError):
            raise HostError(400, "A frame needs valid base64 data and rows and cols in range.") from None
        out.append(("data", data, frame["rows"], frame["cols"]))
    return out


class Terminal:
    """The ``terminal/*`` procedures: owner-only, brokered to a relay on the agent's host. ``repo`` is set by the API glue.
    One daemon reaper thread (started on the first open, stopped by ``close_all``) enforces the idle expiry and ends the
    terminals of a host that stopped polling, because a timer per terminal would need cancelling on every stream."""

    def __init__(self, bridge, error, *, ttl=_KEEP_SECONDS, clock=time.monotonic):
        self.b = bridge
        self.Error = error
        self.repo = None
        self.ttl = ttl
        self._clock = clock
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)   # a queued command wakes the host's long-poll
        self._open_lock = threading.Lock()
        self._clients: dict[str, _Client] = {}   # terminal id -> its client (current clients only)
        self._lives: dict[str, _Live] = {}       # agent id -> its terminal
        self._queues: dict[str, collections.deque] = {}   # host id -> commands not yet acknowledged
        self._polled: dict[str, float] = {}      # host id -> when its last wait ended
        self._blocked: dict[str, int] = {}       # host id -> its current wait (a replaced wait no longer counts)
        self._gen: dict[str, int] = {}           # host id -> newest wait, which ends any older one
        self._epoch = secrets.token_hex(4)       # per process: an ack from another epoch is ignored
        self._counter = 0
        self._stop = None
        self._reaper = None

    def procs(self) -> dict:
        return {"terminal/open": self.open, "terminal/input": self.input, "terminal/resize": self.resize,
                "terminal/close": self.close, "terminal/subscribe": self.subscribe}

    # host side ----------------------------------------------------------------------------------------------
    def _connected(self, host_id, now) -> bool:   # call with self._lock held
        if self._blocked.get(host_id):
            return True
        seen = self._polled.get(host_id)
        return seen is not None and now - seen < _SILENT_SECONDS

    def host_connected(self, host_id) -> bool:
        with self._lock:
            return self._connected(host_id, self._clock())

    @property
    def _waiters(self) -> int:
        return len(self._blocked)

    def _acked(self, command_id) -> int:   # call with self._lock held
        """The counter an ack covers; 0 (nothing) for a cursor of another epoch or beyond what was ever issued."""
        try:
            epoch, counter, _ = str(command_id).split("-", 2)
            counter = int(counter)
        except ValueError:
            return 0
        return counter if epoch == self._epoch and 0 < counter <= self._counter else 0

    def wait_commands(self, host_id, timeout, after=None) -> list[dict]:
        """Long-poll: acknowledge up to ``after``, then return the commands queued for ``host_id`` as soon as there are
        any; ``[]`` after ``timeout`` seconds. A newer wait for the same host ends this one with ``[]``."""
        end = time.monotonic() + max(0.0, min(float(timeout), _MAX_WAIT))
        with self._cond:
            if host_id not in self._blocked and self._waiters >= _MAX_WAITERS:   # a takeover needs no free slot
                raise HostError(429, "Too many waiting hosts.", {"Retry-After": "5"})
            self._gen[host_id] = mine = self._gen.get(host_id, 0) + 1
            self._blocked[host_id] = mine              # the older wait stops counting at once
            self._cond.notify_all()                    # ...and sees it was replaced
            acked = self._acked(after) if after else 0
            try:
                queue = self._queues.get(host_id)
                if queue and acked:
                    for command in [c for c in queue if c["_n"] <= acked]:
                        queue.remove(command)
                while True:
                    if self._gen.get(host_id) != mine:
                        return []
                    self._expire_commands(host_id)
                    queue = self._queues.get(host_id)
                    if queue:                          # delivered, not removed: only an ack removes
                        return [{k: v for k, v in c.items() if k[0] != "_"} for c in queue]
                    left = end - time.monotonic()
                    if left <= 0 or self.b._closed.is_set():
                        return []
                    self._cond.wait(min(left, _WAIT_SLICE))
            finally:
                if self._blocked.get(host_id) == mine:   # a replaced wait leaves the slot to its replacement
                    del self._blocked[host_id]
                self._polled[host_id] = self._clock()  # the connected window starts when the wait ends

    def accept_frames(self, host_id, terminal_id, frames) -> None:
        """Store what a relay pushed for a terminal. Only a terminal opened on THAT host is accepted, and a retired id
        (taken over or closed) is as unknown as a made-up one."""
        parsed = _parse_frames(frames)
        with self._lock:
            client = self._clients.get(terminal_id) if isinstance(terminal_id, str) else None
            live = client.pty if client is not None else None
            if live is None or live.host != host_id or live.closed:
                raise HostError(404, "No such terminal.")
            for item in parsed:
                if not live.alive:
                    break
                if item[0] == "ended":
                    self._finish(live, item[1], item[2], host_knows=True)
                else:
                    live.add(item[1], True)

    def _finish(self, live, code, reason, *, host_knows=False) -> None:   # call with self._lock held
        """End a terminal and drop its queued commands, which would be stale: the ones the host is not told about
        (``host_knows``: it ended it or is gone) lose their ``close`` too."""
        live.end(code, reason)
        live.host_knows_end = live.host_knows_end or host_knows
        if live.client is not None:
            self._purge(live.host, live.client.tid, keep_close=not host_knows)

    def _purge(self, host, tid, *, keep_close=True) -> None:   # call with self._lock held
        queue = self._queues.get(host)
        if queue:
            for command in [c for c in queue if c.get("terminal") == tid and not (keep_close and c["kind"] == "close")]:
                queue.remove(command)

    def _expire_commands(self, host) -> None:   # call with self._lock held
        queue, now = self._queues.get(host), self._clock()
        expired = []
        while queue and now - queue[0]["_t"] > _COMMAND_TTL:
            expired.append(queue.popleft())
        for command in expired:   # an input that was never delivered would leave a gap in n: end that terminal
            client = self._clients.get(command.get("terminal"))
            if command["kind"] != "close" and client is not None and client.pty.alive:
                self._finish(client.pty, None, "stale")

    def _room(self, host, need) -> None:   # call with self._lock held
        """Make room for ``need`` more commands. Never drops an open or a close: it ends the terminal whose input is
        oldest (all its queued inputs go, so n never has a gap), and refuses when there is no input to drop."""
        queue = self._queues.setdefault(host, collections.deque())
        self._expire_commands(host)
        while len(queue) + need > _QUEUE_CAP:
            victim = next((c for c in queue if c["kind"] == "input"), None)
            if victim is None:
                raise self.Error(503, "UNAVAILABLE", "The host is not keeping up.", {"Retry-After": "5"})
            client = self._clients.get(victim["terminal"])
            if client is None:
                queue.remove(victim)
            else:
                self._finish(client.pty, None, "input_overflow")

    def _enqueue(self, host, command) -> None:   # call with self._lock held
        if command["kind"] != "close":                 # a close always fits: it frees the host's pane, and callers hold room
            self._room(host, 1)
        client = self._clients.get(command.get("terminal"))
        if command["kind"] != "close" and client is not None and not client.pty.alive:
            return                                     # the room it needed just ended its own terminal
        self._queues.setdefault(host, collections.deque())
        self._counter += 1
        command["_n"], command["_t"] = self._counter, self._clock()
        command["id"] = f"{self._epoch}-{self._counter}-{secrets.token_urlsafe(12)}"
        self._queues[host].append(command)
        self._cond.notify_all()

    def _expire_silent_hosts(self) -> None:
        now = self._clock()
        with self._lock:
            for live in self._lives.values():
                if not live.alive or self._blocked.get(live.host):
                    continue
                # a new terminal gets the full grace from its open, whatever the host's last poll was
                if now - max(self._polled.get(live.host, 0.0), live.opened_at) >= _SILENT_SECONDS:
                    self._finish(live, None, "host_offline", host_knows=True)

    def _forget_hosts(self) -> None:
        now = self._clock()
        with self._lock:
            busy = {live.host for live in self._lives.values()}
            for host in set(self._polled) | set(self._queues) | set(self._gen):
                if host not in busy and not self._blocked.get(host) and now - self._polled.get(host, float("-inf")) >= _FORGET_SECONDS:
                    for table in (self._polled, self._queues, self._gen):
                        table.pop(host, None)

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

    def _detach(self, live: _Live) -> None:
        """End the terminal (telling its host) and audit it once (ids and host only, never input or output)."""
        with self._lock:
            tell_host = live.client is not None and not live.host_knows_end and not live.closed
            live.closed = True
            if live.client is not None:
                self._purge(live.host, live.client.tid)   # its close, if any, is the only command that stays
            if tell_host:
                self._enqueue(live.host, {"kind": "close", "terminal": live.client.tid})
        if not live.detached:
            live.detached = True
            self.b._audit(live.account, "rakazo.bridge.terminal.detach", live.agent, f"host={live.host}")

    def _remove(self, live: _Live) -> None:   # call with self._lock held
        if self._lives.get(live.agent) is live:
            del self._lives[live.agent]
        if live.client is not None:
            self._clients.pop(live.client.tid, None)

    def _reapable(self, live: _Live, now: float) -> bool:
        if live.streams:
            return False
        # an ended pane is dropped as soon as a stream has shown its end; otherwise it waits out the ttl
        return now - live.idle_since >= self.ttl or (not live.alive and live.ended_seen)

    def _reap(self) -> None:
        self._expire_silent_hosts()
        self._forget_hosts()
        now = self._clock()
        with self._lock:
            gone = [p for p in self._lives.values() if self._reapable(p, now)]
            for live in gone:
                self._remove(live)
        for live in gone:
            self._detach(live)

    def _reap_loop(self, stop) -> None:
        while not stop.wait(max(0.05, min(1.0, self.ttl / 4))):   # 1 s keeps host_offline well inside 10 s of silence
            try:
                self._reap()
            except Exception as exc:  # noqa: BLE001  the reaper must outlive any one failure
                log.warning("rakazo terminal: reaping failed (%s)", type(exc).__name__)

    def close_all(self) -> None:
        with self._lock:
            stop, self._stop, self._reaper = self._stop, None, None
            gone = list(self._lives.values())
            self._lives.clear()
            self._clients.clear()
            self._cond.notify_all()
        if stop is not None:
            stop.set()
        for live in gone:
            self._detach(live)

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
            info = attach_commands(session)   # the same record checks as the attach button; never builds a command
        except AttachRefused as exc:
            reason = exc.body["reason"]
            raise self.Error(exc.status, _REASONS.get(reason, reason.upper()), exc.body["error"]) from None
        host, pane = info["host_id"], info["pane_id"]
        if not self.host_connected(host):
            raise self.Error(503, "HOST_NOT_CONNECTED", "The agent's host is not connected to the control plane.")
        with self._open_lock:   # one open at a time; the manager lock is only ever held for bookkeeping
            return self._open(ctx, sid, host, pane, rows, cols)

    def _open(self, ctx, sid, host, pane, rows, cols):
        if self.b._closed.is_set():   # the bridge shut down
            raise self.Error(503, "UNAVAILABLE", _NOT_READY)
        self._reap()
        tid = secrets.token_urlsafe(16)
        with self._lock:
            live = self._lives.get(sid)
            reuse = live is not None and not live.closed and live.alive and live.host == host
            if not reuse and len(self._lives) - (live is not None) >= _MAX_SESSIONS:   # before room: a refusal ends nothing
                raise self.Error(429, "TOO_MANY_REQUESTS", "Too many live terminals.", {"Retry-After": "10"})
            self._room(host, 2)                       # a close and an open: refuse before anything changes
            live = self._lives.get(sid)               # making room may have ended this very terminal
            reuse = live is not None and not live.closed and live.alive and live.host == host
            retired = None
            if not reuse:
                retired, live = live, _Live(sid, host, pane, ctx.account, self._clock())
                if retired is not None:           # an ended one is replaced
                    self._remove(retired)
                self._lives[sid] = live
            previous = live.client
            if previous is not None:
                self._clients.pop(previous.tid, None)
                self._purge(host, previous.tid)
                self._enqueue(host, {"kind": "close", "terminal": previous.tid})
            live.client = self._clients[tid] = _Client(tid, live, ctx.account.account_id)
            live.idle_since = live.opened_at = self._clock()
            # reuse still gets a fresh terminal id (n restarts at 1): the relay repaints the pane for it at once
            self._enqueue(host, {"kind": "open", "terminal": tid, "pane_id": pane, "rows": rows, "cols": cols})
        if previous is not None:
            with previous.lock:   # waits for an input mid-queue; later ones see taken and queue nothing
                previous.taken = True                # its stream ends with taken_over; the terminal lives on
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
                with self._lock:
                    if client.pty.alive and self._clients.get(client.tid) is client:   # a refused enqueue (503) burns no n
                        self._enqueue(client.pty.host, {"kind": "input", "terminal": client.tid, "n": n, "data": data})
                client.last_n = n
        return {"ok": True}

    def resize(self, ctx, inp):
        self._owner(ctx)
        client = self._find(ctx, inp)
        rows, cols = self._size(inp)
        zoom = inp.get("zoom")
        if zoom is not None and not isinstance(zoom, bool):
            raise self.Error(400, "BAD_REQUEST", "zoom must be true or false.")
        with self._lock:
            if client.pty.alive and not client.taken and self._clients.get(client.tid) is client:
                host = client.pty.host
                self._room(host, 2 if zoom is not None else 1)   # both commands or neither
                self._enqueue(host, {"kind": "resize", "terminal": client.tid, "rows": rows, "cols": cols})
                if zoom is not None:
                    self._enqueue(host, {"kind": "zoom", "terminal": client.tid, "zoom": zoom})
        return {"ok": True}

    def close(self, ctx, inp):
        self._owner(ctx)
        self._reap()
        with self._lock:
            try:
                live = self._client(ctx, inp).pty
            except self.Error:
                return {"ok": True}
            self._remove(live)
        self._detach(live)
        return {"ok": True}

    def subscribe(self, ctx, inp):
        self._owner(ctx)
        self.b.agents._check_stream(ctx, inp)
        self._reap()
        with self._lock:
            client = self._client(ctx, inp)
            live = client.pty
            live.streams += 1
        done = threading.Event()

        def fetch(after: int) -> list[dict]:
            self._expire_silent_hosts()
            with self._lock:
                chunks = live.chunks_after(after)
                tail = max(after, live.seq) + 1
            if client.taken:   # a newer open owns the terminal: this client just stops, the terminal goes on
                chunks = [c for c in chunks if not c.get("ended")]
                chunks.append({"seq": tail, "taken_over": True})
                done.set()
            elif any(c.get("ended") for c in chunks) or live.closed:
                if not any(c.get("ended") for c in chunks):   # closed under the client: say so
                    chunks.append({"seq": tail, "ended": True, "exit": None})
                live.ended_seen = True
                done.set()
            return chunks

        def release():
            with self._lock:
                live.streams -= 1
                if live.streams == 0:
                    live.idle_since = self._clock()

        try:
            return self.b.agents._stream(ctx, inp, fetch, stop=done.is_set, on_close=release,
                                         poll_seconds=_POLL_SECONDS)
        except BaseException:
            release()
            raise

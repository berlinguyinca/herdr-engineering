"""Host relay for the Shepherd terminal: carries a herdr pane's screen to the control plane and the phone's typing back.

One relay runs per fleet host, as the user that runs herdr. It only makes outbound calls: it long-polls the control
plane's broker (``GET /api/v1/host/terminal/wait``) for commands and pushes screen frames back
(``POST /api/v1/host/terminal/<id>/output``), authenticated with the host's own token from ``host.json`` (``X-Host-Token``,
the credential of every host agent; the control plane takes the host id from it). What it
does with a command is limited to herdr socket calls: it starts no process and touches no file chosen by a command.

Delivery from the broker is at-least-once and the relay makes it exactly-once for typing: every command id and every
``(terminal, n)`` is remembered (also across a restart, in the state file) BEFORE it is acted on, so a redelivered batch
types nothing twice; a gap in ``n`` ends that terminal, because typing around a hole would corrupt the line. The ``after``
cursor goes out with the next wait, only once the commands up to it were handled. A terminal the broker retired (stale,
input_overflow, host_offline, taken over) answers the next frame push with 404, which stops its mirror. Frames are whole
screens, so a failed push is repaired by sending the current screen again, never by queueing frames.

Nothing here logs frame data, input bytes, key material or a full terminal id: only counts and short reasons.
"""
from __future__ import annotations

import base64
import binascii
import collections
import fcntl
import http.client
import json
import logging
import os
import random
import re
import secrets
import signal
import threading
import time
import urllib.parse
from pathlib import Path
from urllib.parse import urlencode

from .control_plane.terminal_attach import _PANE
from .herdr_pane_client import HerdrError
from .terminal_mirror import InputTranslator, PaneMirror, apply

log = logging.getLogger(__name__)

MAX_MIRRORS = 4            # live mirror threads, whatever the broker asks for
MAX_INPUT = 64 * 1024      # bytes per input command (the broker's own cap)
MAX_REPLY = 24 * 1024 * 1024   # 256 queued commands of 64 KB each, base64 encoded
_WAIT = 25                 # seconds the broker may hold a wait
_WAIT_SLACK = 10.0         # http timeout beyond the wait
_POST_TIMEOUT = 10.0
_POST_FRAMES = 20          # frames per output POST (the broker's cap)
_BACKOFF_MIN, _BACKOFF_MAX = 0.5, 8.0
_AUTH_BACKOFF = 30.0       # a refused key must not hammer the control plane
_AUTH_LOG_EVERY = 60.0
_FLUSH_AFTER = 0.05        # silence after which a lone ESC becomes the Esc key
_CHANGE_WAIT = 0.3         # how long a mirror waits for a change before checking whether it should stop
_FAIL_LIMIT = 3            # consecutive herdr failures before a terminal ends with herdr_unavailable
_FAIL_PAUSE = 0.25
_PUSH_RETRY = 2.0          # pause after a refused or failed frame push
_KEEP_IDS = 300            # remembered command ids (the broker queues at most 256)
_JOIN = _POST_TIMEOUT + 1.0   # a mirror blocked in a push may take this long to notice it was stopped
_PROBE_EVERY = 30.0        # a silent pane re-sends its screen this often: the push is a liveness probe (404 = retired)
_TID = re.compile(r"^[A-Za-z0-9_-]{8,128}$")   # the broker mints url-safe random ids
_CMD_ID = re.compile(r"^[0-9a-f]{8}-\d{1,20}-[A-Za-z0-9_-]{1,64}$")
STATE_REL = Path(".local/share/herdr-engineering/state")


class RelayLocked(RuntimeError):
    """Another relay already runs for this host."""


class _Stopped(Exception):
    pass


class ControlPlaneHttp:
    """The relay's only network path: host.json's URL and the host token in ``X-Host-Token`` (never an API key or a
    bearer). Only the host terminal routes are allowed. ``http.client`` never follows a redirect, so the token goes to
    that URL and nowhere else (the same rule as ``host_config``'s no-redirect opener). Connections are kept alive and
    pooled (every keystroke's echo is a frame push, and a new TLS handshake per push is visible); one the gateway has
    closed is replaced transparently. ``request`` returns ``(status, parsed JSON or None)``; network failures raise OSError."""

    _IDLE = 4   # pooled idle connections: the poll loop, a few mirrors

    def __init__(self, config, token):
        self._url = config.control_plane_url.rstrip("/")
        self.token = token
        self.auth_rejected = False
        self._pool_lock = threading.Lock()
        self._idle: list[http.client.HTTPConnection] = []

    @property
    def url(self):
        return self._url

    def retarget(self, url, api_key=None):    # ConfigRefresher.sync() points the client at the live configuration
        url = url.rstrip("/") if url else self._url   # the ingest key it offers is not this relay's credential
        if url != self._url:
            self._url = url
            self.close()

    def close(self) -> None:
        with self._pool_lock:
            idle, self._idle = self._idle, []
        for conn in idle:
            conn.close()

    def _connection(self, timeout):
        """(connection, reused) for the current URL; a pooled one has its socket timeout reset."""
        base = urllib.parse.urlsplit(self._url)
        with self._pool_lock:
            conn = self._idle.pop() if self._idle else None
        if conn is not None and conn.host == base.hostname and conn.port == (base.port or conn.default_port):
            conn.timeout = timeout
            if conn.sock is not None:
                conn.sock.settimeout(timeout)
            return conn, True
        if conn is not None:
            conn.close()
        cls = http.client.HTTPSConnection if base.scheme == "https" else http.client.HTTPConnection
        return cls(base.hostname, base.port, timeout=timeout), False

    def _release(self, conn) -> None:
        with self._pool_lock:
            if len(self._idle) < self._IDLE:
                self._idle.append(conn)
                return
        conn.close()

    def request(self, method, path, body=None, timeout=10.0):
        if not path.startswith("/api/v1/host/terminal/"):
            raise ValueError("unsupported relay route")
        data = json.dumps(body, allow_nan=False).encode() if body is not None else None
        headers = {"Accept": "application/json", "X-Host-Token": self.token}
        if data is not None:
            headers["Content-Type"] = "application/json"
        prefix = urllib.parse.urlsplit(self._url).path.rstrip("/")
        for attempt in (0, 1):
            try:
                conn, reused = self._connection(timeout)
            except (ValueError, OSError):
                raise OSError("InvalidURL") from None
            try:
                conn.request(method, prefix + path, body=data, headers=headers)
                resp = conn.getresponse()
                raw = resp.read(MAX_REPLY + 1)
                status, closing = resp.status, resp.will_close
            except (http.client.RemoteDisconnected, BrokenPipeError, ConnectionResetError):
                conn.close()
                if reused and attempt == 0:   # a pooled socket the gateway closed while idle: retry on a new one
                    continue
                raise OSError("ConnectionClosed") from None
            except (http.client.HTTPException, OSError, ValueError) as exc:
                conn.close()
                raise OSError(type(exc).__name__) from None
            break
        if closing or len(raw) > MAX_REPLY:
            conn.close()
        else:
            self._release(conn)
        if status in (401, 403):
            self.auth_rejected = True
        if status >= 400:
            return status, None
        if len(raw) > MAX_REPLY:
            raise OSError("reply too large")
        try:
            return status, json.loads(raw) if raw else None
        except ValueError:
            return status, None


class _Term:
    def __init__(self, pane, last_n=0, epoch=None):
        self.pane = pane
        self.last_n = last_n
        self.epoch = epoch   # the control-plane process (first part of the command id) that opened it
        self.translator = InputTranslator()
        self.lock = threading.Lock()   # orders typing and the 50 ms flush for this terminal
        self.stop = threading.Event()
        self.thread = None
        self.dead = False


class TerminalRelay:
    def __init__(self, config, client, http, *, state_dir=None, sleep=None, rng=random.random,
                 clock=time.monotonic, refresher=None):
        self.config = config
        self.client = client
        self.http = http
        self.host_id = config.host_id
        self.state_dir = Path(state_dir) if state_dir else Path(
            os.environ.get("HERDR_ENGINEERING_STATE") or Path.home() / STATE_REL)
        self._stop = threading.Event()
        self._sleep = sleep or self._stop.wait
        self._rng = rng
        self._clock = clock              # throttles the auth warning only
        self._refresher = refresher
        self._lock = threading.Lock()    # _terms, _threads
        self._save_lock = threading.Lock()
        self._terms: dict[str, _Term] = {}
        self._threads: list[threading.Thread] = []
        self._ids: collections.OrderedDict[str, None] = collections.OrderedDict()
        self._after: str | None = None
        self._epoch: str | None = None
        self._lock_fd = None
        self._running = False
        self._flush_cv = threading.Condition()
        self._flush_at: dict[str, float] = {}
        self._flusher = None
        self._fails = 0
        self._connected = False
        self._auth_logged: float | None = None
        self._quick_empty = 0
        self._saved: dict = self._load()

    # -- single instance and state ---------------------------------------------------------------
    def acquire_lock(self) -> None:
        if self._lock_fd is not None:
            return
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self.state_dir / "terminal-relay.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            raise RelayLocked("another terminal relay is already running for this host") from None
        self._lock_fd = fd

    def _release_lock(self) -> None:
        if self._lock_fd is not None:
            os.close(self._lock_fd)    # closing drops the flock
            self._lock_fd = None

    def _state_path(self) -> Path:
        return self.state_dir / "terminal-relay-state.json"

    def _load(self) -> dict:
        try:
            data = json.loads(self._state_path().read_text())
            ids = [i for i in data.get("ids", []) if isinstance(i, str) and _CMD_ID.fullmatch(i)][-_KEEP_IDS:]
            epoch = data.get("epoch")
            self._epoch = epoch if isinstance(epoch, str) else None
            terms = {t: v for t, v in data.get("terminals", {}).items()
                     if _TID.fullmatch(t) and isinstance(v, dict) and isinstance(v.get("pane"), str)
                     and _PANE.fullmatch(v["pane"]) and type(v.get("n")) is int and v["n"] >= 0}
        except (OSError, ValueError, AttributeError, TypeError):
            return {"terminals": {}}
        self._ids.update((i, None) for i in ids)
        return {"terminals": dict(list(terms.items())[:MAX_MIRRORS])}

    def _save(self) -> None:
        with self._lock:
            snap = {"ids": list(self._ids),
                    "epoch": self._epoch,
                    "terminals": {t: {"pane": x.pane, "n": x.last_n, "epoch": x.epoch} for t, x in self._terms.items()}}
        with self._save_lock:
            tmp = self.state_dir / f".terminal-relay-state.{secrets.token_hex(4)}.tmp"
            try:
                self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w") as fh:
                    json.dump(snap, fh)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, self._state_path())
            except OSError:
                log.warning("terminal relay: could not save its state")
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    # -- loop --------------------------------------------------------------------------------------
    def run_forever(self, *, once: bool = False) -> None:
        self.acquire_lock()
        self._running = True
        try:
            self._start_flusher()
            self._restore()
            while not self._stop.is_set():
                self._poll()
                if once:
                    break
        except KeyboardInterrupt:   # SIGTERM/SIGINT raised into the blocked long-poll (install_signal_handlers)
            self._stop.set()
        finally:
            self._running = False
            self._stop.set()   # whatever ended the loop, nothing may keep running without it
            with self._flush_cv:
                self._flush_cv.notify_all()
            self._shutdown()

    def stop(self) -> None:
        self._stop.set()
        with self._flush_cv:
            self._flush_cv.notify_all()
        if not self._running:
            self._shutdown()

    def active_terminals(self) -> int:
        with self._lock:
            return len(self._terms)

    def _shutdown(self) -> None:
        with self._lock:
            for term in self._terms.values():
                term.stop.set()
            threads = list(self._threads)
        for thread in threads + [self._flusher]:
            if thread is not None and thread is not threading.current_thread():
                thread.join(5)
        self._release_lock()

    def _backoff(self) -> float:
        base = min(_BACKOFF_MAX, _BACKOFF_MIN * 2 ** self._fails)
        self._fails += 1
        return min(_BACKOFF_MAX, base * (1 - 0.2 * self._rng()))   # jitter downwards: never above the cap

    def _poll(self) -> None:
        try:
            if self._refresher is not None:
                self._refresher.sync(self.http)
            query = {"timeout": _WAIT}
            if self._after:
                query["after"] = self._after
            began = time.monotonic()
            status, body = self.http.request("GET", "/api/v1/host/terminal/wait?" + urlencode(query),
                                             timeout=_WAIT + _WAIT_SLACK)
        except OSError:
            log.warning("terminal relay: the control plane is unreachable")
            self._sleep(self._backoff())
            return
        if status in (401, 403):
            now = self._clock()
            if self._auth_logged is None or now - self._auth_logged >= _AUTH_LOG_EVERY:
                self._auth_logged = now
                log.warning("terminal relay: the control plane refused this host's token (HTTP %d); is host.json's "
                            "token the one in the fleet file?", status)
            self._sleep(_AUTH_BACKOFF * (1 + 0.2 * self._rng()))
            return
        commands = body.get("commands") if isinstance(body, dict) else None
        if status != 200 or not isinstance(commands, list):
            log.warning("terminal relay: unexpected answer from the control plane (HTTP %s)", status)
            self._sleep(self._backoff())
            return
        self._fails = 0
        self._auth_logged = None
        if not self._connected:   # the rollout's health check looks for this line: proof that a wait got through
            self._connected = True
            log.info("terminal relay connected")
        if not commands:   # a timeout, or a newer waiter took over: poll again at once, unless that spins
            self._quick_empty = self._quick_empty + 1 if time.monotonic() - began < 0.01 else 0
            if self._quick_empty >= 5:
                self._quick_empty = 0
                self._sleep(0.5)
            return
        self._quick_empty = 0
        for command in commands:
            if self._stop.is_set():
                return
            self._handle(command)

    # -- commands ----------------------------------------------------------------------------------
    def _handle(self, command) -> None:
        cid = command.get("id") if isinstance(command, dict) else None
        if not isinstance(cid, str) or not _CMD_ID.fullmatch(cid):
            log.warning("terminal relay: ignored a command without a valid id")
            return
        self._new_epoch(cid.split("-", 1)[0])
        if cid in self._ids:
            if command.get("kind") == "close":   # idempotent, and a crash may have saved the id before the terminal went
                self._dispatch(command)
        else:
            self._ids[cid] = None
            while len(self._ids) > _KEEP_IDS:
                self._ids.popitem(last=False)
            if command.get("kind") != "input":   # an input is saved once, by _input, together with its (terminal, n)
                self._save()   # remembered before it is acted on: a crash loses a keystroke rather than typing it twice
            try:
                self._dispatch(command)
            except Exception as exc:   # one bad command must not stop the loop
                log.warning("terminal relay: a %r command failed (%s)", str(command.get("kind"))[:16], type(exc).__name__)
        self._after = cid

    def _new_epoch(self, epoch) -> None:
        """A command from another control-plane process: the old one is gone with every terminal it knew (the app
        opens again), so those terminals would only pin mirror slots. Epochs are random, so 'newer' means 'different'."""
        if epoch == self._epoch and not any(t.epoch != epoch for t in list(self._terms.values())):
            return
        self._epoch = epoch
        with self._lock:
            old = [(t, x) for t, x in self._terms.items() if x.epoch != epoch]
        if old:
            log.info("terminal relay: the control plane restarted; dropping %d terminals", len(old))
        for tid, term in old:
            self._forget(tid, term)
        self._save()

    def _dispatch(self, command) -> None:
        kind, tid = command.get("kind"), command.get("terminal")
        if not isinstance(tid, str) or not _TID.fullmatch(tid):
            log.warning("terminal relay: ignored a command for a malformed terminal id")
            return
        if kind == "open":
            self._open(tid, command.get("pane_id"), command["id"].split("-", 1)[0])
        elif kind == "input":
            self._input(tid, command.get("n"), command.get("data"))
        elif kind == "zoom":
            self._zoom(tid, command.get("zoom"))
        elif kind == "resize":
            pass   # the pane owns its size: the phone fits its view to the pane, the pty is never resized
        elif kind == "close":
            with self._lock:
                term = self._terms.get(tid)
            if term is not None:
                self._forget(tid, term)
        else:
            log.warning("terminal relay: ignored an unknown command kind")

    def _term(self, tid):
        with self._lock:
            return self._terms.get(tid)

    def _pane_state(self, pane) -> str | None:
        """'agent' for an agent pane herdr lists on this host, 'plain' for any other pane, 'gone' when it is not
        listed, None when herdr cannot be asked. The broker only ever opens agent panes, so a plain shell is not served."""
        try:
            for p in self.client.list_panes():
                if p.get("pane_id") == pane:
                    return "agent" if isinstance(p.get("agent"), str) and p["agent"] else "plain"
        except (HerdrError, OSError):
            return None
        return "gone"

    def _open(self, tid, pane, epoch) -> None:
        if self._term(tid) is not None:
            return
        if not isinstance(pane, str) or not _PANE.fullmatch(pane):
            self._refuse(tid, "invalid_pane")
            return
        if self.active_terminals() >= MAX_MIRRORS:   # registered terminals, not threads
            self._probe_all()                        # reap the ones the broker already ended or lost
        if self.active_terminals() >= MAX_MIRRORS:
            self._refuse(tid, "too_many_terminals")
            return
        state = self._pane_state(pane)   # only panes herdr lists on THIS host are served
        if state == "agent":
            self._start(tid, _Term(pane, epoch=epoch))
        else:
            self._refuse(tid, {None: "herdr_unavailable", "gone": "pane_closed"}.get(state, "invalid_pane"))

    def _probe_all(self) -> None:
        """Push every terminal's current screen: a 404 means the broker retired it, and it goes."""
        with self._lock:
            terms = list(self._terms.items())
        for tid, term in terms:
            try:
                frame = PaneMirror(self.client, term.pane).frame()
            except (HerdrError, OSError, ValueError, KeyError, TypeError):
                continue
            if frame is not None and self._push(tid, [frame]) == 404:
                self._forget(tid, term)

    def _refuse(self, tid, reason) -> None:
        log.warning("terminal relay: refused to open a terminal (%s)", reason)
        self._push(tid, [{"ended": True, "exit": None, "reason": reason}])

    def _start(self, tid, term) -> None:
        thread = threading.Thread(target=self._mirror_loop, args=(tid, term), name="terminal-mirror-" + tid[5:13],
                                  daemon=True)
        term.thread = thread
        with self._lock:
            self._terms[tid] = term
            self._threads.append(thread)
        self._save()
        thread.start()

    def _restore(self) -> None:
        # Terminals that were open when the relay last stopped keep their n and get a mirror again; their
        # opens are remembered, so the broker's redelivery does not start them a second time.
        for tid, saved in self._saved["terminals"].items():
            state = self._pane_state(saved["pane"])
            reason = None if state == "agent" else {None: "herdr_unavailable", "gone": "pane_closed"}.get(state, "invalid_pane")
            if reason:
                self._push(tid, [{"ended": True, "exit": None, "reason": reason}])
            else:
                self._start(tid, _Term(saved["pane"], saved["n"], saved.get("epoch")))
        self._saved = {"terminals": {}}
        self._save()

    def _forget(self, tid, term) -> None:
        term.dead = True
        term.stop.set()
        with self._lock:
            if self._terms.get(tid) is term:
                del self._terms[tid]
        with self._flush_cv:
            self._flush_at.pop(tid, None)
        self._save()
        if term.thread is not None and term.thread is not threading.current_thread():
            term.thread.join(_JOIN)

    def _end(self, tid, term, reason) -> None:
        if not term.dead:
            log.info("terminal relay: a terminal ended (%s)", reason)
            self._push(tid, [{"ended": True, "exit": None, "reason": reason}])
        self._forget(tid, term)

    # -- input -------------------------------------------------------------------------------------
    def _input(self, tid, n, data) -> None:
        term = self._term(tid)
        if term is None:   # closed, ended or never opened: a stale input is never typed
            return
        if type(n) is not int or n < 1 or not isinstance(data, str) or len(data) > MAX_INPUT * 4 // 3 + 4:
            log.warning("terminal relay: ignored a malformed input command")
            return
        try:
            raw = base64.b64decode(data, validate=True)
        except (binascii.Error, ValueError):
            log.warning("terminal relay: ignored an input command that is not base64")
            return
        if len(raw) > MAX_INPUT:
            return
        with term.lock:
            if term.dead or n <= term.last_n:   # a retry of what was typed already
                return
            if n != term.last_n + 1:
                log.warning("terminal relay: input gap (expected %d, got %d); ending the terminal", term.last_n + 1, n)
                gap = True
            else:
                gap = False
                term.last_n = n
        if gap:
            self._end(tid, term, "input_gap")
            return
        self._save()   # the single save point of an input command: its id (added by _handle) and this n reach the
        #                disk before the keystroke, so a crash loses a keystroke rather than typing it twice
        with term.lock:
            if not term.dead:
                self._type(tid, term, term.translator.feed(raw))
        with self._flush_cv:
            self._flush_at[tid] = time.monotonic() + _FLUSH_AFTER
            self._flush_cv.notify_all()

    def _type(self, tid, term, actions) -> None:   # call with term.lock held
        if not actions:
            return
        try:
            dropped = apply(self.client, term.pane, actions)
        except HerdrError as exc:
            if "not_found" in exc.code:
                self._end(tid, term, "pane_closed")
            else:
                log.warning("terminal relay: typing failed (%s)", exc.code)
            return
        except (OSError, ValueError) as exc:
            log.warning("terminal relay: typing failed (%s)", type(exc).__name__)
            return
        if dropped:
            log.warning("terminal relay: input dropped %d bytes that herdr cannot carry", dropped)

    def _start_flusher(self) -> None:
        self._flusher = threading.Thread(target=self._flush_loop, name="terminal-flush", daemon=True)
        self._flusher.start()

    def _flush_loop(self) -> None:
        while not self._stop.is_set():
            with self._flush_cv:
                now = time.monotonic()
                due = [t for t, at in self._flush_at.items() if at <= now]
                for tid in due:
                    del self._flush_at[tid]
                if not due:
                    nxt = min(self._flush_at.values(), default=None)
                    self._flush_cv.wait(1.0 if nxt is None else max(0.0, min(1.0, nxt - now)))
                    continue
            for tid in due:
                try:
                    term = self._term(tid)
                    if term is not None:
                        with term.lock:
                            if not term.dead:
                                self._type(tid, term, term.translator.flush())
                except Exception as exc:   # noqa: BLE001  one bad flush must not end the flusher
                    log.warning("terminal relay: a flush failed (%s)", type(exc).__name__)

    def _zoom(self, tid, on) -> None:
        term = self._term(tid)
        if term is None or not isinstance(on, bool):
            return
        try:
            self.client.zoom(term.pane, on)
        except HerdrError as exc:
            log.warning("terminal relay: zoom failed (%s)", exc.code)
        except (OSError, ValueError) as exc:
            log.warning("terminal relay: zoom failed (%s)", type(exc).__name__)

    # -- output ------------------------------------------------------------------------------------
    def _push(self, tid, frames) -> int | None:
        """POST frames (at most 20 per call); the first non-200 status, or None when unreachable."""
        path = f"/api/v1/host/terminal/{tid}/output"
        status = None
        for i in range(0, len(frames), _POST_FRAMES):
            try:
                status, _ = self.http.request("POST", path, {"frames": frames[i:i + _POST_FRAMES]},
                                              timeout=_POST_TIMEOUT)
            except (OSError, ValueError):
                return None
            if status != 200:
                return status
        return status

    def _mirror_loop(self, tid, term) -> None:
        try:
            self._mirror(tid, term)
        except _Stopped:
            pass
        except Exception as exc:   # noqa: BLE001  a dying mirror ends its own terminal, never silently
            log.warning("terminal relay: a mirror failed (%s)", type(exc).__name__)
            try:
                self._end(tid, term, "relay_error")
            except Exception:   # noqa: BLE001
                term.dead = True

    def _mirror(self, tid, term) -> None:
        def pause(seconds):
            if term.stop.wait(seconds) or self._stop.is_set():
                raise _Stopped

        def fresh():   # a new mirror forgets what it sent, so the current screen goes out again
            return PaneMirror(self.client, term.pane, sleep=pause)

        mirror, fails, pushing, last_push = fresh(), 0, True, time.monotonic()
        while not term.stop.is_set() and not self._stop.is_set():
            try:
                changed = mirror.wait_for_change(_CHANGE_WAIT)
                frame = mirror.frame() if changed else None
                fails = 0
            except HerdrError as exc:
                if "not_found" in exc.code:
                    self._end(tid, term, "pane_closed")
                    return
                fails += 1
            except (OSError, ValueError, KeyError, TypeError):
                fails += 1
            else:
                if frame is None and time.monotonic() - last_push >= _PROBE_EVERY:
                    mirror = fresh()   # silence: send the screen again, a 404 then retires this terminal
                    last_push = time.monotonic()
                if frame is not None:
                    last_push = time.monotonic()
                    status = self._push(tid, [frame])
                    if status == 404:   # the broker retired this terminal
                        self._forget(tid, term)
                        return
                    if status != 200:
                        if pushing:
                            log.warning("terminal relay: pushing frames failed (%s)", status or "unreachable")
                        pushing, mirror = False, fresh()
                        pause(_PUSH_RETRY)
                    else:
                        pushing = True
                continue
            if fails >= _FAIL_LIMIT:
                self._end(tid, term, "herdr_unavailable")
                return
            pause(_FAIL_PAUSE)


def install_signal_handlers(relay):
    """SIGTERM/SIGINT stop the relay at once: the handler runs in the main thread, which is blocked in the long-poll,
    and raises there (run_forever turns that into a clean shutdown). Returns a function that restores the old handlers."""
    def handler(signum, frame):
        relay.stop()
        raise KeyboardInterrupt
    previous = {sig: signal.signal(sig, handler) for sig in (signal.SIGINT, signal.SIGTERM)}

    def restore():
        for sig, old in previous.items():
            signal.signal(sig, old)
    return restore

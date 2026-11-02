"""Row model, status mapping and ordering for the Shepherd Agents tab (pure functions)."""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone

from .agent_evolution import _BLOCKED   # the repo's own set of needs-attention statuses
from .redact import redact_text
from .terminal_attach import _CLOSED

STATUS_ORDER = ("blocked", "done", "working", "idle", "unknown", "closed")
_LIVE = frozenset(STATUS_ORDER[:5])
# Real session statuses map onto the vocabulary above: herdr's idle/done/working/blocked pass
# through; "active" (a live process with no detected agent state) is idle; "running"
# (create_session-style sessions) is working; _CLOSED values (e.g. stopped) are closed;
# the _BLOCKED set is blocked; anything else or missing is unknown.
_ALIASES = {"active": "idle", "running": "working"}
_STATUS_LINES = {
    "blocked": "Blocked, needs your attention",
    "done": "Finished a turn, waiting for input",
    "working": "Working",
    "idle": "Idle",
    "unknown": "Status unknown",
    "closed": "Closed",
}
_IDLE_CUTOFF_SECONDS = 7 * 86400


def agent_status(session: dict) -> str:
    status = str(session.get("status") or "").strip().lower()
    if status in _CLOSED:
        return "closed"
    if status in _BLOCKED:
        return "blocked"
    status = _ALIASES.get(status, status)
    return status if status in _LIVE else "unknown"


def _field(session: dict, key: str):
    """A session field that may sit at the top level or in a nested detail dict."""
    value = session.get(key)
    if value:
        return value
    for nested in session.values():
        if isinstance(nested, dict) and nested.get(key):
            return nested[key]
    return None


def agent_row(session: dict, summary: str | None) -> dict:
    status = agent_status(session)
    sid = session.get("session_id")
    machine = _field(session, "machine") or session.get("host_id")
    text = summary.strip() if isinstance(summary, str) else ""
    return {
        "id": sid,
        "name": str(session.get("display_name") or session.get("name") or sid or ""),
        "machine": str(machine) if machine else None,
        "status": status,
        "summary": text or _STATUS_LINES[status],
        "updated_at": str(session.get("updated_at") or session.get("created_at") or ""),
        "has_conversation": bool(_field(session, "agent_session")),
    }


def _epoch(stamp) -> float:
    try:
        parsed = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def sort_rows(rows: list[dict]) -> list[dict]:
    newest = sorted(rows, key=lambda r: _epoch(r.get("updated_at")), reverse=True)
    return sorted(newest, key=lambda r: STATUS_ORDER.index(r["status"]))


def visible(rows: list[dict], *, show_older: bool, now: float) -> list[dict]:
    if show_older:
        return list(rows)
    return [r for r in rows if r["status"] != "closed" and not (
        r["status"] == "idle"
        and now - _epoch(r.get("updated_at")) > _IDLE_CUTOFF_SECONDS)]


_NOT_READY = "The Agents view is not ready."
_DEFAULT_PAGE, _MAX_PAGE = 50, 200


class Agents:
    """The read-only ``agents/*`` procedures. ``repo`` is set by the API glue; every read uses the viewer role."""

    def __init__(self, bridge, error):
        self.b = bridge
        self.Error = error
        self.repo = None
        self._lock = threading.Lock()
        self._counter = int(time.time() * 1000)   # wall-clock seed: seq numbers keep rising across restarts
        self._changes: dict[str, tuple[int, dict]] = {}   # session id -> (seq, row) of its latest change
        self._prints: dict[str, str] = {}                  # session id -> fingerprint of its row as last seen
        self._shown: set[str] = set()                      # ids the list stream has told the app to show
        self.refresh_interval = 1.0                        # seconds between row rebuilds, shared by all subscribers
        self._refresh_lock = threading.Lock()
        self._refreshed_at: float | None = None

    def procs(self) -> dict:
        return {"agents/list": self.list, "agents/get": self.get,
                "agents/messages": self.messages, "agents/event": self.event,
                "agents/subscribe": self.subscribe, "agents/subscribeList": self.subscribe_list}

    # plumbing -----------------------------------------------------------------------------------------------
    def _repo(self):
        if self.repo is None:
            raise self.Error(503, "UNAVAILABLE", _NOT_READY)
        return self.repo

    def _missing(self):
        return self.Error(404, "NOT_FOUND", "No such agent.")

    @staticmethod
    def _sid(inp) -> str:
        sid = inp.get("id")
        return sid if isinstance(sid, str) else ""

    @staticmethod
    def _digests(repo, sessions: list[dict]) -> dict:
        """newest_seq and last_message for every session, under ONE inspector-lock acquisition."""
        return repo.inspector.phone_digest([s.get("session_id") for s in sessions], role="viewer")

    def _row(self, session: dict, digest: tuple) -> dict:
        newest, last = digest
        own = session.get("summary")
        summary = redact_text(own) if isinstance(own, str) and own.strip() else last
        row = agent_row(session, summary)
        row["has_conversation"] = newest is not None
        return row

    # procedures ---------------------------------------------------------------------------------------------
    def list(self, ctx, inp):
        repo = self._repo()
        self._refresh_throttled(repo)
        with self._lock:
            cursor = self._counter   # read before the rows, so a replay from it can only repeat, never skip
        sessions = repo.list_sessions()
        digests = self._digests(repo, sessions)
        rows = sort_rows([self._row(s, digests[s.get("session_id")]) for s in sessions])
        shown = visible(rows, show_older=bool(inp.get("showOlder")), now=time.time())
        return {"agents": shown, "hiddenCount": len(rows) - len(shown), "cursor": cursor}

    def get(self, ctx, inp):
        repo = self._repo()
        session = repo.get_session(self._sid(inp))
        if session is None:
            raise self._missing()
        row = self._row(session, self._digests(repo, [session])[session.get("session_id")])
        return {"agent": row, "hasConversation": row["has_conversation"]}

    def messages(self, ctx, inp):
        repo = self._repo()
        sid = self._sid(inp)
        before = inp.get("beforeSeq")
        limit = inp.get("limit", _DEFAULT_PAGE)
        if isinstance(limit, bool) or not isinstance(limit, int) or (
                before is not None and (isinstance(before, bool) or not isinstance(before, int))):
            raise self.Error(400, "BAD_REQUEST", "limit and beforeSeq must be integers.")
        limit = max(1, min(limit, _MAX_PAGE))
        if repo.get_session(sid) is None:
            raise self._missing()
        try:
            page = repo.inspector.query(sid, "viewer", limit=limit, before_seq=before)
        except KeyError:
            return {"events": [], "hasMore": False, "newestSeq": None, "rev": 0}
        return {"events": page["events"], "hasMore": page["has_more"], "newestSeq": repo.inspector.newest_seq(sid),
                "rev": page["rev"]}

    def event(self, ctx, inp):
        repo = self._repo()
        seq = inp.get("seq")
        if isinstance(seq, bool) or not isinstance(seq, int):
            raise self.Error(400, "BAD_REQUEST", "seq must be an integer.")
        try:
            return repo.inspector.get_event(self._sid(inp), seq, "viewer")
        except KeyError:
            raise self._missing() from None

    # live streams -------------------------------------------------------------------------------------------
    def _stream(self, ctx, inp, fetch):
        """The shared mechanics of ``threads/subscribe``: one capped, token-bound server-sent-event response."""
        from . import rakazo_events as ev
        from .rakazo_bridge import BridgeResponse
        bridge = self.b
        if not bridge._streams.acquire(blocking=False):
            raise self.Error(429, "TOO_MANY_REQUESTS", "Too many live connections.", {"Retry-After": "10"})
        frames = ev.event_stream(fetch, inp["cursor"], should_stop=lambda: bridge._closed.is_set() or not bridge.tokens.valid(
            ctx.token), **bridge.stream_options)
        return BridgeResponse(200, headers={"Content-Type": "text/event-stream", "Cache-Control": "no-store",
                                            "X-Accel-Buffering": "no", "Connection": "close"},
                              frames=frames, on_close=bridge._streams.release)

    def _check_stream(self, ctx, inp):
        cursor = inp.get("cursor")
        if isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < -1:
            raise self.Error(400, "BAD_REQUEST", "cursor must be an integer of at least -1.")
        if not ctx.accepts_stream:
            raise self.Error(400, "BAD_REQUEST", "Subscribe with accept: text/event-stream.")

    def subscribe(self, ctx, inp):
        self._check_stream(ctx, inp)
        repo = self._repo()
        sid = self._sid(inp)
        if repo.get_session(sid) is None:
            raise self._missing()

        def fetch(after: int) -> list[dict]:
            try:
                # the cursor is the per-session revision: a tool finishing re-uses its seq with a higher rev
                return [{"seq": e["rev"], "event": e} for e in repo.inspector.events_after(sid, after, role="viewer")]
            except KeyError:
                return []
        return self._stream(ctx, inp, fetch)

    def _refresh(self, repo) -> None:
        """Fold the current rows into the change log. Rows are built outside the lock; seq numbers are assigned
        under it, so every subscriber reads one consistent history. Hidden rows stay quiet; a row that was shown and
        is now hidden (closed or gone stale) is emitted once so the app can drop it."""
        now = time.time()
        sessions = repo.list_sessions()
        digests = self._digests(repo, sessions)
        rows = [self._row(s, digests[s.get("session_id")]) for s in sessions]
        with self._lock:
            for row in sorted(rows, key=lambda r: _epoch(r.get("updated_at"))):
                rid = row["id"]
                # heartbeats bump updated_at constantly; the fingerprint keeps only its minute (the row keeps the real value)
                print_ = json.dumps({**row, "updated_at": str(row.get("updated_at"))[:16]}, sort_keys=True, default=str)
                shown_now = bool(visible([row], show_older=False, now=now))
                if self._prints.get(rid) == print_ and (shown_now or rid not in self._shown):
                    continue   # unchanged; a shown row that only went stale falls through to its drop frame
                self._prints[rid] = print_
                if shown_now:
                    self._shown.add(rid)
                elif rid in self._shown:
                    self._shown.discard(rid)
                else:
                    continue
                self._counter += 1
                self._changes[rid] = (self._counter, row)

    def _refresh_throttled(self, repo) -> None:
        """At most one rebuild per ``refresh_interval`` however many subscribers poll; the others wait and then read."""
        with self._refresh_lock:
            now = time.monotonic()
            if self._refreshed_at is not None and now - self._refreshed_at < self.refresh_interval:
                return
            self._refresh(repo)
            self._refreshed_at = time.monotonic()

    def _list_fetch(self, repo, after: int) -> list[dict]:
        self._refresh_throttled(repo)
        with self._lock:
            if after > self._counter:   # direct-caller guard; subscribe_list resolves stale cursors at connect time. A cursor from before a restart: resend what is shown now
                fresh = [c for rid, c in self._changes.items() if rid in self._shown]
            else:
                fresh = [c for c in self._changes.values() if c[0] > after]
        return [{"seq": seq, "row": row} for seq, row in sorted(fresh, key=lambda c: c[0])]

    def subscribe_list(self, ctx, inp):
        self._check_stream(ctx, inp)
        repo = self._repo()
        self._refresh_throttled(repo)
        with self._lock:
            stale = inp["cursor"] > self._counter
        if stale:   # a cursor from before a restart: resolve it once here, so event_stream's own cursor then advances normally
            inp = {**inp, "cursor": -1}
        return self._stream(ctx, inp, lambda after: self._list_fetch(repo, after))

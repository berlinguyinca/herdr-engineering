"""Agent inspector store (spec 0200): bounded, redacted, access-controlled.

A *projection* of observable per-agent activity (conversation, tools, files,
tests, usage, associations) layered on the existing ingest transport and roles.
It is not a second event bus:

* transport  = ``POST /api/v1/ingest`` with ``entity_type="inspector"``;
* redaction  = :mod:`.redact` at the ingest boundary;
* durability = the existing sink for redacted conversation and summaries;
  reasoning, tool output and file diffs remain in bounded in-memory rings;
* roles      = the server's viewer / operator / ingest roles.

Reasoning is capability-dependent: it is accepted only when the emitter declared
a reasoning capability, and it is never synthesised.
"""
from __future__ import annotations

import math
import re
import secrets
import threading
import time
from collections import OrderedDict, deque
from datetime import UTC, datetime
from typing import Any

from .redact import redact_payload, redact_text

KINDS = frozenset({
    "capabilities", "message", "reasoning", "tool", "file", "test", "usage",
    "association", "model", "lifecycle",
})
#: Bounded conversation bodies and summaries mirrored to the existing sink.
DURABLE_KINDS = frozenset({"message", "capabilities", "association", "model", "lifecycle", "test"})
VIEWS = ("all", "conversation", "reasoning", "tools", "files", "tests", "usage")
_VIEW_KINDS = {
    "conversation": {"message"},
    "reasoning": {"reasoning"},
    "tools": {"tool"},
    "files": {"file"},
    "tests": {"test"},
    "usage": {"usage", "model"},
}
REASONING_LEVELS = ("none", "summary", "full")
ROLE_RANK = {"viewer": 0, "ingest": 0, "operator": 1}

DEFAULT_LIMITS = {
    "max_events_per_session": 2000,
    "max_bytes_per_session": 4_000_000,
    "max_body_bytes": 65_536,
    "max_diff_bytes": 262_144,
    "max_sessions": 256,
    "max_total_bytes": 256_000_000,
    "retention_hours": 24,
}
#: Hard ceilings: preferences can tighten or loosen limits, never past these.
HARD_CEILINGS = {
    "max_events_per_session": 50_000,
    "max_bytes_per_session": 64_000_000,
    "max_body_bytes": 1_000_000,
    "max_diff_bytes": 4_000_000,
    "max_sessions": 1024,
    "max_total_bytes": 1_000_000_000,
    "retention_hours": 720,
}
PREF_LIMIT_KEYS = ("max_events_per_session", "max_bytes_per_session", "retention_hours")

DEFAULT_PREFS = {
    "layout": "auto",             # auto | split | stacked
    "default_view": "conversation",
    "show_reasoning": False,      # hidden until the viewer opts in
    "show_tool_output": True,
    "tool_output_lines": 20,
    "follow_tail": True,
    "density": "comfortable",     # comfortable | compact
    "max_events_per_session": DEFAULT_LIMITS["max_events_per_session"],
    "max_bytes_per_session": DEFAULT_LIMITS["max_bytes_per_session"],
    "retention_hours": DEFAULT_LIMITS["retention_hours"],
}
_PREF_ENUMS = {
    "layout": ("auto", "split", "stacked"),
    "default_view": tuple(v for v in VIEWS if v != "all") + ("all",),
    "density": ("comfortable", "compact"),
}
_PREF_BOOLS = ("show_reasoning", "show_tool_output", "follow_tail")
_PREF_INTS = {
    "tool_output_lines": (1, 500),
    "max_events_per_session": (50, HARD_CEILINGS["max_events_per_session"]),
    "max_bytes_per_session": (50_000, HARD_CEILINGS["max_bytes_per_session"]),
    "retention_hours": (1, HARD_CEILINGS["retention_hours"]),
}

_SENSITIVE_PATH = re.compile(
    r"(^|/)(\.env(\..*)?|\.netrc|\.npmrc|\.pypirc|id_(rsa|dsa|ecdsa|ed25519)|"
    r"credentials?(\.\w+)?|secrets?(\.\w+)?|\.aws/.*|\.ssh/.*|.*\.(pem|key|p12|pfx|kdbx))$",
    re.IGNORECASE)
_SENSITIVE_CMD = re.compile(
    r"(\bprintenv\b|\benv\b\s*($|\||;)|\bcat\s+\S*(\.env|id_rsa|\.pem|credentials)|"
    r"\.ssh/|\.aws/|\bsecrets?\b|\bpasswd\b|\bkeychain\b)", re.IGNORECASE)

_SESSION_ID = re.compile(r"[A-Za-z0-9_.:@\-]{1,128}")
# Native pane separators are escaped inside the canonical herdr identity.
_CANONICAL_SESSION_ID = re.compile(r"herdr:[A-Za-z0-9_.-]+:[A-Za-z0-9_.-]+:(?:[A-Za-z0-9_.-]|%3A)+")
_MAX_NUM = 2 ** 53
MAX_SUBS_PER_SESSION = 16
MAX_SUBS_TOTAL = 512
_MAX_SEEN_IDS = 1024
_MAX_BATCH = 100
_MARKER = "\n… [{n} bytes truncated] …\n"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _s(value: Any, limit: int = 512) -> str | None:
    """Bounded identifier-ish string: control characters stripped, secrets redacted."""
    if value is None:
        return None
    text = "".join(ch for ch in str(value)[:limit] if ch >= " " and ch != "\x7f")
    return redact_text(text)


def _ts(value: Any, now: float | None = None) -> str:
    """Strict timestamp: parseable ISO-8601 (UTC-normalised, clamped to a sane window) or 'now'.

    Display only: retention runs on the server's own receive clock, so a bogus
    client timestamp can neither pin nor shorten retention.
    """
    now = time.time() if now is None else now
    if isinstance(value, str) and len(value) <= 64:
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            epoch = dt.timestamp()
            epoch = max(now - HARD_CEILINGS["retention_hours"] * 3600, min(now + 300, epoch))
            return datetime.fromtimestamp(epoch, UTC).isoformat()
        except (ValueError, OverflowError, OSError):
            pass
    return datetime.fromtimestamp(now, UTC).isoformat()


def _finite(v: Any) -> bool:
    return (isinstance(v, (int, float)) and not isinstance(v, bool)
            and math.isfinite(v) and abs(v) <= _MAX_NUM)


def truncate_text(text: str, max_bytes: int) -> tuple[str, bool]:
    """Head+tail truncation on a UTF-8 byte budget; never splits a character."""
    raw = text.encode("utf-8", "replace")
    if len(raw) <= max_bytes:
        return text, False
    keep = max(max_bytes - 40, 16)
    head_n, tail_n = keep * 2 // 3, keep - keep * 2 // 3
    head = raw[:head_n].decode("utf-8", "ignore")
    tail = raw[len(raw) - tail_n:].decode("utf-8", "ignore")
    return head + _MARKER.format(n=len(raw) - head_n - tail_n) + tail, True


def _clamp_int(value: Any, lo: int, hi: int) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return max(lo, min(hi, int(value)))


def validate_prefs(raw: Any) -> dict:
    """Return only valid, clamped preference keys; raise ValueError on non-dicts."""
    if not isinstance(raw, dict):
        raise ValueError("preferences must be a JSON object")
    out: dict = {}
    for key, allowed in _PREF_ENUMS.items():
        if key in raw:
            if raw[key] not in allowed:
                raise ValueError(f"{key} must be one of {list(allowed)}")
            out[key] = raw[key]
    for key in _PREF_BOOLS:
        if key in raw:
            if not isinstance(raw[key], bool):
                raise ValueError(f"{key} must be a boolean")
            out[key] = raw[key]
    for key, (lo, hi) in _PREF_INTS.items():
        if key in raw:
            v = _clamp_int(raw[key], lo, hi)
            if v is None:
                raise ValueError(f"{key} must be a number")
            out[key] = v
    return out


class _Session:
    __slots__ = ("session_id", "events", "by_seq", "bytes", "next_seq", "rev",
                 "dropped_ring", "client_dropped", "seen", "capabilities",
                 "assoc", "models", "usage_totals", "usage_latest", "last_event",
                 "last_ingest", "ended", "prefs", "tools", "tests", "subs",
                 "evicted_to")

    def __init__(self, session_id: str):
        self.session_id = session_id
        self.events: deque = deque()
        self.by_seq: dict[int, dict] = {}
        self.bytes = 0
        self.next_seq = 0
        self.rev = 0
        self.dropped_ring = 0
        self.client_dropped = 0
        self.seen: OrderedDict = OrderedDict()
        self.capabilities: dict | None = None
        self.assoc: dict = {}
        self.models: deque = deque(maxlen=20)
        self.usage_totals = {"input_tokens": 0, "output_tokens": 0,
                             "reasoning_tokens": 0, "cache_read_tokens": 0}
        self.usage_latest: dict | None = None
        self.last_event: float = 0.0
        self.last_ingest: str | None = None
        self.ended = False
        self.prefs: dict = {}
        self.tools: dict[str, dict] = {}   # call_id -> live ring entry
        self.tests: dict[str, dict] = {}   # run_id  -> live ring entry
        self.subs: set = set()
        self.evicted_to = -1


class Subscriber:
    """Bounded per-session stream queue. A slow client loses frames, not the server."""

    def __init__(self, maxsize: int = 256):
        self._items: deque = deque()
        self._max = maxsize
        self._cv = threading.Condition()
        self.dropped = 0
        self.closed = False

    def push(self, item: dict) -> None:
        with self._cv:
            if len(self._items) >= self._max:
                # Drop the whole backlog: the client resyncs via since_rev.
                self.dropped += len(self._items) + 1
                self._items.clear()
            else:
                self._items.append(item)
            self._cv.notify()

    def get(self, timeout: float) -> dict | None:
        with self._cv:
            if not self._items and not self.dropped and not self.closed:
                self._cv.wait(timeout)
            if self.dropped:
                n, self.dropped = self.dropped, 0
                return {"type": "gap", "dropped": n}
            if self._items:
                return self._items.popleft()
            return None

    def close(self) -> None:
        with self._cv:
            self.closed = True
            self._cv.notify_all()


class InspectorStore:
    def __init__(self, *, limits: dict | None = None, persist=None,
                 reasoning_roles: str = "operator",
                 sensitive_roles: str = "operator", store_reasoning: bool = True,
                 clock=time.time):
        self.limits = dict(DEFAULT_LIMITS)
        for k, v in (limits or {}).items():
            if k in DEFAULT_LIMITS and isinstance(v, (int, float)) and v > 0:
                self.limits[k] = min(int(v), HARD_CEILINGS[k])
        self._persist = persist
        # Unknown role names fail closed (operator).
        self._min_reasoning = ROLE_RANK.get(reasoning_roles, 1)
        self._min_sensitive = ROLE_RANK.get(sensitive_roles, 1)
        self.store_reasoning = store_reasoning
        self._clock = clock
        self._sessions: OrderedDict[str, _Session] = OrderedDict()
        self._global_prefs: dict = {}
        #: Changes on every process start; clients resync when it differs (seq/rev restart).
        self.epoch = secrets.token_hex(6)
        self._lock = threading.RLock()
        self.stats = {"accepted": 0, "duplicates": 0, "rejected": 0,
                      "evicted_events": 0, "evicted_sessions": 0}

    @classmethod
    def from_config(cls, config=None, **kw) -> InspectorStore:
        """Operator configuration, the fleet file's ``control_plane.inspector_limits``
        mapping (every key optional)::

            reasoning_role    minimum role to read reasoning (viewer|operator, default operator)
            sensitive_role    minimum role to read sensitive output/diffs (default operator)
            store_reasoning   false = never accept/store reasoning (default true)
            max_events / max_bytes / max_sessions / max_total_bytes / retention_hours
        """
        config = config if isinstance(config, dict) else {}
        limits = {}
        for key, name in (("max_events_per_session", "max_events"), ("max_bytes_per_session", "max_bytes"),
                          ("max_sessions", "max_sessions"), ("max_total_bytes", "max_total_bytes"),
                          ("retention_hours", "retention_hours")):
            raw = config.get(name)
            if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0:
                limits[key] = raw
        return cls(
            limits=limits,
            reasoning_roles=str(config.get("reasoning_role", "operator")),
            sensitive_roles=str(config.get("sensitive_role", "operator")),
            store_reasoning=config.get("store_reasoning", True) is not False, **kw)

    # ----------------------------------------------------------- preferences
    def get_prefs(self, session_id: str | None = None) -> dict:
        with self._lock:
            merged = {**DEFAULT_PREFS, **self._global_prefs}
            if session_id and session_id in self._sessions:
                merged.update(self._sessions[session_id].prefs)
            return merged

    def raw_prefs(self, session_id: str | None = None) -> dict:
        with self._lock:
            if session_id is None:
                return dict(self._global_prefs)
            s = self._sessions.get(session_id)
            return dict(s.prefs) if s else {}

    def set_prefs(self, raw: Any, session_id: str | None = None, *,
                  persist: bool = True) -> dict:
        clean = validate_prefs(raw)
        with self._lock:
            if session_id is None:
                self._global_prefs.update(clean)
            else:
                self._session(session_id).prefs.update(clean)
                self._enforce(self._sessions[session_id])
            if persist and self._persist:
                self._safe_persist({
                    "entity_type": "inspector",
                    "entity_id": session_id or "_global",
                    "event_type": "inspector.preferences",
                    "payload": {"scope": "session" if session_id else "global",
                                "prefs": clean},
                    "source_timestamp": _now_iso(),
                })
            return self.get_prefs(session_id)

    # --------------------------------------------------------------- ingest
    def ingest(self, session_id: str, payload: dict, *, persist: bool = True) -> dict:
        """Ingest one envelope payload (``{"events": [...]}`` or a single event)."""
        if (not isinstance(session_id, str) or len(session_id) > 128 or
                not (_SESSION_ID.fullmatch(session_id) or _CANONICAL_SESSION_ID.fullmatch(session_id))):
            raise ValueError("entity_id must be a valid session id (1-128 chars; canonical pane %3A escapes allowed)")
        events = payload.get("events") if isinstance(payload, dict) else None
        if events is None:
            events = [payload]
        if not isinstance(events, list):
            raise ValueError("events must be a list")
        if len(events) > _MAX_BATCH:
            raise ValueError(f"batch too large (max {_MAX_BATCH})")
        result = {"accepted": 0, "duplicates": 0, "rejected": [], "session_id": session_id}
        with self._lock:
            sess = self._session(session_id)
            for i, ev in enumerate(events):
                try:
                    status = self._ingest_one(sess, ev, persist)
                except RuntimeError:
                    # A partially persisted batch is retried by the emitter. Keep
                    # successful entries bounded while leaving the failed id unseen.
                    self._enforce(sess)
                    self._enforce_global(keep=session_id)
                    raise
                except ValueError as exc:
                    self.stats["rejected"] += 1
                    result["rejected"].append({"index": i, "error": str(exc)})
                    continue
                if status == "dup":
                    result["duplicates"] += 1
                    self.stats["duplicates"] += 1
                else:
                    result["accepted"] += 1
                    self.stats["accepted"] += 1
            self._enforce(sess)
            self._enforce_global(keep=session_id)
        return result

    def _ingest_one(self, sess: _Session, ev: Any, persist: bool) -> str:
        if not isinstance(ev, dict):
            raise ValueError("event must be an object")
        kind = ev.get("kind")
        if kind not in KINDS:
            raise ValueError(f"unknown kind {kind!r}")
        eid = _s(ev.get("event_id"), 128) or None
        if eid and eid in sess.seen:
            return "dup"
        try:
            status = self._apply(sess, kind, ev, persist)
        except ValueError:
            raise
        except (TypeError, AttributeError, KeyError, OverflowError, IndexError) as exc:
            raise ValueError(f"malformed {kind} event") from exc
        if eid:  # only remember ids of events that were actually accepted
            sess.seen[eid] = True
            while len(sess.seen) > max(_MAX_SEEN_IDS, self._limits_for(sess)[0]):
                sess.seen.popitem(last=False)
        return status

    def _apply(self, sess: _Session, kind: str, ev: dict, persist: bool) -> str:
        data = self._sanitize(kind, ev)
        base = {
            "agent_id": _s(ev.get("agent_id"), 128) or "main",
            "parent_agent_id": _s(ev.get("parent_agent_id"), 128),
            "event_id": _s(ev.get("event_id"), 128),
            "ts": _ts(ev.get("ts"), self._clock()),
        }
        flagged = bool(ev.get("sensitive"))
        sess.last_event = self._clock()
        sess.last_ingest = _now_iso()
        if sess.ended and kind == "lifecycle" and data.get("phase") in ("started", "restored", "restarted"):
            sess.ended = False

        if kind == "capabilities":
            sess.capabilities = data
            self._store(sess, kind, base, data, flagged, persist)
            return "ok"
        if kind == "reasoning":
            cap = (sess.capabilities or {}).get("reasoning", "none")
            if cap not in ("summary", "full") or not self.store_reasoning:
                raise ValueError("reasoning not exposed by this agent/runtime (capability none)")
        if kind == "tool":
            return self._tool(sess, base, data, flagged)
        if kind == "test":
            return self._test(sess, base, data, flagged, persist)
        if kind == "usage":
            for k in sess.usage_totals:
                v = data.get(k)
                if isinstance(v, int) and v > 0:
                    sess.usage_totals[k] = min(sess.usage_totals[k] + v, _MAX_NUM)
            sess.usage_latest = data
        elif kind == "association":
            sess.assoc.update({k: v for k, v in data.items() if v not in (None, "", {})})
        elif kind == "model":
            sess.models.append({**data, "ts": base["ts"]})
        elif kind == "lifecycle":
            phase = data.get("phase")
            if phase in ("restarted", "interrupted", "ended"):
                # Never claim success for work whose telemetry was cut off.
                for t in sess.tools.values():
                    if t["data"].get("status") == "running":
                        t["data"]["status"] = "interrupted"
                        t["data"]["incomplete"] = True
                        self._touch(sess, t)
                sess.tools = {k: t for k, t in sess.tools.items() if t["data"]["status"] == "running"}
            if phase == "ended":
                sess.ended = True
            dropped = data.get("dropped")
            if isinstance(dropped, int) and dropped > 0:
                sess.client_dropped += dropped
        if kind == "file":
            flagged = flagged or bool(_SENSITIVE_PATH.search(data.get("path") or ""))
        self._store(sess, kind, base, data, flagged, persist)
        return "ok"

    # --- sanitising -------------------------------------------------------
    def _text(self, value: Any, max_bytes: int | None = None) -> tuple[str | None, bool, bool]:
        """Return (redacted+truncated text, truncated, redacted)."""
        if value is None:
            return None, False, False
        raw = value if isinstance(value, str) else str(value)
        cap = max_bytes or self.limits["max_body_bytes"]
        # Bound the work redaction does (and the lock hold time) before scanning.
        pre, pre_trunc = truncate_text(raw, cap * 2)
        red = redact_text(pre)
        text, trunc = truncate_text(red, cap)
        return text, trunc or pre_trunc, red != pre

    def _sanitize(self, kind: str, ev: dict) -> dict:
        d: dict = {}
        flags = {"redacted": False, "truncated": False}

        def text(key: str, src: str | None = None, limit: int | None = None):
            val, trunc, red = self._text(ev.get(src or key), limit)
            if val is not None:
                d[key] = val
            flags["redacted"] |= red
            flags["truncated"] |= trunc or bool(ev.get("truncated"))

        def num(key: str):
            v = ev.get(key)
            if _finite(v):
                d[key] = v

        def sstr(key: str, limit: int = 512):
            v = _s(ev.get(key), limit)
            if v is not None:
                d[key] = redact_text(v)

        if kind == "capabilities":
            r = ev.get("reasoning", "none")
            d["reasoning"] = r if r in REASONING_LEVELS else "none"
            for k in ("streaming_tools", "diffs", "usage"):
                d[k] = bool(ev.get(k))
            sstr("model", 256)
        elif kind == "message":
            sstr("message_id", 128)
            role = ev.get("role")
            if role not in (None, "user", "assistant", "system"):
                raise ValueError("message.role must be user|assistant|system")
            d["role"] = role if role in ("user", "assistant", "system") else "assistant"
            text("text")
        elif kind == "reasoning":
            sstr("message_id", 128)
            src = ev.get("source")
            d["source"] = src if src in ("provider_summary", "provider_raw") else "provider_summary"
            text("text")
        elif kind == "tool":
            cid = _s(ev.get("call_id"), 256)
            if not cid:
                raise ValueError("tool.call_id is required")
            d["call_id"] = cid
            sstr("tool", 128)
            phase = ev.get("phase")
            if phase not in ("start", "update", "end"):
                raise ValueError("tool.phase must be start|update|end")
            d["phase"] = phase
            args = ev.get("args_preview")
            if isinstance(args, (dict, list)):
                args = str(redact_payload(args))
            if args is not None:
                val, trunc, red = self._text(args, 2048)
                d["args_preview"] = val
                flags["redacted"] |= red
                flags["truncated"] |= trunc
            text("output")
            if "is_error" in ev:
                d["is_error"] = bool(ev["is_error"])
            num("exit_code")
            num("duration_ms")
        elif kind == "file":
            p = _s(ev.get("path"), 1024)
            if not p:
                raise ValueError("file.path is required")
            d["path"] = redact_text(p)
            op = ev.get("op")
            d["op"] = op if op in ("read", "write", "edit", "delete") else "read"
            sstr("call_id", 256)
            text("diff", limit=self.limits["max_diff_bytes"])
            num("additions")
            num("deletions")
            if "diff" in d and "additions" not in d:
                lines = d["diff"].splitlines()
                d["additions"] = sum(1 for ln in lines if ln.startswith("+") and not ln.startswith("+++"))
                d["deletions"] = sum(1 for ln in lines if ln.startswith("-") and not ln.startswith("---"))
        elif kind == "test":
            rid = _s(ev.get("run_id"), 256)
            if not rid:
                raise ValueError("test.run_id is required")
            d["run_id"] = rid
            sstr("command", 1024)
            phase = ev.get("phase")
            d["phase"] = phase if phase in ("start", "end") else "end"
            for k in ("passed", "failed", "skipped", "duration_ms"):
                num(k)
            fails = []
            raw_failures = ev.get("failures") or []
            if not isinstance(raw_failures, list):
                raise ValueError("test.failures must be a list")
            for f in raw_failures[:50]:
                if not isinstance(f, dict):
                    continue
                msg, tr, red = self._text(f.get("message"), 4096)
                flags["redacted"] |= red
                flags["truncated"] |= tr
                fails.append({"name": redact_text(_s(f.get("name"), 512) or ""),
                              "message": msg or "",
                              "file": _s(f.get("file"), 512),
                              "line": f["line"] if _finite(f.get("line")) and isinstance(f.get("line"), int) else None})
            if fails:
                d["failures"] = fails
        elif kind == "usage":
            for k in ("input_tokens", "output_tokens", "reasoning_tokens",
                      "cache_read_tokens", "context_tokens", "context_window"):
                v = ev.get(k)
                if _finite(v) and isinstance(v, int) and v >= 0:
                    d[k] = v
            sstr("model", 256)
        elif kind == "association":
            for k in ("mission_id", "handoff_id", "handoff_from", "parent_session_id",
                      "worktree", "branch"):
                sstr(k, 512)
            for key in ("pr", "issue"):
                ref = ev.get(key)
                if isinstance(ref, dict):
                    d[key] = {"number": ref["number"] if _finite(ref.get("number")) and isinstance(ref.get("number"), int) else None,
                              "url": redact_text(_s(ref.get("url"), 1024) or ""),
                              "repo": _s(ref.get("repo"), 256)}
        elif kind == "model":
            for k in ("model", "provider", "previous_model"):
                sstr(k, 256)
            r = ev.get("reason")
            d["reason"] = r if r in ("switch", "handoff", "fallback") else "switch"
        elif kind == "lifecycle":
            ph = ev.get("phase")
            d["phase"] = ph if ph in ("started", "restored", "restarted", "interrupted", "ended") else "started"
            for k in ("restored_from_seq", "dropped"):
                num(k)
        d["_flags"] = flags
        return d

    # --- storage ----------------------------------------------------------
    def _session(self, session_id: str) -> _Session:
        sess = self._sessions.get(session_id)
        if sess is None:
            sess = _Session(session_id)
            self._sessions[session_id] = sess
        else:
            self._sessions.move_to_end(session_id)
        return sess

    @staticmethod
    def _size(data: dict) -> int:
        n = 96
        for v in data.values():
            if isinstance(v, str):
                n += len(v.encode("utf-8", "replace"))
            elif isinstance(v, (list, dict)):
                n += len(str(v))
            else:
                n += 8
        return n

    def _store(self, sess: _Session, kind: str, base: dict, data: dict, flagged: bool,
               persist: bool) -> dict:
        flags = data.pop("_flags", {"redacted": False, "truncated": False})
        entry = {
            "seq": sess.next_seq, "rev": 0, "kind": kind, **base, "recv": self._clock(),
            "sensitive": flagged, "redacted": flags["redacted"],
            "truncated": flags["truncated"], "data": data, "size": 0,
        }
        if (persist and kind in DURABLE_KINDS and self._persist and
                (kind != "message" or data["role"] in ("user", "assistant"))):
            durable = {
                "entity_type": "inspector", "entity_id": sess.session_id,
                "event_type": f"inspector.{kind}", "session_id": sess.session_id,
                "source_timestamp": base["ts"],
                "payload": {**{k: v for k, v in data.items() if k != "failures"},
                            "agent_id": base["agent_id"], "parent_agent_id": base["parent_agent_id"],
                            "event_id": base["event_id"], "kind": kind, "sensitive": flagged,
                            "redacted": entry["redacted"], "truncated": entry["truncated"],
                            "_received_at": entry["recv"], "_sequence": entry["seq"]},
            }
            if kind == "message":
                # The emitter advances its checkpoint on success. Commit before
                # publishing or remembering the id so a failed write is retryable.
                try:
                    self._persist(durable)
                except Exception as exc:
                    raise RuntimeError("conversation persistence unavailable") from exc
            else:
                self._safe_persist(durable)
        sess.next_seq += 1
        entry["size"] = self._size(data)
        sess.bytes += entry["size"]
        sess.events.append(entry)
        sess.by_seq[entry["seq"]] = entry
        self._touch(sess, entry, account=False)
        return entry

    def _touch(self, sess: _Session, entry: dict, *, account: bool = True) -> None:
        if account:
            new = self._size(entry["data"])
            sess.bytes += new - entry["size"]
            entry["size"] = new
        sess.rev += 1
        entry["rev"] = sess.rev
        for sub in list(sess.subs):
            sub.push({"type": "event", "entry": entry["seq"], "rev": entry["rev"]})

    def _tool(self, sess: _Session, base: dict, data: dict, flagged: bool) -> str:
        cid = data["call_id"]
        phase = data.pop("phase")
        flags = data.get("_flags", {})
        live = sess.tools.get(cid)
        if live is None or live["seq"] not in sess.by_seq:
            sess.tools.pop(cid, None)
            live = self._store(sess, "tool", base, {
                "call_id": cid, "tool": data.get("tool") or "unknown", "status": "running",
                "args_preview": data.get("args_preview"), "output": "",
                "incomplete": phase != "start", "_flags": {"redacted": False, "truncated": False}},
                flagged or self._cmd_sensitive(data), False)
            sess.tools[cid] = live
            live["t0"] = self._clock()
        d = live["data"]
        if data.get("tool") and d.get("tool") in (None, "unknown"):
            d["tool"] = data["tool"]
        if data.get("args_preview") and not d.get("args_preview"):
            d["args_preview"] = data["args_preview"]
            live["sensitive"] = live["sensitive"] or self._cmd_sensitive(d)
        live["sensitive"] = live["sensitive"] or flagged
        live["redacted"] |= bool(flags.get("redacted"))
        live["truncated"] |= bool(flags.get("truncated"))
        if "output" in data:
            d["output"] = data["output"]
        if phase == "end":
            d["status"] = "error" if data.get("is_error") else "ok"
            for k in ("exit_code", "duration_ms"):
                if k in data:
                    d[k] = data[k]
            if "duration_ms" not in d and live.get("t0") is not None:
                d["duration_ms"] = int((self._clock() - live["t0"]) * 1000)
            sess.tools.pop(cid, None)
            live.pop("t0", None)
        self._touch(sess, live)
        return "ok"

    @staticmethod
    def _cmd_sensitive(data: dict) -> bool:
        return bool(_SENSITIVE_CMD.search(data.get("args_preview") or ""))

    def _test(self, sess: _Session, base: dict, data: dict, flagged: bool, persist: bool) -> str:
        rid = data["run_id"]
        live = sess.tests.get(rid)
        flags = data.pop("_flags", {})
        if live is None or live["seq"] not in sess.by_seq:
            live = self._store(sess, "test", base, {
                "run_id": rid, "command": data.get("command"), "status": "running",
                "_flags": {"redacted": False, "truncated": False}}, flagged, False)
            sess.tests[rid] = live
        d = live["data"]
        for k in ("command", "passed", "failed", "skipped", "duration_ms", "failures"):
            if k in data:
                d[k] = data[k]
        live["sensitive"] = live["sensitive"] or flagged or self._cmd_sensitive(
            {"args_preview": d.get("command")})
        live["redacted"] |= bool(flags.get("redacted"))
        live["truncated"] |= bool(flags.get("truncated"))
        if data["phase"] == "end":
            d["status"] = "failed" if (d.get("failed") or 0) > 0 else "passed"
            sess.tests.pop(rid, None)
            if persist and self._persist:
                self._safe_persist({
                    "entity_type": "inspector", "entity_id": sess.session_id,
                    "event_type": "inspector.test", "session_id": sess.session_id,
                    "source_timestamp": base["ts"],
                    "payload": {"kind": "test", **{k: v for k, v in d.items() if k != "failures"},
                                # Sensitive runs persist counts/status only, never bodies.
                                "failures": [] if live["sensitive"] else (d.get("failures") or [])[:20],
                                "sensitive": live["sensitive"],
                                **({"command": None} if live["sensitive"] else {}),
                                "agent_id": base["agent_id"]}})
        self._touch(sess, live)
        return "ok"

    def _safe_persist(self, ev: dict) -> None:
        try:
            self._persist(ev)
        except Exception:  # durability is best-effort and never breaks ingest
            pass

    # --- bounds -----------------------------------------------------------
    def _limits_for(self, sess: _Session) -> tuple[int, int, int]:
        p = {**DEFAULT_PREFS, **self._global_prefs, **sess.prefs}
        return (min(p["max_events_per_session"], self.limits["max_events_per_session"]),
                min(p["max_bytes_per_session"], self.limits["max_bytes_per_session"]),
                min(p["retention_hours"], self.limits["retention_hours"]))

    def _drop_oldest(self, sess: _Session) -> None:
        old = sess.events.popleft()
        sess.by_seq.pop(old["seq"], None)
        sess.bytes -= old["size"]
        sess.dropped_ring += 1
        sess.evicted_to = old["seq"]
        self.stats["evicted_events"] += 1
        # Only unlink the live map if it still points at the evicted entry (ids can be reused).
        if old["kind"] == "tool" and sess.tools.get(old["data"].get("call_id")) is old:
            del sess.tools[old["data"]["call_id"]]
        elif old["kind"] == "test" and sess.tests.get(old["data"].get("run_id")) is old:
            del sess.tests[old["data"]["run_id"]]

    def _enforce(self, sess: _Session) -> None:
        max_events, max_bytes, hours = self._limits_for(sess)
        cutoff = self._clock() - hours * 3600
        while sess.events and (len(sess.events) > max_events or
                               (sess.bytes > max_bytes and len(sess.events) > 1)):
            self._drop_oldest(sess)
        while sess.events and sess.events[0]["recv"] < cutoff:   # recv is monotonic
            self._drop_oldest(sess)

    def _enforce_global(self, keep: str | None = None) -> None:
        total = sum(s.bytes for s in self._sessions.values())
        while (len(self._sessions) > self.limits["max_sessions"] or
               total > self.limits["max_total_bytes"]) and len(self._sessions) > 1:
            cands = [s for s in self._sessions.values() if not s.subs and s.session_id != keep]
            victim = next((s for s in cands if s.ended), None) or (cands[0] if cands else None)
            if victim is None:
                break
            total -= victim.bytes
            del self._sessions[victim.session_id]
            self.stats["evicted_sessions"] += 1

    def sweep(self) -> int:
        """Apply retention to every session (called periodically / on demand)."""
        with self._lock:
            before = self.stats["evicted_events"]
            for s in list(self._sessions.values()):
                self._enforce(s)
            return self.stats["evicted_events"] - before

    # ----------------------------------------------------------------- read
    def _allowed(self, entry: dict, role: str) -> bool:
        rank = ROLE_RANK.get(role, 0)
        if entry["kind"] == "reasoning":
            return rank >= self._min_reasoning
        if entry["sensitive"]:
            return rank >= self._min_sensitive
        return True

    def _render(self, entry: dict, role: str, *, full: bool) -> dict:
        out = {k: entry[k] for k in ("seq", "rev", "kind", "agent_id", "parent_agent_id", "ts",
                                     "sensitive", "redacted", "truncated", "size")}
        data = dict(entry["data"])
        if not self._allowed(entry, role):
            for k in ("text", "output", "diff", "failures", "args_preview", "command"):
                if k in data:
                    data[k] = None
            if "path" in data:
                data["path"] = "[withheld]"
            data["withheld"] = True
            data["withheld_reason"] = ("reasoning requires a higher role"
                                       if entry["kind"] == "reasoning"
                                       else "sensitive output requires a higher role")
        else:
            if not full:
                for key in ("output", "diff", "text"):
                    v = data.get(key)
                    if isinstance(v, str) and len(v) > 2000:
                        data[key] = v[:2000]
                        data[key + "_more"] = True
        out["data"] = data
        return out

    def _searchable(self, entry: dict) -> str:
        d = entry["data"]
        return " ".join(str(d.get(k) or "") for k in (
            "text", "tool", "args_preview", "output", "path", "diff", "command",
            "model", "message", "call_id", "run_id", "branch", "worktree")) + " " + " ".join(
            f"{f.get('name')} {f.get('message')}" for f in d.get("failures", []))

    def query(self, session_id: str, role: str = "viewer", *, view: str = "all",
              q: str | None = None, agent_id: str | None = None, status: str | None = None,
              since_rev: int = 0, limit: int = 200, before_seq: int | None = None) -> dict:
        if view not in VIEWS:
            raise ValueError(f"view must be one of {list(VIEWS)}")
        limit = max(1, min(int(limit), 1000))
        with self._lock:
            sess = self._sessions.get(session_id)
            if sess is None:
                raise KeyError(session_id)
            kinds = _VIEW_KINDS.get(view)
            needle = (q or "").lower().strip()
            rows = []
            for e in reversed(sess.events):   # newest first
                if e["rev"] <= since_rev:
                    continue
                if kinds is not None and e["kind"] not in kinds:
                    continue
                if before_seq is not None and e["seq"] >= before_seq:
                    continue
                if agent_id and e["agent_id"] != agent_id:
                    continue
                if status and e["data"].get("status") != status:
                    continue
                if needle:
                    # Never search content the caller is not allowed to read.
                    if not self._allowed(e, role) or needle not in self._searchable(e).lower():
                        continue
                rows.append(e)
                if len(rows) > limit:
                    break
            has_more = len(rows) > limit
            rows = rows[:limit]
            return {
                "session_id": session_id, "view": view, "epoch": self.epoch,
                "events": [self._render(e, role, full=False) for e in rows],
                "has_more": has_more, "rev": sess.rev,
                "dropped": sess.dropped_ring, "oldest_available_seq":
                    sess.events[0]["seq"] if sess.events else None,
                "summary": self._summary(sess, role),
                "capabilities": self._caps(sess),
                "prefs": self.get_prefs(session_id),
            }

    def get_event(self, session_id: str, seq: int, role: str = "viewer") -> dict:
        with self._lock:
            sess = self._sessions.get(session_id)
            if sess is None or seq not in sess.by_seq:
                raise KeyError(f"{session_id}/{seq}")
            return self._render(sess.by_seq[seq], role, full=True)

    def _caps(self, sess: _Session) -> dict:
        c = sess.capabilities
        if c is None:
            return {"declared": False, "reasoning": "unknown"}
        return {"declared": True, **c}

    def _summary(self, sess: _Session, role: str) -> dict:
        counts: dict[str, int] = {}
        agents: dict[str, dict] = {}
        files: OrderedDict = OrderedDict()
        running = []
        tests = []
        for e in sess.events:
            ok = self._allowed(e, role)   # withheld entries contribute counts only
            counts[e["kind"]] = counts.get(e["kind"], 0) + 1
            a = agents.setdefault(e["agent_id"], {"agent_id": e["agent_id"], "events": 0,
                                                   "tools": 0, "parent_agent_id": e["parent_agent_id"],
                                                   "last_ts": e["ts"]})
            a["events"] += 1
            a["last_ts"] = e["ts"]
            d = e["data"]
            if e["kind"] == "tool":
                a["tools"] += 1
                if d.get("status") == "running":
                    running.append({"seq": e["seq"], "tool": d.get("tool"), "agent_id": e["agent_id"],
                                    "call_id": d.get("call_id") if ok else None})
            elif e["kind"] == "file":
                pth = d["path"] if ok else "[withheld]"
                f = files.setdefault(pth, {"path": pth, "ops": [], "additions": 0,
                                                  "deletions": 0, "last_seq": e["seq"],
                                                  "sensitive": False, "agents": []})
                if d["op"] not in f["ops"]:
                    f["ops"].append(d["op"])
                f["additions"] += d.get("additions") or 0
                f["deletions"] += d.get("deletions") or 0
                f["last_seq"] = e["seq"]
                f["sensitive"] = f["sensitive"] or e["sensitive"]
                if e["agent_id"] not in f["agents"]:
                    f["agents"].append(e["agent_id"])
            elif e["kind"] == "test":
                tests.append({"seq": e["seq"], "run_id": d["run_id"], "status": d.get("status"),
                              "passed": d.get("passed"), "failed": d.get("failed"),
                              "skipped": d.get("skipped"),
                              "command": d.get("command") if ok else None,
                              "agent_id": e["agent_id"],
                              "failures": [f["name"] for f in d.get("failures", [])][:10] if ok else []})
        ul = sess.usage_latest or {}
        ctx, win = ul.get("context_tokens"), ul.get("context_window")
        age = (self._clock() - sess.last_event) if sess.last_event else None
        return {
            "counts": counts,
            "agents": sorted(agents.values(), key=lambda a: a["agent_id"]),
            "running_tools": running,
            "files": list(files.values())[-200:],
            "tests": tests[-20:],
            "usage": {**sess.usage_totals, "latest": ul or None,
                      "context_pct": round(100 * ctx / win, 1) if ctx and win and win > 0 else None},
            "models": list(sess.models),
            "association": dict(sess.assoc),
            "reasoning_withheld": ROLE_RANK.get(role, 0) < self._min_reasoning,
            "last_event_age_s": round(age, 1) if age is not None else None,
            "ended": sess.ended,
            "client_dropped": sess.client_dropped,
            "ring": {"events": len(sess.events), "bytes": sess.bytes, "evicted": sess.dropped_ring},
        }

    # --------------------------------------------------------------- stream
    def subscribe(self, session_id: str) -> Subscriber:
        with self._lock:
            if not self.can_subscribe(session_id):
                raise ValueError("too many streams")
            sess = self._session(session_id)
            sub = Subscriber()
            sess.subs.add(sub)
            return sub

    def can_subscribe(self, session_id: str) -> bool:
        with self._lock:
            total = sum(len(x.subs) for x in self._sessions.values())
            cur = self._sessions.get(session_id)
            return total < MAX_SUBS_TOTAL and (cur is None or len(cur.subs) < MAX_SUBS_PER_SESSION)

    def unsubscribe(self, session_id: str, sub: Subscriber) -> None:
        with self._lock:
            s = self._sessions.get(session_id)
            if s:
                s.subs.discard(sub)
        sub.close()

    def stream_entry(self, session_id: str, seq: int, role: str) -> dict | None:
        with self._lock:
            s = self._sessions.get(session_id)
            if s is None or seq not in s.by_seq:
                return None
            return self._render(s.by_seq[seq], role, full=False)

    # --------------------------------------------------------------- health
    def health(self) -> dict:
        with self._lock:
            return {
                "sessions": len(self._sessions),
                "events": sum(len(s.events) for s in self._sessions.values()),
                "bytes": sum(s.bytes for s in self._sessions.values()),
                "subscribers": sum(len(s.subs) for s in self._sessions.values()),
                "limits": dict(self.limits), **self.stats,
            }

    def has_session(self, session_id: str) -> bool:
        with self._lock:
            return session_id in self._sessions

    # ------------------------------------------------------------- hydrate
    def hydrate(self, events: list[dict]) -> int:
        """Restore bounded conversation and summaries in original receive order."""
        def received_at(ev):
            p = ev.get("payload")
            value = p.get("_received_at") if isinstance(p, dict) else None
            if _finite(value):
                return min(value, self._clock())
            for key in ("ingest_timestamp", "source_timestamp"):
                raw = _iso_of(ev.get(key))
                if raw:
                    try:
                        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                        return min(dt.replace(tzinfo=dt.tzinfo or UTC).timestamp(), self._clock())
                    except (ValueError, OverflowError, OSError):
                        pass
            return self._clock()

        def order(ev):
            p = ev.get("payload")
            seq = p.get("_sequence") if isinstance(p, dict) else None
            if not _finite(seq):
                seq = ev.get("sequence")
            return received_at(ev), seq if _finite(seq) else 0

        n = 0
        for ev in sorted(events, key=order):
            p = ev.get("payload") or {}
            if not isinstance(p, dict):
                continue
            sid = ev.get("entity_id")
            et = ev.get("event_type", "")
            try:
                if et == "inspector.preferences":
                    self.set_prefs(p.get("prefs") or {}, None if p.get("scope") == "global" else sid,
                                   persist=False)
                elif isinstance(et, str) and et.startswith("inspector.") and p.get("kind") in DURABLE_KINDS:
                    if p["kind"] == "message" and p.get("role") not in ("user", "assistant"):
                        continue
                    with self._lock:
                        result = self.ingest(sid, {**p, "ts": _iso_of(ev.get("source_timestamp")),
                                                  **({"phase": "end"} if p["kind"] == "test" else {})},
                                             persist=False)
                        if not result["accepted"]:
                            continue
                        sess = self._sessions[sid]
                        # Replay keeps the server receive clock, never the untrusted source time.
                        recv = received_at(ev)
                        if sess.events:
                            entry = sess.events[-1]
                            entry["recv"] = recv
                            entry["redacted"] |= bool(p.get("redacted"))
                            entry["truncated"] |= bool(p.get("truncated"))
                        sess.last_event = recv
                        self._enforce(sess)
                else:
                    continue
                n += 1
            except (ValueError, KeyError):
                continue
        return n


def _iso_of(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    return value if isinstance(value, str) else None

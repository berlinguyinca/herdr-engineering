"""G1: read-only, cross-entity lifecycle events for a half-open time window (Work Command Center weekly history).

Reads the repo's event streams (mission and session entities only) and returns one canonical started / finished /
failed / paused_quota event per work item, with an evidence reference to the stored event. It writes nothing and
exposes no payload content, only ids, kinds and timestamps.

Coverage is deliberately conservative. ``complete`` can only be true when the window starts at or after the
earliest stored lifecycle event (the horizon) AND event emission is provably complete. Emission is NOT provable in
this build (``EMISSION_PROVEN``): a start event is written when the control plane first sees a mission or session,
not when the agent really started; the durable sink is best-effort (a failed write is swallowed); events recorded
while suppressed (rebuilds) are never written; and a mission that never reports an end is never given a finish.
So ``complete`` is always false today; the horizon tells the reader how far back the store reaches.
"""
from __future__ import annotations

import base64
import re
from datetime import UTC, datetime, timedelta

MAX_SPAN = timedelta(days=31)
DEFAULT_LIMIT = 200
MAX_LIMIT = 500
EMISSION_PROVEN = False
REASONS = (
    "start events are written when the control plane first sees the work, not when it truly started",
    "the durable event sink is best-effort and a failed write is not retried",
    "work that never reports an end has no finish event",
)

MISSION_FINISHED = frozenset({"complete", "cancelled"})
MISSION_FAILED = frozenset({"failed"})
SESSION_FINISHED = frozenset({"finished", "complete", "completed", "closed", "stopped", "canceled", "cancelled",
                              "succeeded", "done", "archived", "merged"})
SESSION_FAILED = frozenset({"failed", "error", "crashed"})
_QUOTA = re.compile(r"quota", re.I)
_ZONED = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:\d{2})$", re.I)


def parse_instant(value: object, name: str) -> datetime:
    if not isinstance(value, str) or not _ZONED.match(value):
        raise ValueError(f"{name} must be an ISO-8601 instant with a time zone offset")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00").replace("z", "+00:00")).astimezone(UTC)
    except (ValueError, OverflowError):   # an offset can push year 1 / 9999 out of range on conversion to UTC
        raise ValueError(f"{name} must be an ISO-8601 instant with a time zone offset") from None


def parse_window(since: object, until: object) -> tuple[datetime, datetime]:
    start, end = parse_instant(since, "since"), parse_instant(until, "until")
    if not start < end:
        raise ValueError("since must be before until")
    if end - start > MAX_SPAN:
        raise ValueError("the window may span at most 31 days")
    return start, end


def parse_limit(raw: object) -> int:
    if raw is None:
        return DEFAULT_LIMIT
    if not isinstance(raw, str) or not re.fullmatch(r"[0-9]{1,3}", raw) or not 1 <= int(raw) <= MAX_LIMIT:
        raise ValueError(f"limit must be an integer between 1 and {MAX_LIMIT}")
    return int(raw)


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S") + (
        f".{moment.microsecond:06d}".rstrip("0") if moment.microsecond else "") + "Z"


def _when(event: dict) -> datetime | None:
    raw = event.get("source_timestamp")
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else None   # a zone-less stamp is not evidence


def _payload(event: dict) -> dict:
    payload = event.get("payload")
    return payload if isinstance(payload, dict) else {}


def _stage(event: dict) -> str | None:
    payload = _payload(event)
    kind = event.get("event_type")
    value = payload.get("to") if kind == "MissionStageChanged" else payload.get("stage") if kind in (
        "MissionUpdated", "MissionStarted") else None
    return value.strip().lower() if isinstance(value, str) else None


def _mission_candidates(key: str, stream: list[dict]):
    """(kind, event) pairs; the caller keeps the earliest per kind."""
    for event in stream:
        kind = event.get("event_type")
        if kind in ("MissionStarted", "MissionCreated"):
            yield "started", event
        stage = _stage(event)
        if stage in MISSION_FINISHED:
            yield "finished", event
        elif stage in MISSION_FAILED:
            yield "failed", event
        if kind in ("MissionUpdated", "MissionStageChanged"):
            p = _payload(event)
            if any(isinstance(p.get(f), str) and _QUOTA.search(p[f]) for f in ("status", "stage", "to")):
                yield "paused_quota", event


def _session_candidates(stream: list[dict]):
    for event in stream:
        kind = event.get("event_type")
        if kind == "SessionStarted":
            yield "started", event
        status = _payload(event).get("status")
        status = status.strip().lower().replace("-", "_") if isinstance(status, str) else None
        if kind in ("SessionStarted", "SessionUpdated") and status:
            if status in SESSION_FINISHED:
                yield "finished", event
            elif status in SESSION_FAILED:
                yield "failed", event
            elif _QUOTA.search(status):
                yield "paused_quota", event


def _record(kind: str, event: dict, when: datetime, work_id: str, source: str, entity_id: str, **extra) -> dict:
    return {"_us": int(when.timestamp() * 1_000_000), "kind": kind, "work_id": work_id, "source": source, "entity_id": entity_id,
            "occurred_at": _iso(when), "evidence": {"ref": event.get("event_id"), "event_type": event.get("event_type")},
            **{k: v for k, v in extra.items() if v is not None}}


def _canonical(repo) -> tuple[list[dict], datetime | None]:
    """One event per (work item, kind): the earliest stored one. Also the earliest lifecycle stamp in the store."""
    out: list[dict] = []
    horizon: datetime | None = None
    with repo._event_lock:
        streams = [(k, list(v)) for k, v in repo._events.items() if k[0] in ("mission", "session") and v]
        sessions = {sid: dict(rec) for sid, rec in repo._sessions.items()}
    for (etype, eid), stream in streams:
        stamped = [(w, e) for e in stream if (w := _when(e)) is not None]
        if stamped:
            first = min(w for w, _ in stamped)
            horizon = first if horizon is None or first < horizon else horizon
        when = {id(e): w for w, e in stamped}
        best: dict[str, tuple[datetime, dict]] = {}
        if etype == "mission":
            pairs = _mission_candidates(eid, stream)
        else:
            pairs = _session_candidates(stream)
        for kind, event in pairs:
            w = when.get(id(event))
            if w is None:
                continue
            if kind not in best or w < best[kind][0]:
                best[kind] = (w, event)
        if not best:
            continue
        if etype == "mission":
            for kind, (w, event) in best.items():
                out.append(_record(kind, event, w, f"mission:{eid}", "mission", eid, mission_key=eid))
        else:
            first_event = stream[0]
            host = next((_payload(e).get("host_id") for e in stream if _payload(e).get("host_id")), None) \
                or sessions.get(eid, {}).get("host_id")
            mkey = next((_payload(e).get("mission_key") for e in reversed(stream) if _payload(e).get("mission_key")), None) \
                or sessions.get(eid, {}).get("mission_key") or first_event.get("mission_id") or None
            if mkey is None:
                mkey = next((_payload(e).get("mission_id") for e in stream if _payload(e).get("mission_id")), None)
            work_id = f"session:{host}/{eid}" if host else f"session:{eid}"
            for kind, (w, event) in best.items():
                out.append(_record(kind, event, w, work_id, "session", eid, host_id=host, mission_key=mkey))
    return out, horizon


def _key(item: dict) -> tuple[int, str, str]:
    return item["_us"], item["work_id"], item["kind"]


def _cursor(item: dict) -> str:
    raw = "\x1f".join((str(item["_us"]), item["work_id"], item["kind"]))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _decode_cursor(raw: object) -> tuple[int, str, str]:
    try:
        if not isinstance(raw, str) or not 1 <= len(raw) <= 600 or not re.fullmatch(r"[A-Za-z0-9_-]+", raw):
            raise ValueError
        parts = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode().split("\x1f")
        if len(parts) != 3:
            raise ValueError
        return int(parts[0]), parts[1], parts[2]
    except (ValueError, UnicodeError):
        raise ValueError("invalid cursor") from None


def weekly_events(repo, *, since: str, until: str, limit: str | None = None, after: str | None = None) -> dict:
    start, end = parse_window(since, until)
    size = parse_limit(limit)
    floor = _decode_cursor(after) if after is not None else None
    canonical, horizon = _canonical(repo)
    start_iso, end_iso = _iso(start), _iso(end)
    low, high = int(start.timestamp() * 1_000_000), int(end.timestamp() * 1_000_000)
    window = sorted((e for e in canonical if low <= e["_us"] < high), key=_key)
    if floor is not None:
        window = [e for e in window if _key(e) > floor]
    page, rest = window[:size], window[size:]
    next_cursor = _cursor(page[-1]) if rest else None
    page = [{k: v for k, v in e.items() if k != "_us"} for e in page]
    truncated = bool(rest)
    covers = horizon is not None and horizon <= start
    reasons = list(REASONS)
    if horizon is None:
        reasons.insert(0, "no lifecycle events are stored")
    elif not covers:
        reasons.insert(0, "the window starts before the earliest stored lifecycle event")
    if truncated:
        reasons.insert(0, "more events matched than the limit; follow next_cursor")
    return {
        "window": {"since": start_iso, "until": end_iso, "interval": "[since,until)"},
        "events": page,
        "next_cursor": next_cursor,
        "coverage": {"complete": bool(covers and EMISSION_PROVEN and not truncated),
                     "horizon": _iso(horizon) if horizon else None,
                     "truncated": truncated, "reasons": reasons},
    }

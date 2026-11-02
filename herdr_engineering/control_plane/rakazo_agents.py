"""Row model, status mapping and ordering for the Shepherd Agents tab (pure functions)."""
from __future__ import annotations

from datetime import datetime, timezone

from .terminal_attach import _CLOSED

STATUS_ORDER = ("blocked", "done", "working", "idle", "unknown", "closed")
_LIVE = frozenset(STATUS_ORDER[:5])
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
    machine = _field(session, "machine")
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

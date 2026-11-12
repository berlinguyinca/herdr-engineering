"""On-demand reads of inspector rows older than the in-memory ring.

The ring (``InspectorStore``) keeps the newest events per session. Older durable rows (``inspector.*`` in
``fabric_events``) are read here, one bounded page at a time, straight from Postgres. Nothing is loaded at boot and
nothing read here is put back into the ring, so memory use is independent of how far back a viewer pages.

Configured by ``control_plane.inspector_history`` in the central fleet YAML (no environment variable)::

    inspector_history: {enabled: true, page_size: 100, max_page_size: 500}
"""
from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from typing import Any

_KEYS = ("enabled", "page_size", "max_page_size")
DEFAULTS = {"enabled": True, "page_size": 100, "max_page_size": 500}
_HARD_MAX = 1000
_META = frozenset({"agent_id", "parent_agent_id", "event_id", "kind", "sensitive", "redacted", "truncated",
                   "_received_at", "_sequence"})


class HistoryCursorInvalid(ValueError):
    """The ``before`` cursor is not one this module issued."""


def parse_inspector_history(section: Any) -> dict:
    """Validate ``control_plane.inspector_history``; ValueError names the field, unknown keys are rejected."""
    if section is None:
        return dict(DEFAULTS)
    if not isinstance(section, dict):
        raise ValueError("inspector_history must be a mapping")
    if set(section) - set(_KEYS):
        raise ValueError(f"inspector_history: unknown key (allowed: {', '.join(_KEYS)})")
    out = {**DEFAULTS, **section}
    if not isinstance(out["enabled"], bool):
        raise ValueError("inspector_history.enabled must be a boolean (true or false)")
    for key in ("page_size", "max_page_size"):
        v = out[key]
        if isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= _HARD_MAX:
            raise ValueError(f"inspector_history.{key} must be an integer between 1 and {_HARD_MAX}")
    if out["page_size"] > out["max_page_size"]:
        raise ValueError("inspector_history.page_size must not exceed max_page_size")
    return out


def _micros(stamp: datetime) -> int:
    delta = stamp - datetime(1970, 1, 1, tzinfo=UTC)
    return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds


def encode_cursor(row: dict) -> str:
    raw = json.dumps([1, _micros(row["ingest_timestamp"]), row["sequence"], row["event_id"]],
                     separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, int, str]:
    try:
        if not isinstance(cursor, str) or not 1 <= len(cursor) <= 512:
            raise ValueError
        value = json.loads(base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True))
        if (not isinstance(value, list) or len(value) != 4 or value[0] != 1
                or any(type(value[i]) is not int for i in (1, 2)) or not isinstance(value[3], str)):
            raise ValueError
        return datetime(1970, 1, 1, tzinfo=UTC) + timedelta(microseconds=value[1]), value[2], value[3]
    except (ValueError, TypeError, OverflowError, UnicodeError):
        raise HistoryCursorInvalid("invalid history cursor") from None


async def _query(conn: Any, table: str, session_id: str, limit: int, before: str | None,
                 cutoff: float | None, kinds: frozenset[str] | None) -> list[Any]:
    """Up to ``limit`` + 1 newest rows of one table, in the keyset order shared by the journal and its archive."""
    args: list[Any] = [session_id]
    where = ["entity_type = 'inspector'", "entity_id = $1", "event_type LIKE 'inspector.%'",
             "event_type <> 'inspector.preferences'"]
    if kinds is not None:
        args.append(sorted(kinds))
        where.append(f"payload->>'kind' = ANY(${len(args)}::text[])")
    if cutoff is not None:
        args.append(datetime.fromtimestamp(cutoff, UTC))
        where.append(f"ingest_timestamp < ${len(args)}")
    if before is not None:
        stamp, seq, event_id = decode_cursor(before)
        args += [stamp, seq, event_id]
        n = len(args)
        where.append(f"(ingest_timestamp, sequence, event_id COLLATE \"C\") < (${n-2}, ${n-1}, ${n}::text COLLATE \"C\")")
    args.append(limit + 1)
    return await conn.fetch(
        f"SELECT event_id, sequence, event_type, source_timestamp, ingest_timestamp, payload FROM {table} "
        f"WHERE {' AND '.join(where)} "
        f"ORDER BY ingest_timestamp DESC, sequence DESC, event_id COLLATE \"C\" DESC LIMIT ${len(args)}", *args)


def _order_key(row: dict) -> tuple:
    return (row["ingest_timestamp"], row["sequence"], row["event_id"].encode())


async def fetch_page(conn: Any, table: str, session_id: str, *, limit: int, before: str | None = None,
                     cutoff: float | None = None, kinds: frozenset[str] | None = None,
                     archive_table: str | None = None) -> dict:
    """One page of durable inspector rows for a session, newest first.

    ``table`` is a trusted, already-quoted identifier. ``cutoff`` (epoch seconds) bounds a first page to rows older
    than the ring's oldest event; ``before`` continues from a previous page's ``next_cursor``.

    ``archive_table`` (trusted, quoted) is the inspector archive (``fabric_events_archive``). When it exists, the page is
    the newest ``limit`` rows of the two tables merged under the one keyset order, so the same cursor walks from the
    journal into the archive with no gap and no duplicate (a row present in both tables is returned once). A missing
    archive table is simply not read."""
    rows = [dict(r) for r in await _query(conn, table, session_id, limit, before, cutoff, kinds)]
    if archive_table is not None and await conn.fetchval("SELECT to_regclass($1) IS NOT NULL", archive_table):
        seen = {r["event_id"] for r in rows}
        rows += [d for d in (dict(r) for r in await _query(conn, archive_table, session_id, limit, before, cutoff, kinds))
                 if d["event_id"] not in seen]
        rows.sort(key=_order_key, reverse=True)
    more = len(rows) > limit
    shown = rows[:limit]
    for r in shown:
        if isinstance(r["payload"], str):
            r["payload"] = json.loads(r["payload"])
    return {"rows": shown, "has_more": more, "next_cursor": encode_cursor(shown[-1]) if more and shown else None}


def entries_from_rows(rows: list[dict]) -> list[dict]:
    """Ring-shaped entries (newest first) from durable rows. A tool call journals several rows: keep its newest.
    ``seq`` is None: durable rows carry no ring sequence number."""
    out, seen = [], set()
    for r in rows:
        p = r["payload"] if isinstance(r["payload"], dict) else {}
        kind = p.get("kind")
        if not isinstance(kind, str):
            continue
        if kind == "message" and p.get("role") not in ("user", "assistant"):
            continue
        if kind == "tool" and p.get("call_id") is not None:
            if p["call_id"] in seen:
                continue
            seen.add(p["call_id"])
        stamp = r["source_timestamp"]
        out.append({
            "seq": None, "rev": 0, "kind": kind, "agent_id": p.get("agent_id"),
            "parent_agent_id": p.get("parent_agent_id"),
            "ts": stamp.isoformat() if isinstance(stamp, datetime) else stamp,
            "sensitive": bool(p.get("sensitive")), "redacted": bool(p.get("redacted")),
            "truncated": bool(p.get("truncated")), "size": 0,
            "data": {k: v for k, v in p.items() if k not in _META},
            "history_id": r["event_id"],
        })
    return out

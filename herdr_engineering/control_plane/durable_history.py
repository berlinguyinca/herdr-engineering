"""Read complete public entity streams directly from the durable journal."""
from __future__ import annotations

import base64
import hashlib
import json
import re
from datetime import UTC, datetime

from .history_cursor import fingerprint

_ENTITY_TYPES = frozenset({"host", "session", "service"})
_ACTOR = "COALESCE(e.actor ->> 'id', '')"
_KEY = f"e.sequence, e.ingest_timestamp, e.event_id, {_ACTOR}"
_DESC = f"e.sequence DESC, e.ingest_timestamp DESC, e.event_id DESC, {_ACTOR} DESC"
_HEX = re.compile(r"[0-9a-f]{64}")


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     default=str).encode()).hexdigest()


def _event(row):
    # Import lazily: the bridge also constructs this reader during recovery.
    from .bridge import _rebuild_events

    streams, _ = _rebuild_events([dict(row)])
    return streams[f"{row['entity_type']}|{row['entity_id']}"][0]


def _event_hash(row):
    # The internal insertion/update nonce detects identical delete/reinsert and
    # same-value updates. Neither it nor SQL helper columns enter API events.
    return fingerprint([_event(row), row.get("recovery_instance")])


def _key_hash(row):
    # Use PostgreSQL's text extraction, not a reserialized JSON actor value.
    return fingerprint([row["event_id"], row["history_actor_domain"]])


def _cursor(scope, row):
    data = [2, scope, row["sequence"], row["ingest_timestamp"].astimezone(UTC).isoformat(),
            _key_hash(row), _event_hash(row)]
    return base64.urlsafe_b64encode(json.dumps(data, separators=(",", ":")).encode()).decode().rstrip("=")


def _decode(before, scope):
    if not isinstance(before, str) or not 1 <= len(before) <= 512 or not re.fullmatch(r"[A-Za-z0-9_-]+", before):
        raise ValueError("invalid history cursor")
    try:
        raw = base64.b64decode(before + "=" * (-len(before) % 4), altchars=b"-_", validate=True)
        data = json.loads(raw)
    except (ValueError, UnicodeError):
        raise ValueError("invalid history cursor") from None
    if (base64.urlsafe_b64encode(raw).decode().rstrip("=") != before
            or not isinstance(data, list) or not data or type(data[0]) is not int):
        raise ValueError("invalid history cursor")
    if data[0] == 1:
        if (len(data) != 4 or type(data[1]) is not int or not 0 < data[1] < 2**63
                or data[2] != scope or not isinstance(data[3], str) or not _HEX.fullmatch(data[3])):
            raise ValueError("invalid history cursor")
        raise ValueError("staleHistoryCursor")
    if (data[0] != 2 or len(data) != 6 or data[1] != scope
            or type(data[2]) is not int or not -(2**63) <= data[2] < 2**63
            or not isinstance(data[3], str) or len(data[3]) > 40
            or any(not isinstance(value, str) or not _HEX.fullmatch(value) for value in (data[4], data[5]))):
        raise ValueError("invalid history cursor")
    try:
        stamp = datetime.fromisoformat(data[3])
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            raise ValueError
        stamp = stamp.astimezone(UTC)
    except (ValueError, OverflowError):
        raise ValueError("invalid history cursor") from None
    return data[2], stamp, data[4], data[5]


class DurableHistoryReader:
    """Keyset pages and the legacy full stream for compacted public entities.

    Callers serialize access to the connection. Database failures propagate;
    compacted memory is never a substitute for complete journal history.
    """

    def __init__(self, conn, events_table="fabric_events"):
        if not isinstance(events_table, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", events_table):
            raise ValueError("invalid history events table")
        self._conn = conn
        self._table = f'"{events_table}"'

    @staticmethod
    def _validate_entity(entity_type, entity_id):
        if (not isinstance(entity_type, str) or entity_type not in _ENTITY_TYPES
                or not isinstance(entity_id, str) or not entity_id):
            raise ValueError("unsupported history entity")

    async def page(self, entity_type, entity_id, *, limit=50, before=None):
        self._validate_entity(entity_type, entity_id)
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("limit must be an integer between 1 and 200")
        scope = _digest([entity_type, entity_id])
        anchor = _decode(before, scope) if before is not None else None
        where = "e.entity_type=$1 AND e.entity_id=$2"
        args = [entity_type, entity_id]
        select = f"SELECT e.*, {_ACTOR} AS history_actor_domain FROM {self._table} e"
        async with self._conn.transaction(isolation="repeatable_read", readonly=True):
            latest = await self._conn.fetchrow(
                f"SELECT e.sequence, e.event_id FROM {self._table} e WHERE {where} ORDER BY {_DESC} LIMIT 1",
                *args)
            if anchor is not None:
                sequence, stamp, key_hash, event_hash = anchor
                candidates = await self._conn.fetch(
                    f"{select} WHERE {where} AND e.sequence=$3 AND e.ingest_timestamp=$4",
                    *args, sequence, stamp)
                match = next((row for row in candidates if _key_hash(row) == key_hash), None)
                if match is None or _event_hash(match) != event_hash:
                    raise ValueError("staleHistoryCursor")
                where += f" AND ({_KEY}) < ($3::bigint, $4::timestamptz, $5::text, $6::text)"
                args.extend((sequence, stamp, match["event_id"], match["history_actor_domain"]))
            args.append(limit + 1)
            rows = await self._conn.fetch(f"{select} WHERE {where} ORDER BY {_DESC} LIMIT ${len(args)}", *args)
            chosen = rows[:limit]
            more = len(rows) > limit
            return {"events": [_event(row) for row in reversed(chosen)], "has_more": more,
                    "next_cursor": _cursor(scope, chosen[-1]) if more else None,
                    "latest_sequence": latest["sequence"] if latest else None,
                    "last_event_id": latest["event_id"] if latest else None}

    async def stream(self, entity_type, entity_id):
        self._validate_entity(entity_type, entity_id)
        async with self._conn.transaction(isolation="repeatable_read", readonly=True):
            rows = await self._conn.fetch(
                f"SELECT e.* FROM {self._table} e WHERE e.entity_type=$1 AND e.entity_id=$2 ORDER BY {_KEY}",
                entity_type, entity_id)
            return [_event(row) for row in rows]

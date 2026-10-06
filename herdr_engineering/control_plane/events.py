"""Event envelope, sequencing, and idempotent ingestion (Spec §48-51)."""
from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


@dataclass(frozen=True)
class Event:
    event_id: str
    schema_version: int = 1
    source_timestamp: datetime = field(
        default_factory=lambda: datetime.now(UTC))
    ingest_timestamp: datetime = field(
        default_factory=lambda: datetime.now(UTC))
    entity_type: str = ""
    entity_id: str = ""
    mission_id: str | None = None
    session_id: str | None = None
    sequence: int = 0
    event_type: str = ""
    payload: dict[str, Any] = field(default_factory=dict)


def make_event(entity_type: str, entity_id: str, event_type: str, *,
               payload: dict[str, Any] | None = None,
               mission_id: str | None = None,
               session_id: str | None = None,
               source: datetime | None = None) -> Event:
    now = source or datetime.now(UTC)
    return Event(
        event_id=f"ev_{secrets.token_hex(16)}",
        source_timestamp=now,
        ingest_timestamp=datetime.now(UTC),
        entity_type=entity_type,
        entity_id=entity_id,
        mission_id=mission_id,
        session_id=session_id,
        event_type=event_type,
        payload=payload or {},
    )


def stream_key(entity_type: str, entity_id: str) -> str:
    return f"{entity_type}:{entity_id}"


class EventStore:
    """Persistent event log with per-stream sequence + idempotent append.

    ``conn`` is any object exposing async ``fetch(sql, *args)`` and
    ``execute(sql, *args)`` (a real asyncpg connection or a test fake).
    """

    def __init__(self, conn: Any, *, table: str = "fabric_events"):
        self._conn = conn
        self._table = table

    async def next_sequence(self, entity_type: str, entity_id: str) -> int:
        row = await self._conn.fetch(
            f"SELECT COALESCE(max(sequence),-1) AS max FROM {self._table} "
            "WHERE entity_type=$1 AND entity_id=$2",
            entity_type, entity_id,
        )
        return int(row[0]["max"]) + 1

    async def append(self, event: Event) -> bool:
        """Insert the event; return False if event_id already exists."""
        try:
            row = await self._conn.fetchrow(
                f"INSERT INTO {self._table} "
                "(event_id, sequence, event_type, entity_type, entity_id, "
                " mission_id, session_id, payload, source_timestamp, "
                " ingest_timestamp, schema_version) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11) "
                "ON CONFLICT (event_id) DO NOTHING RETURNING event_id",
                event.event_id, event.sequence, event.event_type,
                event.entity_type, event.entity_id, event.mission_id,
                event.session_id, event.payload, event.source_timestamp,
                event.ingest_timestamp, event.schema_version,
            )
        except Exception:
            return False
        return row is not None

    async def read_stream(self, entity_type: str, entity_id: str,
                          after: int = -1) -> list[Event]:
        rows = await self._conn.fetch(
            f"SELECT * FROM {self._table} "
            "WHERE entity_type=$1 AND entity_id=$2 AND sequence > $3 "
            "ORDER BY sequence",
            entity_type, entity_id, after,
        )
        return [_row_to_event(r) for r in rows]


def _row_to_event(row: dict[str, Any]) -> Event:
    return Event(
        event_id=row["event_id"],
        schema_version=row.get("schema_version", 1),
        source_timestamp=row["source_timestamp"],
        ingest_timestamp=row["ingest_timestamp"],
        entity_type=row["entity_type"],
        entity_id=row["entity_id"],
        mission_id=row.get("mission_id"),
        session_id=row.get("session_id"),
        sequence=row["sequence"],
        event_type=row["event_type"],
        payload=row.get("payload") or {},
    )

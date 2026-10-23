"""Event envelope, sequencing, and idempotent ingestion (Spec §48-51)."""
from __future__ import annotations

import json
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
    # Envelope v2 (docs/contracts/event-envelope-v2.md). All optional: v1
    # events leave them None and are stored with the legacy column set.
    source: str | None = None
    source_sequence: int | None = None
    causation_id: str | None = None
    correlation_id: str | None = None
    actor: dict[str, Any] | None = None
    visibility_scope: str | None = None
    evidence_refs: list[dict[str, Any]] | None = None
    org_id: str | None = None
    project_id: str | None = None
    room_id: str | None = None
    workspace_id: str | None = None
    task_id: str | None = None
    agent_id: str | None = None


# Envelope v2 columns beyond the legacy set, in INSERT order.
V2_TEXT_FIELDS = ("source", "causation_id", "correlation_id", "visibility_scope",
                  "org_id", "project_id", "room_id", "workspace_id", "task_id",
                  "agent_id")
V2_JSON_FIELDS = ("actor", "evidence_refs")
V2_COLUMNS = ("source", "source_sequence", "causation_id", "correlation_id",
              "actor", "visibility_scope", "evidence_refs", "org_id",
              "project_id", "room_id", "workspace_id", "task_id", "agent_id")

_UNIQUE_VIOLATION = "23505"


class SequenceCollision(Exception):
    """(source, source_sequence) is already held by a different event."""


def _is_unique_violation(exc: BaseException) -> bool:
    return getattr(exc, "sqlstate", None) == _UNIQUE_VIOLATION


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
        """Insert the event.

        Returns True when stored and False only when ``event_id`` already
        exists (a duplicate). A ``(source, source_sequence)`` collision raises
        :class:`SequenceCollision`. Every other failure propagates: a failed
        write must never be reported as a duplicate.
        """
        # asyncpg's jsonb codec expects a str, not a dict (it encodes str as
        # JSON). Serialize the payload here so the durable write path works.
        payload = event.payload
        if not isinstance(payload, str):
            payload = json.dumps(payload or {})
        legacy_args = (
            event.event_id, event.sequence, event.event_type,
            event.entity_type, event.entity_id, event.mission_id,
            event.session_id, payload, event.source_timestamp,
            event.ingest_timestamp, event.schema_version,
        )
        v2 = any(getattr(event, name) is not None for name in V2_COLUMNS)
        if v2:
            # v1 events keep the legacy 11-column INSERT so they still work
            # against a database that has not applied migration 0003.
            sql = (
                f"INSERT INTO {self._table} "
                "(event_id, sequence, event_type, entity_type, entity_id, "
                " mission_id, session_id, payload, source_timestamp, "
                " ingest_timestamp, schema_version, "
                " source, source_sequence, causation_id, correlation_id, "
                " actor, visibility_scope, evidence_refs, org_id, project_id, "
                " room_id, workspace_id, task_id, agent_id) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,"
                "$16,$17,$18,$19,$20,$21,$22,$23,$24) "
                "ON CONFLICT (event_id) DO NOTHING RETURNING event_id")
            args = legacy_args + (
                event.source, event.source_sequence, event.causation_id,
                event.correlation_id,
                None if event.actor is None else json.dumps(event.actor),
                event.visibility_scope,
                None if event.evidence_refs is None
                else json.dumps(event.evidence_refs),
                event.org_id, event.project_id, event.room_id,
                event.workspace_id, event.task_id, event.agent_id)
        else:
            sql = (
                f"INSERT INTO {self._table} "
                "(event_id, sequence, event_type, entity_type, entity_id, "
                " mission_id, session_id, payload, source_timestamp, "
                " ingest_timestamp, schema_version) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11) "
                "ON CONFLICT (event_id) DO NOTHING RETURNING event_id")
            args = legacy_args
        try:
            row = await self._conn.fetchrow(sql, *args)
        except Exception as exc:
            # ON CONFLICT (event_id) absorbs event_id duplicates, so a unique
            # violation reaching here is the (source, source_sequence) index.
            if v2 and _is_unique_violation(exc):
                raise SequenceCollision(
                    f"source {event.source!r} sequence "
                    f"{event.source_sequence} is already used") from exc
            raise
        return row is not None

    async def identity(self, event_id: str) -> tuple[str | None, int | None] | None:
        """(source, source_sequence) stored for ``event_id``, or None if absent."""
        rows = await self._conn.fetch(
            f"SELECT source, source_sequence FROM {self._table} WHERE event_id=$1",
            event_id)
        if not rows:
            return None
        return rows[0]["source"], rows[0]["source_sequence"]

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
    payload = row.get("payload") or {}
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (ValueError, TypeError):
            payload = {}
    def _json(value):
        if isinstance(value, str):
            try:
                return json.loads(value)
            except (ValueError, TypeError):
                return None
        return value

    v2 = {name: row.get(name) for name in V2_TEXT_FIELDS}
    v2["source_sequence"] = row.get("source_sequence")
    for name in V2_JSON_FIELDS:
        v2[name] = _json(row.get(name))
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
        payload=payload,
        **v2,
    )

"""Durable write-through sink (Spec §50): persist recorded repo events.

The in-memory ``ControlPlaneRepo`` is the live view served by the API/UI.
``record_event`` is the single choke point every entity mutation flows through
(it already broadcasts to SSE listeners). This sink makes the same events
durable by appending them to the Postgres event log (``fabric_events``) via the
async ``EventStore``, bridged to the synchronous web request path through a
dedicated event loop (``_LoopRunner``), so a restart re-hydrates exactly what
was ingested.
"""
from __future__ import annotations

import json
import logging
import secrets
import threading
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime
from typing import Any

from .bridge import _LoopRunner
from .events import Event, EventStore

log = logging.getLogger(__name__)


def _parse_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            pass
    from datetime import UTC
    return datetime.now(UTC)


def _dict_to_event(ev: dict) -> Event:
    return Event(
        event_id=ev.get("event_id") or f"ev_{secrets.token_hex(16)}",
        schema_version=ev.get("schema_version", 1),
        source_timestamp=_parse_dt(ev.get("source_timestamp")),
        ingest_timestamp=_parse_dt(ev.get("ingest_timestamp")),
        entity_type=ev.get("entity_type", ""),
        entity_id=ev.get("entity_id", ""),
        mission_id=ev.get("mission_id"),
        session_id=ev.get("session_id"),
        sequence=ev.get("sequence", 0),
        event_type=ev.get("event_type", ""),
        payload=ev.get("payload") or {},
    )


class DurableEventSink:
    """Persists each recorded repo event to the durable event log.

    Construct with a ``dsn`` (lazily connects on its own loop) or with an
    existing asyncpg ``conn`` plus ``run`` (tests).
    """

    def __init__(self, dsn: str | None = None, *, conn: Any = None,
                 run: Callable | None = None,
                 events_table: str = "fabric_events"):
        if run is None:
            run = _LoopRunner().run
        self._dsn = dsn
        self._conn = conn
        self._run = run
        self._events_table = events_table
        self._store = EventStore(conn, table=events_table)
        self._write_lock = threading.RLock()

    def _ensure_store(self) -> None:
        conn = self._store._conn
        closed = getattr(conn, "is_closed", None)
        if self._dsn and (conn is None or (callable(closed) and closed())):
            import asyncpg
            self._conn = self._run(asyncpg.connect(self._dsn))
            self._store = EventStore(self._conn, table=self._events_table)

    def record(self, ev: dict) -> None:
        """Persist one event. Best-effort: never raises, never breaks the repo."""
        try:
            self.record_required(ev)
        except Exception:
            log.exception("durable sink failed to persist event %s",
                          ev.get("event_id"))

    def record_required(self, ev: dict) -> None:
        """Persist a command receipt, propagating failure before side effects."""
        # The synchronous web server has concurrent request threads; asyncpg
        # permits one operation per connection, including reconnection.
        with self._write_lock:
            self._ensure_store()
            event = _dict_to_event(ev)
            if event.entity_type in {"scheduler_reservation", "scheduler_lease", "scheduler_recovery", "scheduler_observation"} or event.event_type == "HostPolicyChanged":
                event = replace(event, sequence=self._run(self._store.next_sequence(
                    event.entity_type, event.entity_id)))
            if not self._run(self._store.append(event)):
                raise RuntimeError("durable event insert was not acknowledged")

    @contextmanager
    def scheduler_admission(self):
        """Serialize admission across control-plane processes in PostgreSQL."""
        with self._write_lock:
            self._ensure_store()
            conn = self._store._conn
            self._run(conn.execute("BEGIN"))
            try:
                self._run(conn.execute("SELECT pg_advisory_xact_lock(724965310221)"))
                rows = self._run(conn.fetch(
                    f"SELECT DISTINCT ON (entity_id) payload FROM {self._events_table} "
                    "WHERE entity_type='scheduler_reservation' "
                    "ORDER BY entity_id, sequence DESC"))
                yield [json.loads(row["payload"]) if isinstance(row["payload"], str)
                       else dict(row["payload"]) for row in rows]
                self._run(conn.execute("COMMIT"))
            except BaseException:
                self._run(conn.execute("ROLLBACK"))
                raise

    def scheduler_observations(self):
        """Latest token-bound host facts, read under the admission transaction."""
        rows = self._run(self._store._conn.fetch(
            f"SELECT DISTINCT ON (entity_id) entity_id, payload FROM {self._events_table} "
            "WHERE entity_type='scheduler_observation' ORDER BY entity_id, sequence DESC"))
        return {row["entity_id"]: json.loads(row["payload"]) if isinstance(row["payload"], str)
                else dict(row["payload"]) for row in rows}

    def scheduler_policies(self):
        """Read authoritative policies while the caller holds admission lock."""
        rows = self._run(self._store._conn.fetch(
            f"SELECT DISTINCT ON (entity_id) entity_id, payload FROM {self._events_table} "
            "WHERE entity_type='host' AND event_type='HostPolicyChanged' "
            "ORDER BY entity_id, sequence DESC"))
        return {row["entity_id"]: json.loads(row["payload"]) if isinstance(row["payload"], str)
                else dict(row["payload"]) for row in rows}

    def scheduler_recovery_latest(self, operation_id):
        with self._write_lock:
            self._ensure_store()
            rows = self._run(self._store._conn.fetch(
                f"SELECT payload FROM {self._events_table} "
                "WHERE entity_type='scheduler_recovery' AND entity_id=$1 ORDER BY sequence DESC LIMIT 1",
                operation_id))
        if not rows:
            return None
        payload = rows[0]["payload"]
        return json.loads(payload) if isinstance(payload, str) else dict(payload)

    @contextmanager
    def scheduler_recovery_operation(self, operation_id):
        """Serialize one recovery while each transition commits before side effects."""
        if not self._dsn:
            raise RuntimeError("recovery requires a dedicated PostgreSQL lock connection")
        import asyncpg
        connection = self._run(asyncpg.connect(self._dsn))
        try:
            self._run(connection.execute(
                "SELECT pg_advisory_lock(hashtextextended($1, 724965310221))", operation_id))
            yield
        finally:
            # Closing the dedicated session releases its lock even if the
            # coordinator loses transport or an unlock statement cannot run.
            self._run(connection.close())

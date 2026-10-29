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

import copy
import json
import logging
import secrets
import threading
from collections.abc import Callable
from dataclasses import asdict
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime
from typing import Any

from .agent_identity import (
    AGENT_IDENTITY_LOCK,
    AgentIdentityConflict,
    AgentIdentityRegistry,
    AgentIdentityVersionConflict,
)
from .bridge import _LoopRunner, _rebuild_events
from .events import V2_COLUMNS, Event, EventStore

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


def prune_events_sql(table: str) -> str:
    """The retention DELETE; migration 0003 indexes fabric_events(entity_type, source_timestamp) for it."""
    return f"DELETE FROM {table} WHERE entity_type=$1 AND source_timestamp < $2"


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
        **{name: ev.get(name) for name in V2_COLUMNS},
    )


class EventAlreadyStored(RuntimeError):
    """The durable log already holds this event_id (not a write failure)."""


class DurableEventSink:
    """Persists each recorded repo event to the durable event log.

    Construct with a ``dsn`` (lazily connects on its own loop) or with an
    existing asyncpg ``conn`` plus ``run`` (tests).
    """

    verify_agent_identity_noop = True

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

    def record_required(self, ev: dict) -> dict | None:
        """Persist a command receipt, propagating failure before side effects."""
        # The synchronous web server has concurrent request threads; asyncpg
        # permits one operation per connection, including reconnection.
        with self._write_lock:
            self._ensure_store()
            if ev.get("entity_type") == "agent_identity":
                return self._run(self._record_agent_identity(ev))
            event = _dict_to_event(ev)
            if (event.entity_type in {"scheduler_reservation", "scheduler_lease", "scheduler_observation"}
                    or event.event_type == "HostPolicyChanged"):
                event = replace(event, sequence=self._run(self._store.next_sequence(
                    event.entity_type, event.entity_id)))
            # append() returns False only for an existing event_id; any write
            # error propagates from here unchanged.
            if not self._run(self._store.append(event)):
                raise EventAlreadyStored(
                    "durable event insert was not acknowledged "
                    "(event_id already stored)")

    async def _record_agent_identity(self, ev: dict) -> dict:
        """Validate global reservations and publish on the same database cut.

        Independent processes can have older in-memory registries. Return the
        acknowledged durable event rather than publishing a tentative local name.
        """
        if ev.get("event_type") != "agent_identity.updated":
            raise ValueError("invalid private agent identity event type")
        AgentIdentityRegistry().hydrate([ev])
        conn = self._store._conn
        # A waiter must read AFTER the previous lock holder commits, even if
        # the connection's default isolation is repeatable read.
        async with conn.transaction(isolation="read_committed"):
            await conn.execute("SET LOCAL lock_timeout='5s'")
            await conn.execute("SELECT pg_advisory_xact_lock($1)", AGENT_IDENTITY_LOCK)
            rows = await conn.fetch("""
                SELECT * FROM fabric_events
                WHERE entity_type = 'agent_identity'
                ORDER BY ingest_timestamp, sequence, event_id
            """)
            streams, _ = _rebuild_events([dict(row) for row in rows])
            history = [event for stream in streams.values() for event in stream]
            registry = AgentIdentityRegistry()
            registry.hydrate(history, quarantine_conflicts=True, tolerate_version_resets=True)
            identity, proposed = ev["entity_id"], ev["payload"]

            def current_event():
                for event in reversed(history):
                    if event["entity_type"] != "agent_identity" or event["entity_id"] != identity:
                        continue
                    candidate = AgentIdentityRegistry()
                    candidate.hydrate([event])
                    if candidate.get(identity) == registry.get(identity):
                        return copy.deepcopy(event)
                raise ValueError("canonical agent identity has no durable acknowledgement")

            if proposed["version"] == 1 and registry.get(identity) is not None:
                if registry.is_conflicted(identity):
                    raise AgentIdentityConflict("saved agent name is ambiguous; use its stable identity")
                return current_event()
            if proposed["version"] == 1:
                registry.allocate(identity, proposed["display_name"])
            else:
                try:
                    registry.rename(identity, proposed["display_name"], proposed["version"] - 1)
                except AgentIdentityVersionConflict as error:
                    error.current_event = current_event()
                    error.current_conflicted = registry.is_conflicted(identity)
                    raise
                if registry.get(identity)["version"] < proposed["version"]:
                    return current_event()  # A globally verified no-op never appends another event.
            stored = _dict_to_event({**ev, "payload": copy.deepcopy(registry._records[identity])})
            if not await self._store.append(stored):
                raise EventAlreadyStored("durable log already holds this identity event")
            normalized, _ = _rebuild_events([asdict(stored)])
            return next(iter(normalized.values()))[0]
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

    def scheduler_leases(self):
        """Read latest lease state while the caller holds the admission lock."""
        rows = self._run(self._store._conn.fetch(
            f"SELECT DISTINCT ON (entity_id) entity_id, payload FROM {self._events_table} "
            "WHERE entity_type='scheduler_lease' ORDER BY entity_id, sequence DESC"))
        return {row["entity_id"]: json.loads(row["payload"]) if isinstance(row["payload"], str)
                else dict(row["payload"]) for row in rows}

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
            "AND NOT (payload ? '_envelope') "
            "ORDER BY entity_id, sequence DESC"))
        return {row["entity_id"]: json.loads(row["payload"]) if isinstance(row["payload"], str)
                else dict(row["payload"]) for row in rows}

    def stored_identity(self, event_id: str, actor_id: str = ""):
        """(source, source_sequence) of this principal's stored event, or None when absent."""
        with self._write_lock:
            self._ensure_store()
            return self._run(self._store.identity(event_id, actor_id))

    def record_source_state(self, state: dict) -> None:
        """Best-effort upsert of per-source ingest state (fabric_event_sources)."""
        import json
        try:
            with self._write_lock:
                self._ensure_store()
                self._run(self._store._conn.execute(
                    "INSERT INTO fabric_event_sources "
                    "(source_actor, source, last_sequence, last_seen_at, gaps) VALUES ($1,$2,$3,$4,$5) "
                    "ON CONFLICT (source_actor, source) DO UPDATE SET "
                    "last_sequence=EXCLUDED.last_sequence, "
                    "last_seen_at=EXCLUDED.last_seen_at, gaps=EXCLUDED.gaps",
                    state.get("actor") or "", state["source"], state["last_sequence"],
                    _parse_dt(state["last_seen_at"]), json.dumps(state["gaps"])))
        except Exception:
            log.exception("durable sink failed to persist source state %s",
                          state.get("source"))


    def prune_events_before(self, entity_type: str, cutoff: datetime) -> None:
        """Delete one entity type's durable events older than ``cutoff`` (retention)."""
        with self._write_lock:
            self._ensure_store()
            self._run(self._store._conn.execute(
                prune_events_sql(self._events_table), entity_type, cutoff))

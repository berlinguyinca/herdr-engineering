"""Durable <-> structured bridge (Spec §106, §114-115).

The API/UI serve from the in-memory ``ControlPlaneRepo``. ``load_into``
hydrates a (typically fresh) repo from a read-only durable view of the Phase-1
Postgres tables (``missions``, ``sessions``, ``services``, ``hosts``,
``fabric_events``), mapping each row into the repo's canonical record shape.

The mapping is a pure function of a ``DurableSource`` (a small read-only
protocol), so it is fully unit-testable with fakes — no live Postgres needed in
CI. ``PostgresSource`` is the real asyncpg-backed implementation.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
from collections.abc import Callable
from typing import Any


def _iso(value: Any):
    if isinstance(value, _dt.datetime):
        return value.isoformat()
    return value


class DurableSource:
    """Read-only durable view of control-plane state (sync protocol)."""

    def missions(self) -> list[dict]:
        raise NotImplementedError

    def sessions(self) -> list[dict]:
        raise NotImplementedError

    def services(self) -> list[dict]:
        raise NotImplementedError

    def hosts(self) -> list[dict]:
        raise NotImplementedError

    def events(self) -> list[dict]:
        raise NotImplementedError


class PostgresSource(DurableSource):
    """Asyncpg-backed durable source (runs its queries via ``run``)."""

    def __init__(self, conn: Any, *, run: Callable | None = None,
                 events_table: str = "fabric_events"):
        self._conn = conn
        self._run = run or asyncio.run
        self._events_table = events_table
        self._cache: dict | None = None

    def _load(self) -> dict:
        if self._cache is None:
            self._cache = self._run(self._fetch_all())
        return self._cache

    async def _fetch_all(self) -> dict:
        conn = self._conn
        return {
            "missions": [dict(r) for r in await conn.fetch("SELECT * FROM missions")],
            "sessions": [dict(r) for r in await conn.fetch("SELECT * FROM sessions")],
            "services": [dict(r) for r in await conn.fetch("SELECT * FROM services")],
            "hosts": [dict(r) for r in await conn.fetch("SELECT * FROM hosts")],
            "events": [dict(r) for r in await conn.fetch(
                f"SELECT * FROM {self._events_table} ORDER BY ingest_timestamp, sequence")],
        }

    def missions(self) -> list[dict]:
        return self._load()["missions"]

    def sessions(self) -> list[dict]:
        return self._load()["sessions"]

    def services(self) -> list[dict]:
        return self._load()["services"]

    def hosts(self) -> list[dict]:
        return self._load()["hosts"]

    def events(self) -> list[dict]:
        return self._load()["events"]


def _rebuild_events(rows: list[dict]) -> tuple[dict, list]:
    """Fold fabric_events rows into repo's {stream: [event]} + chronological order."""
    streams: dict = {}
    order: list = []
    for row in rows:
        etype, eid = row.get("entity_type"), row.get("entity_id")
        if not etype or not eid:
            continue
        key = f"{etype}|{eid}"
        stream = streams.setdefault(key, [])
        seq = int(row.get("sequence", len(stream)))
        stream.append({
            "event_id": row.get("event_id"),
            "schema_version": row.get("schema_version") or 1,
            "source_timestamp": _iso(row.get("source_timestamp")),
            "ingest_timestamp": _iso(row.get("ingest_timestamp")),
            "entity_type": etype, "entity_id": eid,
            "mission_id": row.get("mission_id"),
            "session_id": row.get("session_id"),
            "sequence": seq, "event_type": row.get("event_type"),
            "payload": row.get("payload") or {},
        })
        order.append((etype, eid, seq))
    for stream in streams.values():
        stream.sort(key=lambda e: e["sequence"])
    return streams, order


def load_into(repo, source: DurableSource) -> dict:
    """Hydrate ``repo`` from ``source``. Idempotent; returns counts.

    Designed for a fresh repo at server startup: replaces all structured state
    with the durable view so the API/UI read exactly what Postgres holds.
    """
    missions: dict = {}
    for row in source.missions():
        mid = row["mission_id"]
        created = _iso(row.get("created_at"))
        missions[mid] = {
            "mission_id": mid,
            "title": row.get("title", ""),
            "purpose": row.get("purpose") or "",
            "stage": row.get("stage") or row.get("status") or "ready",
            "status": row.get("status") or row.get("stage") or "ready",
            "created_at": created,
            "updated_at": _iso(row.get("updated_at") or row.get("created_at")),
            "sessions": [],
        }

    sessions: dict = {}
    for row in source.sessions():
        sid = row["session_id"]
        mid = row.get("mission_id")
        sessions[sid] = {
            "session_id": sid, "mission_id": mid,
            "agent_role": row.get("agent_role") or "",
            "model": row.get("model") or "",
            "status": row.get("status") or "active",
            "created_at": _iso(row.get("created_at")),
            "updated_at": _iso(row.get("updated_at") or row.get("created_at")),
            "messages": row.get("messages") or 0,
            "tool_calls": row.get("tool_calls") or 0,
        }
        if mid in missions:
            missions[mid]["sessions"].append(sid)

    services: dict = {}
    for row in source.services():
        svcid = row["service_id"]
        services[svcid] = {
            "service_id": svcid, "service_name": row.get("service_name"),
            "host_id": row.get("host_id"), "port": row.get("port"),
            "session_id": row.get("session_id"),
            "status": row.get("status") or "healthy",
            "created_at": _iso(row.get("created_at")),
            "updated_at": _iso(row.get("updated_at") or row.get("created_at")),
            "url": f"http://{row.get('host_id')}:{row.get('port')}",
        }

    hosts: dict = {}
    for row in source.hosts():
        hid = row["host_id"]
        hosts[hid] = {
            "host_id": hid, "host_name": row.get("hostname"),
            "tailnet_ip": row.get("tailnet_ip") or row.get("tailnet_ipv4"),
            "status": row.get("status") or "healthy",
            "created_at": _iso(row.get("created_at")),
            "updated_at": _iso(row.get("updated_at") or row.get("created_at")),
        }

    events, event_order = _rebuild_events(source.events())

    repo.import_state({
        "version": 1,
        "missions": missions, "sessions": sessions, "services": services,
        "hosts": hosts, "artifacts": {}, "blobs": {}, "leases": {},
        "telemetry": {}, "events": events, "event_order": event_order,
    })
    return {"missions": len(missions), "sessions": len(sessions),
            "services": len(services), "hosts": len(hosts),
            "events": len(event_order)}

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
import json
import threading
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


class _LoopRunner:
    """Runs coroutines on a single dedicated event loop (daemon thread).

    asyncpg connections are bound to the loop they were created on and cannot
    be used across separate ``asyncio.run`` calls, so all async work (connect
    AND fetch) must happen on one persistent loop.
    """

    def __init__(self):
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, name="herdr-cp-pg-loop", daemon=True)
        self._thread.start()

    def run(self, coro: Any):
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result()


class PostgresSource(DurableSource):
    """Asyncpg-backed durable source (runs its queries via ``run``).

    Construct with ``dsn`` for a real Postgres (lazily connects on its own
    persistent loop so connect+fetch share one loop), or with a ``conn`` plus
    ``run`` for tests.
    """

    def __init__(self, conn: Any = None, *, run: Callable | None = None,
                 events_table: str = "fabric_events", dsn: str | None = None):
        if run is None:
            run = _LoopRunner().run
        self._conn = conn
        self._dsn = dsn
        self._run = run
        self._events_table = events_table
        self._cache: dict | None = None

    def _load(self) -> dict:
        if self._cache is None:
            self._cache = self._run(self._fetch_all())
        return self._cache

    async def _fetch_all(self) -> dict:
        if self._conn is None:
            if self._dsn is None:
                raise ValueError("PostgresSource needs a conn or dsn")
            import asyncpg
            self._conn = await asyncpg.connect(self._dsn)
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


def _ev_payload(event: dict) -> dict:
    """Return an event's payload as a dict (asyncpg yields jsonb as a str)."""
    p = event.get("payload") or {}
    if isinstance(p, str):
        try:
            p = json.loads(p)
        except (ValueError, TypeError):
            p = {}
    return p


def _reconstruct_from_events(streams: dict) -> tuple[dict, dict, dict]:
    """Replay the event log to rebuild hosts/missions/sessions (event sourcing).

    ``streams`` is the ``{stream: [events]}`` dict from ``_rebuild_events``.
    Flattened into ingest order, this reconstructs the structured entities that
    the durable write path persists *as events*, so ingestion survives a
    restart even though only ``fabric_events`` is written to. Returns
    ``(missions, sessions, hosts)`` dicts keyed by id.
    """
    missions: dict = {}
    sessions: dict = {}
    hosts: dict = {}
    flat: list[dict] = []
    for stream in streams.values():
        flat.extend(stream)
    flat.sort(key=lambda e: (e.get("ingest_timestamp") or "",
                             e.get("sequence") or 0))
    for ev in flat:
        et, eid = ev.get("entity_type"), ev.get("entity_id")
        if not et or not eid:
            continue
        ts = _iso(ev.get("source_timestamp"))
        p = _ev_payload(ev)
        etype = ev.get("event_type") or ""
        if et == "host":
            h = hosts.setdefault(eid, {
                "host_id": eid, "host_name": None, "tailnet_ip": None,
                "cpu": None, "mem": None, "status": "healthy",
                "created_at": ts, "updated_at": ts,
            })
            if p.get("host_name"):
                h["host_name"] = p["host_name"]
            if p.get("tailnet_ip"):
                h["tailnet_ip"] = p["tailnet_ip"]
            if p.get("cpu") is not None:
                h["cpu"] = p["cpu"]
            if p.get("mem") is not None:
                h["mem"] = p["mem"]
            h["status"] = "unavailable" if "unavailable" in etype.lower() else "healthy"
            h["updated_at"] = ts
        elif et == "mission":
            m = missions.setdefault(eid, {
                "mission_id": eid, "title": eid, "purpose": "",
                "stage": "active", "status": "active", "created_at": ts,
                "updated_at": ts, "sessions": [],
            })
            if p.get("title"):
                m["title"] = p["title"]
            if p.get("purpose"):
                m["purpose"] = p["purpose"]
            if etype == "MissionStageChanged" and p.get("to"):
                m["stage"] = m["status"] = p["to"]
            elif p.get("stage"):
                m["stage"] = m["status"] = p["stage"]
            m["updated_at"] = ts
        elif et == "session":
            s = sessions.setdefault(eid, {
                "session_id": eid, "mission_id": None, "host_id": None,
                "agent_role": "", "model": "", "status": "active",
                "created_at": ts, "updated_at": ts, "messages": 0,
                "tool_calls": 0,
            })
            if p.get("mission_id"):
                s["mission_id"] = p["mission_id"]
            if p.get("host_id"):
                s["host_id"] = p["host_id"]
            if p.get("agent_role"):
                s["agent_role"] = p["agent_role"]
            if p.get("model"):
                s["model"] = p["model"]
            if etype == "SessionAction":
                action = p.get("action")
                if action in ("stop", "terminate"):
                    s["status"] = "stopped"
                elif action:
                    s["status"] = "active"
            elif p.get("status"):
                s["status"] = p["status"]
            if "messagesent" in etype.lower() or etype == "SessionMessage":
                s["messages"] = (s.get("messages") or 0) + 1
            if "toolcalled" in etype.lower() or etype == "SessionToolCall":
                s["tool_calls"] = (s.get("tool_calls") or 0) + 1
            s["updated_at"] = ts
    for sid, s in sessions.items():
        mid = s.get("mission_id")
        if mid and mid in missions and sid not in missions[mid]["sessions"]:
            missions[mid]["sessions"].append(sid)
    return missions, sessions, hosts


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

    # Reconstruct the ingestion-managed entities from the event log so durable
    # events (the only thing the write-through sink persists) survive restart.
    # Structured-table rows still win where they carry richer state; event
    # replay fills in entities whose events were persisted but whose structured
    # row was never written.
    def _overlay(base: dict, extra: dict) -> None:
        """Fill gaps in ``base`` from ``extra``; structured rows win."""
        for k, v in extra.items():
            if k == "sessions":
                merged = base.setdefault("sessions", [])
                for sid in v:
                    if sid not in merged:
                        merged.append(sid)
            elif base.get(k) in (None, "", []):
                base[k] = v

    rmissions, rsessions, rhosts = _reconstruct_from_events(events)
    for mid, m in rmissions.items():
        _overlay(missions.setdefault(mid, m), m)
    for sid, s in rsessions.items():
        _overlay(sessions.setdefault(sid, s), s)
    for hid, h in rhosts.items():
        _overlay(hosts.setdefault(hid, h), h)
    for sid, s in sessions.items():
        mid = s.get("mission_id")
        if mid in missions and sid not in missions[mid]["sessions"]:
            missions[mid]["sessions"].append(sid)

    repo.import_state({
        "version": 1,
        "missions": missions, "sessions": sessions, "services": services,
        "hosts": hosts, "artifacts": {}, "blobs": {}, "leases": {},
        "telemetry": {}, "events": events, "event_order": event_order,
    })
    return {"missions": len(missions), "sessions": len(sessions),
            "services": len(services), "hosts": len(hosts),
            "events": len(event_order)}

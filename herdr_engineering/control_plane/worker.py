"""Background worker scaffold (Spec §95): stale leases, health reconcile,
snapshots, artifact GC. Bounded, never raises, runs outside the web frontend."""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

log = logging.getLogger(__name__)
LEASE_TTL = 90.0
HOST_TTL = 300.0
ARTIFACT_TTL = 3600.0


async def run_one_pass(conn, *, mission_store=None, session_store=None,
                       service_store=None, host_store=None,
                       snapshot_store=None, artifact_store=None,
                       lease_ttl=LEASE_TTL, host_ttl=HOST_TTL,
                       artifact_ttl=ARTIFACT_TTL) -> dict:
    summary: dict[str, Any] = {}

    # Stale services -> unavailable (never delete).
    stale_services = []
    if service_store is not None:
        for svc in await service_store.list():
            last = svc.get("last_seen")
            if last is None:
                continue
            if datetime.now(UTC) - last > timedelta(seconds=lease_ttl) \
                    and svc.get("health") != "unavailable" \
                    and svc.get("status") != "stopped":
                await service_store.update_health(svc["service_id"], "unavailable")
                stale_services.append(svc["service_id"])
    summary["stale_services"] = stale_services

    # Stale hosts -> unavailable (never delete).
    stale_hosts = []
    if host_store is not None:
        rows = await host_store.fetch("SELECT host_id, last_heartbeat FROM hosts")
        for row in rows:
            hb = row.get("last_heartbeat")
            if hb is None:
                continue
            if datetime.now(UTC) - hb > timedelta(seconds=host_ttl):
                await host_store.execute(
                    "UPDATE hosts SET status='unavailable' WHERE host_id=$1",
                    row["host_id"])
                stale_hosts.append(row["host_id"])
    summary["stale_hosts"] = stale_hosts

    # Snapshots for active missions.
    snapshots = 0
    if mission_store is not None and snapshot_store is not None:
        for m in await mission_store.list(status="active"):
            await snapshot_store.save("mission", m["mission_id"],
                                      dict(m), sequence=0)
            snapshots += 1
    summary["snapshots"] = snapshots

    # Artifact GC: Phase 1 scaffold only; real reachability GC in Phase 5.
    summary["artifact_gc"] = 0
    return summary


async def run_worker(conn, *, interval: float = 30.0,
                     stop: asyncio.Event | None = None, **stores) -> None:
    stop = stop or asyncio.Event()
    while not stop.is_set():
        try:
            await run_one_pass(conn, **stores)
        except Exception as exc:  # bounded: never crash the worker
            log.exception("worker pass failed: %s", exc)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except TimeoutError:
            continue

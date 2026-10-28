"""Background worker scaffold (Spec §95): stale leases, health reconcile,
snapshots, artifact GC. Bounded, never raises, runs outside the web frontend."""
from __future__ import annotations

import asyncio
import json
import logging
import socket
from datetime import UTC, datetime, timedelta
from typing import Any

log = logging.getLogger(__name__)
LEASE_TTL = 90.0
HOST_TTL = 300.0
ARTIFACT_TTL = 3600.0


async def _run_artifact_job_pass(conn, *, worker_id: str,
                                 runner, max_jobs: int = 1) -> dict:
    # Claim and mark running atomically; release the row lock before rendering.
    completed = failed = claimed = 0
    if max_jobs > 0:
        # Execution proves this identity exists, not its fleet health or policy.
        await conn.execute(
            "INSERT INTO hosts (host_id, hostname) VALUES ($1,$2) "
            "ON CONFLICT (host_id) DO NOTHING", worker_id, socket.gethostname())
    for _ in range(max(0, max_jobs)):
        job = await conn.fetchrow(
            "WITH candidate AS ("
            "SELECT job_id FROM artifact_build_jobs WHERE status='queued' "
            "ORDER BY created_at, job_id FOR UPDATE SKIP LOCKED LIMIT 1) "
            "UPDATE artifact_build_jobs AS jobs SET status='running', assigned_host_id=$1, "
            "started_at=now() FROM candidate WHERE jobs.job_id=candidate.job_id "
            "RETURNING jobs.*", worker_id)
        if job is None:
            break
        claimed += 1
        job_id = job["job_id"]
        try:
            result = await runner(dict(job))
            updated = await conn.execute(
                "UPDATE artifact_build_jobs SET status='completed', result_version_id=$1, "
                "completed_at=now() WHERE job_id=$2 AND status='running' "
                "AND assigned_host_id=$3", result["version_id"], job_id, worker_id)
            completed += int(updated == "UPDATE 1")
        except Exception as exc:  # noqa: BLE001 - durable failure state is required
            updated = await conn.execute(
                "UPDATE artifact_build_jobs SET status='failed', error=$1::jsonb, "
                "completed_at=now() WHERE job_id=$2 AND status='running' "
                "AND assigned_host_id=$3",
                json.dumps({"message": str(exc)}), job_id, worker_id)
            failed += int(updated == "UPDATE 1")
    return {"artifact_jobs_claimed": claimed,
            "artifact_jobs_completed": completed,
            "artifact_jobs_failed": failed}


async def run_one_pass(conn, *, mission_store=None, session_store=None,
                       service_store=None, host_store=None,
                       snapshot_store=None, artifact_store=None,
                       lease_ttl=LEASE_TTL, host_ttl=HOST_TTL,
                       artifact_ttl=ARTIFACT_TTL, worker_id=None,
                       artifact_job_runner=None, max_artifact_jobs=1) -> dict:
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
    summary.update({"artifact_jobs_claimed": 0,
                    "artifact_jobs_completed": 0,
                    "artifact_jobs_failed": 0})
    if conn is not None and worker_id and artifact_job_runner is not None:
        summary.update(await _run_artifact_job_pass(
            conn, worker_id=worker_id, runner=artifact_job_runner,
            max_jobs=max_artifact_jobs))
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

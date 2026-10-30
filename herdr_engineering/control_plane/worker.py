"""Background worker scaffold (Spec §95): stale leases, health reconcile,
snapshots, artifact GC. Bounded, never raises, runs outside the web frontend."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import socket
import stat
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .share_bundle import remove_bundle, remove_state

log = logging.getLogger(__name__)
LEASE_TTL = 90.0
HOST_TTL = 300.0
ARTIFACT_TTL = 3600.0


SHARE_INTERVAL = 60.0
SHARE_ORPHAN_GRACE = 3600.0  # never touch a share directory modified within this window (in-flight approve)
_SHARE_INGEST_ROUNDS = 64  # each round reads at most 16 MiB
_TOKEN_DIR = re.compile(r"[0-9a-f]{64}\Z")
_SHARE_DIR = re.compile(r"share_[0-9a-f]{24}\Z")


def _old_real_dirs(directory: Path, pattern: re.Pattern, now: float):
    """Real (non-symlink) subdirectories of ``directory`` whose name matches ``pattern`` and that were last modified
    more than the grace period ago.  Anything else (files, symlinks, odd names) is ignored."""
    try:
        names = os.listdir(directory)
    except OSError:
        return
    for name in names:
        if not pattern.match(name):
            continue
        try:
            info = os.lstat(directory / name)
        except OSError:
            continue
        if stat.S_ISDIR(info.st_mode) and now - info.st_mtime > SHARE_ORPHAN_GRACE:
            yield name


async def _sweep_expired(store, shares_root: Path, now: float) -> int:
    expired = 0
    for share in await store.live_expired(datetime.fromtimestamp(now, UTC)):
        share_id = share["share_id"]
        try:
            remove_state(shares_root, share["token_hash"])  # first: the link stops working at once
            await store.mark_revoked(share_id, actor="sweep")
            await store.add_event(share_id, "expired", "sweep", {})
            remove_bundle(shares_root, share_id)
            expired += 1
        except Exception:  # noqa: BLE001 - one bad share must not stop the rest
            log.exception("share expiry sweep failed for %s", share_id)
    return expired


async def _sweep_orphans(store, shares_root: Path, now: float) -> int:
    removed = 0
    token_dirs = list(_old_real_dirs(shares_root / "by-token", _TOKEN_DIR, now))
    bundle_dirs = list(_old_real_dirs(shares_root, _SHARE_DIR, now))
    if not token_dirs and not bundle_dirs:
        return 0
    live = await store.live_token_hashes()  # read after listing: a share approved meanwhile is already protected
    states = await store.known_share_states()
    for name in token_dirs:
        if name not in live:
            try:
                remove_state(shares_root, name)
                removed += 1
            except Exception:  # noqa: BLE001
                log.exception("share orphan token dir %s not removed", name)
    for name in bundle_dirs:
        if states.get(name) not in ("live", "pending"):
            try:
                remove_bundle(shares_root, name)
                removed += 1
            except Exception:  # noqa: BLE001
                log.exception("share orphan bundle %s not removed", name)
    return removed


async def share_housekeeping(store, shares_root: Path, events_path: Path, now: float, *,
                             marker_path: Path | None = None) -> dict:
    """Expire due shares, ingest the share container's audit events, and remove orphaned projections/bundles."""
    shares_root, events_path = Path(shares_root), Path(events_path)
    result = {"expired": 0, "ingested": 0, "orphans_removed": 0}
    result["expired"] = await _sweep_expired(store, shares_root, now)
    try:
        for _ in range(_SHARE_INGEST_ROUNDS):
            count = await store.ingest_events_file(events_path, marker_path=marker_path)
            if not count:
                break
            result["ingested"] += count
    except Exception:  # noqa: BLE001
        log.exception("share events ingest failed")
    try:
        result["orphans_removed"] = await _sweep_orphans(store, shares_root, now)
    except Exception:  # noqa: BLE001
        log.exception("share orphan sweep failed")
    return result


class ShareHousekeeper:
    """Runs ``share_housekeeping`` at most once per ``interval`` seconds; never raises out of the worker pass."""

    def __init__(self, shares_root, events_dir, marker_dir, *, interval: float = SHARE_INTERVAL, clock=time.time):
        self.shares_root, self.events_dir, self.marker_dir = Path(shares_root), Path(events_dir), Path(marker_dir)
        self.interval, self._clock = interval, clock
        self._last: float | None = None

    async def tick(self, conn) -> dict | str:
        from .artifact_shares import ShareStore
        now = self._clock()
        if self._last is not None and now - self._last < self.interval:
            return "skipped"
        self._last = now
        try:
            try:
                self.marker_dir.mkdir(parents=True, exist_ok=True)
            except OSError:
                log.exception("share marker dir %s not creatable", self.marker_dir)
            store = conn if hasattr(conn, "live_expired") else ShareStore(conn)
            return await share_housekeeping(store, self.shares_root, self.events_dir / "events.jsonl", now,
                                            marker_path=self.marker_dir / "events.marker.json")
        except Exception:  # noqa: BLE001
            log.exception("share housekeeping failed")
            return "error"


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
                json.dumps({"message": str(exc),
                            **({"code": exc.code} if getattr(exc, "code", None) else {})}),
                job_id, worker_id)
            failed += int(updated == "UPDATE 1")
    return {"artifact_jobs_claimed": claimed,
            "artifact_jobs_completed": completed,
            "artifact_jobs_failed": failed}


async def run_one_pass(conn, *, mission_store=None, session_store=None,
                       service_store=None, host_store=None,
                       snapshot_store=None, artifact_store=None,
                       lease_ttl=LEASE_TTL, host_ttl=HOST_TTL,
                       artifact_ttl=ARTIFACT_TTL, worker_id=None,
                       artifact_job_runner=None, max_artifact_jobs=1,
                       inspector_snapshotter=None, fabric_compactor=None,
                       share_housekeeper=None) -> dict:
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
    if conn is not None and inspector_snapshotter is not None:
        summary["inspector_snapshot"] = await inspector_snapshotter.tick(conn)
    if conn is not None and fabric_compactor is not None:
        summary["fabric_compaction"] = await fabric_compactor.tick(conn)
    if conn is not None and share_housekeeper is not None:
        summary["share_housekeeping"] = await share_housekeeper.tick(conn)
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

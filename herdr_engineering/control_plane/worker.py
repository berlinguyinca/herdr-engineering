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

from .artifact_collab_store import CollabGrantStore
from .artifacts import ArtifactStore
from .share_bundle import remove_bundle, remove_state

log = logging.getLogger(__name__)
LEASE_TTL = 90.0
HOST_TTL = 300.0
ARTIFACT_TTL = 3600.0


SHARE_INTERVAL = 60.0
SHARE_ORPHAN_GRACE = 3600.0  # never touch a share directory modified within this window (in-flight approve)
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
        # reads events.jsonl (and a pending events.jsonl.1) to the end and rotates it past EVENTS_ROTATE_BYTES:
        # share-public drops lines once the file reaches its cap, so an unrotated file would silence audit
        result["ingested"] = await store.ingest_and_rotate_events(events_path, marker_path=marker_path, now=now)
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


class ArtifactReconciler:
    """Runs the artifact registry's idempotent repair pass (``reconcile``: orphan blobs marked, expired uploads closed,
    held tombstones purged) at most once per ``interval`` seconds; never raises out of the worker pass.

    The worker has no graph emitter, so lineage re-emission (``emit``) is not part of this scheduled pass; whatever holds
    an emitter can still call ``reconcile(emit=...)``."""

    def __init__(self, store_factory, *, interval: float = 300.0, grace_seconds: float = 60.0,
                 orphan_ttl_seconds: float = 24 * 3600.0, limit: int = 100, clock=time.time):
        self.store_factory, self.interval, self._clock = store_factory, interval, clock
        self.kwargs = {"grace_seconds": grace_seconds, "orphan_ttl_seconds": orphan_ttl_seconds, "limit": limit}
        self._last: float | None = None

    async def tick(self, conn) -> dict | str:
        now = self._clock()
        if self._last is not None and now - self._last < self.interval:
            return "skipped"
        self._last = now
        try:
            return await self.store_factory(conn).reconcile(**self.kwargs)
        except Exception:  # noqa: BLE001
            log.exception("artifact reconcile failed")
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
                       share_housekeeper=None, artifact_reconciler=None) -> dict:
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
    if conn is not None and artifact_reconciler is not None:
        summary["artifact_reconcile"] = await artifact_reconciler.tick(conn)
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


# ----------------------------------------------------------------------------------------------------------------------
# The collaborator bridge loop (plan 3, Task 9)
# ----------------------------------------------------------------------------------------------------------------------

BRIDGE_RECONNECT = 5.0


class _ConnPool:
    """A small lazy pool of ``size`` connections made by ``connect``.

    A connection is opened on first use. One that was in use when an exception passed through (or that reports itself
    closed) is closed and replaced on the next use, so a broken connection never goes back into the pool."""

    def __init__(self, connect, size: int):
        self._connect = connect
        self._slots: asyncio.Queue = asyncio.Queue()
        for _ in range(size):
            self._slots.put_nowait(None)
        self._open: list = []
        self._closed = False

    def acquire(self):
        return _PoolLease(self)

    async def _take(self):
        slot = await self._slots.get()
        try:
            if slot is None or slot.is_closed():
                slot = await self._connect()
                self._open.append(slot)
            return slot
        except BaseException:
            self._slots.put_nowait(None)
            raise

    async def _give(self, conn, ok: bool) -> None:
        if ok and not self._closed and not conn.is_closed():
            self._slots.put_nowait(conn)
            return
        self._slots.put_nowait(None)
        await _close_quietly(conn)

    async def aclose(self) -> None:
        self._closed = True
        while self._slots.qsize():
            conn = self._slots.get_nowait()
            if conn is not None:
                await _close_quietly(conn)
        for conn in self._open:
            if not conn.is_closed():
                await _close_quietly(conn)


class _PoolLease:
    def __init__(self, pool: _ConnPool):
        self._pool, self._conn = pool, None

    async def __aenter__(self):
        self._conn = await self._pool._take()
        return self._conn

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self._pool._give(self._conn, exc_type is None)


class _Held:
    """``async with held() as conn``: always the same, already open connection."""

    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc) -> None:
        return None


async def _close_quietly(conn) -> None:
    try:
        await conn.close()
    except Exception:  # noqa: BLE001 - a connection that will not close cleanly is dropped
        pass


class CollabBridgeLoop:
    """The fast side of the collaborator bridge: three independent tasks beside the 30 s worker pass.

    * **grants** (own connection, every ``interval``): refreshes the shared retraction gate every
      ``retractions_every``, resolves invite grants from the fresh ``agents.json`` and publishes ``grants.json``
      (``heartbeat``). It never awaits a mirror refresh, so a revoke reaches ``grants.json`` within about ``interval``
      and the file stays fresh whatever the mirror or the applier are doing.
    * **mirror** (own connection): ``mirror.sync`` every ``mirror_every``, at once for artifacts whose grants changed
      or whose post-commit refresh failed, then the staging and result sweeps. A slow refresh delays only this task.
    * **applier** (a dedicated scan connection plus a pool for the lanes): ``applier.tick`` every ``interval``.

    ``agents.json`` is written by the bridge in collab-public, not here. Each task logs its own exceptions (type only)
    and reconnects after ``reconnect_delay`` (5 s); a failing task never stops the others. The gate, limits and clock
    are the mirror's and the applier's own (one of each, built by the caller)."""

    def __init__(self, dsn_connect, mirror, applier, *, interval: float = 1.0, mirror_every: float = 30.0,
                 retractions_every: float = 30.0, heartbeat: float = 30.0, sweep_every: float | None = None,
                 reconnect_delay: float = BRIDGE_RECONNECT, close_timeout: float = 10.0):
        self.dsn_connect, self.mirror, self.applier = dsn_connect, mirror, applier
        self.interval, self.mirror_every, self.retractions_every = interval, mirror_every, retractions_every
        self.heartbeat = heartbeat
        self.sweep_every = mirror_every if sweep_every is None else sweep_every
        self.reconnect_delay, self.close_timeout = reconnect_delay, close_timeout
        self.gate = applier.gate
        self.rustfs = mirror.rustfs
        self._force: set[str] = set()                    # artifacts the grants task saw change, for the mirror task

    # -- lifecycle ----------------------------------------------------------------------------------------------
    async def run(self, stop: asyncio.Event) -> None:
        pool = _ConnPool(self.dsn_connect, self.mirror.limits.apply_concurrency + 1)
        tasks = [asyncio.create_task(self._supervise(name, body, stop, pool), name=name) for name, body in (
            ("collab-grants", self._grants), ("collab-mirror", self._mirror), ("collab-applier", self._applier))]
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            try:
                await self.applier.aclose(self.close_timeout)
            finally:
                await pool.aclose()
                self.mirror.close()

    async def _supervise(self, name: str, body, stop: asyncio.Event, pool: _ConnPool) -> None:
        while not stop.is_set():
            conn = None
            try:
                conn = await self.dsn_connect()
                await body(conn, stop, pool)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - logged by type only: a message may carry a DSN
                log.warning("collab bridge %s task failed (%s); reconnecting in %.0fs", name, type(exc).__name__,
                            self.reconnect_delay)
            finally:
                if conn is not None:
                    await _close_quietly(conn)
            await self._wait(stop, self.reconnect_delay)

    @staticmethod
    async def _wait(stop: asyncio.Event, seconds: float) -> None:
        """Sleep ``seconds``, or less when ``stop`` is set."""
        try:
            await asyncio.wait_for(stop.wait(), timeout=seconds)
        except TimeoutError:
            pass

    # -- grants -------------------------------------------------------------------------------------------------
    async def _grants(self, conn, stop: asyncio.Event, _pool) -> None:
        store = ArtifactStore(conn, self.rustfs)
        grants = CollabGrantStore(conn)
        loaded: float | None = None
        while not stop.is_set():
            now = self.mirror.clock()
            if loaded is None or now - loaded >= self.retractions_every or now < loaded:
                try:
                    self.gate.load_retractions(await store.load_retractions())
                    loaded = now
                except Exception as exc:  # noqa: BLE001 - keep publishing with the gate as it is
                    log.warning("collab bridge: retraction refresh failed (%s)", type(exc).__name__)
                    loaded = now
            try:
                joins = await asyncio.to_thread(self._invite_joins)
                if joins:
                    await grants.resolve_invite_grants(joins)
            except Exception as exc:  # noqa: BLE001 - an invite problem must never hold back a revoke
                log.warning("collab bridge: invite grants failed (%s)", type(exc).__name__)
            published = await self.mirror.publish_grants(grants, store, heartbeat=self.heartbeat, gate=self.gate)
            if published.changed:
                self._force |= published.changed           # the mirror task syncs them on its next pass
            await self._wait(stop, self.interval)

    def _invite_joins(self) -> dict[str, dict]:
        """invite id -> {agent_id, hard_expiry} for the active agents of a fresh ``agents.json``; blocking."""
        agents = self.applier._fresh_feed_agents()
        joins: dict[str, dict] = {}
        for agent_id, row in (agents or {}).items():
            if isinstance(row, dict) and row.get("status") == "active" and isinstance(row.get("join_id"), str):
                joins[row["join_id"]] = {"agent_id": agent_id, "hard_expiry": row.get("hard_expiry")}
        return joins

    # -- mirror -------------------------------------------------------------------------------------------------
    async def _mirror(self, conn, stop: asyncio.Event, _pool) -> None:
        store = ArtifactStore(conn, self.rustfs)
        grants = CollabGrantStore(conn)
        last_sync: float | None = None
        last_sweep: float | None = None
        while not stop.is_set():
            now = self.mirror.clock()
            force, self._force = self._force | self.applier.take_mirror_force(), set()
            if force or last_sync is None or now - last_sync >= self.mirror_every or now < last_sync:
                try:
                    await self.mirror.sync(store, gate=self.gate, force=force, grants=grants)
                except BaseException:
                    self._force |= force                  # not lost: the next pass (after the reconnect) retries
                    raise
                last_sync = now
            if last_sweep is None or now - last_sweep >= self.sweep_every or now < last_sweep:
                last_sweep = now
                await self._sweeps(conn, grants)
            await self._wait(stop, self.interval)

    async def _sweeps(self, conn, grants: CollabGrantStore) -> None:
        """Staging sweep (only with a fresh ``agents.json``) and result sweep. ANY exception is logged by type."""
        try:
            agents = await asyncio.to_thread(self.applier._fresh_feed_agents)
            if agents is not None:
                await self.applier.sweep_staging(conn, agents=agents, grants=await grants.live_grants())
        except Exception as exc:  # noqa: BLE001 - e.g. RecursionError: a sweep never kills the task
            log.warning("collab bridge: staging sweep failed (%s)", type(exc).__name__)
        try:
            await self.applier.sweep_results()
        except Exception as exc:  # noqa: BLE001
            log.warning("collab bridge: result sweep failed (%s)", type(exc).__name__)

    # -- applier ------------------------------------------------------------------------------------------------
    async def _applier(self, conn, stop: asyncio.Event, pool: _ConnPool) -> None:
        # the scan runs on this task's own connection and never waits for a lane's (see the applier's docstring);
        # the pool holds one connection per lane plus one for the post-commit mirror refresher
        scan = _Held(conn)
        wait = min(self.interval, 0.5)
        while not stop.is_set():
            await self.applier.tick(pool.acquire, scan_acquire=lambda: scan, wait=wait)
            await self._wait(stop, self.interval)

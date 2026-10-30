"""Postgres lease store for P05: the same contract as ``team_tasks.LeaseStore``, atomic across processes.

Every transition is a single conditional statement (``INSERT .. ON CONFLICT DO UPDATE .. WHERE`` / ``UPDATE .. WHERE``),
so the database, not a lock in one Python process, decides who holds a task. ``now`` is passed in so tests can move
time; production passes the wall clock. Results must be JSON-serializable (jsonb)."""
from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from .team_tasks import Lease, LeaseError, TaskSpec


class PgLeaseStore:
    def __init__(self, pool, clock: Callable[[], datetime] | None = None) -> None:
        self.pool = pool
        self._clock = clock or (lambda: datetime.now(UTC))

    async def acquire(self, task: TaskSpec, worker: str, ttl_s: int = 60) -> Lease:
        now = self._clock()
        expires = now + timedelta(seconds=ttl_s)
        actions = sorted(task.allowed_actions)
        row = await self.pool.fetchrow(
            "INSERT INTO team_task_leases (task_id, next_token, token, holder, expires_at, actions) "
            "VALUES ($1, 2, 1, $2, $3, $4) "
            "ON CONFLICT (task_id) DO UPDATE SET holder = $2, token = team_task_leases.next_token, "
            "  next_token = team_task_leases.next_token + 1, expires_at = $3, revoked = false, actions = $4, "
            "  updated_at = now() "
            "WHERE NOT team_task_leases.done AND (team_task_leases.revoked OR team_task_leases.expires_at <= $5) "
            "RETURNING token",
            task.task_id, worker, expires, actions, now)
        if row is None:
            cur = await self.pool.fetchrow("SELECT done FROM team_task_leases WHERE task_id = $1", task.task_id)
            raise LeaseError("already_completed" if cur and cur["done"] else "held_by_another_worker")
        return Lease(task.task_id, worker, row["token"], expires, task.allowed_actions)

    async def heartbeat(self, task_id: str, token: int, ttl_s: int = 60) -> Lease:
        now = self._clock()
        expires = now + timedelta(seconds=ttl_s)
        row = await self.pool.fetchrow(
            "UPDATE team_task_leases SET expires_at = $3, updated_at = now() "
            "WHERE task_id = $1 AND token = $2 AND NOT done AND NOT revoked AND expires_at > $4 "
            "RETURNING holder, actions", task_id, token, expires, now)
        if row is None:
            await self._explain(task_id, token)
        return Lease(task_id, row["holder"], token, expires, frozenset(row["actions"]))

    async def complete(self, task_id: str, token: int, result: Any) -> Any:
        now = self._clock()
        row = await self.pool.fetchrow(
            "UPDATE team_task_leases SET done = true, done_token = token, result = $3, revoked = true, updated_at = now() "
            "WHERE task_id = $1 AND token = $2 AND NOT done AND NOT revoked AND expires_at > $4 RETURNING 1 AS ok",
            task_id, token, result, now)
        if row is not None:
            return result
        cur = await self.pool.fetchrow("SELECT done, done_token, result FROM team_task_leases WHERE task_id = $1", task_id)
        if cur is None:
            raise LeaseError("unknown_task")
        if cur["done"]:
            if token == cur["done_token"]:
                return cur["result"]
            raise LeaseError("already_completed_by_another_token")
        await self._explain(task_id, token)
        raise LeaseError("lease_expired_or_revoked")

    async def abort(self, task_id: str, token: int | None = None) -> None:
        await self.pool.execute(
            "UPDATE team_task_leases SET revoked = true, updated_at = now() "
            "WHERE task_id = $1 AND NOT done AND ($2::bigint IS NULL OR token = $2)", task_id, token)

    async def authorizes(self, task_id: str, token: int, action: str) -> bool:
        row = await self.pool.fetchrow(
            "SELECT 1 FROM team_task_leases WHERE task_id = $1 AND token = $2 AND NOT done AND NOT revoked "
            "AND expires_at > $3 AND $4 = ANY(actions)", task_id, token, self._clock(), action)
        return row is not None

    async def _explain(self, task_id: str, token: int) -> None:
        cur = await self.pool.fetchrow("SELECT token FROM team_task_leases WHERE task_id = $1", task_id)
        if cur is None or cur["token"] != token:
            raise LeaseError("stale_fencing_token")
        raise LeaseError("lease_expired_or_revoked")

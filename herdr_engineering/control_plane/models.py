"""Mission / Session / Service stores (Spec §19, §26-27, §45)."""
from __future__ import annotations

from typing import Any

from . import ids


class _Base:
    def __init__(self, conn: Any):
        self._conn = conn


class MissionStore(_Base):
    async def create(self, title: str, *, purpose=None, repository=None,
                     branch=None, worktree=None, owner=None) -> str:
        mid = ids.new_id("mission")
        await self._conn.execute(
            "INSERT INTO missions (mission_id, title, purpose, repository, "
            " branch, worktree, owner) VALUES ($1,$2,$3,$4,$5,$6,$7)",
            mid, title, purpose, repository, branch, worktree, owner)
        return mid

    async def get(self, mission_id: str) -> dict | None:
        return await self._conn.fetchrow(
            "SELECT * FROM missions WHERE mission_id=$1", mission_id)

    async def list(self, status: str | None = None) -> list[dict]:
        if status is None:
            return await self._conn.fetch("SELECT * FROM missions")
        return await self._conn.fetch(
            "SELECT * FROM missions WHERE status=$1", status)

    async def update(self, mission_id: str, **fields: Any) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k}=${i + 1}" for i, k in enumerate(fields))
        await self._conn.execute(
            f"UPDATE missions SET {cols}, updated_at=now() "
            f"WHERE mission_id=${len(fields) + 1}", *fields.values(), mission_id)


class SessionStore(_Base):
    async def create(self, mission_id=None, *, title=None, agent_role=None,
                     model=None, host_id=None) -> str:
        sid = ids.new_id("session")
        await self._conn.execute(
            "INSERT INTO sessions (session_id, mission_id, title, agent_role, "
            " model, host_id) VALUES ($1,$2,$3,$4,$5,$6)",
            sid, mission_id, title, agent_role, model, host_id)
        return sid

    async def get(self, session_id: str) -> dict | None:
        return await self._conn.fetchrow(
            "SELECT * FROM sessions WHERE session_id=$1", session_id)

    async def list(self, status: str | None = None) -> list[dict]:
        if status is None:
            return await self._conn.fetch("SELECT * FROM sessions")
        return await self._conn.fetch(
            "SELECT * FROM sessions WHERE status=$1", status)

    async def touch(self, session_id: str) -> None:
        await self._conn.execute(
            "UPDATE sessions SET last_activity=now() WHERE session_id=$1", session_id)


class ServiceStore(_Base):
    async def register(self, *, service_name: str, host_id: str, port: int,
                       route=None, mission_id=None, session_id=None,
                       owner=None, purpose=None) -> str:
        svcid = ids.new_id("service")
        await self._conn.execute(
            "INSERT INTO services (service_id, service_name, route, port, host_id, "
            " mission_id, session_id, owner, purpose) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)",
            svcid, service_name, route, port, host_id, mission_id,
            session_id, owner, purpose)
        return svcid

    async def get(self, service_id: str) -> dict | None:
        return await self._conn.fetchrow(
            "SELECT * FROM services WHERE service_id=$1", service_id)

    async def list(self, host_id=None, mission_id=None) -> list[dict]:
        clauses, args = [], []
        if host_id:
            clauses.append(f"host_id=${len(args) + 1}")
            args.append(host_id)
        if mission_id:
            clauses.append(f"mission_id=${len(args) + 1}")
            args.append(mission_id)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        return await self._conn.fetch(f"SELECT * FROM services{where}", *args)

    async def update_health(self, service_id: str, health: str) -> None:
        await self._conn.execute(
            "UPDATE services SET health=$1, last_seen=now() WHERE service_id=$2",
            health, service_id)

    async def mark_stopped(self, service_id: str) -> None:
        await self._conn.execute(
            "UPDATE services SET status='stopped', stopped_at=now() "
            "WHERE service_id=$1", service_id)

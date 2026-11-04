"""Member usage accounting and optional limits (plan 5, Task 3).

Accounting always runs; a limit is enforced only when its effective value is set (``None`` = unlimited).
``bytes_used`` counts every ``artifact_blobs`` row scoped to one of the member's artifacts that is not purged (all
versions; a digest is one row per artifact, so dedup is within an artifact only).  ``bytes_retracted`` is the part of
it on soft-deleted artifacts.
"""
from __future__ import annotations

from typing import Any

import asyncpg

from .artifact_members import MemberLimits, is_member_principal
from .artifacts import ArtifactError

_SOFT_DELETED = ("retracted", "tombstoned")


class QuotaExceeded(ArtifactError):
    """``code`` is ``member_bytes``, ``member_artifacts`` or ``artifact_bytes``."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


async def _lock_usage(conn: asyncpg.Connection, principal: str) -> asyncpg.Record:
    if not is_member_principal(principal):
        raise ValueError("not a member principal")
    await conn.execute(
        "INSERT INTO artifact_member_usage (member_principal) VALUES ($1) ON CONFLICT DO NOTHING", principal)
    return await conn.fetchrow(
        "SELECT bytes_used FROM artifact_member_usage WHERE member_principal=$1 FOR UPDATE", principal)


async def check_new_artifact(conn: asyncpg.Connection, principal: str, limits: MemberLimits) -> None:
    await _lock_usage(conn, principal)
    if limits.max_artifacts is None:
        return
    count = await conn.fetchval(
        "SELECT count(*) FROM artifacts WHERE created_by=$1 AND lifecycle<>'purged'", principal)
    if count >= limits.max_artifacts:
        raise QuotaExceeded("member_artifacts", f"artifact limit of {limits.max_artifacts} reached")


def _manifest_total(output_manifest: list[dict[str, Any]]) -> int:
    return sum(int(entry.get("size_bytes") or 0) for entry in output_manifest)


async def check_publish(conn: asyncpg.Connection, principal: str, limits: MemberLimits, *, new_bytes: int,
                        output_manifest: list[dict[str, Any]]) -> None:
    """Run before assembly, with the declared sizes of not-yet-verified uploads; locks the usage row."""
    row = await _lock_usage(conn, principal)
    if limits.max_member_bytes is not None and row["bytes_used"] + new_bytes > limits.max_member_bytes:
        raise QuotaExceeded("member_bytes", f"storage limit of {limits.max_member_bytes} bytes exceeded")
    if limits.max_artifact_bytes is not None and _manifest_total(output_manifest) > limits.max_artifact_bytes:
        raise QuotaExceeded("artifact_bytes", f"artifact size limit of {limits.max_artifact_bytes} bytes exceeded")


async def settle(conn: asyncpg.Connection, principal: str) -> dict[str, int]:
    """Recompute ``bytes_used`` / ``bytes_retracted`` from blobs; call after each publish, delete and purge."""
    await _lock_usage(conn, principal)
    row = await conn.fetchrow(
        "SELECT COALESCE(sum(b.size_bytes), 0)::bigint AS used,"
        " COALESCE(sum(b.size_bytes) FILTER (WHERE a.lifecycle = ANY($2::text[])), 0)::bigint AS retracted"
        " FROM artifact_blobs b JOIN artifacts a ON a.artifact_id = b.scope"
        " WHERE a.created_by=$1 AND a.lifecycle<>'purged'", principal, list(_SOFT_DELETED))
    await conn.execute(
        "UPDATE artifact_member_usage SET bytes_used=$2, bytes_retracted=$3, updated_at=now()"
        " WHERE member_principal=$1", principal, row["used"], row["retracted"])
    return {"bytes_used": row["used"], "bytes_retracted": row["retracted"]}

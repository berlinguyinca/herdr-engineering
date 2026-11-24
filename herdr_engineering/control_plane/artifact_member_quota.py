"""Member usage accounting and optional limits (plan 5, Task 3).

Accounting always runs; a limit is enforced only when its effective value is set (``None`` = unlimited).
``bytes_used`` counts every ``artifact_blobs`` row scoped to one of the member's artifacts that is not purged (all
versions; a digest is one row per artifact, so dedup is within an artifact only).  ``bytes_retracted`` is the part of
it on soft-deleted artifacts.

Purged means what ``ArtifactLifecycleMixin.purge_due`` leaves behind: the artifact stays ``tombstoned`` but every one of
its versions has ``purged_at`` set and its ``artifact_blobs`` rows are gone (``artifacts.lifecycle`` has no ``purged``
value; migration 0031's CHECK forbids it).  An artifact with no versions yet is not purged.

Transactions: ``check_new_artifact`` / ``check_publish`` take ``SELECT ... FOR UPDATE`` on the member's usage row, so
the check, the publish it guards and the following ``settle`` must all run in ONE transaction on the caller's
connection (the lock is released at commit); ``settle`` must follow every publish, delete and purge.
"""
from __future__ import annotations

from typing import Any

import asyncpg

from .artifact_members import MemberLimits, is_member_principal
from .artifacts import ArtifactError

_SOFT_DELETED = ("retracted", "tombstoned")
# Real purge state (see module docstring): has versions, and none of them is still unpurged.
_NOT_PURGED = ("(NOT EXISTS (SELECT 1 FROM artifact_versions v WHERE v.artifact_id=a.artifact_id)"
               " OR EXISTS (SELECT 1 FROM artifact_versions v WHERE v.artifact_id=a.artifact_id AND v.purged_at IS NULL))")


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
        f"SELECT count(*) FROM artifacts a WHERE a.created_by=$1 AND {_NOT_PURGED}", principal)
    if count >= limits.max_artifacts:
        raise QuotaExceeded("member_artifacts", f"artifact limit of {limits.max_artifacts} reached")


def _natural(value: Any, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{what} must be a non-negative integer")
    return value


def _manifest_total(output_manifest: list[dict[str, Any]]) -> int:
    return sum(_natural(entry.get("size_bytes"), "manifest size_bytes") for entry in output_manifest)


async def check_publish(conn: asyncpg.Connection, principal: str, limits: MemberLimits, *, new_bytes: int,
                        output_manifest: list[dict[str, Any]]) -> None:
    """Run before assembly, with the declared sizes of not-yet-verified uploads; locks the usage row."""
    new_bytes = _natural(new_bytes, "new_bytes")
    manifest_total = _manifest_total(output_manifest)
    row = await _lock_usage(conn, principal)
    if limits.max_member_bytes is not None and row["bytes_used"] + new_bytes > limits.max_member_bytes:
        raise QuotaExceeded("member_bytes", f"storage limit of {limits.max_member_bytes} bytes exceeded")
    if limits.max_artifact_bytes is not None and manifest_total > limits.max_artifact_bytes:
        raise QuotaExceeded("artifact_bytes", f"artifact size limit of {limits.max_artifact_bytes} bytes exceeded")


async def settle(conn: asyncpg.Connection, principal: str) -> dict[str, int]:
    """Recompute ``bytes_used`` / ``bytes_retracted`` from blobs; call after each publish, delete and purge."""
    await _lock_usage(conn, principal)
    row = await conn.fetchrow(
        "SELECT COALESCE(sum(b.size_bytes), 0)::bigint AS used,"
        " COALESCE(sum(b.size_bytes) FILTER (WHERE a.lifecycle = ANY($2::text[])), 0)::bigint AS retracted"
        " FROM artifact_blobs b JOIN artifacts a ON a.artifact_id = b.scope"
        f" WHERE a.created_by=$1 AND {_NOT_PURGED}", principal, list(_SOFT_DELETED))
    await conn.execute(
        "UPDATE artifact_member_usage SET bytes_used=$2, bytes_retracted=$3, updated_at=now()"
        " WHERE member_principal=$1", principal, row["used"], row["retracted"])
    return {"bytes_used": row["used"], "bytes_retracted": row["retracted"]}

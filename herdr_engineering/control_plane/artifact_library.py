"""Artifact library: per-owner folders and filing of readable artifacts (migration 0051).

``owner`` is the principal string used for ``artifacts.created_by`` (``member:<id>``).  Anything the caller may not
see (someone else's folder, an unreadable or missing artifact) is the same ``LibraryError(404, "not_found")``.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Literal

import asyncpg

FolderSource = Literal["manual", "suggested", "auto"]

MAX_FOLDER_NAME = 80

# Same visibility as ArtifactRegistry.list_records: owned, or an unexpired ACL read/admin grant; tombstoned excluded.
READABLE_SQL = (
    "SELECT 1 FROM artifacts a WHERE a.artifact_id=$2 AND a.lifecycle<>'tombstoned' AND "
    "(a.created_by=$1 OR EXISTS (SELECT 1 FROM artifact_acl acl "
    "WHERE acl.artifact_id=a.artifact_id AND acl.principal_id=$1 "
    "AND (acl.expires_at IS NULL OR acl.expires_at > now()) "
    "AND (acl.permissions ? 'read' OR acl.permissions ? 'admin')))"
)

_FOLDER_SELECT = (
    "SELECT f.folder_id, f.owner, f.name, "
    "(SELECT count(*) FROM artifact_library_items i WHERE i.folder_id=f.folder_id) AS item_count "
    "FROM artifact_library_folders f"
)


class LibraryError(Exception):
    def __init__(self, status: int, code: str):
        super().__init__(code)
        self.status = status
        self.code = code


NOT_FOUND = (404, "not_found")


@dataclass(frozen=True)
class Folder:
    folder_id: str
    owner: str
    name: str
    item_count: int

    @classmethod
    def from_row(cls, row: asyncpg.Record) -> Folder:
        return cls(row["folder_id"], row["owner"], row["name"], int(row["item_count"]))


def _clean_name(name: str) -> str:
    cleaned = (name or "").strip()
    if not cleaned or len(cleaned) > MAX_FOLDER_NAME:
        raise LibraryError(400, "invalid_name")
    return cleaned


class LibraryStore:
    def __init__(self, pool: asyncpg.Pool):
        self._pool = pool

    async def create_folder(self, owner: str, name: str) -> Folder:
        cleaned = _clean_name(name)
        folder_id = "fld_" + secrets.token_hex(12)
        try:
            await self._pool.execute(
                "INSERT INTO artifact_library_folders (folder_id, owner, name) VALUES ($1,$2,$3)",
                folder_id, owner, cleaned)
        except asyncpg.UniqueViolationError:
            raise LibraryError(409, "duplicate_name") from None
        return Folder(folder_id, owner, cleaned, 0)

    async def rename_folder(self, owner: str, folder_id: str, name: str) -> Folder:
        cleaned = _clean_name(name)
        try:
            updated = await self._pool.fetchval(
                "UPDATE artifact_library_folders SET name=$3 WHERE folder_id=$1 AND owner=$2 RETURNING folder_id",
                folder_id, owner, cleaned)
        except asyncpg.UniqueViolationError:
            raise LibraryError(409, "duplicate_name") from None
        if updated is None:
            raise LibraryError(*NOT_FOUND)
        return await self._folder(owner, folder_id)

    async def delete_folder(self, owner: str, folder_id: str) -> None:
        deleted = await self._pool.fetchval(
            "DELETE FROM artifact_library_folders WHERE folder_id=$1 AND owner=$2 RETURNING folder_id",
            folder_id, owner)  # items cascade; artifacts are untouched
        if deleted is None:
            raise LibraryError(*NOT_FOUND)

    async def list_folders(self, owner: str) -> list[Folder]:
        rows = await self._pool.fetch(_FOLDER_SELECT + " WHERE f.owner=$1 ORDER BY lower(f.name), f.folder_id", owner)
        return [Folder.from_row(r) for r in rows]

    async def file_artifact(self, owner: str, folder_id: str, artifact_id: str, *,
                            source: FolderSource = "manual") -> None:
        async with self._pool.acquire() as conn:
            await self._require_folder(conn, owner, folder_id)
            if await conn.fetchval(READABLE_SQL, owner, artifact_id) is None:
                raise LibraryError(*NOT_FOUND)
            await conn.execute(
                "INSERT INTO artifact_library_items (folder_id, artifact_id, source) VALUES ($1,$2,$3) "
                "ON CONFLICT (folder_id, artifact_id) DO NOTHING", folder_id, artifact_id, source)

    async def unfile_artifact(self, owner: str, folder_id: str, artifact_id: str) -> None:
        async with self._pool.acquire() as conn:
            await self._require_folder(conn, owner, folder_id)
            await conn.execute("DELETE FROM artifact_library_items WHERE folder_id=$1 AND artifact_id=$2",
                               folder_id, artifact_id)

    async def _folder(self, owner: str, folder_id: str) -> Folder:
        row = await self._pool.fetchrow(_FOLDER_SELECT + " WHERE f.folder_id=$1 AND f.owner=$2", folder_id, owner)
        if row is None:
            raise LibraryError(*NOT_FOUND)
        return Folder.from_row(row)

    @staticmethod
    async def _require_folder(conn: asyncpg.Connection, owner: str, folder_id: str) -> None:
        if await conn.fetchval("SELECT 1 FROM artifact_library_folders WHERE folder_id=$1 AND owner=$2",
                               folder_id, owner) is None:
            raise LibraryError(*NOT_FOUND)

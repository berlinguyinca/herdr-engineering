"""Artifact library: per-owner folders and filing of readable artifacts (migration 0051).

``owner`` is the principal string used for ``artifacts.created_by`` (``member:<id>``).  Anything the caller may not
see (someone else's folder, an unreadable or missing artifact) is the same ``LibraryError(404, "not_found")``.
"""

from __future__ import annotations

import secrets
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

import asyncpg

FolderSource = Literal["manual", "suggested", "auto"]
Source = Literal["mine", "assigned", "opened", "pending"]
ItemStatus = Literal["active", "expired", "revoked", "pending"]

DEFAULT_SOURCES: frozenset[Source] = frozenset({"mine", "assigned", "opened"})  # pending is opt-in
MAX_PAGE = 200

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
    def __init__(self, status: int, code: str, message: str | None = None):
        super().__init__(message or code)
        self.status = status
        self.code = code
        self.message = message or code


PENDING_CAP = 25


PENDING_CAP_STATUS = 429
PENDING_CAP_CODE = "pending_cap"
PENDING_CAP_MESSAGE = f"You have {PENDING_CAP} pending artifacts; accept or discard some in your library before pushing more."


class PendingCapExceeded(LibraryError):
    def __init__(self) -> None:
        super().__init__(PENDING_CAP_STATUS, PENDING_CAP_CODE, PENDING_CAP_MESSAGE)


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


@dataclass(frozen=True)
class FeedItem:
    artifact_id: str
    title: str
    sharer: str
    shared_at: datetime
    source: Source
    status: ItemStatus
    expires_at: datetime | None
    share_id: str | None
    folder_ids: tuple[str, ...]
    artifact_type: str
    size_bytes: int


@dataclass(frozen=True)
class FeedPage:
    items: list[FeedItem]
    total: int
    next_offset: int | None


MAX_QUERY = 200


def library_matches(
    q: str,
    *,
    title: str,
    sharer: str,
    artifact_type: str,
    filename: str,
    status: str,
    folder_names: Iterable[str],
) -> bool:
    """The python twin of the SQL ``q`` rule in ``LibraryStore.feed`` (case-insensitive substring, literal ``%`` and
    ``_``) for readers of the member view file: title, sharer, type, the filename only while the item is still readable
    (active or pending), and the member's own folder names.  An empty ``q`` matches everything."""
    if not q:
        return True
    needle = q.lower()
    fields = [title, sharer, artifact_type, *folder_names]
    if status in ("active", "pending"):
        fields.append(filename)
    return any(needle in (f or "").lower() for f in fields)


def _like(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


# $1 = member principal, $2 = member_id, $3 = lowercased email, $4 = groups.  Every candidate carries its badge
# priority (lower wins); DISTINCT ON keeps one row per artifact.  Tombstoned artifacts never appear.
_TITLE = "coalesce(nullif(a.display_name,''), nullif(a.original_filename,''), a.artifact_id)"
_CANDIDATES_SQL = f"""
SELECT a.artifact_id, {_TITLE} AS title, a.created_by AS sharer, a.created_at AS shared_at, 'pending' AS source,
       0 AS prio, 'pending' AS status, NULL::timestamptz AS expires_at, NULL::text AS share_id,
       a.artifact_type, coalesce(a.size_bytes,0) AS size_bytes
  FROM artifacts a WHERE a.created_by=$1 AND a.intake='pending' AND a.lifecycle NOT IN ('retracted','tombstoned')
UNION ALL
SELECT a.artifact_id, {_TITLE}, a.created_by, a.created_at, 'mine', 1, 'active', NULL, NULL,
       a.artifact_type, coalesce(a.size_bytes,0)
  FROM artifacts a WHERE a.created_by=$1 AND a.intake='accepted' AND a.lifecycle NOT IN ('retracted','tombstoned')
UNION ALL
SELECT a.artifact_id, {_TITLE}, acl.granted_by, acl.created_at, 'assigned', 2, 'active', acl.expires_at, NULL,
       a.artifact_type, coalesce(a.size_bytes,0)
  FROM artifact_acl acl JOIN artifacts a ON a.artifact_id=acl.artifact_id
 WHERE acl.principal_id=$1 AND (acl.expires_at IS NULL OR acl.expires_at > now())
   AND (acl.permissions ? 'read' OR acl.permissions ? 'admin')
   AND a.intake='accepted' AND a.lifecycle NOT IN ('retracted','tombstoned')
UNION ALL
SELECT a.artifact_id, coalesce(nullif(s.title,''), {_TITLE}), coalesce(s.created_by,''), coalesce(s.approved_at, s.created_at),
       'assigned', 2, 'active', s.expires_at, s.share_id, a.artifact_type, coalesce(a.size_bytes,0)
  FROM artifact_shares s JOIN artifacts a ON a.artifact_id=s.artifact_id
 WHERE s.state='live' AND s.audience='lab' AND (s.expires_at IS NULL OR s.expires_at > now())
   AND (EXISTS (SELECT 1 FROM unnest(s.allow_emails) e WHERE lower(e)=$3) OR s.allow_groups && $4::text[])
   AND a.intake='accepted' AND a.lifecycle NOT IN ('retracted','tombstoned')
UNION ALL
SELECT a.artifact_id, coalesce(nullif(c.title,''), nullif(s.title,''), {_TITLE}), coalesce(c.sharer, s.created_by, ''),
       c.claimed_at, 'opened', 3,
       CASE WHEN s.state='revoked' THEN 'revoked'
            WHEN s.expires_at IS NOT NULL AND s.expires_at <= now() THEN 'expired' ELSE 'active' END,
       s.expires_at, s.share_id,
       CASE WHEN s.state='revoked' OR (s.expires_at IS NOT NULL AND s.expires_at <= now())
            THEN '' ELSE a.artifact_type END,
       CASE WHEN s.state='revoked' OR (s.expires_at IS NOT NULL AND s.expires_at <= now())
            THEN 0 ELSE coalesce(a.size_bytes,0) END
  FROM artifact_share_claims c JOIN artifact_shares s ON s.share_id=c.share_id
  JOIN artifacts a ON a.artifact_id=s.artifact_id
 WHERE c.member_id=$2 AND a.lifecycle NOT IN ('retracted','tombstoned') AND a.intake='accepted'
"""


# A pending artifact still counts until it is deleted (member delete retracts it) or tombstoned.
_PENDING_LIVE = "created_by=$1 AND intake='pending' AND lifecycle NOT IN ('retracted','tombstoned')"


async def count_pending_on(conn: asyncpg.Connection | asyncpg.Pool, owner: str) -> int:
    return int(await conn.fetchval(f"SELECT count(*) FROM artifacts WHERE {_PENDING_LIVE}", owner))


async def require_pending_on(
    conn: asyncpg.Connection | asyncpg.Pool, owner: str, artifact_id: str
) -> None:
    """Raise not_found unless ``artifact_id`` is ``owner``'s own pending artifact."""
    found = await conn.fetchval(
        f"SELECT 1 FROM artifacts WHERE artifact_id=$2 AND {_PENDING_LIVE}", owner, artifact_id
    )
    if found is None:
        raise LibraryError(*NOT_FOUND)


async def accept_pending_on(
    conn: asyncpg.Connection | asyncpg.Pool, owner: str, artifact_id: str
) -> None:
    done = await conn.execute(
        f"UPDATE artifacts SET intake='accepted' WHERE artifact_id=$2 AND {_PENDING_LIVE}",
        owner,
        artifact_id,
    )
    if done != "UPDATE 1":
        raise LibraryError(*NOT_FOUND)


class LibraryStore:
    def __init__(self, pool: asyncpg.Pool):
        self._pool = pool

    async def count_pending(self, owner: str) -> int:
        return await count_pending_on(self._pool, owner)

    async def accept_pending(self, owner: str, artifact_id: str) -> None:
        await accept_pending_on(self._pool, owner, artifact_id)

    async def discard_pending(
        self, owner: str, artifact_id: str, delete: Callable[[str], Awaitable[object]]
    ) -> None:
        """Delete ``owner``'s own pending artifact through ``delete`` (the member delete path)."""
        await require_pending_on(self._pool, owner, artifact_id)
        await delete(artifact_id)

    async def close(self) -> None:
        """Close the connection pool (server shutdown)."""
        await self._pool.close()

    async def discard_member_pending(self, owner: str, artifact_id: str, *, shares_root: Any = None,
                                     rustfs: Any = None) -> None:
        """Discard a member's own pending push through the member delete (``delete_member_rows``): retract, revoke
        every share inside the transaction, settle the quota.  ``not_found`` unless ``owner`` has it pending."""
        from .artifact_member_intents import delete_member_rows
        from .artifacts import ArtifactStore
        async with self._pool.acquire() as conn:
            await require_pending_on(conn, owner, artifact_id)
            if not await delete_member_rows(conn, ArtifactStore(conn, rustfs), shares_root, owner, artifact_id):
                raise LibraryError(*NOT_FOUND)

    async def create_folder(self, owner: str, name: str) -> Folder:
        cleaned = _clean_name(name)
        folder_id = "fld_" + secrets.token_hex(12)
        try:
            await self._pool.execute(
                "INSERT INTO artifact_library_folders (folder_id, owner, name) VALUES ($1,$2,$3)",
                folder_id,
                owner,
                cleaned,
            )
        except asyncpg.UniqueViolationError:
            raise LibraryError(409, "duplicate_name") from None
        return Folder(folder_id, owner, cleaned, 0)

    async def rename_folder(self, owner: str, folder_id: str, name: str) -> Folder:
        cleaned = _clean_name(name)
        try:
            updated = await self._pool.fetchval(
                "UPDATE artifact_library_folders SET name=$3 WHERE folder_id=$1 AND owner=$2 RETURNING folder_id",
                folder_id,
                owner,
                cleaned,
            )
        except asyncpg.UniqueViolationError:
            raise LibraryError(409, "duplicate_name") from None
        if updated is None:
            raise LibraryError(*NOT_FOUND)
        return await self._folder(owner, folder_id)

    async def delete_folder(self, owner: str, folder_id: str) -> None:
        deleted = await self._pool.fetchval(
            "DELETE FROM artifact_library_folders WHERE folder_id=$1 AND owner=$2 RETURNING folder_id",
            folder_id,
            owner,
        )  # items cascade; artifacts are untouched
        if deleted is None:
            raise LibraryError(*NOT_FOUND)

    async def list_folders(self, owner: str) -> list[Folder]:
        rows = await self._pool.fetch(
            _FOLDER_SELECT + " WHERE f.owner=$1 ORDER BY lower(f.name), f.folder_id", owner
        )
        return [Folder.from_row(r) for r in rows]

    async def file_artifact(
        self,
        owner: str,
        folder_id: str,
        artifact_id: str,
        *,
        source: FolderSource = "manual",
        member_id: str | None = None,
        member_email: str = "",
        member_groups: frozenset[str] = frozenset(),
    ) -> None:
        """File an artifact the member can see in their feed (own, own pending, assigned by ACL or lab share, opened
        through a live link): the feed-visibility rule, so any row the UI offers can be filed.  Anything else is the
        same ``not_found`` as a missing artifact."""
        async with self._pool.acquire() as conn:
            await self._require_folder(conn, owner, folder_id)
            visible = await conn.fetchval(
                f"WITH c AS ({_CANDIDATES_SQL}) SELECT 1 FROM c WHERE c.artifact_id=$5 "
                "AND c.status IN ('active','pending') LIMIT 1",
                owner,
                member_id or owner.removeprefix("member:"),
                (member_email or "").lower(),
                sorted(member_groups),
                artifact_id,
            )
            if visible is None:
                raise LibraryError(*NOT_FOUND)
            await conn.execute(
                "INSERT INTO artifact_library_items (folder_id, artifact_id, source) VALUES ($1,$2,$3) "
                "ON CONFLICT (folder_id, artifact_id) DO NOTHING",
                folder_id,
                artifact_id,
                source,
            )

    async def unfile_artifact(self, owner: str, folder_id: str, artifact_id: str) -> None:
        async with self._pool.acquire() as conn:
            await self._require_folder(conn, owner, folder_id)
            await conn.execute(
                "DELETE FROM artifact_library_items WHERE folder_id=$1 AND artifact_id=$2",
                folder_id,
                artifact_id,
            )

    async def _folder(self, owner: str, folder_id: str) -> Folder:
        row = await self._pool.fetchrow(
            _FOLDER_SELECT + " WHERE f.folder_id=$1 AND f.owner=$2", folder_id, owner
        )
        if row is None:
            raise LibraryError(*NOT_FOUND)
        return Folder.from_row(row)

    @staticmethod
    async def _require_folder(conn: asyncpg.Connection, owner: str, folder_id: str) -> None:
        if (
            await conn.fetchval(
                "SELECT 1 FROM artifact_library_folders WHERE folder_id=$1 AND owner=$2",
                folder_id,
                owner,
            )
            is None
        ):
            raise LibraryError(*NOT_FOUND)

    async def record_claim(self, member_id: str, share_id: str) -> None:
        """Keep a received share in the member's library.  Only a currently live share can be claimed; title and
        sharer are snapshotted by the first claim and kept by later ones."""
        live = await self._pool.fetchval(
            "SELECT 1 FROM artifact_shares s JOIN artifacts a ON a.artifact_id=s.artifact_id "
            "WHERE s.share_id=$1 AND s.state='live' AND (s.expires_at IS NULL OR s.expires_at > now()) "
            "AND a.lifecycle NOT IN ('retracted','tombstoned') AND a.intake='accepted'",
            share_id,
        )
        if live is None:
            raise LibraryError(*NOT_FOUND)
        await self._pool.execute(
            "INSERT INTO artifact_share_claims (member_id, share_id, title, sharer) "
            "SELECT $1, s.share_id, coalesce(nullif(s.title,''), nullif(a.display_name,''), "
            "nullif(a.original_filename,''), a.artifact_id), s.created_by "
            "FROM artifact_shares s JOIN artifacts a ON a.artifact_id=s.artifact_id WHERE s.share_id=$2 "
            "ON CONFLICT (member_id, share_id) DO NOTHING",
            member_id,
            share_id,
        )

    async def feed(
        self,
        member_id: str,
        member_email: str,
        member_groups: frozenset[str],
        *,
        q: str | None = None,
        sources: frozenset[Source] | None = None,
        folder_id: str | None = None,
        artifact_type: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
        principal: str | None = None,
    ) -> FeedPage:
        """Everything the member can see, one row per artifact (badge priority mine > assigned > opened),
        newest first.  Visibility, search and filters apply before LIMIT; ``total`` counts the filtered set.
        ``principal`` overrides ``member:<member_id>`` for a viewer that is not a member (the dashboard's API key)."""
        limit = max(1, min(int(limit), MAX_PAGE))
        offset = max(0, int(offset))
        wanted = sorted(DEFAULT_SOURCES if sources is None else sources)
        args: list[object] = [
            principal or f"member:{member_id}",
            member_id,
            (member_email or "").lower(),
            sorted(member_groups),
            wanted,
        ]
        where = ["c.source = ANY($5::text[])"]

        def arg(value: object) -> str:
            args.append(value)
            return f"${len(args)}"

        outer = []
        if q:
            if len(q) > MAX_QUERY:
                raise LibraryError(400, "query_too_long")
            p = arg(_like(q))
            esc = f"ILIKE {p} ESCAPE '\\'"
            # Filename only for rows whose artifact is still readable (revoked/expired rows blank the type too);
            # folder names only from the caller's own folders ($1), never another member's.
            outer.append(
                f"(d.title {esc} OR d.sharer {esc} OR d.artifact_type {esc}"
                f" OR (d.status IN ('active','pending') AND EXISTS (SELECT 1 FROM artifacts qa"
                f" WHERE qa.artifact_id=d.artifact_id AND qa.original_filename {esc}))"
                " OR EXISTS (SELECT 1 FROM artifact_library_items qi JOIN artifact_library_folders qf"
                " ON qf.folder_id=qi.folder_id WHERE qi.artifact_id=d.artifact_id AND qf.owner=$1"
                f" AND qf.name {esc}))"
            )
        if artifact_type:
            outer.append(f"d.artifact_type = {arg(artifact_type)}")
        if since:
            outer.append(f"d.shared_at >= {arg(since)}")
        if until:
            outer.append(f"d.shared_at < {arg(until)}")
        if folder_id:
            outer.append(
                "EXISTS (SELECT 1 FROM artifact_library_items i JOIN artifact_library_folders f "
                f"ON f.folder_id=i.folder_id WHERE i.artifact_id=d.artifact_id AND f.owner=$1 "
                f"AND f.folder_id={arg(folder_id)})"
            )
        filtered = (
            f"WITH c AS ({_CANDIDATES_SQL}), "
            "d AS (SELECT DISTINCT ON (c.artifact_id) c.* FROM c WHERE "
            + " AND ".join(where)
            + " ORDER BY c.artifact_id, c.prio, c.shared_at DESC), "
            "f AS (SELECT d.* FROM d" + (" WHERE " + " AND ".join(outer) if outer else "") + ")"
        )
        total = await self._pool.fetchval(filtered + " SELECT count(*) FROM f", *args)
        lim, off = arg(limit), arg(offset)
        rows = await self._pool.fetch(
            filtered
            + ", p AS (SELECT * FROM f ORDER BY shared_at DESC, artifact_id LIMIT "
            + lim
            + " OFFSET "
            + off
            + ") "
            "SELECT p.*, coalesce((SELECT array_agg(i.folder_id ORDER BY i.folder_id) "
            "FROM artifact_library_items i JOIN artifact_library_folders fo ON fo.folder_id=i.folder_id "
            "WHERE i.artifact_id=p.artifact_id AND fo.owner=$1), '{}') AS folder_ids "
            "FROM p ORDER BY p.shared_at DESC, p.artifact_id",
            *args,
        )
        items = [
            FeedItem(
                r["artifact_id"],
                r["title"],
                r["sharer"],
                r["shared_at"],
                r["source"],
                r["status"],
                r["expires_at"],
                r["share_id"],
                tuple(r["folder_ids"]),
                r["artifact_type"],
                int(r["size_bytes"]),
            )
            for r in rows
        ]
        nxt = offset + len(items)
        return FeedPage(items, int(total), nxt if nxt < total else None)

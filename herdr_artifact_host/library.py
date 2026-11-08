"""Artifact library: per-member folders, filing, claims, pending intake and the unified feed (host schema).

``owner`` / member id is the bare member id used for ``artifacts.owner``, ``library_folders.owner``,
``artifact_acl.principal_id`` and ``share_claims.member_id``.  Anything the caller may not see (someone else's
folder, an unreadable or missing artifact) is the same ``LibraryError(404, "not_found")``.  Every query is
parameterized; the only SQL text built at runtime is placeholder numbering (``$n``), never user input.

Identity convention (Tasks 5/6 and the importer must follow it): a member is always the BARE member id, never a
``member:<id>`` principal.  That id is what goes in ``artifacts.owner``, ``library_folders.owner``,
``artifact_acl.principal_id``, ``artifact_acl.granted_by``, ``share_claims.member_id`` and ``shares.created_by`` for
member-created shares; "sharer" in the feed is whatever those columns hold.

Writes take a ``Viewer`` and refuse ``OPERATOR_READ_ALL`` (403 ``read_only``) and a blank member id (400).

The pending cap is NOT atomic: ``ensure_pending_capacity`` only counts.  Task 6 must run the count and the insert
of a new pending artifact in ONE transaction holding ``pg_advisory_xact_lock(LOCK_NS_PENDING_CLAIM, hashtext(owner))`` (the
two-argument form; ``LOCK_NS_PENDING_CLAIM`` lives in ``db.py`` beside the other namespaces), or concurrent
pushes can exceed ``PENDING_CAP``.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

import asyncpg


class FolderSource(StrEnum):
    MANUAL = "manual"
    SUGGESTED = "suggested"
    AUTO = "auto"


class Source(StrEnum):
    MINE = "mine"
    ASSIGNED = "assigned"
    OPENED = "opened"
    PENDING = "pending"


class ItemStatus(StrEnum):
    ACTIVE = "active"
    EXPIRED = "expired"
    REVOKED = "revoked"
    PENDING = "pending"


DEFAULT_SOURCES: frozenset[Source] = frozenset(
    {Source.MINE, Source.ASSIGNED, Source.OPENED}
)  # pending is opt-in
MAX_PAGE = 200
MAX_QUERY = 200

MAX_FOLDER_NAME = 80
MAX_FOLDERS = 200  # per member
MAX_FOLDER_ITEMS = 2000  # per folder

PENDING_CAP = 25
PENDING_CAP_STATUS = 429
PENDING_CAP_CODE = "pending_cap"
PENDING_CAP_MESSAGE = f"You have {PENDING_CAP} pending artifacts; accept or discard some in your library before pushing more."

NOT_FOUND = (404, "not_found")

# Same visibility as the feed: the owner sees their accepted or pending artifact; a grantee (unexpired ACL
# read/admin) only an accepted one.  Retracted, tombstoned and discarded never.  $1 = member id, $2 = artifact id.
READABLE_SQL = (
    "SELECT 1 FROM artifacts a WHERE a.artifact_id=$2 AND a.lifecycle NOT IN ('retracted','tombstoned') AND "
    "((a.owner=$1 AND a.intake IN ('accepted','pending')) OR (a.intake='accepted' AND EXISTS ("
    "SELECT 1 FROM artifact_acl acl "
    "WHERE acl.artifact_id=a.artifact_id AND acl.principal_id=$1 "
    "AND (acl.expires_at IS NULL OR acl.expires_at > now()) "
    "AND acl.permissions && ARRAY['read','admin'])))"
)

_FOLDER_SELECT = (
    "SELECT f.folder_id, f.owner, f.name, "
    "(SELECT count(*) FROM library_items i WHERE i.folder_id=f.folder_id) AS item_count "
    "FROM library_folders f"
)


class LibraryError(Exception):
    def __init__(self, status: int, code: str, message: str | None = None) -> None:
        super().__init__(message or code)
        self.status = status
        self.code = code
        self.message = message or code


class PendingCapExceeded(LibraryError):
    def __init__(self) -> None:
        super().__init__(PENDING_CAP_STATUS, PENDING_CAP_CODE, PENDING_CAP_MESSAGE)


@dataclass(frozen=True)
class Viewer:
    """Who is reading.  A member sees their own feed; ``OPERATOR_READ_ALL`` sees every member's items, read-only."""

    member_id: str
    email: str = ""
    groups: frozenset[str] = frozenset()
    read_all: bool = False


OPERATOR_READ_ALL = Viewer(member_id="", read_all=True)


def _member(viewer: Viewer) -> Viewer:
    """Writes and per-member state need a real member; the operator is read-only."""
    if viewer.read_all:
        raise LibraryError(403, "read_only", "this viewer is read-only")
    if not viewer.member_id.strip():
        raise LibraryError(400, "invalid_member", "a member id is required")
    return viewer


@dataclass(frozen=True)
class ItemRef:
    """A pending item addressed by artifact id (the owner is the acting viewer)."""

    artifact_id: str


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


@dataclass(frozen=True)
class FeedQuery:
    q: str | None = None
    sources: frozenset[Source] = DEFAULT_SOURCES
    folder_id: str | None = None
    artifact_type: str | None = None
    since: datetime | None = None
    until: datetime | None = None
    limit: int = 50
    offset: int = 0


def _like(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


_LIVE = "a.lifecycle NOT IN ('retracted','tombstoned')"
_TITLE = "coalesce(nullif(a.display_name,''), nullif(a.original_filename,''), a.artifact_id)"
_REVOKED_OR_EXPIRED = "s.state='revoked' OR (s.expires_at IS NOT NULL AND s.expires_at <= now())"

# $1 = member id, $2 = lowercased email, $3 = groups.  Every candidate carries its badge priority (lower wins);
# DISTINCT ON keeps one row per artifact.  Retracted and tombstoned artifacts never appear.
_MEMBER_CANDIDATES_SQL = f"""
SELECT a.artifact_id, {_TITLE} AS title, a.owner AS sharer, a.created_at AS shared_at, 'pending' AS source,
       0 AS prio, 'pending' AS status, NULL::timestamptz AS expires_at, NULL::text AS share_id,
       a.artifact_type, a.size_bytes
  FROM artifacts a WHERE a.owner=$1 AND a.intake='pending' AND {_LIVE}
UNION ALL
SELECT a.artifact_id, {_TITLE}, a.owner, a.created_at, 'mine', 1, 'active', NULL, NULL,
       a.artifact_type, a.size_bytes
  FROM artifacts a WHERE a.owner=$1 AND a.intake='accepted' AND {_LIVE}
UNION ALL
SELECT a.artifact_id, {_TITLE}, acl.granted_by, acl.created_at, 'assigned', 2, 'active', acl.expires_at, NULL,
       a.artifact_type, a.size_bytes
  FROM artifact_acl acl JOIN artifacts a ON a.artifact_id=acl.artifact_id
 WHERE acl.principal_id=$1 AND (acl.expires_at IS NULL OR acl.expires_at > now())
   AND acl.permissions && ARRAY['read','admin']
   AND a.intake='accepted' AND {_LIVE}
UNION ALL
SELECT a.artifact_id, coalesce(nullif(s.title,''), {_TITLE}), coalesce(s.created_by,''),
       coalesce(s.approved_at, s.created_at), 'assigned', 2, 'active', s.expires_at, s.share_id,
       a.artifact_type, a.size_bytes
  FROM shares s JOIN artifacts a ON a.artifact_id=s.artifact_id
 WHERE s.state='live' AND s.audience='lab' AND (s.expires_at IS NULL OR s.expires_at > now())
   AND (($2::text<>'' AND EXISTS (SELECT 1 FROM unnest(s.allow_emails) e WHERE lower(e)=$2)) OR s.allow_groups && $3::text[])
   AND a.intake='accepted' AND {_LIVE}
UNION ALL
SELECT a.artifact_id, coalesce(nullif(c.title,''), nullif(s.title,''), {_TITLE}), coalesce(c.sharer, s.created_by, ''),
       c.claimed_at, 'opened', 3,
       CASE WHEN s.state='revoked' THEN 'revoked'
            WHEN s.expires_at IS NOT NULL AND s.expires_at <= now() THEN 'expired' ELSE 'active' END,
       s.expires_at, s.share_id,
       CASE WHEN {_REVOKED_OR_EXPIRED} THEN '' ELSE a.artifact_type END,
       CASE WHEN {_REVOKED_OR_EXPIRED} THEN 0 ELSE a.size_bytes END
  FROM share_claims c JOIN shares s ON s.share_id=c.share_id
  JOIN artifacts a ON a.artifact_id=s.artifact_id
 WHERE c.member_id=$1 AND s.audience='public' AND {_LIVE} AND a.intake='accepted'
"""

# The read-only operator sees every member's artifacts once, badged by intake (no per-member parameters).
_ALL_CANDIDATES_SQL = f"""
SELECT a.artifact_id, {_TITLE} AS title, a.owner AS sharer, a.created_at AS shared_at,
       CASE WHEN a.intake='pending' THEN 'pending' ELSE 'mine' END AS source,
       CASE WHEN a.intake='pending' THEN 0 ELSE 1 END AS prio,
       CASE WHEN a.intake='pending' THEN 'pending' ELSE 'active' END AS status,
       NULL::timestamptz AS expires_at, NULL::text AS share_id, a.artifact_type, a.size_bytes
  FROM artifacts a WHERE a.intake IN ('pending','accepted') AND {_LIVE}
"""

# A pending artifact counts until it is accepted, discarded, retracted or tombstoned.
_PENDING_LIVE = "owner=$1 AND intake='pending' AND lifecycle NOT IN ('retracted','tombstoned')"


class LibraryStore:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def close(self) -> None:
        await self._pool.close()

    # --- pending intake -------------------------------------------------------------------------------------

    async def count_pending(self, owner: str) -> int:
        return int(
            await self._pool.fetchval(
                f"SELECT count(*) FROM artifacts WHERE {_PENDING_LIVE}", owner
            )
        )

    async def ensure_pending_capacity(self, owner: str) -> None:
        """Raise ``PendingCapExceeded`` when ``owner`` already holds ``PENDING_CAP`` pending artifacts."""
        if await self.count_pending(owner) >= PENDING_CAP:
            raise PendingCapExceeded

    async def accept(self, viewer: Viewer, item: ItemRef) -> None:
        owner = _member(viewer).member_id
        done = await self._pool.execute(
            f"UPDATE artifacts SET intake='accepted', updated_at=now() WHERE artifact_id=$2 AND {_PENDING_LIVE}",
            owner,
            item.artifact_id,
        )
        if done != "UPDATE 1":
            raise LibraryError(*NOT_FOUND)

    async def discard(self, viewer: Viewer, item: ItemRef) -> None:
        """Discard the viewer's own pending artifact: mark it discarded, retract it and revoke every share, in one
        transaction (the control plane's member-delete effect)."""
        owner = _member(viewer).member_id
        async with self._pool.acquire() as conn, conn.transaction():
            done = await conn.execute(
                "UPDATE artifacts SET intake='discarded', lifecycle='retracted', updated_at=now() "
                f"WHERE artifact_id=$2 AND {_PENDING_LIVE}",
                owner,
                item.artifact_id,
            )
            if done != "UPDATE 1":
                raise LibraryError(*NOT_FOUND)
            await conn.execute(
                "WITH r AS (UPDATE shares SET state='revoked', revoked_at=now() "
                "WHERE artifact_id=$2 AND state<>'revoked' RETURNING share_id) "
                "INSERT INTO share_events (share_id, kind, actor) SELECT share_id, 'revoked', $1 FROM r",
                owner,
                item.artifact_id,
            )

    async def can_read(self, member_id: str, artifact_id: str) -> bool:
        return await self._pool.fetchval(READABLE_SQL, member_id, artifact_id) is not None

    # --- folders --------------------------------------------------------------------------------------------

    async def create_folder(self, viewer: Viewer, name: str) -> Folder:
        owner = _member(viewer).member_id
        cleaned = _clean_name(name)
        folder_id = "fld_" + secrets.token_hex(12)
        if (
            int(
                await self._pool.fetchval(
                    "SELECT count(*) FROM library_folders WHERE owner=$1", owner
                )
            )
            >= MAX_FOLDERS
        ):
            raise LibraryError(
                429,
                "folder_cap",
                f"You have {MAX_FOLDERS} folders; delete one before creating another.",
            )
        try:
            await self._pool.execute(
                "INSERT INTO library_folders (folder_id, owner, name) VALUES ($1,$2,$3)",
                folder_id,
                owner,
                cleaned,
            )
        except asyncpg.UniqueViolationError:
            raise LibraryError(409, "duplicate_name") from None
        return Folder(folder_id, owner, cleaned, 0)

    async def rename_folder(self, viewer: Viewer, folder_id: str, name: str) -> Folder:
        owner = _member(viewer).member_id
        cleaned = _clean_name(name)
        try:
            updated = await self._pool.fetchval(
                "UPDATE library_folders SET name=$3 WHERE folder_id=$1 AND owner=$2 RETURNING folder_id",
                folder_id,
                owner,
                cleaned,
            )
        except asyncpg.UniqueViolationError:
            raise LibraryError(409, "duplicate_name") from None
        if updated is None:
            raise LibraryError(*NOT_FOUND)
        return await self._folder(owner, folder_id)

    async def delete_folder(self, viewer: Viewer, folder_id: str) -> None:
        owner = _member(viewer).member_id
        deleted = await self._pool.fetchval(
            "DELETE FROM library_folders WHERE folder_id=$1 AND owner=$2 RETURNING folder_id",
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

    async def _folder(self, owner: str, folder_id: str) -> Folder:
        row = await self._pool.fetchrow(
            _FOLDER_SELECT + " WHERE f.folder_id=$1 AND f.owner=$2", folder_id, owner
        )
        if row is None:
            raise LibraryError(*NOT_FOUND)
        return Folder.from_row(row)

    async def file_artifact(
        self,
        viewer: Viewer,
        folder_id: str,
        artifact_id: str,
        *,
        source: FolderSource = FolderSource.MANUAL,
    ) -> None:
        """File an artifact the member can see in their feed (own, own pending, assigned by ACL or lab share, opened
        through a live link): the feed-visibility rule, so any row the UI offers can be filed.  Anything else is the
        same ``not_found`` as a missing artifact."""
        member = _member(viewer)
        async with self._pool.acquire() as conn:
            owns = await conn.fetchval(
                "SELECT 1 FROM library_folders WHERE folder_id=$1 AND owner=$2",
                folder_id,
                member.member_id,
            )
            if owns is None:
                raise LibraryError(*NOT_FOUND)
            visible = await conn.fetchval(
                f"WITH c AS ({_MEMBER_CANDIDATES_SQL}) SELECT 1 FROM c WHERE c.artifact_id=$4 "
                "AND c.status IN ('active','pending') LIMIT 1",
                member.member_id,
                member.email.lower(),
                sorted(member.groups),
                artifact_id,
            )
            if visible is None:
                raise LibraryError(*NOT_FOUND)
            already = await conn.fetchval(
                "SELECT 1 FROM library_items WHERE folder_id=$1 AND artifact_id=$2",
                folder_id,
                artifact_id,
            )
            if (
                already is None
                and int(
                    await conn.fetchval(
                        "SELECT count(*) FROM library_items WHERE folder_id=$1", folder_id
                    )
                )
                >= MAX_FOLDER_ITEMS
            ):
                raise LibraryError(
                    429,
                    "folder_items_cap",
                    f"A folder holds at most {MAX_FOLDER_ITEMS} items; use another folder.",
                )
            await conn.execute(
                "INSERT INTO library_items (folder_id, artifact_id, source) VALUES ($1,$2,$3) "
                "ON CONFLICT (folder_id, artifact_id) DO NOTHING",
                folder_id,
                artifact_id,
                source.value,
            )

    async def unfile_artifact(self, viewer: Viewer, folder_id: str, artifact_id: str) -> None:
        owner = _member(viewer).member_id
        await self._folder(owner, folder_id)
        await self._pool.execute(
            "DELETE FROM library_items WHERE folder_id=$1 AND artifact_id=$2",
            folder_id,
            artifact_id,
        )

    # --- claims ---------------------------------------------------------------------------------------------

    async def record_claim(self, viewer: Viewer, share_id: str) -> None:
        """Keep a received share in the member's library.  Only a currently live public share can be claimed (a lab
        share is decided by its allow-list, never kept); title and sharer are snapshotted by the first claim."""
        member_id = _member(viewer).member_id
        live = await self._pool.fetchval(
            "SELECT 1 FROM shares s JOIN artifacts a ON a.artifact_id=s.artifact_id "
            "WHERE s.share_id=$1 AND s.state='live' AND s.audience='public' "
            "AND (s.expires_at IS NULL OR s.expires_at > now()) "
            "AND a.lifecycle NOT IN ('retracted','tombstoned') AND a.intake='accepted'",
            share_id,
        )
        if live is None:
            raise LibraryError(*NOT_FOUND)
        await self._pool.execute(
            "INSERT INTO share_claims (member_id, share_id, title, sharer) "
            "SELECT $1, s.share_id, coalesce(nullif(s.title,''), nullif(a.display_name,''), "
            "nullif(a.original_filename,''), a.artifact_id), s.created_by "
            "FROM shares s JOIN artifacts a ON a.artifact_id=s.artifact_id WHERE s.share_id=$2 "
            "ON CONFLICT (member_id, share_id) DO NOTHING",
            member_id,
            share_id,
        )

    # --- feed -----------------------------------------------------------------------------------------------

    async def feed(self, viewer: Viewer, query: FeedQuery) -> FeedPage:
        """Everything the viewer can see, one row per artifact (badge priority mine > assigned > opened), newest
        first.  Visibility, search and filters apply before LIMIT; ``total`` counts the filtered set.  Read-only."""
        if not viewer.read_all:
            _member(viewer)
        limit = max(1, min(int(query.limit), MAX_PAGE))
        offset = max(0, int(query.offset))
        if query.q and len(query.q) > MAX_QUERY:
            raise LibraryError(400, "query_too_long")
        args: list[object] = [viewer.member_id]  # $1: also the folder owner
        if viewer.read_all:
            candidates = _ALL_CANDIDATES_SQL
        else:
            candidates = _MEMBER_CANDIDATES_SQL
            args += [viewer.email.lower(), sorted(viewer.groups)]

        def arg(value: object) -> str:
            args.append(value)
            return f"${len(args)}"

        sources = arg(sorted(s.value for s in query.sources))
        outer: list[str] = []
        if query.q:
            esc = f"ILIKE {arg(_like(query.q))} ESCAPE '\\'"
            # d.title carries share/claim titles; the artifact's own search_text (trigram-indexed: name, filename,
            # type, owner) only for rows still readable, so a revoked/expired row never leaks its filename; folder
            # names only from the viewer's own folders ($1), never another member's.
            outer.append(
                f"(d.title {esc} OR d.sharer {esc} OR d.artifact_type {esc}"
                f" OR (d.status IN ('active','pending') AND EXISTS (SELECT 1 FROM artifacts qa"
                f" WHERE qa.artifact_id=d.artifact_id AND qa.search_text {esc}))"
                " OR EXISTS (SELECT 1 FROM library_items qi JOIN library_folders qf"
                " ON qf.folder_id=qi.folder_id WHERE qi.artifact_id=d.artifact_id AND qf.owner=$1"
                f" AND qf.name {esc}))"
            )
        if query.artifact_type:
            outer.append(f"d.artifact_type = {arg(query.artifact_type)}")
        if query.since:
            outer.append(f"d.shared_at >= {arg(query.since)}")
        if query.until:
            outer.append(f"d.shared_at < {arg(query.until)}")
        if query.folder_id:
            outer.append(
                "EXISTS (SELECT 1 FROM library_items i JOIN library_folders f ON f.folder_id=i.folder_id "
                f"WHERE i.artifact_id=d.artifact_id AND f.owner=$1 AND f.folder_id={arg(query.folder_id)})"
            )
        filtered = (
            f"WITH c AS ({candidates}), "
            f"d AS (SELECT DISTINCT ON (c.artifact_id) c.* FROM c WHERE $1::text IS NOT NULL AND c.source = ANY({sources}::text[]) "
            "ORDER BY c.artifact_id, c.prio, c.shared_at DESC), "
            "f AS (SELECT d.* FROM d" + (" WHERE " + " AND ".join(outer) if outer else "") + ")"
        )
        total = int(await self._pool.fetchval(filtered + " SELECT count(*) FROM f", *args))
        lim, off = arg(limit), arg(offset)
        rows = await self._pool.fetch(
            filtered
            + f", p AS (SELECT * FROM f ORDER BY shared_at DESC, artifact_id LIMIT {lim} OFFSET {off}) "
            "SELECT p.*, coalesce((SELECT array_agg(i.folder_id ORDER BY i.folder_id) "
            "FROM library_items i JOIN library_folders fo ON fo.folder_id=i.folder_id "
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
                Source(r["source"]),
                ItemStatus(r["status"]),
                r["expires_at"],
                r["share_id"],
                tuple(r["folder_ids"]),
                r["artifact_type"],
                int(r["size_bytes"]),
            )
            for r in rows
        ]
        nxt = offset + len(items)
        return FeedPage(items, total, nxt if nxt < total else None)

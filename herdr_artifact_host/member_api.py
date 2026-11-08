"""The signed-in member surface of the artifact host: the page (``/``, ``/my/``, ``/my/callback``) and ``/api/member/*``.

Ported from the control plane's ``member_web.py``; the view-file and worker-intent indirection is gone.  Handlers call
``LibraryStore``, ``BlobStore`` and the database directly (``MemberService``), and events are written to ``share_events``
in the same transaction as the change they describe.

Contract carried over:

* Served only for the configured public Host (the surface checks it first); anything unconfigured is the uniform 404.
* The session is ``Authorization: Member <session>`` (``member_session``: its own domain tag, never a cookie, so there is
  no ambient credential for a foreign site to ride on).  A claim cookie, a lab session, a write cookie, a Bearer agent
  token or anything else is the same 401 ``{"error": "unauthorized"}``.  The access is re-checked against the group
  directory on every request, and against the member's ``sessions_valid_after`` (set by ``POST /api/member/signout``,
  which ends every session issued before it).  The session exchange needs the page's ``X-Nonce`` to equal the ``nonce``
  claim of the ID token.  Without a ``members.json`` the exchange fails closed unless ``member_group_sync_required`` is
  explicitly False (the token's ``cognito:groups`` claim then decides, and the session lasts at most 15 minutes).
* Every non-GET needs exactly one ``Origin: <public origin>`` (403 otherwise, decided before authentication and before
  any body is read).  Error bodies are fixed strings; nothing about a token, a path or an exception reaches a response.
* Identity is the BARE member id (the Cognito user) in ``artifacts.owner``, ACL principals, ``granted_by``, claims and
  ``shares.created_by``.  Anything on an artifact the caller does not own is the same 404 as a missing artifact: no
  existence oracle.
* Uploads: chunk runs are staged on disk (``BlobStore.tmp_dir/up/<member>/<artifact>/<upload>``), at most
  ``MAX_UPLOAD_BYTES`` (17 MiB) per file and per run, refused from their headers (size, per-member and per-artifact
  staging caps, upload slots) before a body byte is read.  A body is streamed to disk under a deadline, never held in
  memory.  ``abort`` takes the same spec as ``status`` and re-derives the upload id.  Staged bytes go away with the
  artifact (delete, discard, publish) or ``MemberService.sweep_staging``.  Publish assembles and hash-verifies the file, writes the
  BlobStore, then writes every row in ONE transaction: a failure leaves no ``artifact_blobs`` / version rows and removes
  the blob files it wrote (a size and quota pre-check runs before any blob is written; per-sha advisory locks make the
  cleanup safe against a concurrent publish of the same content).
* Shares are created ``pending`` and made live only by the owner as a signed-in user (an agent, token or operator actor is
  refused).  Expiry defaults to 30 days and is at most 90.  The share token exists only in the creation response.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import ipaddress
import json
import logging
import os
import re
import secrets
import shutil
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, Final
from urllib.parse import parse_qs

import asyncpg
from aiohttp import web

from .blobs import BlobCorrupt, BlobMissing, BlobStore
from .cognito import CognitoAuthenticator, SyncedDirectory
from .db import LOCK_NS_BLOB
from .library import (
    PENDING_CAP,
    FeedQuery,
    ItemRef,
    LibraryError,
    LibraryStore,
    Source,
    Viewer,
    settle_quota,
)
from .media_type import safe_media_type
from .member_session import check_session, mint_session
from .share_bundle import ShareBundleError, choose_entry, content_type_for, normalise_name
from .share_http import RateLimiter
from .share_store import token_digest

log = logging.getLogger("herdr_artifact_host.member")

CHUNK_BYTES: Final = 512 * 1024
MAX_UPLOAD_BYTES: Final = 17 * 1024 * 1024  # per file, and the largest run a single PUT may carry
RUN_MAX_CHUNKS: Final = MAX_UPLOAD_BYTES // CHUNK_BYTES + 1
DEFAULT_SHARE_DAYS: Final = 30
MAX_SHARE_DAYS: Final = 90
MAX_JSON_BODY: Final = 256 * 1024
MAX_FILES: Final = 200
READ_BLOCK: Final = 64 * 1024
RUN_SLOTS: Final = 8  # concurrent run bodies being streamed to disk, all members together
MAX_ALLOW_ENTRIES: Final = 50
UPLOAD_ID_RE: Final = re.compile(r"upl_[0-9a-f]{24}")
SHA_RE: Final = re.compile(r"[0-9a-f]{64}")
_DIGITS: Final = re.compile(r"[0-9]{1,15}")
_ARTIFACT: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,127}")
_ID: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
_FOLDER: Final = re.compile(r"fld_[0-9a-f]{24}")
_PROFILE: Final = re.compile(r"[a-z][a-z0-9_-]{0,31}")
_IDEMPOTENCY: Final = re.compile(r"[\x21-\x7e]{8,128}")
_EMAIL: Final = re.compile(r"[\x21-\x7e]{3,254}")
_GROUP: Final = re.compile(r"[\x21-\x7e]{1,128}")
_PATH_OK: Final = re.compile(r"/[A-Za-z0-9_.~%/@+-]{0,1000}")
_INLINE_TYPES: Final = frozenset(
    {"text/plain", "image/png", "image/jpeg", "image/gif", "image/webp", "application/pdf"}
)
_JSON: Final = "application/json"
_HTML: Final = "text/html; charset=utf-8"
_JS: Final = "text/javascript; charset=utf-8"
_CSS: Final = "text/css; charset=utf-8"
_FONT: Final = "font/woff2"
_FONT_CACHE: Final = "public, max-age=86400"

_BODY_KEYS: Final[dict[str, tuple[frozenset[str], frozenset[str]]]] = {
    "create": (
        frozenset({"title", "filename", "profile", "idempotency_key"}),
        frozenset({"title", "filename"}),
    ),
    "delete": (frozenset({"confirm"}), frozenset({"confirm"})),
    "status": (  # also the body of an abort: the upload id is re-derived from it
        frozenset({"name", "size_bytes", "file_sha256"}),
        frozenset({"name", "size_bytes", "file_sha256"}),
    ),
    "publish": (
        frozenset({"base_version_id", "files", "message", "entry"}),
        frozenset({"files"}),
    ),
    "share_create": (
        frozenset(
            {
                "version_id",
                "audience",
                "allow_groups",
                "allow_emails",
                "expires_in_days",
                "entry",
                "title",
            }
        ),
        frozenset({"version_id", "audience"}),
    ),
    "approve": (frozenset({"confirm", "idempotency_key"}), frozenset({"confirm"})),
    "folder": (frozenset({"name"}), frozenset({"name"})),
    "folder_item": (frozenset({"artifact_id"}), frozenset({"artifact_id"})),
}


class ActorKind(StrEnum):
    USER = "user"  # the signed-in member at the web page
    AGENT = "agent"
    TOKEN = "token"
    OPERATOR = "operator"


@dataclass(frozen=True)
class Actor:
    """Who is acting for a member.  Only ``USER`` may approve a share."""

    kind: ActorKind
    member_id: str


class MemberError(Exception):
    """A finished refusal: only ``code`` and the fixed ``message`` ever reach a client."""

    def __init__(
        self, status: int, code: str, message: str = "", headers: dict[str, str] | None = None
    ):
        super().__init__(code)
        self.status, self.code, self.message = status, code, message
        self.headers = dict(headers or {})


def _bad(message: str = "bad request") -> MemberError:
    return MemberError(400, "bad_request", message)


def _not_found() -> MemberError:
    return MemberError(404, "not_found", "not found")


def _unauthorized() -> MemberError:
    return MemberError(401, "unauthorized", "sign in again")


@dataclass(frozen=True)
class MemberLimits:
    max_member_bytes: int = 1 << 30  # defaults, overridden per member by member_usage
    max_artifact_bytes: int = 64 * 1024 * 1024
    max_artifacts: int = 500
    writes_per_minute: int = 30
    chunks_per_minute: int = 1200
    share_creates_per_hour: int = 20
    session_exchanges_per_minute: int = 20  # failed exchanges per client address
    session_exchanges_global_per_minute: int = (
        600  # failed exchanges, all clients: a backstop, not the main limit
    )
    staging_bytes_per_member: int = 512 * 1024 * 1024
    staging_bytes_per_artifact: int = 128 * 1024 * 1024
    uploads_per_member: int = 2  # run bodies being received at once
    body_deadline_seconds: float = 120.0  # a run body that takes longer is dropped (408)
    content_streams: int = 8  # content responses being sent at once, all members together
    claim_fallback_session_seconds: int = 15 * 60


def now_dt(now: Callable[[], float]) -> datetime:
    return datetime.fromtimestamp(now(), UTC)


def iso(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(UTC).isoformat()


# ----------------------------------------------------------------------------- settings


@dataclass(frozen=True)
class MemberSettings:
    authenticator: CognitoAuthenticator
    secret: bytes = field(repr=False)
    session_seconds: int
    public_origin: str
    cognito_origin: str
    client_id: str
    group: str
    assets: dict[str, tuple[bytes, str]]
    page_csp: str
    limits: MemberLimits
    claim_fallback: bool = (
        False  # members.json optional: the token's group claim decides when it is absent
    )
    directory_present: Callable[[], bool] = lambda: True


def synced_lookup(directory: SyncedDirectory, required: bool) -> Callable[[str], list[str] | None]:
    """The group directory as the authenticator's lookup.  A stale sync always fails closed; a MISSING one fails closed
    only when ``required`` (a deployment that has no sync job relies on the token's group claim alone)."""

    def lookup(user: str) -> list[str] | None:
        try:
            return directory.groups(user)
        except FileNotFoundError:
            return None if required else [directory.group]

    return lookup


# ----------------------------------------------------------------------------- the service


class MemberService:
    """Everything a member may do, against the database and the blob store.  Every method takes the acting ``Actor``."""

    def __init__(
        self,
        pool: asyncpg.Pool,
        blobs: BlobStore,
        library: LibraryStore,
        limits: MemberLimits,
        public_origin: str | None,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.pool, self.blobs, self.library, self.limits = pool, blobs, library, limits
        self.public_origin, self.now = public_origin, now
        self.writes = RateLimiter(limits.writes_per_minute, now)
        self.chunk_rate = RateLimiter(limits.chunks_per_minute, now)
        self.share_rate_window = timedelta(hours=1)
        self._run_slots = asyncio.Semaphore(RUN_SLOTS)
        self._uploading: dict[str, int] = {}  # run bodies being received, per member
        self.content_streams = asyncio.Semaphore(limits.content_streams)
        self._staging = blobs.tmp_dir / "up"

    # -- identity

    @staticmethod
    def _member(actor: Actor) -> str:
        member = actor.member_id
        if not isinstance(member, str) or not member.strip():
            raise MemberError(403, "forbidden", "forbidden")
        return member

    async def record_sign_in(
        self, member_id: str, email: str, groups: list[str], vouched_until: float
    ) -> None:
        """Remember what sign-in verified.  The email and groups are used only until ``vouched_until`` (the end of the
        session that vouched for them), so a removed member's stale lab visibility ends with their session."""
        await self.pool.execute(
            "INSERT INTO member_usage (member_id, email, groups, last_used_at, identity_expires_at) "
            "VALUES ($1,$2,$3,now(),$4) ON CONFLICT (member_id) DO UPDATE SET email=EXCLUDED.email, "
            "groups=EXCLUDED.groups, identity_expires_at=EXCLUDED.identity_expires_at, "
            "last_used_at=now(), updated_at=now()",
            member_id,
            email,
            groups,
            now_dt(lambda: vouched_until),
        )

    def _vouched(self, row: asyncpg.Record | None) -> bool:
        return (
            row is not None
            and row["identity_expires_at"] is not None
            and row["identity_expires_at"] > now_dt(self.now)
        )

    async def sessions_valid_after(self, member: str) -> int | None:
        """Epoch milliseconds before which this member's sessions are void (sign-out), or None."""
        got = await self.pool.fetchval(
            "SELECT sessions_valid_after FROM member_usage WHERE member_id=$1", member
        )
        return None if got is None else round(got.timestamp() * 1000)

    async def signout(self, actor: Actor) -> dict[str, Any]:
        """End every session issued to this member so far (a bearer session cannot be recalled any other way)."""
        member = self._member(actor)
        stamp = int(self.now() * 1000)
        async with self.pool.acquire() as conn, conn.transaction():
            await self._lock_usage(conn, member)
            await conn.execute(
                "UPDATE member_usage SET sessions_valid_after=$2, updated_at=now() WHERE member_id=$1",
                member,
                now_dt(lambda: stamp / 1000),
            )
        await self._audit(member, "signout", "ok")
        return {"signed_out": True}

    async def viewer(self, actor: Actor) -> Viewer:
        """The caller's own feed identity, from what sign-in verified (never from request input).  The operator's
        read-all viewer is never built here."""
        member = self._member(actor)
        row = await self.pool.fetchrow(
            "SELECT email, groups, identity_expires_at FROM member_usage WHERE member_id=$1", member
        )
        vouched = self._vouched(row)
        email = (row["email"] or "") if row and vouched else ""
        groups = frozenset(row["groups"]) if row and vouched else frozenset()
        return Viewer(member_id=member, email=email, groups=groups)

    async def _audit(
        self,
        member: str,
        action: str,
        outcome: str,
        code: str | None = None,
        share_id: str | None = None,
        audience: str | None = None,
    ) -> None:
        try:
            await self.pool.execute(
                "INSERT INTO artifact_member_events (member_id, action, outcome, code, share_id, audience) "
                "VALUES ($1,$2,$3,$4,$5,$6)",
                member,
                action,
                outcome,
                code,
                share_id,
                audience,
            )
        except Exception:
            log.warning("member audit write failed")

    def _throttle_write(self, member: str) -> None:
        if not self.writes.allow(member):
            raise MemberError(
                429, "rate_limited", "too many changes, retry shortly", {"Retry-After": "10"}
            )

    # -- reads

    async def me(self, actor: Actor) -> dict[str, Any]:
        member = self._member(actor)
        row = await self.pool.fetchrow(
            "SELECT bytes_used, bytes_retracted, max_member_bytes, max_artifact_bytes, max_artifacts, "
            "sharing_disabled, email, identity_expires_at FROM member_usage WHERE member_id=$1",
            member,
        )
        used = int(row["bytes_used"]) if row else 0
        return {
            "member_id": member,
            "email": (row["email"] if self._vouched(row) else None) or "",
            "usage": {
                "bytes_used": used,
                "max_member_bytes": int(
                    (row and row["max_member_bytes"]) or self.limits.max_member_bytes
                ),
                "max_artifact_bytes": int(
                    (row and row["max_artifact_bytes"]) or self.limits.max_artifact_bytes
                ),
                "max_artifacts": int((row and row["max_artifacts"]) or self.limits.max_artifacts),
                "pending_cap": PENDING_CAP,
            },
            "sharing_enabled": not (row and row["sharing_disabled"]),
        }

    async def library_page(self, actor: Actor, params: dict[str, str]) -> dict[str, Any]:
        viewer = await self.viewer(actor)
        sources: frozenset[Source] | None = None
        if "sources" in params:
            try:
                sources = frozenset(Source(s) for s in params["sources"].split(",") if s)
            except ValueError:
                raise _bad() from None
        base = FeedQuery()
        try:
            query = FeedQuery(
                q=params.get("q") or None,
                sources=sources if sources else base.sources,
                folder_id=params.get("folder") or None,
                artifact_type=params.get("type") or None,
                limit=int(params.get("limit", base.limit)),
                offset=int(params.get("offset", 0)),
            )
        except ValueError:
            raise _bad() from None
        page = await self.library.feed(viewer, query)
        folders = await self.library.list_folders(viewer.member_id)
        return {
            "items": [
                {
                    "artifact_id": i.artifact_id,
                    "title": i.title,
                    "sharer": i.sharer,
                    "shared_at": iso(i.shared_at),
                    "source": i.source.value,
                    "status": i.status.value,
                    "expires_at": iso(i.expires_at),
                    "folder_ids": list(i.folder_ids),
                    "artifact_type": i.artifact_type,
                    "size_bytes": i.size_bytes,
                }
                for i in page.items
            ],
            "total": page.total,
            "next_offset": page.next_offset,
            "folders": [
                {"folder_id": f.folder_id, "name": f.name, "item_count": f.item_count}
                for f in folders
            ],
        }

    async def _readable(self, member: str, artifact_id: str) -> None:
        if not await self.library.can_read(member, artifact_id):
            raise _not_found()

    async def head(self, actor: Actor, artifact_id: str) -> dict[str, Any]:
        member = self._member(actor)
        await self._readable(member, artifact_id)
        row = await self.pool.fetchrow(
            "SELECT a.artifact_id, a.owner, a.display_name, a.original_filename, a.artifact_type, a.size_bytes, "
            "a.intake, a.lifecycle, a.current_version_id, a.created_at, a.updated_at, "
            "v.version_number, v.output_manifest::text AS manifest "
            "FROM artifacts a LEFT JOIN artifact_versions v ON v.version_id=a.current_version_id "
            "AND v.artifact_id=a.artifact_id WHERE a.artifact_id=$1",
            artifact_id,
        )
        if row is None:
            raise _not_found()
        manifest = json.loads(row["manifest"]) if row["manifest"] else []
        return {
            "artifact_id": row["artifact_id"],
            "owned": row["owner"] == member,
            "title": row["display_name"] or row["artifact_id"],
            "filename": row["original_filename"],
            "artifact_type": row["artifact_type"],
            "size_bytes": int(row["size_bytes"]),
            "intake": row["intake"],
            "lifecycle": row["lifecycle"],
            "current_version_id": row["current_version_id"],
            "version_number": row["version_number"],
            "files": [
                {"name": f["name"], "size_bytes": f["size_bytes"], "sha256": f["sha256"]}
                for f in manifest
            ],
            "created_at": iso(row["created_at"]),
            "updated_at": iso(row["updated_at"]),
        }

    async def history(self, actor: Actor, artifact_id: str, limit: int) -> dict[str, Any]:
        member = self._member(actor)
        await self._readable(member, artifact_id)
        rows = await self.pool.fetch(
            "SELECT version_id, version_number, state, created_by, created_at, "
            "jsonb_array_length(output_manifest) AS file_count FROM artifact_versions WHERE artifact_id=$1 "
            "ORDER BY version_number DESC LIMIT $2",
            artifact_id,
            max(1, min(limit, 200)),
        )
        return {
            "versions": [
                {
                    "version_id": r["version_id"],
                    "version_number": r["version_number"],
                    "state": r["state"],
                    "created_by": r["created_by"],
                    "created_at": iso(r["created_at"]),
                    "file_count": r["file_count"],
                }
                for r in rows
            ]
        }

    async def content(self, actor: Actor, artifact_id: str, name: str) -> tuple[str, str, str]:
        """``(sha256, content_type, disposition)`` of one file of the current version of a readable artifact; the
        surface streams the blob (``stream_blob``)."""
        member = self._member(actor)
        await self._readable(member, artifact_id)
        try:
            wanted = normalise_name(name)
        except ShareBundleError:
            raise _not_found() from None
        manifest = await self.pool.fetchval(
            "SELECT v.output_manifest::text FROM artifacts a JOIN artifact_versions v "
            "ON v.version_id=a.current_version_id AND v.artifact_id=a.artifact_id WHERE a.artifact_id=$1 "
            "AND v.state='ready'",
            artifact_id,
        )
        item = next((f for f in json.loads(manifest or "[]") if f.get("name") == wanted), None)
        if item is None:
            raise _not_found()
        ctype = safe_media_type(item.get("media_type") or content_type_for(wanted))
        return item["sha256"], ctype, "inline" if ctype in _INLINE_TYPES else "attachment"

    def open_blob(self, sha: str) -> tuple[Any, int]:
        """The verified blob handle and its size (blocking: call in a thread)."""
        try:
            handle, _ = self.blobs.open_checked(sha)
        except (BlobMissing, BlobCorrupt):
            log.warning("member content: blob unavailable")
            raise _not_found() from None
        return handle, os.fstat(handle.fileno()).st_size

    # -- artifacts

    async def create(
        self, actor: Actor, title: str, filename: str, profile: str, idempotency_key: str | None
    ) -> dict[str, Any]:
        """A new, empty, accepted artifact the member owns (a pending one is an agent push: ``LibraryStore.insert_artifact``
        with ``intake='pending'`` is its atomic path)."""
        member = self._member(actor)
        title, filename = title.strip(), filename.strip()
        if (
            not 1 <= len(title) <= 200
            or not 1 <= len(filename) <= 255
            or not _PROFILE.fullmatch(profile)
        ):
            raise _bad("title, filename or profile is invalid")
        try:
            normalise_name(filename)
        except ShareBundleError:
            raise _bad("filename is invalid") from None
        if idempotency_key is not None and not _IDEMPOTENCY.fullmatch(idempotency_key):
            raise _bad("idempotency_key is invalid")
        self._throttle_write(member)
        request_hash = hashlib.sha256(json.dumps([title, filename, profile]).encode()).hexdigest()
        artifact_id = "art_" + secrets.token_hex(12)
        async with self.pool.acquire() as conn, conn.transaction():
            usage = await self._lock_usage(conn, member)
            if idempotency_key is not None:
                prior = await conn.fetchrow(
                    "SELECT request_hash, artifact_id FROM artifact_member_creates WHERE member_id=$1 "
                    "AND idempotency_key=$2",
                    member,
                    idempotency_key,
                )
                if prior is not None:
                    if prior["request_hash"] != request_hash:
                        raise MemberError(
                            409, "conflict", "idempotency_key was used with other arguments"
                        )
                    return {"artifact_id": prior["artifact_id"], "created": False}
            count = await conn.fetchval(
                "SELECT count(*) FROM artifacts WHERE owner=$1 AND lifecycle NOT IN ('retracted','tombstoned')",
                member,
            )
            if int(count) >= int(usage["max_artifacts"] or self.limits.max_artifacts):
                raise MemberError(429, "artifact_cap", "artifact limit reached")
            await self.library.insert_artifact(
                Viewer(member_id=member),
                artifact_id=artifact_id,
                display_name=title,
                filename=filename,
                artifact_type=profile,
                intake="accepted",
                conn=conn,
            )
            if idempotency_key is not None:
                await conn.execute(
                    "INSERT INTO artifact_member_creates (member_id, idempotency_key, request_hash, artifact_id) "
                    "VALUES ($1,$2,$3,$4)",
                    member,
                    idempotency_key,
                    request_hash,
                    artifact_id,
                )
        await self._audit(member, "create", "ok")
        return {"artifact_id": artifact_id, "created": True}

    @staticmethod
    async def _lock_usage(conn: asyncpg.Connection, member: str) -> asyncpg.Record:
        """The member's usage row, created if absent and locked until the transaction ends (serialises quota)."""
        await conn.execute(
            "INSERT INTO member_usage (member_id) VALUES ($1) ON CONFLICT DO NOTHING", member
        )
        row = await conn.fetchrow(
            "SELECT * FROM member_usage WHERE member_id=$1 FOR UPDATE", member
        )
        assert row is not None
        return row

    @staticmethod
    async def _owned(
        conn: asyncpg.Connection | asyncpg.Pool,
        member: str,
        artifact_id: str,
        *,
        lock: bool = False,
    ):
        row = await conn.fetchrow(
            "SELECT artifact_id, size_bytes, current_version_id, intake, lifecycle FROM artifacts "
            "WHERE artifact_id=$1 AND owner=$2 AND lifecycle NOT IN ('retracted','tombstoned') "
            "AND intake IN ('accepted','pending')" + (" FOR UPDATE" if lock else ""),
            artifact_id,
            member,
        )
        if row is None:
            raise _not_found()
        return row

    async def delete(self, actor: Actor, artifact_id: str, confirm: object) -> dict[str, Any]:
        member = self._member(actor)
        if confirm is not True:
            raise _bad("confirm must be true to delete an artifact")
        self._throttle_write(member)
        async with self.pool.acquire() as conn, conn.transaction():
            await self._lock_usage(conn, member)
            row = await self._owned(conn, member, artifact_id, lock=True)
            await conn.execute(
                "UPDATE artifacts SET lifecycle='retracted', updated_at=now() WHERE artifact_id=$1",
                artifact_id,
            )
            await conn.execute(
                "WITH r AS (UPDATE shares SET state='revoked', revoked_at=now() WHERE artifact_id=$1 "
                "AND state<>'revoked' RETURNING share_id) "
                "INSERT INTO share_events (share_id, kind, actor) SELECT share_id, 'revoked', $2 FROM r",
                artifact_id,
                member,
            )
            await settle_quota(conn, member, int(row["size_bytes"]))
        await self._drop_staging(member, artifact_id)
        await self._audit(member, "delete", "ok")
        return {"artifact_id": artifact_id, "deleted": True}

    # -- library

    async def folder_create(self, actor: Actor, name: str) -> dict[str, Any]:
        viewer = Viewer(member_id=self._member(actor))
        f = await self.library.create_folder(viewer, name)
        return {"folder_id": f.folder_id, "name": f.name, "item_count": f.item_count}

    async def folder_rename(self, actor: Actor, folder_id: str, name: str) -> dict[str, Any]:
        f = await self.library.rename_folder(Viewer(member_id=self._member(actor)), folder_id, name)
        return {"folder_id": f.folder_id, "name": f.name, "item_count": f.item_count}

    async def folder_delete(self, actor: Actor, folder_id: str) -> dict[str, Any]:
        await self.library.delete_folder(Viewer(member_id=self._member(actor)), folder_id)
        return {"deleted": True}

    async def folder_file(self, actor: Actor, folder_id: str, artifact_id: str) -> dict[str, Any]:
        await self.library.file_artifact(await self.viewer(actor), folder_id, artifact_id)
        return {"filed": True}

    async def folder_unfile(self, actor: Actor, folder_id: str, artifact_id: str) -> dict[str, Any]:
        await self.library.unfile_artifact(
            Viewer(member_id=self._member(actor)), folder_id, artifact_id
        )
        return {"filed": False}

    async def accept(self, actor: Actor, artifact_id: str) -> dict[str, Any]:
        await self.library.accept(Viewer(member_id=self._member(actor)), ItemRef(artifact_id))
        return {"accepted": True}

    async def discard(self, actor: Actor, artifact_id: str) -> dict[str, Any]:
        member = self._member(actor)
        await self.library.discard(Viewer(member_id=member), ItemRef(artifact_id))
        await self._drop_staging(member, artifact_id)
        return {"discarded": True}

    # -- uploads

    @staticmethod
    def upload_id_for(member: str, artifact_id: str, name: str, file_sha: str, size: int) -> str:
        """Derived, never chosen.  ``name`` is last and the only field that may contain ``|``."""
        pre = f"{member}|{artifact_id}|{file_sha}|{size}|{name}".encode()
        return "upl_" + hashlib.sha256(b"herdr-host-upload-v1\x00" + pre).hexdigest()[:24]

    @staticmethod
    def _file_args(name: object, size: object, file_sha: object) -> tuple[str, int, str]:
        if (
            not isinstance(name, str)
            or not isinstance(file_sha, str)
            or not SHA_RE.fullmatch(file_sha)
        ):
            raise _bad("file name or sha256 is invalid")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise _bad("size_bytes is invalid")
        if size > MAX_UPLOAD_BYTES:
            raise MemberError(413, "too_large", "file is too large")
        try:
            return normalise_name(name), size, file_sha
        except ShareBundleError:
            raise _bad("file name is invalid") from None

    @staticmethod
    def _chunks(size: int) -> int:
        return max(1, -(-size // CHUNK_BYTES)) if size else 0

    @staticmethod
    def _member_key(member: str) -> str:
        return hashlib.sha256(b"herdr-host-staging-v1\x00" + member.encode()).hexdigest()[:24]

    def _artifact_dir(self, member: str, artifact_id: str) -> Path:
        return self._staging / self._member_key(member) / artifact_id

    def _dir(self, member: str, artifact_id: str, upload_id: str) -> Path:
        return self._artifact_dir(member, artifact_id) / upload_id

    async def _drop_staging(self, member: str, artifact_id: str) -> None:
        await asyncio.to_thread(shutil.rmtree, self._artifact_dir(member, artifact_id), True)

    @staticmethod
    def _du(root: Path) -> int:
        total = 0
        for p in root.rglob("*") if root.is_dir() else ():
            try:
                if p.is_file():
                    total += p.stat().st_size
            except OSError:
                continue
        return total

    def _staging_after(
        self, member: str, artifact_id: str, upload_id: str, first: int, length: int
    ) -> tuple[int, int]:
        """``(member_bytes, artifact_bytes)`` staged if this run were stored: what is there now, minus the chunks this
        run would replace, plus the run."""
        replaced = 0
        folder = self._dir(member, artifact_id, upload_id)
        if folder.is_dir():
            wanted = {f"{i:07d}" for i in range(first, first + max(1, -(-length // CHUNK_BYTES)))}
            for p in folder.iterdir():
                if p.name in wanted and p.is_file():
                    replaced += p.stat().st_size
        mine = self._du(self._staging / self._member_key(member)) - replaced + length
        here = self._du(self._artifact_dir(member, artifact_id)) - replaced + length
        return mine, here

    def sweep_staging(self, max_age_seconds: float) -> int:
        """Remove every staged upload nothing has written to for ``max_age_seconds``; returns how many.  Blocking:
        housekeeping calls it in a thread."""
        cutoff = self.now() - max_age_seconds
        removed = 0
        if not self._staging.is_dir():
            return 0
        for member_dir in list(self._staging.iterdir()):
            for artifact_dir in list(member_dir.iterdir()) if member_dir.is_dir() else ():
                for upload in list(artifact_dir.iterdir()) if artifact_dir.is_dir() else ():
                    try:
                        newest = max(
                            [upload.stat().st_mtime]
                            + [p.stat().st_mtime for p in upload.rglob("*")]
                        )
                    except OSError:
                        continue
                    if newest < cutoff:
                        shutil.rmtree(upload, ignore_errors=True)
                        removed += 1
                try:
                    artifact_dir.rmdir()  # only if now empty
                except OSError:
                    pass
            try:
                member_dir.rmdir()
            except OSError:
                pass
        return removed

    def precheck_run(self, content_length: int, size: int, first: int) -> None:
        """Everything that can be decided from the headers alone, before one body byte is read."""
        total = self._chunks(size)
        if content_length < 1 or content_length > MAX_UPLOAD_BYTES:
            raise MemberError(413, "too_large", "run is too large")
        if first >= max(total, 1) or first * CHUNK_BYTES + content_length > size:
            raise _bad("run is outside the file")
        if (first * CHUNK_BYTES + content_length != size) and content_length % CHUNK_BYTES:
            raise _bad("a run must be whole chunks unless it ends the file")

    async def upload_run(
        self,
        actor: Actor,
        artifact_id: str,
        upload_id: str,
        name: str,
        size: int,
        file_sha: str,
        first: int,
        run_sha: str,
        length: int,
        spool: Callable[[Path], Awaitable[str]],
    ) -> dict[str, Any]:
        """Stage one run.  ``spool(path)`` streams the request body into ``path`` and returns its sha256; it runs under
        ``body_deadline_seconds`` and a per-member slot, after the staging caps were checked from the headers."""
        member = self._member(actor)
        await self._owned(self.pool, member, artifact_id)
        name, size, file_sha = self._file_args(name, size, file_sha)
        if self.upload_id_for(member, artifact_id, name, file_sha, size) != upload_id:
            raise _bad("upload_id does not match the headers")
        if not SHA_RE.fullmatch(run_sha):
            raise _bad()
        n_chunks = max(1, -(-length // CHUNK_BYTES))
        if not all(self.chunk_rate.allow(member + "\x00chunks") for _ in range(n_chunks)):
            raise MemberError(
                429, "rate_limited", "too many chunks, retry shortly", {"Retry-After": "10"}
            )
        if self._uploading.get(member, 0) >= self.limits.uploads_per_member:
            raise MemberError(429, "busy", "too many uploads in progress", {"Retry-After": "5"})
        self._uploading[member] = self._uploading.get(member, 0) + 1
        folder = self._dir(member, artifact_id, upload_id)
        scratch = folder / f".in-{secrets.token_hex(6)}"
        try:
            mine, here = await asyncio.to_thread(
                self._staging_after, member, artifact_id, upload_id, first, length
            )
            if (
                mine > self.limits.staging_bytes_per_member
                or here > self.limits.staging_bytes_per_artifact
            ):
                raise MemberError(413, "staging_full", "too many bytes are waiting to be published")
            async with self._run_slots:
                await asyncio.to_thread(scratch.mkdir, parents=True)
                body = scratch / "body"
                try:
                    got = await asyncio.wait_for(spool(body), self.limits.body_deadline_seconds)
                except TimeoutError:
                    raise MemberError(408, "timeout", "the upload took too long") from None
                if got != run_sha:
                    raise _bad("run sha256 does not match")
                await asyncio.to_thread(self._store_run, folder, scratch, size, first)
        finally:
            await asyncio.to_thread(shutil.rmtree, scratch, True)
            left = self._uploading.get(member, 1) - 1
            if left > 0:
                self._uploading[member] = left
            else:
                self._uploading.pop(member, None)
        return await self._status(folder, size, upload_id)

    def _store_run(self, folder: Path, scratch: Path, size: int, first: int) -> None:
        """Split the received body into chunk files (one chunk in memory at a time) and move them into place."""
        total = self._chunks(size)
        with (scratch / "body").open("rb") as src:
            index = first
            while piece := src.read(CHUNK_BYTES):
                expected = CHUNK_BYTES if index < total - 1 else size - CHUNK_BYTES * (total - 1)
                if len(piece) != expected:
                    raise _bad("chunk has the wrong length")
                (scratch / f"{index:07d}").write_bytes(piece)
                index += 1
        for p in scratch.iterdir():
            if p.name.isdigit():
                os.replace(p, folder / p.name)

    async def _status(self, folder: Path, size: int, upload_id: str) -> dict[str, Any]:
        def scan() -> list[int]:
            return (
                sorted(int(p.name) for p in folder.iterdir() if p.name.isdigit())
                if folder.is_dir()
                else []
            )

        have = await asyncio.to_thread(scan)
        total = self._chunks(size)
        missing = [i for i in range(total) if i not in set(have)]
        ranges: list[list[int]] = []
        for i in missing[:1000]:
            if ranges and ranges[-1][1] == i - 1:
                ranges[-1][1] = i
            else:
                ranges.append([i, i])
        return {
            "upload_id": upload_id,
            "chunks": total,
            "received": len(have),
            "missing": ranges,
            "complete": not missing,
        }

    async def upload_status(
        self, actor: Actor, artifact_id: str, name: object, size: object, file_sha: object
    ) -> dict[str, Any]:
        member = self._member(actor)
        await self._owned(self.pool, member, artifact_id)
        name_n, size_n, sha_n = self._file_args(name, size, file_sha)
        upload_id = self.upload_id_for(member, artifact_id, name_n, sha_n, size_n)
        return await self._status(self._dir(member, artifact_id, upload_id), size_n, upload_id)

    async def upload_abort(
        self,
        actor: Actor,
        artifact_id: str,
        upload_id: str,
        name: object,
        size: object,
        file_sha: object,
    ) -> dict[str, Any]:
        """Drop one staged upload.  The caller names the file; the id is re-derived from it and must match."""
        member = self._member(actor)
        await self._owned(self.pool, member, artifact_id)
        if not UPLOAD_ID_RE.fullmatch(upload_id):
            raise _bad()
        name_n, size_n, sha_n = self._file_args(name, size, file_sha)
        if self.upload_id_for(member, artifact_id, name_n, sha_n, size_n) != upload_id:
            raise _bad("upload_id does not match the file")
        await asyncio.to_thread(shutil.rmtree, self._dir(member, artifact_id, upload_id), True)
        return {"aborted": True}

    # -- publish

    def _check_quota(self, usage: asyncpg.Record | None, current_size: int, total: int) -> int:
        """The member's projected bytes if this artifact became ``total`` bytes; raises 413 ``quota`` when over."""

        def limit(column: str, default: int) -> int:
            return int((usage and usage[column]) or default)

        if total > limit("max_artifact_bytes", self.limits.max_artifact_bytes):
            raise MemberError(413, "quota", "artifact is over its size limit")
        projected = int(usage["bytes_used"] if usage else 0) - current_size + total
        if projected > limit("max_member_bytes", self.limits.max_member_bytes):
            raise MemberError(413, "quota", "storage quota exceeded")
        return projected

    def _assemble(self, folder: Path, name: str, size: int, file_sha: str) -> str:
        """Verify and install one staged file in the blob store; returns its sha.  Nothing is stored unless the bytes
        hash to ``file_sha`` and fill ``size`` exactly."""
        total = self._chunks(size)
        paths = [folder / f"{i:07d}" for i in range(total)]
        if not all(p.is_file() for p in paths):
            raise MemberError(409, "incomplete_upload", f"upload of {name} is incomplete")
        digest, seen = hashlib.sha256(), 0
        for p in paths:
            piece = p.read_bytes()
            digest.update(piece)
            seen += len(piece)
        if seen != size or digest.hexdigest() != file_sha:
            raise MemberError(409, "upload_mismatch", f"upload of {name} does not match its sha256")
        stream = _Chain(paths)
        try:
            stored = self.blobs.put(stream)
        finally:
            stream.close()
        if stored != file_sha:
            raise MemberError(
                409, "upload_mismatch", f"upload of {name} changed while it was stored"
            )
        return stored

    async def publish(
        self,
        actor: Actor,
        artifact_id: str,
        *,
        base_version_id: object,
        files: object,
        message: object,
        entry: object,
    ) -> dict[str, Any]:
        """Publish the complete file set of the next version.  Blob store first, then every row in one transaction."""
        member = self._member(actor)
        if not isinstance(files, list) or not 1 <= len(files) <= MAX_FILES:
            raise _bad(f"files must list 1 to {MAX_FILES} files")
        if message is not None and (not isinstance(message, str) or len(message) > 500):
            raise _bad("message is invalid")
        if base_version_id is not None and not isinstance(base_version_id, str):
            raise _bad()
        if entry is not None and not isinstance(entry, str):
            raise _bad()
        self._throttle_write(member)
        await self._owned(self.pool, member, artifact_id)
        specs: dict[str, tuple[int, str]] = {}
        for f in files:
            if not isinstance(f, dict) or set(f) != {"name", "size_bytes", "file_sha256"}:
                raise _bad("each file needs name, size_bytes and file_sha256")
            name, size, sha = self._file_args(f["name"], f["size_bytes"], f["file_sha256"])
            if name in specs:
                raise _bad("duplicate file name")
            specs[name] = (size, sha)
        try:
            chosen = choose_entry(entry, sorted(specs)) if entry is not None or specs else None
        except ShareBundleError:
            chosen = None  # an entry is only needed when a share is made
        total = sum(s for s, _ in specs.values())
        # lock-free pre-check: an over-quota publish must not write (and then have to remove) a single blob
        early = await self.pool.fetchrow(
            "SELECT bytes_used, max_member_bytes, max_artifact_bytes FROM member_usage WHERE member_id=$1",
            member,
        )
        current_row = await self._owned(self.pool, member, artifact_id)
        self._check_quota(early, int(current_row["size_bytes"]), total)
        version_id = "ver_" + secrets.token_hex(12)
        shas = sorted({sha for _, sha in specs.values()})
        written: list[str] = []
        committed = False
        manifest: list[dict[str, Any]] = []
        # One connection for the whole publish: it holds a session-level advisory lock per content hash from before
        # the first blob write until the rows are committed (or the blobs removed), so the cleanup of a failed publish
        # can never delete a blob a concurrent publish of the same content has just written.
        async with self.pool.acquire() as conn:
            try:
                for (
                    sha
                ) in shas:  # sorted: two publishes never wait on one another in opposite orders
                    await conn.execute(
                        "SELECT pg_advisory_lock($1, hashtext($2))", LOCK_NS_BLOB, sha
                    )
                for name, (size, sha) in specs.items():
                    folder = self._dir(
                        member,
                        artifact_id,
                        self.upload_id_for(member, artifact_id, name, sha, size),
                    )
                    stored = await asyncio.to_thread(self._assemble, folder, name, size, sha)
                    written.append(stored)
                    manifest.append(
                        {
                            "name": name,
                            "sha256": stored,
                            "size_bytes": size,
                            "media_type": safe_media_type(content_type_for(name)),
                        }
                    )
                async with conn.transaction():
                    usage = await self._lock_usage(conn, member)
                    row = await self._owned(conn, member, artifact_id, lock=True)
                    current = row["current_version_id"]
                    if (base_version_id or None) != current:
                        raise MemberError(409, "stale_base", "the artifact has a newer version")
                    projected = self._check_quota(usage, int(row["size_bytes"]), total)
                    for item in manifest:
                        await conn.execute(
                            "INSERT INTO artifact_blobs (sha256, size_bytes, media_type) VALUES ($1,$2,$3) "
                            "ON CONFLICT (sha256) DO NOTHING",
                            item["sha256"],
                            item["size_bytes"],
                            item["media_type"],
                        )
                    number = int(
                        await conn.fetchval(
                            "SELECT coalesce(max(version_number), 0) + 1 FROM artifact_versions "
                            "WHERE artifact_id=$1",
                            artifact_id,
                        )
                    )
                    await conn.execute(
                        "INSERT INTO artifact_versions (version_id, artifact_id, version_number, "
                        "parent_version_ids, state, output_manifest, provenance, created_by) "
                        "VALUES ($1,$2,$3,$4,'ready',$5::jsonb,$6::jsonb,$7)",
                        version_id,
                        artifact_id,
                        number,
                        [current] if current else [],
                        json.dumps(manifest),
                        json.dumps({"message": message or "", "entry": chosen}),
                        member,
                    )
                    await conn.execute(
                        "UPDATE artifacts SET current_version_id=$2, size_bytes=$3, updated_at=now() "
                        "WHERE artifact_id=$1",
                        artifact_id,
                        version_id,
                        total,
                    )
                    await conn.execute(
                        "UPDATE member_usage SET bytes_used=$2, updated_at=now() WHERE member_id=$1",
                        member,
                        projected,
                    )
                committed = True
            finally:
                try:
                    if not committed:
                        await self._reap(conn, written)
                finally:
                    try:
                        await conn.execute("SELECT pg_advisory_unlock_all()")
                    except Exception:
                        log.warning("member publish: could not release blob locks")
        await self._drop_staging(member, artifact_id)
        await self._audit(member, "publish", "ok")
        return {
            "artifact_id": artifact_id,
            "version_id": version_id,
            "version_number": number,
            "size_bytes": total,
        }

    async def _reap(self, conn: asyncpg.Connection, written: list[str]) -> None:
        """Remove the blob files a failed publish wrote, except any that has an ``artifact_blobs`` row.  The caller
        holds the sha's advisory lock, so no concurrent publish is between its write and its insert."""
        for sha in dict.fromkeys(written):
            try:
                if await conn.fetchval("SELECT 1 FROM artifact_blobs WHERE sha256=$1", sha) is None:
                    await asyncio.to_thread(self.blobs.remove, sha)
            except Exception:
                log.warning("member publish: blob cleanup failed")

    # -- shares

    async def share_create(
        self, actor: Actor, artifact_id: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        member = self._member(actor)
        version_id, audience = body["version_id"], body["audience"]
        if not isinstance(version_id, str) or not _ARTIFACT.fullmatch(version_id):
            raise _bad("version_id is invalid")
        if audience not in ("public", "lab"):
            raise _bad("audience must be public or lab")
        emails = self._allow_list(body.get("allow_emails"), _EMAIL, lower=True)
        groups = self._allow_list(body.get("allow_groups"), _GROUP)
        if audience == "lab" and not (emails or groups):
            raise _bad("a lab share needs allow_emails or allow_groups")
        if audience == "public" and (emails or groups):
            raise _bad("a public share takes no allow list")
        days = body.get("expires_in_days", DEFAULT_SHARE_DAYS)
        if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= MAX_SHARE_DAYS:
            raise _bad(f"expires_in_days must be 1 to {MAX_SHARE_DAYS}")
        title = body.get("title")
        if title is not None and (not isinstance(title, str) or len(title) > 200):
            raise _bad("title is invalid")
        entry_arg = body.get("entry")
        if entry_arg is not None and not isinstance(entry_arg, str):
            raise _bad("entry is invalid")
        self._throttle_write(member)
        token = secrets.token_urlsafe(32)
        share_id = "shr_" + secrets.token_hex(12)
        expires = now_dt(self.now) + timedelta(days=days)
        async with self.pool.acquire() as conn, conn.transaction():
            usage = await self._lock_usage(conn, member)
            if usage["sharing_disabled"]:
                raise MemberError(403, "sharing_disabled", "sharing is disabled for this account")
            await self._owned(conn, member, artifact_id, lock=True)
            art = await conn.fetchrow(
                "SELECT intake, lifecycle FROM artifacts WHERE artifact_id=$1", artifact_id
            )
            if art is None or art["intake"] != "accepted" or art["lifecycle"] != "active":
                raise _not_found()
            ver = await conn.fetchrow(
                "SELECT output_manifest::text AS manifest FROM artifact_versions WHERE version_id=$1 "
                "AND artifact_id=$2 AND state='ready'",
                version_id,
                artifact_id,
            )
            if ver is None:
                raise _not_found()
            files = json.loads(ver["manifest"])
            try:
                entry = choose_entry(entry_arg, sorted(f["name"] for f in files))
            except ShareBundleError:
                raise _bad("entry is not in the version") from None
            recent = await conn.fetchval(
                "SELECT count(*) FROM shares WHERE created_by=$1 AND created_at > $2",
                member,
                now_dt(self.now) - self.share_rate_window,
            )
            if int(recent) >= self.limits.share_creates_per_hour:
                raise MemberError(
                    429, "rate_limited", "too many share links this hour", {"Retry-After": "60"}
                )
            await conn.execute(
                "INSERT INTO shares (share_id, artifact_id, version_id, audience, token_hash, allow_emails, "
                "allow_groups, state, entry, file_count, total_bytes, title, expires_at, created_by) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7,'pending',$8,$9,$10,$11,$12,$13)",
                share_id,
                artifact_id,
                version_id,
                audience,
                token_digest(token),
                emails,
                groups,
                entry,
                len(files),
                sum(int(f["size_bytes"]) for f in files),
                (title or None),
                expires,
                member,
            )
            await conn.execute(
                "INSERT INTO share_events (share_id, kind, actor, detail) VALUES ($1,'created',$2,$3::jsonb)",
                share_id,
                member,
                json.dumps({"audience": audience}),
            )
        await self._audit(member, "share_create", "ok", None, share_id, audience)
        # the one and only place the token is shown; only its digest is stored
        base = self.public_origin or ""
        return {
            "share_id": share_id,
            "state": "pending",
            "audience": audience,
            "token": token,
            "url": f"{base}/s/{token}/",
            "expires_at": iso(expires),
        }

    @staticmethod
    def _allow_list(value: object, pattern: re.Pattern[str], *, lower: bool = False) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, list) or len(value) > MAX_ALLOW_ENTRIES:
            raise _bad("allow list is invalid")
        out: list[str] = []
        for item in value:
            if (
                not isinstance(item, str)
                or not item.isascii()
                or not pattern.fullmatch(item.strip())
            ):
                raise _bad("allow list is invalid")
            item = item.strip().lower() if lower else item.strip()
            if item not in out:
                out.append(item)
        return out

    async def _owned_share(
        self, conn: asyncpg.Connection, member: str, artifact_id: str, share_id: str
    ):
        row = await conn.fetchrow(
            "SELECT s.share_id, s.state, s.audience, s.expires_at FROM shares s JOIN artifacts a "
            "ON a.artifact_id=s.artifact_id WHERE s.share_id=$1 AND s.artifact_id=$2 AND a.owner=$3 "
            "AND a.lifecycle NOT IN ('retracted','tombstoned') FOR UPDATE OF s",
            share_id,
            artifact_id,
            member,
        )
        if row is None:
            raise _not_found()
        return row

    async def share_approve(
        self,
        actor: Actor,
        artifact_id: str,
        share_id: str,
        confirm: object,
        idempotency_key: object = None,
    ) -> dict[str, Any]:
        """Make a pending share live.  Only the owner, as a signed-in user: an agent, a token or the operator is refused."""
        member = self._member(actor)
        if actor.kind is not ActorKind.USER:
            await self._audit(member, "share_approve", "refused", "actor_cannot_approve", share_id)
            raise MemberError(403, "forbidden", "only the signed-in owner can approve a share link")
        if confirm is not True:
            raise _bad("confirm must be true to make a share link live")
        if idempotency_key is not None and (
            not isinstance(idempotency_key, str) or not _IDEMPOTENCY.fullmatch(idempotency_key)
        ):
            raise _bad("idempotency_key is invalid")
        self._throttle_write(member)
        async with self.pool.acquire() as conn, conn.transaction():
            usage = await self._lock_usage(conn, member)
            if usage["sharing_disabled"]:  # revoke and the event log stay open
                raise MemberError(403, "sharing_disabled", "sharing is disabled for this account")
            row = await self._owned_share(conn, member, artifact_id, share_id)
            if row["state"] != "pending":
                if (
                    idempotency_key is not None
                    and row["state"] == "live"
                    and await conn.fetchval(
                        "SELECT 1 FROM share_events WHERE share_id=$1 AND kind='approved' "
                        "AND detail->>'idempotency_key'=$2",
                        share_id,
                        idempotency_key,
                    )
                ):
                    return {"share_id": share_id, "state": "live"}  # the same request again
                raise MemberError(409, "not_pending", "the share link is not pending")
            if row["expires_at"] is not None and row["expires_at"] <= now_dt(self.now):
                raise MemberError(409, "expired", "the share link has expired")
            await conn.execute(
                "UPDATE shares SET state='live', approved_by=$2, approved_at=now() WHERE share_id=$1",
                share_id,
                member,
            )
            await conn.execute(
                "INSERT INTO share_events (share_id, kind, actor, detail) VALUES ($1,'approved',$2,$3::jsonb)",
                share_id,
                member,
                json.dumps({"idempotency_key": idempotency_key} if idempotency_key else {}),
            )
        await self._audit(member, "share_approve", "ok", None, share_id, row["audience"])
        return {"share_id": share_id, "state": "live"}

    async def share_revoke(self, actor: Actor, artifact_id: str, share_id: str) -> dict[str, Any]:
        member = self._member(actor)
        self._throttle_write(member)
        async with self.pool.acquire() as conn, conn.transaction():
            row = await self._owned_share(conn, member, artifact_id, share_id)
            if row["state"] != "revoked":
                await conn.execute(
                    "UPDATE shares SET state='revoked', revoked_at=now() WHERE share_id=$1",
                    share_id,
                )
                await conn.execute(
                    "INSERT INTO share_events (share_id, kind, actor) VALUES ($1,'revoked',$2)",
                    share_id,
                    member,
                )
        await self._audit(member, "share_revoke", "ok", None, share_id, row["audience"])
        return {"share_id": share_id, "state": "revoked"}

    async def share_events(self, actor: Actor, artifact_id: str, share_id: str) -> dict[str, Any]:
        member = self._member(actor)
        async with self.pool.acquire() as conn:
            ok = await conn.fetchval(
                "SELECT 1 FROM shares s JOIN artifacts a ON a.artifact_id=s.artifact_id WHERE s.share_id=$1 "
                "AND s.artifact_id=$2 AND a.owner=$3",
                share_id,
                artifact_id,
                member,
            )
            if ok is None:
                raise _not_found()
            rows = await conn.fetch(
                "SELECT kind, actor, at, detail::text AS detail FROM share_events WHERE share_id=$1 "
                "ORDER BY event_id DESC LIMIT 200",
                share_id,
            )
        return {
            "events": [
                {
                    "kind": r["kind"],
                    "actor": r["actor"],
                    "at": iso(r["at"]),
                    "detail": json.loads(r["detail"]),
                }
                for r in rows
            ]
        }

    async def share_list(self, actor: Actor, artifact_id: str | None = None) -> dict[str, Any]:
        """The member's links (those on artifacts they own).  Never a token or URL: those exist once, at creation."""
        member = self._member(actor)
        rows = await self.pool.fetch(
            "SELECT s.share_id, s.artifact_id, s.version_id, s.audience, s.state, s.entry, s.title, "
            "s.expires_at, s.created_at, s.approved_at, s.revoked_at, s.allow_emails, s.allow_groups "
            "FROM shares s JOIN artifacts a ON a.artifact_id=s.artifact_id WHERE a.owner=$1 "
            "AND a.lifecycle NOT IN ('retracted','tombstoned') AND ($2::text IS NULL OR s.artifact_id=$2) "
            "ORDER BY s.created_at DESC LIMIT 500",
            member,
            artifact_id,
        )
        now = now_dt(self.now)
        out = []
        for r in rows:
            status = r["state"]
            if status == "live" and r["expires_at"] is not None and r["expires_at"] <= now:
                status = "expired"
            out.append(
                {
                    "share_id": r["share_id"],
                    "artifact_id": r["artifact_id"],
                    "version_id": r["version_id"],
                    "audience": r["audience"],
                    "state": r["state"],
                    "status": status,
                    "entry": r["entry"],
                    "title": r["title"],
                    "expires_at": iso(r["expires_at"]),
                    "created_at": iso(r["created_at"]),
                    "approved_at": iso(r["approved_at"]),
                    "revoked_at": iso(r["revoked_at"]),
                    "allow_emails": list(r["allow_emails"]),
                    "allow_groups": list(r["allow_groups"]),
                }
            )
        return {"links": out}


class _Chain(io.RawIOBase):
    """The concatenation of staged chunk files, read sequentially (the blob store streams from it)."""

    def __init__(self, paths: list[Path]) -> None:
        super().__init__()
        self._paths, self._i, self._cur = paths, 0, None

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        view = memoryview(buffer)
        while True:
            if self._cur is None:
                if self._i >= len(self._paths):
                    return 0
                self._cur = self._paths[self._i].open("rb")
                self._i += 1
            n = self._cur.readinto(view)
            if n:
                return n
            self._cur.close()
            self._cur = None

    def close(self) -> None:
        if self._cur is not None:
            self._cur.close()
            self._cur = None
        super().close()


# ----------------------------------------------------------------------------- HTTP


class MemberSurface:
    """The aiohttp side: Host and Origin rules, authentication, body limits, routing to ``MemberService``."""

    def __init__(
        self,
        settings: MemberSettings,
        service: MemberService,
        uniform_not_found: Callable[[], web.Response],
        now: Callable[[], float] = time.time,
    ) -> None:
        self.s, self.svc, self.not_found, self.now = settings, service, uniform_not_found, now
        # only FAILED exchanges are counted: per client address (the real limit) and, much higher, for everyone
        self.exchange_failures = RateLimiter(settings.limits.session_exchanges_per_minute, now)
        self.exchange_failures_all = RateLimiter(
            settings.limits.session_exchanges_global_per_minute, now, max_keys=1
        )

    @staticmethod
    def owns(raw_path: str) -> bool:
        path = raw_path.split("?", 1)[0]
        return path == "/" or path == "/my" or path.startswith(("/my/", "/api/member/"))

    # -- responses

    def _json(self, status: int, body: Any, headers: dict[str, str] | None = None) -> web.Response:
        out = {
            "Content-Type": _JSON,
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "Server": "herdr-share",
            **(headers or {}),
        }
        return web.Response(status=status, body=json.dumps(body).encode(), headers=out)

    def _error(self, err: MemberError) -> web.Response:
        body = {"error": err.code}
        if err.message:
            body["message"] = err.message
        resp = self._json(err.status, body, err.headers)
        resp.force_close()  # an unread request body must never be parsed as the next request
        return resp

    async def handle(self, request: web.Request) -> web.StreamResponse:
        try:
            return await self._handle(request)
        except MemberError as err:
            return self._error(err)
        except LibraryError as err:
            status = err.status if err.status in (400, 403, 404, 409, 429) else 400
            code = err.code if status != 404 else "not_found"
            return self._error(
                MemberError(status, code, "not found" if status == 404 else err.message)
            )
        except (asyncio.CancelledError, ConnectionResetError):
            raise
        except Exception as exc:  # one fixed line: the type only, it could hold a path or a token
            log.warning("member: internal error (%s)", type(exc).__name__)
            return self._error(MemberError(500, "internal", "internal error"))

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        raw = request.raw_path
        path = raw.split("?", 1)[0]
        query = raw.partition("?")[2]
        if len(raw) > 1024 or not _PATH_OK.fullmatch(path):
            return self.not_found()
        method = request.method
        if method not in ("GET", "HEAD", "POST", "PUT", "DELETE"):
            raise MemberError(405, "method_not_allowed", "method not allowed")
        if method not in ("GET", "HEAD"):
            origins = request.headers.getall("Origin", [])
            if len(origins) != 1 or origins[0] != self.s.public_origin:
                raise MemberError(403, "forbidden", "forbidden")
        if method in ("GET", "HEAD") and path in self.s.assets:
            return self._static(path)
        segs = path.strip("/").split("/")
        if segs[:2] != ["api", "member"] or any(s in ("", ".", "..") for s in segs):
            return self.not_found()
        rest = segs[2:]
        if method == "GET" and rest == ["config"]:
            return self._config()
        if method == "POST" and rest == ["session"]:
            return await self._session(request)
        actor = await self._authenticate(request)
        return await self._route(request, actor, method, rest, query)

    # -- public routes

    def _static(self, path: str) -> web.Response:
        body, ctype = self.s.assets[path]
        headers = {
            "Content-Type": ctype,
            "Cache-Control": _FONT_CACHE if ctype == _FONT else "no-store",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "X-Frame-Options": "DENY",
            "Server": "herdr-share",
        }
        if ctype == _HTML:
            headers["Content-Security-Policy"] = self.s.page_csp
        return web.Response(status=200, body=body, headers=headers)

    def _config(self) -> web.Response:
        s = self.s
        return self._json(
            200,
            {
                "cognito_domain": s.cognito_origin,
                "client_id": s.client_id,
                "redirect_uri": f"{s.public_origin}/my/callback",
                "logout_uri": f"{s.public_origin}/",
                "chunk_bytes": CHUNK_BYTES,
                "max_upload_bytes": MAX_UPLOAD_BYTES,
                "run_chunks": MAX_UPLOAD_BYTES // CHUNK_BYTES,
            },
        )

    @staticmethod
    def _single(request: web.Request, name: str, limit: int = 8192) -> str:
        values = request.headers.getall(name, [])
        if len(values) != 1 or not values[0] or len(values[0]) > limit or not values[0].isascii():
            raise _unauthorized()
        return values[0]

    @staticmethod
    def _client(request: web.Request) -> str:
        """The limiter key for a client: the socket peer, or (only when that peer is a loopback or private address,
        i.e. our own proxy) the last X-Forwarded-For entry the proxy appended."""
        peername = request.transport.get_extra_info("peername") if request.transport else None
        peer = str(peername[0]) if peername else "unknown"
        forwarded = request.headers.get("X-Forwarded-For", "").split(",")[-1].strip()
        try:
            address = ipaddress.ip_address(peer)
            trusted = address.is_loopback or address.is_private
        except ValueError:
            trusted = False
        return forwarded[:64] if forwarded and trusted else peer

    async def _session(self, request: web.Request) -> web.Response:
        client = self._client(request)
        if self.exchange_failures.blocked(client) or self.exchange_failures_all.blocked("*"):
            raise MemberError(429, "rate_limited", "too many sign-ins", {"Retry-After": "60"})
        try:
            return await self._exchange(request)
        except MemberError as err:
            if err.status in (400, 401):
                self.exchange_failures.allow(client)
                self.exchange_failures_all.allow("*")
            raise

    async def _exchange(self, request: web.Request) -> web.Response:
        if request.headers.getall("Content-Length", ["0"]) not in (
            ["0"],
            [""],
        ) or request.headers.getall("Transfer-Encoding", []):
            raise _bad()
        auth, id_token = self._single(request, "Authorization"), self._single(request, "X-Id-Token")
        nonce = self._single(request, "X-Nonce", 256)
        scheme, _, access = auth.partition(" ")
        if scheme != "Bearer" or not access or access != access.strip():
            raise _unauthorized()

        def verify() -> tuple[str, float, str, list[str], bool] | None:
            try:
                claims = self.s.authenticator.verify_claims(access)
                if not claims:
                    return None
                user = claims["user"]
                email = self.s.authenticator.verify_id_token(id_token, user=user, nonce=nonce)
                if not email:
                    return None
                return (
                    user,
                    float(claims["exp"]),
                    email,
                    list(claims.get("groups") or []),
                    bool(claims.get("groups_capped")),
                )
            except Exception:
                return None

        got = await asyncio.to_thread(verify)
        if got is None:
            raise _unauthorized()
        user, token_exp, email, groups, capped = got
        now = self.now()
        exp = min(now + self.s.session_seconds, token_exp)
        if self.s.claim_fallback and not await asyncio.to_thread(self.s.directory_present):
            # no group directory vouches for this member: the token's claim alone decides, so the session is short
            exp = min(exp, now + self.s.limits.claim_fallback_session_seconds)
        if not exp > now + 1:
            raise _unauthorized()
        try:
            session = mint_session(self.s.secret, user, exp, int(now * 1000))
        except ValueError:
            raise _unauthorized() from None
        # a capped list is a truncated one: record no groups rather than a partial picture of them
        recorded = [] if capped else [g for g in groups if _GROUP.fullmatch(g)][:64]
        await self.svc.record_sign_in(user, email, recorded, exp)
        return self._json(200, {"session": session, "expires_at": int(exp), "member_id": user})

    async def _authenticate(self, request: web.Request) -> Actor:
        auth = self._single(request, "Authorization", 1024)
        scheme, _, token = auth.partition(" ")
        if scheme != "Member":
            raise _unauthorized()
        got = check_session(self.s.secret, token, self.now())
        if got is None:
            raise _unauthorized()
        identity = got["identity"]
        # the group directory decides on EVERY request, so removing a member ends their access within a minute
        if not await asyncio.to_thread(self.s.authenticator.exists, identity):
            raise _unauthorized()
        # a sign-out voids every session issued before it (a session without an issue time cannot be proven newer)
        valid_after = await self.svc.sessions_valid_after(identity)
        if valid_after is not None and got.get("iat_ms", 0) <= valid_after:
            raise _unauthorized()
        return Actor(ActorKind.USER, identity)

    # -- request bodies

    @staticmethod
    def _query(query: str, allowed: set[str]) -> dict[str, str]:
        if not query:
            return {}
        try:
            pairs = parse_qs(query, keep_blank_values=True, strict_parsing=True, max_num_fields=8)
        except ValueError:
            raise _bad() from None
        if set(pairs) - allowed or any(len(v) != 1 for v in pairs.values()):
            raise _bad()
        return {k: v[0] for k, v in pairs.items()}

    @staticmethod
    def _no_dups(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for k, v in pairs:
            if k in out:
                raise ValueError("duplicate key")
            out[k] = v
        return out

    @staticmethod
    def _length(request: web.Request) -> int:
        if request.headers.getall("Transfer-Encoding", []):
            raise _bad()
        lengths = request.headers.getall("Content-Length", [])
        if len(lengths) > 1 or (lengths and not _DIGITS.fullmatch(lengths[0].strip())):
            raise _bad()
        return int(lengths[0]) if lengths else 0

    async def _read(self, request: web.Request, length: int) -> bytes:
        """A small (JSON) body, under the body deadline."""

        async def read() -> bytes:
            chunks: list[bytes] = []
            left = length
            while left:
                block = await request.content.read(min(READ_BLOCK, left))
                if not block:
                    raise _bad()
                chunks.append(block)
                left -= len(block)
            return b"".join(chunks)

        try:
            return await asyncio.wait_for(read(), self.s.limits.body_deadline_seconds)
        except TimeoutError:
            raise MemberError(408, "timeout", "the request took too long") from None

    @staticmethod
    async def _spool(request: web.Request, length: int, path: Path) -> str:
        """Stream a run body to ``path`` (never held in memory); returns its sha256."""
        digest, left = hashlib.sha256(), length
        with path.open("wb") as out:
            while left:
                block = await request.content.read(min(READ_BLOCK, left))
                if not block:
                    raise _bad()
                digest.update(block)
                out.write(block)
                left -= len(block)
        return digest.hexdigest()

    async def _body(self, request: web.Request, kind: str | None) -> dict[str, Any]:
        length = self._length(request)
        if length > MAX_JSON_BODY:
            raise MemberError(413, "too_large", "request body is too large")
        body: dict[str, Any] = {}
        if length:
            if request.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != _JSON:
                raise MemberError(415, "unsupported_media_type", "application/json required")
            data = await self._read(request, length)
            try:
                parsed = json.loads(data.decode("utf-8"), object_pairs_hook=self._no_dups)
            except (ValueError, UnicodeError, RecursionError):
                raise _bad() from None
            if not isinstance(parsed, dict):
                raise _bad()
            body = parsed
        if kind is None:
            if body:
                raise _bad()
            return body
        allowed, required = _BODY_KEYS[kind]
        if set(body) - allowed:
            raise _bad("unknown argument")
        if required - set(body):
            raise _bad("missing argument")
        return body

    # -- routing

    @staticmethod
    def _artifact(value: str) -> str:
        if not _ARTIFACT.fullmatch(value):
            raise _not_found()
        return value

    @staticmethod
    def _ident(value: str) -> str:
        if not _ID.fullmatch(value):
            raise _not_found()
        return value

    @staticmethod
    def _folder(value: str) -> str:
        if not _FOLDER.fullmatch(value):
            raise _not_found()
        return value

    @staticmethod
    def _str(body: dict[str, Any], key: str) -> str:
        value = body.get(key)
        if not isinstance(value, str):
            raise _bad(f"{key} must be a string")
        return value

    async def _route(
        self, request: web.Request, actor: Actor, method: str, rest: list[str], query: str
    ) -> web.StreamResponse:
        svc, n = self.svc, len(rest)
        ok = self._json
        if method in ("GET", "HEAD"):
            if rest == ["me"]:
                self._query(query, set())
                return ok(200, await svc.me(actor))
            if rest == ["library"]:
                params = self._query(query, {"q", "sources", "folder", "type", "limit", "offset"})
                return ok(200, await svc.library_page(actor, params))
            if rest == ["links"]:
                self._query(query, set())
                return ok(200, await svc.share_list(actor))
            if n == 2 and rest[0] == "artifacts":
                self._query(query, set())
                return ok(200, await svc.head(actor, self._artifact(rest[1])))
            if n == 3 and rest[0] == "artifacts" and rest[2] == "history":
                got = self._query(query, {"limit"})
                limit = got.get("limit", "50")
                if not _DIGITS.fullmatch(limit):
                    raise _bad()
                return ok(200, await svc.history(actor, self._artifact(rest[1]), int(limit)))
            if n == 3 and rest[0] == "artifacts" and rest[2] == "shares":
                self._query(query, set())
                aid = self._artifact(rest[1])
                await svc._owned(svc.pool, svc._member(actor), aid)
                return ok(200, await svc.share_list(actor, aid))
            if n == 5 and rest[0] == "artifacts" and rest[2] == "shares" and rest[4] == "events":
                self._query(query, set())
                return ok(
                    200,
                    await svc.share_events(actor, self._artifact(rest[1]), self._ident(rest[3])),
                )
            if n >= 4 and rest[0] == "artifacts" and rest[2] == "content":
                self._query(query, set())
                return await self._content(
                    request, actor, self._artifact(rest[1]), "/".join(rest[3:])
                )
            raise _not_found()
        if method == "DELETE":
            if n == 2 and rest[0] == "folders":
                folder = self._folder(rest[1])
                await self._body(request, None)
                return ok(200, await svc.folder_delete(actor, folder))
            if n == 4 and rest[0] == "folders" and rest[2] == "items":
                folder = self._folder(rest[1])
                await self._body(request, None)
                return ok(200, await svc.folder_unfile(actor, folder, self._artifact(rest[3])))
            raise _not_found()
        if method == "PUT":
            if n == 2 and rest[0] == "folders":
                folder = self._folder(rest[1])
                return ok(
                    200,
                    await svc.folder_rename(
                        actor, folder, self._str(await self._body(request, "folder"), "name")
                    ),
                )
            if n == 6 and rest[0] == "artifacts" and rest[2] == "uploads" and rest[4] == "runs":
                self._query(query, set())
                return await self._run(request, actor, rest)
            raise _not_found()
        return await self._post(request, actor, rest, query)

    async def _post(
        self, request: web.Request, actor: Actor, rest: list[str], query: str
    ) -> web.StreamResponse:
        svc, n = self.svc, len(rest)
        ok = self._json
        if rest == ["artifacts"]:
            b = await self._body(request, "create")
            key = b.get("idempotency_key")
            if key is not None and not isinstance(key, str):
                raise _bad()
            return ok(
                200,
                await svc.create(
                    actor,
                    self._str(b, "title"),
                    self._str(b, "filename"),
                    b.get("profile", "other") if isinstance(b.get("profile", "other"), str) else "",
                    key,
                ),
            )
        if rest == ["signout"]:
            await self._body(request, None)
            return ok(200, await svc.signout(actor))
        if rest == ["folders"]:
            return ok(
                200,
                await svc.folder_create(
                    actor, self._str(await self._body(request, "folder"), "name")
                ),
            )
        if n == 3 and rest[0] == "folders" and rest[2] == "items":
            folder = self._folder(rest[1])
            b = await self._body(request, "folder_item")
            return ok(
                200,
                await svc.folder_file(actor, folder, self._artifact(self._str(b, "artifact_id"))),
            )
        if n == 3 and rest[0] == "pending" and rest[2] in ("accept", "discard"):
            aid = self._artifact(rest[1])
            await self._body(request, None)
            return ok(200, await (svc.accept if rest[2] == "accept" else svc.discard)(actor, aid))
        if n == 3 and rest[0] == "artifacts":
            aid = self._artifact(rest[1])
            if rest[2] == "delete":
                return ok(
                    200,
                    await svc.delete(actor, aid, (await self._body(request, "delete"))["confirm"]),
                )
            if rest[2] == "publish":
                b = await self._body(request, "publish")
                return ok(
                    200,
                    await svc.publish(
                        actor,
                        aid,
                        base_version_id=b.get("base_version_id"),
                        files=b["files"],
                        message=b.get("message"),
                        entry=b.get("entry"),
                    ),
                )
            if rest[2] == "shares":
                return ok(
                    200,
                    await svc.share_create(actor, aid, await self._body(request, "share_create")),
                )
        if n == 4 and rest[0] == "artifacts" and rest[2:] == ["uploads", "status"]:
            aid = self._artifact(rest[1])
            b = await self._body(request, "status")
            return ok(
                200,
                await svc.upload_status(actor, aid, b["name"], b["size_bytes"], b["file_sha256"]),
            )
        if n == 5 and rest[0] == "artifacts" and rest[2] == "uploads" and rest[4] == "abort":
            aid = self._artifact(rest[1])
            b = await self._body(request, "status")
            return ok(
                200,
                await svc.upload_abort(
                    actor, aid, rest[3], b["name"], b["size_bytes"], b["file_sha256"]
                ),
            )
        if n == 5 and rest[0] == "artifacts" and rest[2] == "shares":
            aid, sid = self._artifact(rest[1]), self._ident(rest[3])
            if rest[4] == "approve":
                b = await self._body(request, "approve")
                return ok(
                    200,
                    await svc.share_approve(
                        actor, aid, sid, b["confirm"], b.get("idempotency_key")
                    ),
                )
            if rest[4] == "revoke":
                await self._body(request, None)
                return ok(200, await svc.share_revoke(actor, aid, sid))
        raise _not_found()

    async def _content(
        self, request: web.Request, actor: Actor, artifact_id: str, name: str
    ) -> web.StreamResponse:
        """Stream one file, with at most ``content_streams`` responses in flight (503 beyond that)."""
        from urllib.parse import unquote

        sha, ctype, disposition = await self.svc.content(
            actor, artifact_id, unquote(name, errors="strict")
        )
        base = name.rsplit("/", 1)[-1]
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", unquote(base))[:100] or "file"
        streams = self.svc.content_streams
        if streams.locked():
            raise MemberError(
                503, "busy", "too many downloads, retry shortly", {"Retry-After": "2"}
            )
        async with streams:
            handle, size = await asyncio.to_thread(self.svc.open_blob, sha)
            try:
                resp = web.StreamResponse(
                    status=200,
                    headers={
                        "Content-Type": ctype,
                        "Content-Length": str(size),
                        "Cache-Control": "no-store",
                        "X-Content-Type-Options": "nosniff",
                        "Referrer-Policy": "no-referrer",
                        "X-Frame-Options": "DENY",
                        "Server": "herdr-share",
                        "Content-Security-Policy": "default-src 'none'; sandbox",
                        "Content-Disposition": "inline"
                        if disposition == "inline"
                        else f'attachment; filename="{safe}"',
                    },
                )
                await resp.prepare(request)
                try:
                    if request.method != "HEAD":
                        deadline = self.s.limits.body_deadline_seconds
                        while block := await asyncio.to_thread(handle.read, READ_BLOCK):
                            await asyncio.wait_for(resp.write(block), deadline)
                    await resp.write_eof()
                except (
                    TimeoutError
                ):  # a reader that stopped reading: the headers are out, so only close
                    if request.transport is not None:
                        request.transport.close()
                    raise ConnectionResetError from None
                return resp
            finally:
                handle.close()

    async def _run(self, request: web.Request, actor: Actor, rest: list[str]) -> web.Response:
        """``PUT /artifacts/{id}/uploads/{upload_id}/runs/{first}``: every refusal below that can be decided from the
        headers happens before the first body byte is read."""
        artifact_id, upload_id, first_s = self._artifact(rest[1]), rest[3], rest[5]
        if not UPLOAD_ID_RE.fullmatch(upload_id) or not _DIGITS.fullmatch(first_s):
            raise _bad()
        lengths = request.headers.getall("Content-Length", [])
        if (
            request.headers.getall("Transfer-Encoding", [])
            or len(lengths) != 1
            or not _DIGITS.fullmatch(lengths[0].strip())
        ):
            raise _bad("Content-Length is required exactly once")
        length = int(lengths[0])
        if length > MAX_UPLOAD_BYTES:
            raise MemberError(413, "too_large", "run is too large")

        def once(name: str) -> str:
            values = request.headers.getall(name, [])
            if (
                len(values) != 1
                or not values[0].strip()
                or not values[0].isascii()
                or len(values[0]) > 1024
            ):
                raise _bad(f"{name} is required exactly once")
            return values[0].strip()

        from urllib.parse import unquote

        try:
            name = unquote(once("X-Name"), errors="strict")
        except UnicodeDecodeError:
            raise _bad("X-Name is not valid UTF-8") from None
        size_s = once("X-Size-Bytes")
        if not _DIGITS.fullmatch(size_s):
            raise _bad()
        size, first = int(size_s), int(first_s)
        if size > MAX_UPLOAD_BYTES:
            raise MemberError(413, "too_large", "file is too large")
        file_sha, run_sha = once("X-File-SHA256"), once("X-Run-SHA256")
        if not SHA_RE.fullmatch(file_sha) or not SHA_RE.fullmatch(run_sha):
            raise _bad()
        self.svc.precheck_run(length, size, first)
        result = await self.svc.upload_run(
            actor,
            artifact_id,
            upload_id,
            name,
            size,
            file_sha,
            first,
            run_sha,
            length,
            lambda path: self._spool(request, length, path),
        )
        return self._json(200, result)


def build_member_settings(
    *,
    secret: bytes,
    session_seconds: int,
    public_origin: str,
    cognito_origin: str,
    client_id: str,
    client_ids: list[str],
    pool_id: str,
    group: str,
    sync_dir: Path,
    sync_required: bool,
    member_static: Path,
    share_static: Path,
    limits: MemberLimits,
) -> MemberSettings:
    directory = SyncedDirectory(str(sync_dir), group)
    authenticator = CognitoAuthenticator(
        pool_id=pool_id,
        client_ids=client_ids,
        required_group=group,
        jwks_fetch=directory.jwks,
        group_lookup=synced_lookup(directory, sync_required),
    )
    files = {
        "/": (member_static / "member.html", _HTML),
        "/my/": (member_static / "member.html", _HTML),
        "/my/callback": (member_static / "member.html", _HTML),
        "/my/static/member.js": (member_static / "member.js", _JS),
        "/my/static/member.css": (member_static / "member.css", _CSS),
        "/my/static/lab-signin.js": (share_static / "lab-signin.js", _JS),
        "/my/static/tokens.css": (share_static / "tokens.css", _CSS),
        "/my/static/fonts/inter-latin-400.woff2": (
            share_static / "fonts" / "inter-latin-400.woff2",
            _FONT,
        ),
        "/my/static/fonts/inter-latin-600.woff2": (
            share_static / "fonts" / "inter-latin-600.woff2",
            _FONT,
        ),
    }
    assets = {url: (path.read_bytes(), ctype) for url, (path, ctype) in files.items()}
    scripts = " ".join(f"{public_origin}{u}" for u in assets if u.endswith(".js"))
    styles = " ".join(f"{public_origin}{u}" for u in assets if u.endswith(".css"))
    # Exact files, never 'self': the same origin serves member-uploaded bundles (/s/<token>/f/x.js), which a bare
    # 'self' would let an HTML-injection bug load as a script and drive this API.
    csp = (
        f"default-src 'none'; script-src {scripts}; style-src {styles}; img-src data:; font-src 'self'; "
        f"connect-src {public_origin}/api/member/ {cognito_origin}; frame-src 'none'; object-src 'none'; "
        "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    )
    return MemberSettings(
        authenticator=authenticator,
        secret=secret,
        session_seconds=session_seconds,
        public_origin=public_origin,
        cognito_origin=cognito_origin,
        client_id=client_id,
        group=group,
        assets=assets,
        page_csp=csp,
        limits=limits,
        claim_fallback=not sync_required,
        directory_present=lambda: (Path(sync_dir) / "members.json").is_file(),
    )

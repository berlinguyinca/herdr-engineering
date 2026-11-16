"""Persistence for artifact share links (spec 2026-10-30-artifact-share-links-design.md, "Data").

``ShareStore`` is built like ``ArtifactStore``: it owns one asyncpg connection and exposes async methods, so it can
sit behind ``SyncArtifactAdapter`` (``adapter("create", ...)``).  The state machine is pending -> live -> revoked;
there is no ``expired`` state: the worker sweep revokes an expired share with ``mark_revoked(actor="sweep")`` and
records an ``expired`` event.

Conventions:

* ``create``, ``mark_live`` and ``mark_revoked`` write their own ``created`` / ``approved`` / ``revoked`` audit event
  in the same transaction as the state change, so callers must NOT add those kinds again.  ``add_event`` is for the
  other kinds (``opened``, ``denied``, ``expired``).  A repeated ``mark_revoked`` is a no-op and adds no event.
* ``get`` and ``live_expired`` return the row INCLUDING ``token_hash`` (internal: the API/worker derive the
  ``by-token/<hash>`` projection path from it).  Every other method returns rows WITHOUT it.
* The token itself never reaches this module; ``new_token`` returns it once for the caller to show.
* ``ingest_events_file`` consumes the append-only ``events.jsonl`` written by the database-free (untrusted) share
  container.  The file is never truncated in place (the container may be appending concurrently); the worker rotates
  it with ``ingest_and_rotate_events`` (rename to ``.1``, drained and deleted on a later sweep).  A marker file records
  ``{offset, fingerprint}`` of the consumed prefix; the fingerprint hashes the first and last bytes of that prefix, so a
  file that was deleted/recreated (even with a reused inode) or rewritten is detected and re-read from 0.  Every
  inserted row carries ``ingest_key = sha256(line):<offset>`` under a unique index, so replays (crash before the marker
  write, or a restart from 0) never double-insert.  A final line without a newline is left for the next run.  Each line
  is inserted in its own savepoint and a line that cannot be stored is skipped AND consumed, so one poison line never
  wedges ingestion.  Only ``opened``/``denied`` are accepted from the file.  The marker defaults to
  ``<file>.offset``, but the events directory is shared with the container, so the worker should pass ``marker_path``
  inside a directory only it can write.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import re
import secrets
import time
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import asyncpg

from .artifact_members import MemberStore, is_member_principal
from .jsonl_ingest import JsonlIngestor, open_regular
from .jsonl_ingest import transaction as _transaction
from .share_bundle import BundleIndex

__all__ = ["ShareStore", "ShareError", "new_token"]

AUDIENCES = ("lab", "public")
EVENT_KINDS = ("created", "approved", "opened", "denied", "revoked", "expired", "annotated", "annotation_edited",
               "annotation_withdrawn", "annotation_dismissed", "annotation_addressed", "claimed")
PUBLIC_MIN_DAYS, PUBLIC_MAX_DAYS = 1, 90
# Callers compute "now + N days" slightly before create() reads the clock; tolerate that skew on the lower bound.
_SKEW = timedelta(minutes=5)
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
# ASCII only: ``str.lower`` folds e.g. U+212A KELVIN SIGN to "k", so a non-ASCII entry could match another address.
_EMAIL = re.compile(r"[!-?A-~]+@[!-?A-~]+\Z", re.ASCII)   # printable ASCII except "@" and space
_MAX_INGEST_BYTES = 16 * 1024 * 1024
_MAX_LINE_BYTES = 16 * 1024
_MAX_DETAIL_BYTES = 4096
_MAX_ACTOR_CHARS = 200
# The worker renames events.jsonl to events.jsonl.1 once this much of it is ingested (share-public stops appending at
# its own 8 MiB cap, so this must stay well under that).  The renamed file is deleted only once fully read AND not
# written for ROTATED_GRACE_SECONDS: share-public opens the path per append, so a writer that opened the old inode
# just before the rename can still land one line in it.
EVENTS_ROTATE_BYTES = 4 * 1024 * 1024
ROTATED_GRACE_SECONDS = 30.0
# The share container writes the events file, so it may only report what it observed; the lifecycle kinds
# (created/approved/revoked/expired) are written by this store alone.
INGESTIBLE_KINDS = ("opened", "denied", "claimed")
# ``claimed`` is the container's report that a visitor with a verified session opened the share.  It is only kept for an
# actor the ``members`` resolver of ``ingest_events_file`` maps to an approved member (the same rules as
# ``member_for_actor``); any other claim is dropped without a trace.

_COLUMNS = ("share_id, artifact_id, version_id, audience, allow_emails, allow_groups, state, entry, file_count, "
            "total_bytes, title, expires_at, created_by, created_at, approved_by, approved_at, revoked_at")


PENDING_NOT_ACCEPTED = "pending_not_accepted"      # an agent push the owner has not accepted yet is never shareable


class ShareError(ValueError):
    """A share request or transition that violates the rules (never a server fault).

    ``code`` is an optional machine-readable reason (``"expired"`` for a share whose expiry passed before approval).
    """

    def __init__(self, *args, code: str | None = None):
        super().__init__(*args)
        self.code = code


def new_token() -> tuple[str, str]:
    """A fresh 256-bit URL-safe token and its sha256 hex.  Only the hash is ever stored."""
    token = secrets.token_urlsafe(32)
    return token, hashlib.sha256(token.encode("ascii")).hexdigest()


def _row(record: Any, *, keep_hash: bool = False) -> dict:
    row = dict(record)
    row["allow_emails"] = list(row.get("allow_emails") or [])
    row["allow_groups"] = list(row.get("allow_groups") or [])
    if not keep_hash:
        row.pop("token_hash", None)
    return row


def _clean_list(values: Any, what: str, pattern: re.Pattern | None = None, lower: bool = False) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str) or not isinstance(values, (list, tuple, set)):
        raise ShareError(f"{what} must be a list")
    out: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise ShareError(f"{what} contains an empty entry")
        value = value.strip()
        if pattern is not None and not pattern.match(value):   # before lower(): it folds some non-ASCII to ASCII
            raise ShareError(f"{what}: {value!r} is not valid")
        if lower:
            value = value.lower()
        if value not in out:
            out.append(value)
    return out


def _member_actor(actor: str | None) -> dict:
    return {"kind": "member" if is_member_principal(actor) else "operator", "id": actor or "unknown"}


async def _may_share(conn: Any, artifact_id: str, principal: str) -> bool:
    """``ArtifactStore.report_permission(.., "share")``: the owner, or a live grant holding ``share`` or ``admin``."""
    artifact = await conn.fetchrow("SELECT created_by FROM artifacts WHERE artifact_id=$1", artifact_id)
    if artifact is None:
        return False
    if artifact["created_by"] == principal:
        return True
    grant = await conn.fetchrow(
        "SELECT permissions FROM artifact_acl WHERE artifact_id=$1 AND principal_id=$2 "
        "AND (expires_at IS NULL OR expires_at > now())", artifact_id, principal)
    if not grant:
        return False
    permissions = grant["permissions"]
    if isinstance(permissions, str):
        permissions = json.loads(permissions)
    return "share" in permissions or "admin" in permissions


_SPEND_LOCK_SQL = "SELECT pg_advisory_xact_lock(hashtext('artifact_share_live_bytes'))"


class _Held:
    """Lets ``LibraryStore`` run on the connection the ingest transaction already holds."""

    def __init__(self, conn: Any):
        self._conn = conn

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)

    def acquire(self):
        @asynccontextmanager
        async def held():
            yield self._conn
        return held()


class ShareStore:
    def __init__(self, conn: Any, *, lock_connect: Any = None):
        """``lock_connect`` (an async callable returning a fresh connection) lets ``acquire_spend`` hold the spend lock
        on a connection of its own, for a store that sits behind a shared adapter connection."""
        self._conn = conn
        self._lock_connect = lock_connect
        self._spend: tuple[Any, Any] | None = None

    @asynccontextmanager
    async def spend_lock(self):
        """Cross-process lock around reading the live-bytes total and spending it (``mark_live``).

        A transaction-scoped Postgres advisory lock: the API and the worker are different processes, so an in-process
        lock cannot serialise their approvals.  Released when the transaction ends.
        """
        async with _transaction(self._conn) as conn:
            await conn.execute(_SPEND_LOCK_SQL)
            yield

    async def acquire_spend(self) -> None:
        """The same lock as ``spend_lock`` for callers that cannot wrap the work in ``async with`` (a synchronous caller
        on another thread): held until ``release_spend``.  Not re-entrant."""
        if self._spend is not None:
            raise RuntimeError("the spend lock is already held by this store")
        conn = self._conn if self._lock_connect is None else await self._lock_connect()
        tx = conn.transaction()
        try:
            await tx.start()
            await conn.execute(_SPEND_LOCK_SQL)
        except BaseException:
            await self._end_spend(conn, tx)
            raise
        self._spend = (conn, tx)

    async def release_spend(self) -> None:
        """End the transaction ``acquire_spend`` began.  Commit, so audit rows written meanwhile survive."""
        held, self._spend = self._spend, None
        if held is not None:
            await self._end_spend(*held)

    async def _end_spend(self, conn: Any, tx: Any) -> None:
        try:
            try:
                await tx.commit()
            except BaseException:
                with contextlib.suppress(Exception):
                    await tx.rollback()
                raise           # a commit that failed means nothing was approved: never report success
        finally:
            if conn is not self._conn:
                await conn.close()

    async def _member_event(self, conn: Any, principal: str, action: str, *, actor: str | None, artifact_id: str | None,
                            share_id: str | None, audience: str | None, outcome: str, code: str | None = None,
                            detail: dict | None = None) -> None:
        await MemberStore(conn).record(
            principal, action, actor=_member_actor(actor), artifact_id=artifact_id, share_id=share_id,
            audience=audience if audience in AUDIENCES else None, outcome=outcome, code=code, detail=detail)

    # ----------------------------------------------------------------- shares
    async def create(self, *, artifact_id: str, version_id: str, audience: str,
                     allow_emails: list[str] | None = None, allow_groups: list[str] | None = None,
                     expires_at: datetime | None = None, entry: str | None = None,
                     title: str | None = None, created_by: str | None = None,
                     member_groups: frozenset[str] | None = None,
                     member_emails: frozenset[str] | None = None) -> dict:
        """Create a ``pending`` share after validating the audience rules; returns the row (no token).

        A ``member:`` creator additionally needs the ``share`` permission on the artifact (else ``not_found``) and, for a
        lab share, an allow-list inside ``member_groups`` / ``member_emails`` (``None`` refuses: fail closed; the caller
        supplies them, nothing is merged in here).  Every member outcome writes an ``artifact_member_events`` row.
        """
        if isinstance(created_by, str) and created_by.startswith(("collab:", "member:")) \
                and not is_member_principal(created_by):
            # collab principals never create shares; a malformed "member:" string must not slip into the operator class
            # (``live_total_bytes`` splits on the SQL prefix ``member:%``, so the two definitions must not disagree).
            raise ShareError("not found", code="not_found")
        if not is_member_principal(created_by):
            return await self._create(artifact_id, version_id, audience, allow_emails, allow_groups, expires_at, entry,
                                      title, created_by, None, None)
        try:
            return await self._create(artifact_id, version_id, audience, allow_emails, allow_groups, expires_at, entry,
                                      title, created_by, member_groups, member_emails)
        except ShareError as exc:
            await self._member_event(self._conn, created_by, "share_create", actor=created_by,
                                     artifact_id=artifact_id, share_id=None, audience=audience, outcome="refused",
                                     code=exc.code or "invalid", detail={"reason": str(exc)})
            raise

    async def _create(self, artifact_id, version_id, audience, allow_emails, allow_groups, expires_at, entry, title,
                      created_by, member_groups, member_emails) -> dict:
        member = is_member_principal(created_by)
        if member:
            for name, scope in (("member_groups", member_groups), ("member_emails", member_emails)):
                if isinstance(scope, (str, bytes)):
                    raise TypeError(f"{name} must be a collection of strings, not a bare string")
            if member_emails is not None:
                member_emails = frozenset(e.strip().lower() for e in member_emails)
        if audience not in AUDIENCES:
            raise ShareError("audience must be 'lab' or 'public'")
        emails = _clean_list(allow_emails, "allow_emails", _EMAIL, lower=True)
        groups = _clean_list(allow_groups, "allow_groups")
        now = datetime.now(UTC)
        if expires_at is not None and (not isinstance(expires_at, datetime) or expires_at.tzinfo is None):
            raise ShareError("expires_at must be a timezone-aware datetime")
        if audience == "lab":
            if not emails and not groups:
                raise ShareError("a lab share needs at least one allowed email or group")
            if expires_at is not None and expires_at <= now:
                raise ShareError("expires_at must be in the future")
        else:
            if emails or groups:
                raise ShareError("a public share cannot carry an allow-list")
            if expires_at is None:
                raise ShareError("a public share must expire")
            if not (now + timedelta(days=PUBLIC_MIN_DAYS) - _SKEW <= expires_at
                    <= now + timedelta(days=PUBLIC_MAX_DAYS)):
                raise ShareError(f"a public share must expire within {PUBLIC_MIN_DAYS}-{PUBLIC_MAX_DAYS} days")
        share_id = "share_" + secrets.token_hex(12)
        async with _transaction(self._conn) as conn:
            if member:
                # The usage row lock serialises this insert with the operator's disable-sharing (which updates that
                # row, then lists open shares): a link is either refused here or visible to that revocation.
                from .artifact_member_quota import _lock_usage
                await _lock_usage(conn, created_by)
                if await conn.fetchval("SELECT sharing_disabled FROM artifact_member_usage WHERE member_principal=$1",
                                       created_by):
                    raise ShareError("sharing is disabled for this member", code="sharing_disabled")
                if not await _may_share(conn, artifact_id, created_by):
                    raise ShareError("not found", code="not_found")
                if audience == "lab":
                    if (member_groups is None or member_emails is None or not set(groups) <= set(member_groups)
                            or not set(emails) <= set(member_emails)):
                        raise ShareError("the audience is outside what this member may share with",
                                         code="member_audience_refused")
            if await conn.fetchval("SELECT intake FROM artifacts WHERE artifact_id=$1", artifact_id) == "pending":
                raise ShareError("an agent push must be accepted before it can be shared", code=PENDING_NOT_ACCEPTED)
            if await conn.fetchval("SELECT 1 FROM artifact_versions WHERE version_id=$1 AND artifact_id=$2",
                                   version_id, artifact_id) is None:
                raise ShareError("version does not belong to that artifact")
            record = await conn.fetchrow(
                "INSERT INTO artifact_shares (share_id, artifact_id, version_id, audience, allow_emails, "
                "allow_groups, entry, title, expires_at, created_by) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) "
                f"RETURNING {_COLUMNS}",
                share_id, artifact_id, version_id, audience, emails, groups, entry, title, expires_at, created_by)
            await self._event(conn, share_id, "created", created_by or "unknown", {"audience": audience})
            if member:
                await self._member_event(conn, created_by, "share_create", actor=created_by, artifact_id=artifact_id,
                                         share_id=share_id, audience=audience, outcome="ok")
        return _row(record)

    async def get(self, share_id: str) -> dict | None:
        """The row including ``token_hash`` (internal use), or ``None``."""
        record = await self._conn.fetchrow(
            f"SELECT {_COLUMNS}, token_hash FROM artifact_shares WHERE share_id=$1", share_id)
        return _row(record, keep_hash=True) if record else None

    async def list_for_artifact(self, artifact_id: str) -> list[dict]:
        """Newest first; never includes ``token_hash``."""
        records = await self._conn.fetch(
            f"SELECT {_COLUMNS} FROM artifact_shares WHERE artifact_id=$1 ORDER BY created_at DESC, share_id",
            artifact_id)
        return [_row(r) for r in records]

    async def mark_live(self, share_id: str, *, token_hash: str, index: BundleIndex, approved_by: str) -> dict:
        """pending -> live, recording the bundle summary and the token hash.  Refused for any other state.

        For a member-created share the creator's ``share`` permission is re-checked (a grant revoked since create
        refuses with ``not_found`` and the share stays pending); the outcome is a member event either way.
        """
        if not isinstance(token_hash, str) or not _HEX64.match(token_hash):
            raise ShareError("token_hash must be 64 lowercase hex characters")
        seen = await self._conn.fetchrow("SELECT created_by, artifact_id, audience FROM artifact_shares "
                                         "WHERE share_id=$1", share_id)
        member = seen is not None and is_member_principal(seen["created_by"])
        try:
            return await self._mark_live(share_id, token_hash, index, approved_by, member)
        except ShareError as exc:
            if member:
                await self._member_event(self._conn, seen["created_by"], "share_approve", actor=approved_by,
                                         artifact_id=seen["artifact_id"], share_id=share_id,
                                         audience=seen["audience"], outcome="refused", code=exc.code or "invalid",
                                         detail={"reason": str(exc)})
            raise

    async def _mark_live(self, share_id: str, token_hash: str, index: BundleIndex, approved_by: str,
                         member: bool) -> dict:
        async with _transaction(self._conn) as conn:
            # Lock order: the artifact row first (FOR SHARE), then the share row. Purge holds the artifact row FOR UPDATE
            # and then rewrites the artifact's share rows, so taking them the other way round could deadlock.
            # FOR SHARE: a concurrent delete (which locks the artifact row FOR UPDATE) either finished first, so the link
            # is refused here, or waits for this commit and then revokes the link it finds live
            owner_artifact = await conn.fetchval("SELECT artifact_id FROM artifact_shares WHERE share_id=$1", share_id)
            lifecycle = intake = None
            if owner_artifact is not None:
                found = await conn.fetchrow("SELECT lifecycle, intake FROM artifacts WHERE artifact_id=$1 FOR SHARE",
                                            owner_artifact)
                lifecycle, intake = (found["lifecycle"], found["intake"]) if found else (None, None)
            current = await conn.fetchrow("SELECT state, expires_at, created_by, artifact_id, audience FROM "
                                          "artifact_shares WHERE share_id=$1 FOR UPDATE", share_id)
            if current is None:
                raise ShareError("unknown share")
            if member and not await _may_share(conn, current["artifact_id"], current["created_by"]):
                raise ShareError("not found", code="not_found")
            if current["state"] != "pending":
                raise ShareError(f"share is {current['state']}, not pending")
            if lifecycle is not None and lifecycle != "active":
                raise ShareError("artifact is not active")
            if intake == "pending":
                raise ShareError("an agent push must be accepted before it can be shared", code=PENDING_NOT_ACCEPTED)
            if current["expires_at"] is not None and current["expires_at"] <= datetime.now(UTC):
                raise ShareError("share has already expired", code="expired")
            if await conn.fetchval("SELECT 1 FROM artifact_shares WHERE token_hash=$1", token_hash) is not None:
                raise ShareError("token hash already in use")
            try:
                record = await conn.fetchrow(
                    "UPDATE artifact_shares SET state='live', token_hash=$2, entry=$3, file_count=$4, "
                    f"total_bytes=$5, approved_by=$6, approved_at=now() WHERE share_id=$1 RETURNING {_COLUMNS}",
                    share_id, token_hash, index.entry, len(index.files), index.total_bytes, approved_by)
            except asyncpg.UniqueViolationError as exc:
                raise ShareError("token hash already in use") from exc
            await self._event(conn, share_id, "approved", approved_by,
                              {"file_count": len(index.files), "total_bytes": index.total_bytes})
            if member:
                await self._member_event(conn, current["created_by"], "share_approve", actor=approved_by,
                                         artifact_id=current["artifact_id"], share_id=share_id,
                                         audience=current["audience"], outcome="ok",
                                         detail={"total_bytes": index.total_bytes})
        return _row(record)

    async def mark_revoked(self, share_id: str, *, actor: str) -> dict:
        """pending|live -> revoked.  Revoking a revoked share returns it unchanged and adds no event."""
        async with _transaction(self._conn) as conn:
            current = await conn.fetchrow(f"SELECT {_COLUMNS} FROM artifact_shares WHERE share_id=$1 FOR UPDATE",
                                          share_id)
            if current is None:
                raise ShareError("unknown share")
            if current["state"] == "revoked":
                return _row(current)
            record = await conn.fetchrow(
                f"UPDATE artifact_shares SET state='revoked', revoked_at=now() WHERE share_id=$1 RETURNING {_COLUMNS}",
                share_id)
            await self._event(conn, share_id, "revoked", actor, {"was": current["state"]})
            if is_member_principal(current["created_by"]):
                await self._member_event(conn, current["created_by"], "share_revoke", actor=actor,
                                         artifact_id=current["artifact_id"], share_id=share_id,
                                         audience=current["audience"], outcome="ok", detail={"was": current["state"]})
        return _row(record)

    async def live_expired(self, now: datetime) -> list[dict]:
        """Live shares whose expiry has passed, WITH ``token_hash`` (the sweep needs the projection path)."""
        records = await self._conn.fetch(
            f"SELECT {_COLUMNS}, token_hash FROM artifact_shares WHERE state='live' AND expires_at IS NOT NULL "
            "AND expires_at <= $1 ORDER BY expires_at, share_id", now)
        return [_row(r, keep_hash=True) for r in records]

    async def live_token_hashes(self) -> set[str]:
        """Token hashes of every live share (the orphan sweep keeps exactly these ``by-token`` directories)."""
        records = await self._conn.fetch("SELECT token_hash FROM artifact_shares WHERE state='live' "
                                         "AND token_hash IS NOT NULL")
        return {r["token_hash"] for r in records}

    async def known_share_states(self) -> dict[str, str]:
        """``{share_id: state}`` for every share (the orphan sweep keeps bundles of live and pending shares)."""
        records = await self._conn.fetch("SELECT share_id, state FROM artifact_shares")
        return {r["share_id"]: r["state"] for r in records}

    async def live_total_bytes(self, *, member: bool) -> int:
        """Bytes of live shares created by members (``member=True``) or by everyone else (operators, ``False``)."""
        return int(await self._conn.fetchval(
            "SELECT COALESCE(SUM(total_bytes), 0) FROM artifact_shares WHERE state='live' "
            "AND COALESCE(created_by LIKE 'member:%', false) = $1", member))

    # ----------------------------------------------------------------- events
    async def _event(self, conn: Any, share_id: str, kind: str, actor: str, detail: dict | None,
                     at: datetime | None = None, ingest_key: str | None = None) -> bool:
        status = await conn.execute(
            "INSERT INTO artifact_share_events (share_id, at, kind, actor, detail, ingest_key) "
            "SELECT $1, COALESCE($2, now()), $3, $4, $5::jsonb, $6 "
            "WHERE EXISTS (SELECT 1 FROM artifact_shares WHERE share_id=$1) ON CONFLICT DO NOTHING",
            share_id, at, kind, actor, json.dumps(detail or {}), ingest_key)
        return status.endswith(" 1")

    async def add_event(self, share_id: str, kind: str, actor: str, detail: dict | None = None) -> None:
        if kind not in EVENT_KINDS:
            raise ShareError(f"unknown event kind {kind!r}")
        if not isinstance(actor, str) or not actor:
            raise ShareError("actor is required")
        if not await self._event(self._conn, share_id, kind, actor, detail):
            raise ShareError("unknown share")

    async def events(self, share_id: str, limit: int = 200) -> list[dict]:
        """Oldest first, at most ``limit`` rows (detail decoded)."""
        records = await self._conn.fetch(
            "SELECT share_id, at, kind, actor, detail::text AS detail FROM artifact_share_events "
            "WHERE share_id=$1 ORDER BY at, event_id LIMIT $2", share_id, max(1, int(limit)))
        return [{**dict(r), "detail": json.loads(r["detail"])} for r in records]

    async def ingest_events_file(self, path: Path, *, marker_path: Path | None = None,
                                 members: Callable[[str], str | None] | None = None) -> int:
        """Ingest complete JSON lines appended to ``path`` since the last run; returns rows inserted.

        See the module docstring for the offset/fingerprint/idempotency design and why ``marker_path`` exists.
        Invalid lines (bad JSON, NUL/surrogates, forbidden kind, missing fields, unknown share_id, oversize) are
        skipped but consumed.  A line of at least ``_MAX_INGEST_BYTES`` without a newline is skipped through its end.
        Only a regular file is read: a symlink or FIFO planted in the (public-writable) events directory is ignored.
        """
        path = Path(path)
        marker = Path(marker_path) if marker_path is not None else path.with_name(path.name + ".offset")
        return await self._ingestor(members).ingest_file(path, marker)

    async def ingest_and_rotate_events(self, path: Path, *, marker_path: Path | None = None,
                                       rotate_bytes: int | None = None, now: float | None = None,
                                       grace: float | None = None,
                                       members: Callable[[str], str | None] | None = None) -> int:
        """Ingest ``path`` (and a pending ``path.1``) and rotate ``path`` once ``rotate_bytes`` of it are consumed.

        The mechanics (leftover ``.1`` drain, rename, marker move, crash safety) live in
        ``JsonlIngestor.ingest_and_rotate``.  Returns rows inserted.
        """
        path = Path(path)
        marker = Path(marker_path) if marker_path is not None else path.with_name(path.name + ".offset")
        return await self._ingestor(members).ingest_and_rotate(
            path, marker,
            rotate_bytes=EVENTS_ROTATE_BYTES if rotate_bytes is None else rotate_bytes,
            now=time.time() if now is None else now,
            grace=ROTATED_GRACE_SECONDS if grace is None else grace)

    def _ingestor(self, members: Callable[[str], str | None] | None = None) -> JsonlIngestor:
        async def store(conn: Any, parsed: tuple, key: str) -> bool:
            share_id, kind, actor, at, detail = parsed
            if kind == "claimed":
                return await self._claim(conn, members, share_id, actor, at, key)
            return await self._event(conn, share_id, kind, actor, detail, at, key)
        # built per call so the module limit and ``_open_regular`` are read at call time
        return JsonlIngestor(self._conn, parse_line=self._parse_line, store=store, max_line_bytes=_MAX_LINE_BYTES,
                             max_ingest_bytes=_MAX_INGEST_BYTES, opener=self._open_regular)

    async def _claim(self, conn: Any, members: Callable[[str], str | None] | None, share_id: str, actor: str,
                     at: datetime, key: str) -> bool:
        """Store a ``claimed`` event and keep the share in the member's library.

        ``members`` maps the verified actor to a member id (None: not an approved member, or no resolver).  Such a claim
        is dropped (False), never an error; so is a claim on a share that is not live (the event is still kept as an
        audit row, the library is not touched).  Replays are idempotent: ``ingest_key`` and the claims primary key."""
        member = members(actor) if members is not None else None
        if not isinstance(member, str) or not member:
            return False
        stored = await self._event(conn, share_id, "claimed", actor, {"share_id": share_id}, at, key)
        if stored:
            from .artifact_library import LibraryError, LibraryStore
            try:
                await LibraryStore(_Held(conn)).record_claim(member, share_id)
            except LibraryError:
                pass
        return stored

    @staticmethod
    def _open_regular(path: Path):
        return open_regular(path)

    @staticmethod
    def _clean(value: Any) -> bool:
        """True when no string in ``value`` (keys included) holds NUL or a surrogate (Postgres/UTF-8 reject them)."""
        stack = [value]
        while stack:
            item = stack.pop()
            if isinstance(item, str):
                if any(c == "\x00" or "\ud800" <= c <= "\udfff" for c in item):
                    return False
            elif isinstance(item, dict):
                stack.extend(item.keys())
                stack.extend(item.values())
            elif isinstance(item, list):
                stack.extend(item)
        return True

    @classmethod
    def _parse_line(cls, raw: bytes):
        if len(raw) > _MAX_LINE_BYTES:
            return None
        try:
            obj = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(obj, dict) or not cls._clean(obj):
            return None
        share_id, kind, actor = obj.get("share_id"), obj.get("kind"), obj.get("actor")
        if not (isinstance(share_id, str) and share_id and kind in INGESTIBLE_KINDS
                and isinstance(actor, str) and actor and isinstance(obj.get("at"), str)):
            return None
        try:
            at = datetime.fromisoformat(obj["at"])
        except ValueError:
            return None
        if at.tzinfo is None:
            at = at.replace(tzinfo=UTC)
        detail = obj.get("detail")
        if not isinstance(detail, dict) or len(json.dumps(detail)) > _MAX_DETAIL_BYTES:
            detail = {}
        return share_id[:200], kind, actor[:_MAX_ACTOR_CHARS], at, detail

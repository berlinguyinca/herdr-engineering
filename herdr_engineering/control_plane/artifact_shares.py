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
  container.  The file is never truncated (the container may be appending concurrently).  A marker file records
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

import hashlib
import json
import os
import re
import secrets
import tempfile
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import asyncpg

from .share_bundle import BundleIndex

__all__ = ["ShareStore", "ShareError", "new_token"]

AUDIENCES = ("lab", "public")
EVENT_KINDS = ("created", "approved", "opened", "denied", "revoked", "expired")
PUBLIC_MIN_DAYS, PUBLIC_MAX_DAYS = 1, 90
# Callers compute "now + N days" slightly before create() reads the clock; tolerate that skew on the lower bound.
_SKEW = timedelta(minutes=5)
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\Z")
_MAX_INGEST_BYTES = 16 * 1024 * 1024
_MAX_LINE_BYTES = 16 * 1024
_MAX_DETAIL_BYTES = 4096
_MAX_ACTOR_CHARS = 200
_FINGERPRINT_BYTES = 64
# The share container writes the events file, so it may only report what it observed; the lifecycle kinds
# (created/approved/revoked/expired) are written by this store alone.
INGESTIBLE_KINDS = ("opened", "denied")

_COLUMNS = ("share_id, artifact_id, version_id, audience, allow_emails, allow_groups, state, entry, file_count, "
            "total_bytes, title, expires_at, created_by, created_at, approved_by, approved_at, revoked_at")


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


@asynccontextmanager
async def _transaction(conn: Any):
    transaction = getattr(conn, "transaction", None)
    if transaction is None:
        yield conn
        return
    async with transaction():
        yield conn


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
        value = value.strip().lower() if lower else value.strip()
        if pattern is not None and not pattern.match(value):
            raise ShareError(f"{what}: {value!r} is not valid")
        if value not in out:
            out.append(value)
    return out


class ShareStore:
    def __init__(self, conn: Any):
        self._conn = conn

    # ----------------------------------------------------------------- shares
    async def create(self, *, artifact_id: str, version_id: str, audience: str,
                     allow_emails: list[str] | None = None, allow_groups: list[str] | None = None,
                     expires_at: datetime | None = None, entry: str | None = None,
                     title: str | None = None, created_by: str | None = None) -> dict:
        """Create a ``pending`` share after validating the audience rules; returns the row (no token)."""
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
            if await conn.fetchval("SELECT 1 FROM artifact_versions WHERE version_id=$1 AND artifact_id=$2",
                                   version_id, artifact_id) is None:
                raise ShareError("version does not belong to that artifact")
            record = await conn.fetchrow(
                "INSERT INTO artifact_shares (share_id, artifact_id, version_id, audience, allow_emails, "
                "allow_groups, entry, title, expires_at, created_by) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) "
                f"RETURNING {_COLUMNS}",
                share_id, artifact_id, version_id, audience, emails, groups, entry, title, expires_at, created_by)
            await self._event(conn, share_id, "created", created_by or "unknown", {"audience": audience})
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
        """pending -> live, recording the bundle summary and the token hash.  Refused for any other state."""
        if not isinstance(token_hash, str) or not _HEX64.match(token_hash):
            raise ShareError("token_hash must be 64 lowercase hex characters")
        async with _transaction(self._conn) as conn:
            current = await conn.fetchrow("SELECT state, expires_at FROM artifact_shares WHERE share_id=$1 FOR UPDATE",
                                          share_id)
            if current is None:
                raise ShareError("unknown share")
            if current["state"] != "pending":
                raise ShareError(f"share is {current['state']}, not pending")
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
        return _row(record)

    async def live_expired(self, now: datetime) -> list[dict]:
        """Live shares whose expiry has passed, WITH ``token_hash`` (the sweep needs the projection path)."""
        records = await self._conn.fetch(
            f"SELECT {_COLUMNS}, token_hash FROM artifact_shares WHERE state='live' AND expires_at IS NOT NULL "
            "AND expires_at <= $1 ORDER BY expires_at, share_id", now)
        return [_row(r, keep_hash=True) for r in records]

    async def live_total_bytes(self) -> int:
        return int(await self._conn.fetchval(
            "SELECT COALESCE(SUM(total_bytes), 0) FROM artifact_shares WHERE state='live'"))

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

    async def ingest_events_file(self, path: Path, *, marker_path: Path | None = None) -> int:
        """Ingest complete JSON lines appended to ``path`` since the last run; returns rows inserted.

        See the module docstring for the offset/fingerprint/idempotency design and why ``marker_path`` exists.
        Invalid lines (bad JSON, NUL/surrogates, forbidden kind, missing fields, unknown share_id, oversize) are
        skipped but consumed.  A line of at least ``_MAX_INGEST_BYTES`` without a newline is skipped through its end.
        """
        path = Path(path)
        marker = Path(marker_path) if marker_path is not None else path.with_name(path.name + ".offset")
        try:
            handle = path.open("rb")
        except FileNotFoundError:
            return 0
        with handle:
            size = os.fstat(handle.fileno()).st_size
            offset, skipping = self._read_marker(marker, handle, size)
            handle.seek(offset)
            chunk = handle.read(_MAX_INGEST_BYTES)
            full = len(chunk) >= _MAX_INGEST_BYTES
            consumed = 0
            inserted = 0
            if skipping:  # discard the rest of an oversize line
                newline = chunk.find(b"\n")
                if newline < 0:
                    consumed, skipping = len(chunk), True
                else:
                    consumed, skipping = newline + 1, False
            start = consumed
            end = chunk.rfind(b"\n") + 1  # only complete lines; a torn tail waits for its newline
            if end > start:
                async with _transaction(self._conn) as conn:
                    position = offset + start
                    for raw in chunk[start:end].splitlines(keepends=True):
                        parsed = self._parse_line(raw)
                        if parsed is not None:
                            share_id, kind, actor, at, detail = parsed
                            key = hashlib.sha256(raw).hexdigest() + f":{position}"
                            try:
                                async with _transaction(conn):
                                    if await self._event(conn, share_id, kind, actor, detail, at, key):
                                        inserted += 1
                            except (asyncpg.PostgresError, UnicodeError, ValueError):
                                pass  # poison line: skipped, but consumed below
                        position += len(raw)
                consumed = end
            elif end == 0 and start == 0 and full:  # one huge line with no newline: skip it
                consumed, skipping = len(chunk), True
            if consumed == 0 and not skipping:
                return 0
            new_offset = offset + consumed
            self._write_marker(marker, handle, new_offset, skipping)
        return inserted

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

    @staticmethod
    def _fingerprint(handle: Any, offset: int) -> str:
        """sha256 over the first and last ``_FINGERPRINT_BYTES`` of the consumed prefix ``[0, offset)``."""
        n = min(_FINGERPRINT_BYTES, offset)
        handle.seek(0)
        first = handle.read(n)
        handle.seek(offset - n)
        last = handle.read(n)
        return hashlib.sha256(first + b"|" + last).hexdigest()

    @classmethod
    def _read_marker(cls, marker: Path, handle: Any, size: int) -> tuple[int, bool]:
        try:
            data = json.loads(marker.read_text())
            offset, skipping = data["offset"], bool(data.get("skipping", False))
            if not isinstance(offset, int) or offset < 0 or offset > size:
                return 0, False
            if offset == 0 or cls._fingerprint(handle, offset) == data.get("fp"):
                return offset, skipping
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            pass
        return 0, False

    @classmethod
    def _write_marker(cls, marker: Path, handle: Any, offset: int, skipping: bool) -> None:
        fp = cls._fingerprint(handle, offset) if offset else ""
        fd, tmp = tempfile.mkstemp(prefix=marker.name + ".", suffix=".tmp", dir=marker.parent)
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump({"offset": offset, "fp": fp, "skipping": skipping}, fh)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, marker)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

"""Database access of the public share surface: share state by token digest, audit events and lab annotations.

Every request loads the share fresh (``load_share``), so a revoke or an expiry is seen by the very next request. The
token is only ever looked up by its sha256 digest; the token itself is never stored, logged or put in an event. Annotation
writes re-check the share inside one transaction (the worker-side ingest rules of the control plane now run in the
request).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import asyncpg

from .annotation_anchor import ANNOTATION_ID_RE
from .share_bundle import ShareBundleError, choose_entry, normalise_name
from .share_lab import still_admitted

SHARED_BY_MAX = 120
FALLBACK_SHARER = "a lab member"
OWN_ROWS_MAX = 500
_SHARE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_DEAD_LIFECYCLES = frozenset({"retracted", "tombstoned"})

_LOAD_SQL = """
SELECT s.share_id, s.artifact_id, s.version_id, s.audience, s.allow_emails, s.allow_groups, s.state, s.entry, s.title,
       s.expires_at, s.created_by, a.lifecycle, a.intake, v.state AS version_state, v.output_manifest::text AS manifest,
       u.display_name
FROM shares s
JOIN artifacts a ON a.artifact_id = s.artifact_id
JOIN artifact_versions v ON v.version_id = s.version_id AND v.artifact_id = s.artifact_id
LEFT JOIN member_usage u ON u.member_id = s.created_by
WHERE s.token_hash = $1
"""


class ShareState(dict[str, Any]):
    """A validated, live share; ``index`` maps each normalised file name to ``{name, sha256, size}``."""

    index: dict[str, dict[str, Any]]
    artifact_id: str
    version_id: str


@dataclass(frozen=True)
class Loaded:
    """``state`` when the share is live and well formed; else None with a fixed ``reason`` (and ``share_id`` when the
    token resolved to a row, so the denial can be audited)."""

    state: ShareState | None
    reason: str
    share_id: str | None


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def build_index(manifest: object) -> dict[str, dict[str, Any]]:
    if not isinstance(manifest, list):
        raise ValueError("manifest is not a list")
    index: dict[str, dict[str, Any]] = {}
    for item in manifest:
        if not isinstance(item, dict):
            raise ValueError("manifest entry is not an object")
        name, sha, size = item.get("name"), item.get("sha256"), item.get("size_bytes")
        if not isinstance(sha, str) or not _SHA256.fullmatch(sha):
            raise ValueError("manifest entry has no sha256")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise ValueError("manifest entry has no size")
        name = normalise_name(name)
        if name in index:
            raise ValueError("duplicate manifest entry")
        index[name] = {"name": name, "sha256": sha, "size": size}
    return index


def _shared_by(row: asyncpg.Record) -> str:
    for candidate in (row["display_name"], row["created_by"]):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()[:SHARED_BY_MAX]
    return FALLBACK_SHARER


def _prepare(row: asyncpg.Record, now: float) -> tuple[ShareState | None, str]:
    if (
        row["state"] != "live"
        or row["lifecycle"] in _DEAD_LIFECYCLES
        or row["intake"] != "accepted"
        or row["version_state"] != "ready"
    ):
        return None, "not_live"
    expires: datetime | None = row["expires_at"]
    if expires is None and row["audience"] == "public":
        return None, "invalid_state"  # public shares always expire: fail closed
    if expires is not None and now >= expires.timestamp():
        return None, "expired"
    share_id = row["share_id"]
    if not _SHARE_ID.fullmatch(share_id) or row["audience"] not in ("public", "lab"):
        return None, "invalid_state"
    try:
        index = build_index(json.loads(row["manifest"]))
        entry = normalise_name(row["entry"]) if row["entry"] else choose_entry(None, sorted(index))
    except (ValueError, ShareBundleError, TypeError):
        return None, "invalid_state"
    if entry not in index:
        return None, "invalid_state"
    state = ShareState(
        share_id=share_id,
        audience=row["audience"],
        allow_emails=list(row["allow_emails"]),
        allow_groups=list(row["allow_groups"]),
        entry=entry,
        title=row["title"],
        expires=expires.timestamp() if expires is not None else None,
        shared_by=_shared_by(row),
    )
    state.index = index
    state.artifact_id = row["artifact_id"]
    state.version_id = row["version_id"]
    return state, ""


async def load_share(pool: asyncpg.Pool, token: str, now: float) -> Loaded:
    """The share for ``token`` (looked up by digest), judged against ``now`` and the CURRENT database rows."""
    row = await pool.fetchrow(_LOAD_SQL, token_digest(token))
    if row is None:
        return Loaded(None, "unknown_token", None)
    state, reason = _prepare(row, now)
    return Loaded(state, reason, row["share_id"])


async def record_event(
    pool: asyncpg.Pool, share_id: str, kind: str, actor: str, detail: dict[str, Any]
) -> None:
    await pool.execute(
        "INSERT INTO share_events (share_id, kind, actor, detail) VALUES ($1, $2, $3, $4::jsonb)",
        share_id,
        kind,
        actor[:200],
        json.dumps(detail, sort_keys=True, ensure_ascii=True),
    )


# ----------------------------------------------------------------------------- lab annotations


@dataclass(frozen=True)
class AnnotationLimits:
    per_user_per_day: int
    max_per_share: int


@dataclass(frozen=True)
class Written:
    """Outcome of an annotation write: ``ok`` or a fixed code (``not_live``, ``not_on_list``, ``quota``, ``not_editable``,
    ``invalid``)."""

    outcome: str
    created_at: datetime | None = None


_SHARE_LOCK_SQL = """
SELECT s.state, s.audience, s.allow_emails, s.allow_groups, s.expires_at, s.artifact_id, s.version_id,
       a.lifecycle, a.intake, v.state AS version_state
FROM shares s
JOIN artifacts a ON a.artifact_id = s.artifact_id
JOIN artifact_versions v ON v.version_id = s.version_id AND v.artifact_id = s.artifact_id
WHERE s.share_id = $1 FOR SHARE OF s
"""


async def _lock_live_lab_share(
    conn: asyncpg.Connection, share_id: str, email: str, basis: str
) -> asyncpg.Record | str:
    """The share row (shared-locked for the transaction) when it is still live, unexpired and admits ``email``; else a code."""
    row = await conn.fetchrow(_SHARE_LOCK_SQL, share_id)
    if row is None or row["audience"] != "lab":
        return "not_live"
    expires: datetime | None = row["expires_at"]
    if (
        row["state"] != "live"
        or row["lifecycle"] in _DEAD_LIFECYCLES
        or row["intake"] != "accepted"
        or row["version_state"] != "ready"
        or (expires is not None and expires <= datetime.now(UTC))
    ):
        return "not_live"
    admission = {
        "audience": "lab",
        "allow_emails": list(row["allow_emails"]),
        "allow_groups": list(row["allow_groups"]),
    }
    if not still_admitted(admission, email, basis):
        return "not_on_list"
    return row


async def create_annotation(
    pool: asyncpg.Pool,
    *,
    share_id: str,
    annotation_id: str,
    identity: str,
    email: str,
    basis: str,
    anchor: dict[str, Any],
    body: str,
    owner_emails: frozenset[str],
    limits: AnnotationLimits,
) -> Written:
    if not ANNOTATION_ID_RE.fullmatch(annotation_id):
        return Written("invalid")
    now = datetime.now(UTC)
    async with pool.acquire() as conn, conn.transaction():
        row = await _lock_live_lab_share(conn, share_id, email, basis)
        if isinstance(row, str):
            return Written(row)
        # Lock order is fixed (share, then author) so concurrent creates cannot deadlock.  The share lock makes the
        # per-share cap exact: the shared row lock above does not stop two writers both counting below the cap.
        await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", "share-cap:" + share_id)
        await conn.execute(
            "SELECT pg_advisory_xact_lock(hashtext($1))", identity
        )  # serialises the cap per author
        day = await conn.fetchval(
            "SELECT count(*) FROM annotations WHERE author_identity = $1 AND ingested_at > now() - interval '1 day'",
            identity,
        )
        total = await conn.fetchval(
            "SELECT count(*) FROM annotations WHERE share_id = $1", share_id
        )
        if day >= limits.per_user_per_day or total >= limits.max_per_share:
            return Written("quota")
        lowered = email.lower()
        role = "owner" if lowered in owner_emails else "member"
        status = await conn.execute(
            "INSERT INTO annotations (annotation_id, artifact_id, version_id, share_id, file_name, anchor, body, "
            "author_identity, author_email, author_role, created_at) "
            "VALUES ($1,$2,$3,$4,$5,$6::jsonb,$7,$8,$9,$10,$11) ON CONFLICT DO NOTHING",
            annotation_id,
            row["artifact_id"],
            row["version_id"],
            share_id,
            anchor["file"],
            json.dumps(anchor, sort_keys=True),
            body,
            identity,
            lowered,
            role,
            now,
        )
        if not status.endswith(" 1"):
            return Written("invalid")  # the annotation id is already taken
        await conn.execute(
            "INSERT INTO share_events (share_id, kind, actor, detail) VALUES ($1, 'annotated', $2, $3::jsonb)",
            share_id,
            identity[:200],
            json.dumps({"annotation_id": annotation_id, "file": anchor["file"]}, sort_keys=True),
        )
        return Written("ok", now)


async def change_annotation(
    pool: asyncpg.Pool,
    *,
    share_id: str,
    annotation_id: str,
    identity: str,
    email: str,
    basis: str,
    new_body: str | None,
) -> Written:
    """Edit (``new_body``) or withdraw (``None``) the caller's OWN open annotation of this share."""
    if not ANNOTATION_ID_RE.fullmatch(annotation_id):
        return Written("invalid")
    now = datetime.now(UTC)
    async with pool.acquire() as conn, conn.transaction():
        row = await _lock_live_lab_share(conn, share_id, email, basis)
        if isinstance(row, str):
            return Written(row)
        mine = await conn.fetchrow(
            "SELECT author_identity, status, file_name FROM annotations WHERE annotation_id = $1 AND share_id = $2 "
            "FOR UPDATE",
            annotation_id,
            share_id,
        )
        if mine is None or mine["author_identity"] != identity or mine["status"] != "open":
            return Written("not_editable")
        if new_body is not None:
            await conn.execute(
                "UPDATE annotations SET body = $2, edited_at = $3, updated_at = now() WHERE annotation_id = $1",
                annotation_id,
                new_body,
                now,
            )
            kind = "annotation_edited"
        else:
            await conn.execute(
                "UPDATE annotations SET status = 'withdrawn', withdrawn_at = $2, updated_at = now() "
                "WHERE annotation_id = $1",
                annotation_id,
                now,
            )
            kind = "annotation_withdrawn"
        await conn.execute(
            "INSERT INTO share_events (share_id, kind, actor, detail) VALUES ($1, $2, $3, $4::jsonb)",
            share_id,
            kind,
            identity[:200],
            json.dumps({"annotation_id": annotation_id, "file": mine["file_name"]}, sort_keys=True),
        )
        return Written("ok", now)


async def own_annotations(pool: asyncpg.Pool, share_id: str, identity: str) -> list[dict[str, Any]]:
    """The caller's OWN non-withdrawn annotations of the share, newest first; never another viewer's."""
    records = await pool.fetch(
        "SELECT a.annotation_id, a.file_name AS file, a.anchor::text AS anchor, a.body, a.status, "
        "v.version_number, av.version_number AS addressed_in_version_number, a.created_at, a.edited_at "
        "FROM annotations a "
        "JOIN artifact_versions v ON v.version_id = a.version_id AND v.artifact_id = a.artifact_id "
        "LEFT JOIN artifact_versions av ON av.version_id = a.addressed_in_version_id AND av.artifact_id = a.artifact_id "
        "WHERE a.share_id = $1 AND a.author_identity = $2 AND a.status <> 'withdrawn' "
        "ORDER BY a.created_at DESC, a.annotation_id LIMIT $3",
        share_id,
        identity,
        OWN_ROWS_MAX,
    )
    rows: list[dict[str, Any]] = []
    for record in records:
        row = dict(record)
        row["anchor"] = json.loads(row["anchor"])
        for key in ("created_at", "edited_at"):
            row[key] = row[key].isoformat() if row[key] is not None else None
        rows.append(row)
    return rows

"""Persistence for artifact annotations (migration 0046).

Lab members annotate a file of a shared artifact version.  The database-free share container appends JSONL lines
(``create`` / ``edit`` / ``delete``) that the worker feeds to ``AnnotationStore.ingest`` through a ``JsonlIngestor``;
the owner and agents read and resolve them through the other methods.

* ``ingest`` never raises for bad input: it returns an outcome string.  A refusal leaves a ``denied`` share event
  ``{"reason": "annotation_<code>"}``; a success leaves ``annotated`` / ``annotation_edited`` /
  ``annotation_withdrawn``.  The event carries the line's ``ingest_key``, so replaying a line never duplicates it.
* The artifact and version of a new annotation come from the share row, never from the line.  The share must be a lab
  share that was live at the line's (clamped) time, and ``file`` must be one the share exposes.
* ``create`` is idempotent on ``artifact_annotations.ingest_key``; ``edit`` / ``delete`` on ``artifact_annotation_ops``.
* ``owner_emails`` must arrive lowercased.  An author email must be printable ASCII (the share store's rule), so a
  look-alike such as a Kelvin sign can never lower-case into an owner address.
* A refused line's ``denied`` event carries ``<ingest_key>:denied``, so a later accepted replay of the same line still
  records its own success event.
* ``touched_since`` filters on ``updated_at = now()``, which is the START of the writing transaction: a caller must
  read with an overlap margin (e.g. ``since`` minus a minute) and treat the result as a superset.
* ``parse_line`` and ``ingest`` raise only ``ValueError`` / data or integrity errors (what ``JsonlIngestor`` swallows; transient errors abort the pass).
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from .annotation_anchor import ANNOTATION_ID_RE, AnnotationInputError, clean_anchor, clean_comment
from .artifact_share_api import shared_file_names
from .artifact_shares import _EMAIL, ShareStore
from .jsonl_ingest import transaction as _transaction

__all__ = ["AnnotationStore", "AnnotationError"]

STATUSES: Final = ("open", "addressed", "dismissed", "withdrawn")
MAX_FUTURE: Final = timedelta(minutes=5)
MAX_IDENTITY_CHARS: Final = 200
MAX_LIST: Final = 500
MAX_RESOLVE: Final = 50

_ROW_COLUMNS = (
    "a.annotation_id, a.artifact_id, a.version_id, a.share_id, a.file_name AS file, a.anchor::text AS anchor, a.body, "
    "a.author_identity, a.author_email, a.author_role, a.status, a.addressed_in_version_id, a.resolved_by, "
    "a.resolved_at, a.edited_at, a.withdrawn_at, a.created_at, a.updated_at, "
    "v.version_number, av.version_number AS addressed_in_version_number")
_FROM = ("artifact_annotations a JOIN artifact_versions v ON v.version_id = a.version_id "
         "LEFT JOIN artifact_versions av ON av.version_id = a.addressed_in_version_id")


class AnnotationError(ValueError):
    """A refused owner/agent operation; ``code`` is machine-readable."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


def _row(record: Any) -> dict:
    row = dict(record)
    row["anchor"] = json.loads(row["anchor"])
    return row


def _text(value: object, limit: int) -> str | None:
    """``value`` when it is a non-empty str of at most ``limit`` chars encodable as UTF-8 without NUL, else None."""
    if not isinstance(value, str) or not 0 < len(value) <= limit or "\x00" in value:
        return None
    try:
        value.encode("utf-8")
    except UnicodeError:
        return None
    return value


def _when(value: object, now: datetime) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        at = datetime.fromisoformat(value)
    except ValueError:
        return None
    if at.tzinfo is None:
        at = at.replace(tzinfo=UTC)
    return min(at, now + MAX_FUTURE)


def _live_at(share: Any, at: datetime) -> bool:
    approved, revoked, expires = share["approved_at"], share["revoked_at"], share["expires_at"]
    return (approved is not None and at >= approved and (revoked is None or at <= revoked)
            and (expires is None or at <= expires))


class AnnotationStore:
    def __init__(self, conn: Any) -> None:
        self._conn = conn

    # ----------------------------------------------------------------- ingest
    @staticmethod
    def parse_line(raw: bytes) -> dict | None:
        """One JSONL line -> its object, or None (not JSON, not an object).  For ``JsonlIngestor``."""
        try:
            obj = json.loads(raw)
        except (ValueError, RecursionError):
            return None
        return obj if isinstance(obj, dict) else None

    async def ingest(self, conn: Any, record: dict, ingest_key: str, *, owner_emails: frozenset[str],
                     per_user_per_day: int, max_per_share: int, max_comment_chars: int) -> str:
        """Apply one line; returns the outcome (see the module docstring and the task contract)."""
        op = record.get("op", "create")
        share_id = _text(record.get("share_id"), 200)
        if op not in ("create", "edit", "delete") or share_id is None:
            return "invalid"
        async with _transaction(conn):
            share = await conn.fetchrow(
                "SELECT s.share_id, s.artifact_id, s.version_id, s.audience, s.approved_at, s.revoked_at, "
                "s.expires_at, v.output_manifest::text AS output_manifest, "
                "v.report_descriptor::text AS report_descriptor "
                "FROM artifact_shares s JOIN artifact_versions v ON v.version_id = s.version_id "
                "WHERE s.share_id = $1", share_id)
            if share is None:
                return "unknown_share"
            if op == "create":
                code = await self._create(conn, share, record, ingest_key, owner_emails, per_user_per_day,
                                          max_per_share, max_comment_chars)
            else:
                code = await self._change(conn, share, record, ingest_key, op, max_comment_chars)
            if code in ("inserted", "edited", "withdrawn", "duplicate"):
                return code
            await ShareStore(conn)._event(conn, share_id, "denied", _text(record.get("author_identity"), 200) or "unknown",
                                          {"reason": "annotation_" + code}, None, ingest_key + ":denied")
            return code

    async def _create(self, conn: Any, share: Any, record: dict, key: str, owner_emails: frozenset[str],
                      per_user_per_day: int, max_per_share: int, max_comment_chars: int) -> str:
        now = datetime.now(UTC)
        if await conn.fetchval("SELECT 1 FROM artifact_annotations WHERE ingest_key = $1", key):
            return "duplicate"
        if share["audience"] != "lab":
            return "not_lab"
        identity = _text(record.get("author_identity"), MAX_IDENTITY_CHARS)
        email = _text(record.get("author_email"), MAX_IDENTITY_CHARS)
        annotation_id, at = record.get("annotation_id"), _when(record.get("at"), now)
        if (identity is None or email is None or not _EMAIL.match(email) or at is None
                or not isinstance(annotation_id, str) or not ANNOTATION_ID_RE.fullmatch(annotation_id)):
            return "invalid"
        try:
            anchor = clean_anchor(record.get("anchor"))
            body = clean_comment(record.get("body"), max_chars=max_comment_chars)
        except AnnotationInputError:
            return "invalid"
        file = anchor["file"]
        if "file" in record and record["file"] != file:
            return "invalid"
        if not _live_at(share, at):
            return "not_live"
        version = {"output_manifest": json.loads(share["output_manifest"]),
                   "report_descriptor": json.loads(share["report_descriptor"]) if share["report_descriptor"] else None}
        if file not in shared_file_names(version):
            return "file_not_in_version"
        await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", identity)   # serialises the cap per author
        if await conn.fetchval("SELECT count(*) FROM artifact_annotations WHERE author_identity = $1 "
                               "AND ingested_at > now() - interval '1 day'", identity) >= per_user_per_day:
            return "quota_user_day"
        if await conn.fetchval("SELECT count(*) FROM artifact_annotations WHERE share_id = $1",
                               share["share_id"]) >= max_per_share:
            return "quota_share"
        email = email.lower()
        role = "owner" if email in owner_emails else "member"
        status = await conn.execute(
            "INSERT INTO artifact_annotations (annotation_id, artifact_id, version_id, share_id, file_name, anchor, "
            "body, author_identity, author_email, author_role, created_at, ingest_key) "
            "VALUES ($1,$2,$3,$4,$5,$6::jsonb,$7,$8,$9,$10,$11,$12) ON CONFLICT DO NOTHING",
            annotation_id, share["artifact_id"], share["version_id"], share["share_id"], file, json.dumps(anchor),
            body, identity, email, role, at, key)
        if not status.endswith(" 1"):
            return "invalid"                                    # the annotation id is already taken
        await ShareStore(conn)._event(conn, share["share_id"], "annotated", identity,
                                      {"annotation_id": annotation_id, "file": file}, None, key)
        return "inserted"

    async def _change(self, conn: Any, share: Any, record: dict, key: str, op: str, max_comment_chars: int) -> str:
        now = datetime.now(UTC)
        if await conn.fetchval("SELECT 1 FROM artifact_annotation_ops WHERE ingest_key = $1", key):
            return "duplicate"
        identity, at = _text(record.get("author_identity"), MAX_IDENTITY_CHARS), _when(record.get("at"), now)
        annotation_id = record.get("annotation_id")
        if identity is None or at is None or not isinstance(annotation_id, str) \
                or not ANNOTATION_ID_RE.fullmatch(annotation_id):
            return "invalid"
        body = None
        if op == "edit":
            try:
                body = clean_comment(record.get("body"), max_chars=max_comment_chars)
            except AnnotationInputError:
                return "invalid"
        row = await conn.fetchrow("SELECT author_identity, status, file_name FROM artifact_annotations "
                                  "WHERE annotation_id = $1 AND share_id = $2 FOR UPDATE",
                                  annotation_id, share["share_id"])
        if row is None:
            return "not_found"
        if row["author_identity"] != identity:
            return "not_author"
        if row["status"] != "open":
            return "not_open"
        if not _live_at(share, at):
            return "not_live"
        if op == "edit":
            await conn.execute("UPDATE artifact_annotations SET body = $2, edited_at = $3, updated_at = now() "
                               "WHERE annotation_id = $1", annotation_id, body, at)
            kind, outcome = "annotation_edited", "edited"
        else:
            await conn.execute("UPDATE artifact_annotations SET status = 'withdrawn', withdrawn_at = $2, "
                               "updated_at = now() WHERE annotation_id = $1", annotation_id, at)
            kind, outcome = "annotation_withdrawn", "withdrawn"
        await conn.execute("INSERT INTO artifact_annotation_ops (ingest_key, annotation_id, op, at) "
                           "VALUES ($1,$2,$3,$4)", key, annotation_id, op, at)
        await ShareStore(conn)._event(conn, share["share_id"], kind, identity,
                                      {"annotation_id": annotation_id, "file": row["file_name"]}, None, key)
        return outcome

    # ------------------------------------------------------------------ reads
    async def list_for_artifact(self, artifact_id: str, *, status: str = "open", limit: int = 200,
                                before: str | None = None) -> tuple[list[dict], str | None]:
        """Newest first.  ``before`` is the cursor returned by the previous page (``<created_at iso>|<id>``)."""
        if status not in (*STATUSES, "all"):
            raise AnnotationError("bad_status")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIST:
            raise AnnotationError("bad_limit")
        args: list[Any] = [artifact_id]
        where = "a.artifact_id = $1"
        if status != "all":
            args.append(status)
            where += f" AND a.status = ${len(args)}"
        if before is not None:
            stamp, sep, last_id = before.partition("|")
            try:
                created = datetime.fromisoformat(stamp)
            except ValueError:
                raise AnnotationError("bad_cursor") from None
            if not sep or created.tzinfo is None:
                raise AnnotationError("bad_cursor")
            args += [created, last_id]
            where += (f" AND (a.created_at < ${len(args) - 1} OR "
                      f"(a.created_at = ${len(args) - 1} AND a.annotation_id > ${len(args)}))")
        args.append(limit + 1)
        records = await self._conn.fetch(
            f"SELECT {_ROW_COLUMNS} FROM {_FROM} WHERE {where} "
            f"ORDER BY a.created_at DESC, a.annotation_id LIMIT ${len(args)}", *args)
        rows = [_row(r) for r in records]
        cursor = None
        if len(rows) > limit:
            rows = rows[:limit]
            cursor = f"{rows[-1]['created_at'].isoformat()}|{rows[-1]['annotation_id']}"
        return rows, cursor

    async def own_rows(self, share_id: str) -> dict[str, list[dict]]:
        """Each author's newest 500 non-withdrawn rows of a share, keyed by author identity."""
        records = await self._conn.fetch(
            "SELECT * FROM (SELECT a.annotation_id, a.file_name AS file, a.anchor::text AS anchor, a.body, a.status, "
            "a.author_identity, v.version_number, av.version_number AS addressed_in_version_number, a.created_at, "
            "a.edited_at, row_number() OVER (PARTITION BY a.author_identity "
            "ORDER BY a.created_at DESC, a.annotation_id) AS n "
            f"FROM {_FROM} WHERE a.share_id = $1 AND a.status <> 'withdrawn') t WHERE n <= $2 "
            "ORDER BY created_at DESC, annotation_id", share_id, MAX_LIST)
        out: dict[str, list[dict]] = {}
        for record in records:
            row = _row(record)
            identity = row.pop("author_identity")
            row.pop("n")
            out.setdefault(identity, []).append(row)
        return out

    async def open_for_agents(self, artifact_id: str, limit: int = 200) -> list[dict]:
        """Open annotations without any author email or identity."""
        records = await self._conn.fetch(
            "SELECT a.annotation_id, a.version_id, v.version_number, a.file_name AS file, a.anchor::text AS anchor, "
            "a.body, a.author_role, a.created_at, a.edited_at "
            f"FROM {_FROM} WHERE a.artifact_id = $1 AND a.status = 'open' "
            "ORDER BY a.created_at DESC, a.annotation_id LIMIT $2", artifact_id, max(1, min(int(limit), MAX_LIST)))
        return [_row(r) for r in records]

    async def touched_since(self, since: datetime) -> tuple[set[str], set[str]]:
        """``(share ids, artifact ids)`` of annotations created or changed at or after ``since``."""
        records = await self._conn.fetch("SELECT DISTINCT share_id, artifact_id FROM artifact_annotations "
                                         "WHERE updated_at >= $1", since)
        return {r["share_id"] for r in records}, {r["artifact_id"] for r in records}

    # ------------------------------------------------------- owner and agents
    async def _locked(self, conn: Any, artifact_id: str, annotation_id: str) -> Any:
        return await conn.fetchrow("SELECT a.annotation_id, a.share_id, a.status, v.version_number "
                                   "FROM artifact_annotations a JOIN artifact_versions v ON v.version_id = a.version_id "
                                   "WHERE a.artifact_id = $1 AND a.annotation_id = $2 FOR UPDATE OF a",
                                   artifact_id, annotation_id)

    async def _fetch_one(self, conn: Any, annotation_id: str) -> dict:
        return _row(await conn.fetchrow(f"SELECT {_ROW_COLUMNS} FROM {_FROM} WHERE a.annotation_id = $1",
                                        annotation_id))

    async def dismiss(self, artifact_id: str, annotation_id: str, *, actor: str) -> dict:
        async with _transaction(self._conn) as conn:
            row = await self._locked(conn, artifact_id, annotation_id)
            if row is None:
                raise AnnotationError("not_found")
            if row["status"] == "dismissed":
                return await self._fetch_one(conn, annotation_id)
            if row["status"] != "open":
                raise AnnotationError("not_open")
            await conn.execute("UPDATE artifact_annotations SET status = 'dismissed', resolved_by = $2, "
                               "resolved_at = now(), updated_at = now() WHERE annotation_id = $1",
                               annotation_id, actor)
            await ShareStore(conn)._event(conn, row["share_id"], "annotation_dismissed", actor,
                                          {"annotation_id": annotation_id})
            return await self._fetch_one(conn, annotation_id)

    async def resolve(self, artifact_id: str, annotation_ids: list[str], *, version_id: str, actor: str) -> dict:
        """Mark open annotations addressed in ``version_id`` (a strictly newer version of the same artifact)."""
        resolved: list[str] = []
        skipped: list[dict] = []
        if not isinstance(annotation_ids, list) or len(annotation_ids) > MAX_RESOLVE:
            return {"resolved": [], "skipped": [{"annotation_id": None, "reason": "invalid"}]}
        valid = [a for a in annotation_ids if isinstance(a, str) and ANNOTATION_ID_RE.fullmatch(a)]
        skipped.extend({"annotation_id": a if isinstance(a, str) and a.isprintable() else None, "reason": "invalid"}
                       for a in annotation_ids if a not in valid)
        async with _transaction(self._conn) as conn:
            number = await conn.fetchval("SELECT version_number FROM artifact_versions "
                                         "WHERE version_id = $1 AND artifact_id = $2", version_id, artifact_id)
            if number is None:
                raise AnnotationError("bad_version")
            for annotation_id in dict.fromkeys(valid):
                row = await self._locked(conn, artifact_id, annotation_id)
                reason = ("not_found" if row is None else "not_open" if row["status"] != "open"
                          else "version_not_newer" if row["version_number"] >= number else None)
                if reason is not None:
                    skipped.append({"annotation_id": annotation_id, "reason": reason})
                    continue
                await conn.execute(
                    "UPDATE artifact_annotations SET status = 'addressed', addressed_in_version_id = $2, "
                    "resolved_by = $3, resolved_at = now(), updated_at = now() WHERE annotation_id = $1",
                    annotation_id, version_id, actor)
                await ShareStore(conn)._event(conn, row["share_id"], "annotation_addressed", actor,
                                              {"annotation_id": annotation_id, "version_id": version_id})
                resolved.append(annotation_id)
        return {"resolved": resolved, "skipped": skipped}

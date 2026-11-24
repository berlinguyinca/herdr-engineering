"""Artifact lifecycle (archive / restore / tombstone / purge) and two-phase publish reconciliation.

Deletion policy: no registry row is ever deleted (a trigger forbids it for versions). ``archive`` hides and is reversible.
``tombstone`` records a reason, starts a hold window (``purge_after``) and is restorable until bytes are purged. ``purge``
is a system operation: it marks the versions ``purged`` and COMMITS that record together with the keys to delete
(``artifact_pending_deletes``); the object bytes are deleted only afterwards, and only when no other reference to the
same content-addressed key exists (re-checked at delete time), so a failure leaves orphaned bytes, never a restorable
artifact without bytes. Hashes, provenance and the audit trail remain. Every step is an append-only
``artifact_lifecycle_events`` row and is idempotent by ``request_key`` (a key already used for another action is
refused). A legal hold is the durable ``artifacts.legal_hold`` flag: restore, re-tombstone and purge all need the
explicit override, which is recorded.

Reconciliation repairs the window between "version committed" and "graph edges emitted", marks verified blobs that no
version references (never deleting them), expires stale upload sessions and runs due purges.
"""
from __future__ import annotations

import inspect
import json
import re
from collections.abc import Callable
from typing import Any

from .artifact_members import is_member_principal

MIN_HOLD_DAYS = 1.0
DEFAULT_HOLD_DAYS = 30.0
MAX_BULK = 200
MAX_REASON = 1000
DELETE_MODES = ("tombstone", "purge_now")
ANONYMIZED = "purged"
_NOT_PURGED = ("NOT EXISTS (SELECT 1 FROM artifact_lifecycle_events pe WHERE pe.artifact_id=a.artifact_id "
               "AND pe.action='purge')")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_TRANSITIONS = {
    "archive": ({"active"}, "archived"),
    "restore": ({"archived", "tombstoned"}, "active"),
    "tombstone": ({"active", "archived", "retracted"}, "tombstoned"),
}


def _error(message: str, code: str | None = None):
    from .artifacts import ArtifactError
    exc = ArtifactError(message)
    exc.code = code                                     # type: ignore[attr-defined]
    return exc


def _is_held(row: Any) -> bool:
    """A legal hold: the durable flag, or (rows written by code from before the flag existed, rolling deploys) a
    tombstone with no purge date."""
    return bool(row["legal_hold"]) or (row["lifecycle"] == "tombstoned" and row["purge_after"] is None)


def _code(exc: BaseException) -> str | None:
    return getattr(exc, "code", None)


class ArtifactLifecycleMixin:
    _conn: Any
    _rf: Any

    async def _lifecycle(self, action: str, artifact_id: str, *, actor: str, reason: str = "",
                         request_key: str | None = None, hold_days: float | None = DEFAULT_HOLD_DAYS,
                         legal_hold: bool = False, override_legal_hold: bool = False,
                         allowed_from: set[str] | None = None) -> dict:
        """``allowed_from`` narrows the permitted current states; it is checked under the row lock (a caller that
        decided from an earlier unlocked read cannot act on a state that changed in between)."""
        from .artifacts import ArtifactError, _transaction
        if not actor:
            raise ArtifactError("actor is required")
        reason = (reason or "").strip()[:1000]
        if action == "tombstone" and not reason:
            raise ArtifactError("a tombstone needs a reason")
        permitted, to_state = _TRANSITIONS[action]
        if allowed_from is not None:
            permitted = permitted & set(allowed_from)
        async with _transaction(self._conn):
            row = await self._conn.fetchrow(
                "SELECT artifact_id, lifecycle, legal_hold, purge_after FROM artifacts WHERE artifact_id=$1 FOR UPDATE",
                artifact_id)
            if row is None:
                raise ArtifactError(f"no artifact {artifact_id}")
            if request_key:
                prior = await self._conn.fetchrow(
                    "SELECT action, to_state FROM artifact_lifecycle_events WHERE artifact_id=$1 AND request_key=$2",
                    artifact_id, request_key)
                if prior:
                    if prior["action"] != action:
                        raise _error("request key was reused for a different lifecycle action", "request_key_reused")
                    if row["lifecycle"] != prior["to_state"]:
                        # a stale key: the recorded action no longer describes the artifact's state
                        raise _error("request key belongs to an earlier action that has since been undone",
                                     "request_key_reused")
                    return {"artifact_id": artifact_id, "lifecycle": prior["to_state"], "replayed": True}
            current = row["lifecycle"]
            if current not in permitted:
                raise ArtifactError(f"cannot {action} an artifact that is {current}")
            held = _is_held(row)
            detail: dict = {}
            if action == "restore" and current == "tombstoned":
                if await self._conn.fetchval(
                        "SELECT 1 FROM artifact_lifecycle_events WHERE artifact_id=$1 AND action='purge' LIMIT 1",
                        artifact_id):
                    raise _error("artifact bytes were already purged and cannot be restored", "purged")
            if held and action in ("restore", "tombstone") and not (action == "tombstone" and legal_hold):
                if not override_legal_hold:
                    raise _error("artifact is under legal hold", "legal_hold")
                detail["legal_hold_overridden"] = True
            if action == "archive":
                await self._conn.execute(
                    "UPDATE artifacts SET lifecycle='archived', archived_at=now() WHERE artifact_id=$1", artifact_id)
            elif action == "restore":
                if current == "tombstoned":
                    before = await self._conn.fetchval(
                        "SELECT from_state FROM artifact_lifecycle_events WHERE artifact_id=$1 AND action='tombstone' "
                        "ORDER BY event_id DESC LIMIT 1", artifact_id)
                    if before in ("retracted", "archived"):
                        to_state = before          # restoring must not un-hide or un-archive what was hidden before
                await self._conn.execute(
                    "UPDATE artifacts SET lifecycle=$2, archived_at=CASE WHEN $2='archived' THEN now() ELSE NULL END, "
                    "tombstoned_at=NULL, purge_after=NULL, legal_hold=false WHERE artifact_id=$1", artifact_id, to_state)
            else:
                days = None if legal_hold or hold_days is None else float(max(MIN_HOLD_DAYS, hold_days))
                if legal_hold:
                    detail["legal_hold"] = True
                await self._conn.execute(
                    "UPDATE artifacts SET lifecycle='tombstoned', tombstoned_at=now(), legal_hold=$3, "
                    "purge_after=CASE WHEN $2::float8 IS NULL THEN NULL ELSE now() + ($2::float8 * interval '1 day') END "
                    "WHERE artifact_id=$1", artifact_id, days, bool(legal_hold))
            await self._conn.execute(
                "INSERT INTO artifact_lifecycle_events (artifact_id, action, from_state, to_state, actor, reason, "
                "request_key, detail) VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb)",
                artifact_id, action, current, to_state, actor, reason, request_key, json.dumps(detail))
        return {"artifact_id": artifact_id, "lifecycle": to_state, "replayed": False}

    async def archive_artifact(self, artifact_id: str, *, actor: str, reason: str = "",
                               request_key: str | None = None) -> dict:
        return await self._lifecycle("archive", artifact_id, actor=actor, reason=reason, request_key=request_key)

    async def restore_artifact(self, artifact_id: str, *, actor: str, reason: str = "",
                               request_key: str | None = None, override_legal_hold: bool = False,
                               allowed_from: set[str] | None = None) -> dict:
        return await self._lifecycle("restore", artifact_id, actor=actor, reason=reason, request_key=request_key,
                                     override_legal_hold=override_legal_hold, allowed_from=allowed_from)

    async def tombstone_artifact(self, artifact_id: str, *, actor: str, reason: str,
                                 request_key: str | None = None, legal_hold: bool = False,
                                 hold_days: float = DEFAULT_HOLD_DAYS, override_legal_hold: bool = False) -> dict:
        """``legal_hold`` sets the durable hold flag: no purge date, and no purge, restore or re-tombstone without the
        explicit override. Otherwise the hold window is at least ``MIN_HOLD_DAYS``."""
        return await self._lifecycle("tombstone", artifact_id, actor=actor, reason=reason, request_key=request_key,
                                     hold_days=hold_days, legal_hold=legal_hold,
                                     override_legal_hold=override_legal_hold)

    async def lifecycle_of(self, artifact_id: str) -> str | None:
        return await self._conn.fetchval("SELECT lifecycle FROM artifacts WHERE artifact_id=$1", artifact_id)

    async def tombstoned_ids(self) -> list[str]:
        return [r["artifact_id"] for r in await self._conn.fetch(
            "SELECT artifact_id FROM artifacts WHERE lifecycle='tombstoned'")]

    async def list_lifecycle_events(self, artifact_id: str) -> list[dict]:
        rows = await self._conn.fetch(
            "SELECT action, from_state, to_state, actor, reason, created_at FROM artifact_lifecycle_events "
            "WHERE artifact_id=$1 ORDER BY event_id", artifact_id)
        return [self._jsonable(dict(row)) for row in rows]

    # ------------------------------------------------------------------ purge
    @staticmethod
    def _version_keys(version: dict) -> set[str]:
        keys: set[str] = set()
        manifest = version.get("output_manifest") or []
        if isinstance(manifest, str):
            manifest = json.loads(manifest)
        for output in manifest:
            if isinstance(output, dict):
                ref = output.get("storage_key") or output.get("blob_sha256")
                if ref:
                    keys.add(str(ref))
        if version.get("source_manifest_blob"):
            keys.add(str(version["source_manifest_blob"]))
        return keys

    @staticmethod
    def _digest_of(key: str) -> str:
        return key.rsplit("/", 1)[-1]

    @classmethod
    def _canonical_key(cls, key: str) -> str | None:
        """The one object key a content digest lives at (``sha256/xx/yy/<digest>``), or None when ``key`` does not end
        in a digest. Only canonical keys are ever queued or deleted: an alias spelling is never trusted."""
        digest = cls._digest_of(key)
        return f"sha256/{digest[:2]}/{digest[2:4]}/{digest}" if _DIGEST.fullmatch(digest) else None

    async def _key_shared(self, artifact_id: str, key: str) -> bool:
        """True when anything other than ``artifact_id`` still references the content-addressed key: a version of a
        not-yet-purged artifact (live OR tombstoned), a registered blob (any scope, ``fabric`` included), a staged
        collaborator upload, or a legacy ``storage_key``. ``artifact_id=''`` excludes nobody."""
        digest = self._digest_of(key)
        row = await self._conn.fetchrow(
            f"SELECT 1 AS x FROM artifact_versions v JOIN artifacts a ON a.artifact_id=v.artifact_id "
            f"WHERE a.artifact_id<>$1 AND {_NOT_PURGED} AND v.purged_at IS NULL AND ("
            " v.output_manifest @> jsonb_build_array(jsonb_build_object('storage_key', $2::text))"
            " OR v.output_manifest @> jsonb_build_array(jsonb_build_object('blob_sha256', $3::text))"
            " OR v.output_manifest @> jsonb_build_array(jsonb_build_object('sha256', $3::text))"
            " OR v.source_manifest_blob IN ($2::text, $3::text)) LIMIT 1", artifact_id, key, digest)
        if row:
            return True
        row = await self._conn.fetchrow(
            "SELECT 1 AS x FROM artifact_blobs b LEFT JOIN artifacts a ON a.artifact_id=b.scope "
            f"WHERE b.scope<>$1 AND (b.storage_key=$2 OR b.blob_sha256=$3) AND {_NOT_PURGED} LIMIT 1",
            artifact_id, key, digest)
        if row:
            return True
        row = await self._conn.fetchrow(
            "SELECT 1 AS x FROM artifact_collab_uploads u LEFT JOIN artifacts a ON a.artifact_id=u.artifact_id "
            f"WHERE u.artifact_id<>$1 AND (u.storage_key=$2 OR u.sha256=$3) AND {_NOT_PURGED} LIMIT 1",
            artifact_id, key, digest)
        if row:
            return True
        row = await self._conn.fetchrow(
            f"SELECT 1 AS x FROM artifacts a WHERE a.artifact_id<>$1 AND (a.storage_key=$2 OR a.sha256=$3) "
            f"AND {_NOT_PURGED} LIMIT 1", artifact_id, key, digest)
        return row is not None

    async def _lock_digest(self, key: str) -> None:
        """Serialise purge decisions on one content digest for the rest of the transaction."""
        await self._conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", self._digest_of(key))

    async def _lock_digests(self, keys: list[str]) -> None:
        """Exclusive digest locks for several keys, always in sorted digest order (the order every shared taker uses):
        two transactions over overlapping digest sets then queue instead of deadlocking."""
        # sorted by digest text, not by the lock's hash key: every taker uses this order, and two digests whose hashes
        # collide in a way that inverts it are a theoretical concern only
        for digest in sorted({self._digest_of(k) for k in keys if k}):
            await self._conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", digest)

    async def _lock_digests_shared(self, keys: list[str]) -> None:
        """Writers of a reference to content-addressed bytes call this inside their committing transaction, then
        re-check the object exists. It excludes a concurrent purge sweep (exclusive lock on the same digest) until the
        reference commits, after which the sweep sees the reference and keeps the bytes."""
        # sorted by digest text, not by the lock's hash key (see _lock_digests)
        for digest in sorted({self._digest_of(k) for k in keys if k}):
            await self._conn.execute("SELECT pg_advisory_xact_lock_shared(hashtextextended($1, 0))", digest)

    async def purge_due(self, *, limit: int = 20) -> list[dict]:
        """Purge tombstoned artifacts whose hold has expired (never a legal hold). Idempotent; one record per artifact."""
        from .artifacts import _transaction
        await self.drain_pending_deletes()
        rows = await self._conn.fetch(
            "SELECT a.artifact_id FROM artifacts a WHERE a.lifecycle='tombstoned' AND NOT a.legal_hold "
            "AND a.purge_after IS NOT NULL AND a.purge_after <= now() AND ("
            "EXISTS (SELECT 1 FROM artifact_versions v WHERE v.artifact_id=a.artifact_id AND v.purged_at IS NULL) "
            "OR EXISTS (SELECT 1 FROM artifact_blobs b WHERE b.scope=a.artifact_id) "
            f"OR (a.storage_key IS NOT NULL AND {_NOT_PURGED})) ORDER BY a.purge_after LIMIT $1", int(limit))
        results = []
        for row in rows:
            artifact_id = row["artifact_id"]
            async with _transaction(self._conn):
                await self._lock_owner_usage(artifact_id)
                locked = await self._conn.fetchrow(
                    "SELECT lifecycle, legal_hold, purge_after FROM artifacts WHERE artifact_id=$1 FOR UPDATE",
                    artifact_id)
                if locked is None or locked["lifecycle"] != "tombstoned" or _is_held(locked):
                    continue
                purged = await self._purge_locked(artifact_id, actor="system", reason="hold window elapsed")
            purged["deleted"], purged["warning"] = await self._drain_after_commit(purged["queued"])
            results.append({k: v for k, v in purged.items() if k != "queued"})
        return results

    async def _lock_owner_usage(self, artifact_id: str) -> None:
        """A member owner's usage row is locked BEFORE the artifact row (publish takes them in that order), so the
        ``settle`` at the end of a purge cannot deadlock against a publish."""
        owner = await self._conn.fetchval("SELECT created_by FROM artifacts WHERE artifact_id=$1", artifact_id)
        if is_member_principal(owner):
            from .artifact_member_quota import _lock_usage
            await _lock_usage(self._conn, owner)

    async def _purge_locked(self, artifact_id: str, *, actor: str, reason: str, request_key: str | None = None,
                            extra: dict | None = None) -> dict:
        """Commit-side half of a purge, inside the caller's transaction (the artifact row is locked): mark the versions
        purged, queue the unshared keys in ``artifact_pending_deletes`` and write the purge event. No object is touched
        here; the caller deletes after the commit (``drain_pending_deletes``)."""
        versions = [self._row(v) for v in await self._conn.fetch(
            "SELECT * FROM artifact_versions WHERE artifact_id=$1 AND purged_at IS NULL", artifact_id)]
        keys: set[str] = set()
        for version in versions:
            keys |= self._version_keys(version)
        for blob in await self._conn.fetch("SELECT storage_key FROM artifact_blobs WHERE scope=$1", artifact_id):
            keys.add(blob["storage_key"])
        legacy = await self._conn.fetchrow("SELECT storage_key FROM artifacts WHERE artifact_id=$1", artifact_id)
        if legacy and legacy["storage_key"]:
            keys.add(legacy["storage_key"])
        queued, retained = [], []
        normalised = {canonical for canonical in map(self._canonical_key, keys) if canonical}
        # the purge event comes first: _key_shared must already see this artifact as purged only after commit, so the
        # decision below excludes it explicitly
        await self._lock_digests(sorted(normalised))
        for key in sorted(normalised):
            if await self._key_shared(artifact_id, key):
                retained.append(key)
            else:
                queued.append(key)
        await self._conn.execute(
            "UPDATE artifact_versions SET state=CASE WHEN state='retracted' THEN state ELSE 'purged' END, "
            "purged_at=now() WHERE artifact_id=$1 AND purged_at IS NULL", artifact_id)
        await self._conn.execute("DELETE FROM artifact_blobs WHERE scope=$1", artifact_id)
        for key in queued:
            await self._conn.execute(
                "INSERT INTO artifact_pending_deletes (storage_key) VALUES ($1) ON CONFLICT DO NOTHING", key)
        # the people who commented on a purged artifact are not kept: identity and email are blanked, bodies stay
        await self._conn.execute(
            "UPDATE artifact_annotations SET author_identity=$2, author_email=$2 WHERE artifact_id=$1",
            artifact_id, ANONYMIZED)
        await self._conn.execute(
            "INSERT INTO artifact_lifecycle_events (artifact_id, action, from_state, to_state, actor, reason, "
            "request_key, detail) VALUES ($1,'purge','tombstoned','tombstoned',$2,$3,$4,$5::jsonb)",
            artifact_id, actor, reason, request_key,
            json.dumps({"queued_delete": queued, "retained_shared": retained, **(extra or {})}))
        owner = await self._conn.fetchval("SELECT created_by FROM artifacts WHERE artifact_id=$1", artifact_id)
        if is_member_principal(owner):
            await self._anonymise_member_data(artifact_id)
            from .artifact_member_quota import settle
            await settle(self._conn, owner)
        return {"artifact_id": artifact_id, "queued": queued, "deleted": [], "retained_shared": retained}

    async def _anonymise_member_data(self, artifact_id: str) -> None:
        """Per-member data tied to a purged artifact: the recipients and titles of its links, the free-text detail of
        its member audit rows and its access grants. The rows (who did what, when) remain; nothing identifying does."""
        await self._conn.execute(
            "UPDATE artifact_shares SET allow_emails='{}', allow_groups='{}', title=NULL, entry=NULL"
            " WHERE artifact_id=$1",
            artifact_id)
        await self._conn.execute(
            "UPDATE artifact_member_events SET detail='{}'::jsonb WHERE artifact_id=$1", artifact_id)
        await self._conn.execute("DELETE FROM artifact_acl WHERE artifact_id=$1", artifact_id)
        # Personal data collected through the artifact's links: comment text and author identity, the viewers recorded
        # on opened/denied events (the rows stay, so the audit trail of what happened does), staged upload names, and
        # the artifact's own title and filename.
        # The anchor keeps its structural keys (file, offsets) but loses the text quoted from the report.
        await self._conn.execute(
            "UPDATE artifact_annotations SET body='[removed]', author_identity='[removed]', author_email='[removed]',"
            " file_name='[removed]', resolved_by=NULL, anchor=(anchor - 'exact' - 'prefix' - 'suffix')"
            " WHERE artifact_id=$1", artifact_id)
        # Every share event whose actor is not one of our own principals (a member or an api key, i.e. the owner or an
        # operator) names a viewer: opened, denied and the annotation kinds alike. Cleared by actor, not by kind.
        await self._conn.execute(
            "UPDATE artifact_share_events SET actor='[removed]', detail='{}'::jsonb"
            " WHERE actor NOT LIKE 'member:%' AND actor NOT LIKE 'api-key:%' AND actor <> '[removed]'"
            " AND share_id IN (SELECT share_id FROM artifact_shares WHERE artifact_id=$1)", artifact_id)
        # Third-party feedback on the report: text and author go, the rows (that feedback was given) stay; the
        # owner's own feedback is kept. artifact_annotation_ops holds only ingest keys and ids, nothing personal.
        await self._conn.execute("SET LOCAL herdr.purge_redaction = 'on'")
        await self._conn.execute(
            "UPDATE artifact_report_feedback SET body='[removed]', actor='[removed]' WHERE artifact_id=$1"
            " AND actor IS DISTINCT FROM (SELECT created_by FROM artifacts WHERE artifact_id=$1)", artifact_id)
        # The file names in each version's manifest and the commit message / entry file in its provenance name the
        # same things the columns above did. storage_key and sha256 stay: _key_shared and the audit trail need them.
        await self._conn.execute(
            "UPDATE artifact_versions SET"
            " output_manifest=CASE WHEN jsonb_typeof(output_manifest)='array' THEN (SELECT coalesce(jsonb_agg("
            "  CASE WHEN jsonb_typeof(e)='object' AND e ? 'name' THEN jsonb_set(e, '{name}', '\"[removed]\"') ELSE e END"
            "  ORDER BY o), '[]'::jsonb) FROM jsonb_array_elements(output_manifest) WITH ORDINALITY AS t(e, o))"
            "  ELSE output_manifest END,"
            " provenance=(provenance - 'message' - 'entry')"
            "   || CASE WHEN provenance ? 'message' THEN jsonb_build_object('message', '[removed]') ELSE '{}'::jsonb END"
            "   || CASE WHEN provenance ? 'entry' THEN jsonb_build_object('entry', '[removed]') ELSE '{}'::jsonb END"
            " WHERE artifact_id=$1", artifact_id)
        await self._conn.execute("SET LOCAL herdr.purge_redaction = 'off'")
        await self._conn.execute("UPDATE artifact_collab_uploads SET name='[removed]' WHERE artifact_id=$1", artifact_id)
        # collaborator merge attempts carry the file names of their conflicts and a free-text message
        await self._conn.execute(
            "UPDATE artifact_collab_attempts SET message=CASE WHEN message IS NULL THEN NULL ELSE '[removed]' END,"
            " conflict_names='[]'::jsonb WHERE artifact_id=$1", artifact_id)
        await self._conn.execute(
            "UPDATE artifacts SET display_name=NULL, original_filename='[removed]' WHERE artifact_id=$1", artifact_id)


    async def drain_pending_deletes(self, keys: list[str] | None = None, *, limit: int = 100) -> list[str]:
        """Delete queued objects (all, or just ``keys``), re-checking at delete time that nothing references them any
        more. A key that cannot be deleted stays queued for the next sweep. Returns the keys actually deleted."""
        return (await self._drain(keys, limit=limit))[0]

    async def _drain(self, keys: list[str] | None = None, *, limit: int = 100) -> tuple[list[str], list[str]]:
        """``(deleted, failed)``: a failed key stays queued; recording the failure is best effort and never raises."""
        from .artifacts import _transaction
        if keys is None:
            keys = [r["storage_key"] for r in await self._conn.fetch(
                "SELECT storage_key FROM artifact_pending_deletes ORDER BY queued_at LIMIT $1", int(limit))]
        deleted, failed = [], []
        for key in keys:
            try:
                async with _transaction(self._conn):
                    canonical = self._canonical_key(key)
                    if canonical is None or canonical != key:         # never act on an alias spelling
                        await self._conn.execute("DELETE FROM artifact_pending_deletes WHERE storage_key=$1", key)
                        continue
                    await self._lock_digest(key)
                    if await self._key_shared("", key):
                        await self._conn.execute("DELETE FROM artifact_pending_deletes WHERE storage_key=$1", key)
                        continue
                    self._rf.delete(key)
                    await self._conn.execute("DELETE FROM artifact_pending_deletes WHERE storage_key=$1", key)
                    deleted.append(key)
            except Exception:                           # noqa: BLE001 - stays queued, retried next sweep
                failed.append(key)
                try:
                    await self._conn.execute(
                        "UPDATE artifact_pending_deletes SET attempts=attempts+1 WHERE storage_key=$1", key)
                except Exception:                       # noqa: BLE001 - bookkeeping only; the key is still queued
                    pass
        return deleted, failed

    async def _drain_after_commit(self, keys: list[str]) -> tuple[list[str], str | None]:
        """The post-commit half of a purge. The purge is already committed, so nothing here may fail the call: a
        problem becomes the fixed warning ``cleanup_pending`` (the next sweep finishes the job)."""
        try:
            deleted, failed = await self._drain(keys)
        except Exception:                               # noqa: BLE001
            return [], "cleanup_pending"
        return deleted, "cleanup_pending" if failed else None

    # ------------------------------------------------------- owner delete / restore
    @staticmethod
    def _delete_args(actor: str, reason: str) -> str:
        from .artifacts import ArtifactError
        reason = " ".join((reason or "").split())[:MAX_REASON]
        if not actor:
            raise ArtifactError("actor is required")
        if not reason:
            raise ArtifactError("a reason is required")
        return reason

    async def _audit_refused(self, artifact_id: str, actor: str, reason: str, why: str, attempted: str) -> None:
        """An append-only record of a refused owner attempt (best effort: the refusal stands either way)."""
        try:
            await self._conn.execute(
                "INSERT INTO artifact_lifecycle_events (artifact_id, action, from_state, to_state, actor, reason, detail) "
                "SELECT artifact_id, 'refused', lifecycle, lifecycle, $2, $3, $4::jsonb FROM artifacts "
                "WHERE artifact_id=$1", artifact_id, actor, reason, json.dumps({"refused": why, "attempted": attempted}))
        except Exception:                               # noqa: BLE001 - never turns a refusal into an error
            pass

    async def _key_conflict(self, artifact_id: str, request_key: str | None, action: str) -> bool:
        """True when ``request_key`` was already used on this artifact for a different action."""
        if not request_key:
            return False
        used = await self._conn.fetchval(
            "SELECT action FROM artifact_lifecycle_events WHERE artifact_id=$1 AND request_key=$2", artifact_id,
            request_key)
        return used is not None and used != action

    async def purge_now(self, artifact_id: str, *, actor: str, reason: str, request_key: str | None = None,
                        override_legal_hold: bool = False) -> dict:
        """Tombstone (when needed) and purge immediately. The purge record commits first; objects are deleted after.

        ``result`` is ``deleted``, ``already_purged``, ``not_found`` or ``refused`` (``reason`` is then a fixed code:
        ``legal_hold`` or ``request_key_reused``). A repeated ``request_key`` is answered from the audit trail."""
        from .artifacts import ArtifactError, _transaction
        reason = self._delete_args(actor, reason)
        out = {"artifact_id": artifact_id, "result": "not_found", "reason": None, "replayed": False,
               "deleted": [], "retained_shared": [], "warning": None}
        refusal = None
        async with _transaction(self._conn):
            await self._lock_owner_usage(artifact_id)
            row = await self._conn.fetchrow(
                "SELECT lifecycle, legal_hold, purge_after FROM artifacts WHERE artifact_id=$1 FOR UPDATE",
                artifact_id)
            if row is None:
                return out
            prior = await self._conn.fetchrow(
                "SELECT request_key FROM artifact_lifecycle_events WHERE artifact_id=$1 AND action='purge' "
                "ORDER BY (request_key IS NOT DISTINCT FROM $2) DESC, event_id LIMIT 1", artifact_id, request_key)
            if prior is not None:
                return {**out, "result": "already_purged",
                        "replayed": bool(request_key) and prior["request_key"] == request_key}
            if await self._key_conflict(artifact_id, request_key, "purge"):
                refusal = "request_key_reused"
            elif _is_held(row) and not override_legal_hold:
                refusal = "legal_hold"
            else:
                try:
                    if row["lifecycle"] != "tombstoned":
                        await self._lifecycle("tombstone", artifact_id, actor=actor, reason=reason,
                                              hold_days=MIN_HOLD_DAYS, override_legal_hold=override_legal_hold)
                except ArtifactError as exc:
                    refusal = _code(exc) or "not_allowed"
                else:
                    purged = await self._purge_locked(
                        artifact_id, actor=actor, reason=reason, request_key=request_key,
                        extra={"legal_hold_overridden": True} if _is_held(row) else None)
        if refusal:
            await self._audit_refused(artifact_id, actor, reason, refusal, "purge_now")
            return {**out, "result": "refused", "reason": refusal}
        deleted, warning = await self._drain_after_commit(purged["queued"])
        return {**out, "result": "deleted", "deleted": deleted, "retained_shared": purged["retained_shared"],
                "warning": warning}

    async def delete_many(self, artifact_ids: list[str], *, mode: str, actor: str, reason: str,
                          request_key: str | None = None, hold_days: float = DEFAULT_HOLD_DAYS,
                          override_legal_hold: bool = False) -> list[dict]:
        """``tombstone`` or ``purge_now`` every distinct id (at most ``MAX_BULK``); one result per id, in order.
        A failure on one id becomes that id's ``refused`` / ``error`` row; the other ids are still processed."""
        from .artifacts import ArtifactError
        if mode not in DELETE_MODES:
            raise ArtifactError("mode must be tombstone or purge_now")
        reason = self._delete_args(actor, reason)
        results = []
        for artifact_id in self._bulk_ids(artifact_ids):
            try:
                if mode == "purge_now":
                    results.append(await self.purge_now(
                        artifact_id, actor=actor, reason=reason, request_key=request_key,
                        override_legal_hold=override_legal_hold))
                else:
                    results.append(await self._tombstone_result(
                        artifact_id, actor, reason, request_key, hold_days, override_legal_hold))
            except ArtifactError as exc:
                results.append({"artifact_id": artifact_id, "result": "refused", "reason": _code(exc) or "not_allowed",
                                "replayed": False})
            except Exception:                           # noqa: BLE001 - reported per id, never as driver text
                results.append({"artifact_id": artifact_id, "result": "error", "reason": "store_error",
                                "replayed": False})
        return results

    @staticmethod
    def _bulk_ids(artifact_ids: list[str]) -> list[str]:
        from .artifacts import ArtifactError
        ids = list(dict.fromkeys(artifact_ids))
        if not ids:
            raise ArtifactError("at least one artifact id is required")
        if len(ids) > MAX_BULK:
            raise ArtifactError(f"at most {MAX_BULK} artifacts per request")
        return ids

    async def _tombstone_result(self, artifact_id: str, actor: str, reason: str, request_key: str | None,
                                hold_days: float, override_legal_hold: bool = False) -> dict:
        from .artifacts import ArtifactError
        out = {"artifact_id": artifact_id, "result": "tombstoned", "reason": None, "replayed": False,
               "unchanged": False}
        row = await self._conn.fetchrow("SELECT lifecycle FROM artifacts WHERE artifact_id=$1", artifact_id)
        if row is None:
            return {**out, "result": "not_found"}
        if await self._key_conflict(artifact_id, request_key, "tombstone"):
            await self._audit_refused(artifact_id, actor, reason, "request_key_reused", "tombstone")
            return {**out, "result": "refused", "reason": "request_key_reused"}
        if row["lifecycle"] == "tombstoned" and not await self._conn.fetchrow(
                "SELECT 1 AS x FROM artifact_lifecycle_events WHERE artifact_id=$1 AND request_key=$2",
                artifact_id, request_key):
            purged = await self._conn.fetchrow(
                "SELECT 1 AS x FROM artifact_lifecycle_events WHERE artifact_id=$1 AND action='purge'", artifact_id)
            return {**out, "result": "already_purged" if purged else "tombstoned", "unchanged": True}
        try:
            done = await self.tombstone_artifact(artifact_id, actor=actor, reason=reason, request_key=request_key,
                                                 hold_days=hold_days, override_legal_hold=override_legal_hold)
        except ArtifactError as exc:
            why = _code(exc) or "not_allowed"
            await self._audit_refused(artifact_id, actor, reason, why, "tombstone")
            return {**out, "result": "refused", "reason": why}
        return {**out, "replayed": done["replayed"]}

    async def restore_many(self, artifact_ids: list[str], *, actor: str, request_key: str | None = None,
                           override_legal_hold: bool = False) -> list[dict]:
        """Restore tombstoned artifacts whose bytes are not purged; one result per distinct id (at most ``MAX_BULK``)."""
        from .artifacts import ArtifactError
        results = []
        for artifact_id in self._bulk_ids(artifact_ids):
            out = {"artifact_id": artifact_id, "result": "restored", "reason": None, "replayed": False}
            try:
                row = await self._conn.fetchrow("SELECT lifecycle FROM artifacts WHERE artifact_id=$1", artifact_id)
                prior = row is not None and request_key and await self._conn.fetchrow(
                    "SELECT to_state FROM artifact_lifecycle_events WHERE artifact_id=$1 AND request_key=$2 "
                    "AND action='restore'", artifact_id, request_key)
                if row is None:
                    out["result"] = "not_found"
                elif prior and row["lifecycle"] != prior["to_state"]:
                    out.update(result="refused", reason="request_key_reused")      # stale key, state moved on
                elif prior:
                    out["replayed"] = True
                elif await self._key_conflict(artifact_id, request_key, "restore"):
                    out.update(result="refused", reason="request_key_reused")
                elif row["lifecycle"] != "tombstoned":
                    out.update(result="refused", reason="not_tombstoned")
                else:
                    await self.restore_artifact(artifact_id, actor=actor, reason="restored by owner",
                                                request_key=request_key, override_legal_hold=override_legal_hold)
            except ArtifactError as exc:
                out.update(result="refused", reason=_code(exc) or "not_allowed")
            except Exception:                           # noqa: BLE001 - reported per id, never as driver text
                out.update(result="error", reason="store_error")
            if out["result"] == "refused":
                await self._audit_refused(artifact_id, actor, "restore refused", out["reason"], "restore")
            results.append(out)
        return results

    async def list_tombstoned(self, *, limit: int = MAX_BULK) -> list[dict]:
        """Tombstoned artifacts (newest first) with their hold, for the owner's restore view."""
        rows = await self._conn.fetch(
            "SELECT a.artifact_id, a.original_filename, a.artifact_type, a.tombstoned_at, a.purge_after, "
            "(a.legal_hold OR a.purge_after IS NULL) AS legal_hold, "
            f"NOT ({_NOT_PURGED}) AS purged, (SELECT e.reason FROM artifact_lifecycle_events e "
            "WHERE e.artifact_id=a.artifact_id AND e.action='tombstone' ORDER BY e.event_id DESC LIMIT 1) AS reason "
            "FROM artifacts a WHERE a.lifecycle='tombstoned' ORDER BY a.tombstoned_at DESC LIMIT $1",
            max(1, min(MAX_BULK, int(limit))))
        return [self._jsonable(dict(row)) for row in rows]

    # -------------------------------------------------------------- reconcile
    async def mark_linked(self, version_id: str, *, error: str | None = None) -> None:
        """Record the outcome of the post-commit graph emission."""
        if error:
            await self._conn.execute(
                "UPDATE artifact_publish_ledger SET attempts=attempts+1, last_error=$2 WHERE version_id=$1",
                version_id, error[:500])
        else:
            await self._conn.execute(
                "UPDATE artifact_publish_ledger SET state='linked', attempts=attempts+1, last_error=NULL, "
                "linked_at=now() WHERE version_id=$1", version_id)

    async def reconcile(self, *, emit: Callable[[dict], Any] | None = None, grace_seconds: float = 60.0,
                        orphan_ttl_seconds: float = 24 * 3600.0, limit: int = 100) -> dict:
        """One idempotent repair pass. ``emit(version)`` re-emits lineage and returns ``{"error": ...}``."""
        from .artifacts import _transaction
        report = {"relinked": 0, "relink_failed": 0, "orphans_marked": 0, "orphans_cleared": 0,
                  "uploads_expired": 0, "purged": 0}
        if emit is not None:
            async with _transaction(self._conn):
                pending = await self._conn.fetch(
                    "SELECT version_id, artifact_id FROM artifact_publish_ledger WHERE state='committed' "
                    "AND committed_at < now() - ($1 * interval '1 second') ORDER BY committed_at LIMIT $2 "
                    "FOR UPDATE SKIP LOCKED", float(grace_seconds), int(limit))
                for item in pending:
                    version = await self.get_version(item["version_id"], item["artifact_id"])
                    if version is None:
                        continue
                    try:
                        outcome = emit(version)
                        if inspect.isawaitable(outcome):
                            outcome = await outcome
                        error = (outcome or {}).get("error") if isinstance(outcome, dict) else None
                    except Exception as exc:                  # noqa: BLE001 - recorded on the ledger
                        error = str(exc) or type(exc).__name__
                    await self.mark_linked(item["version_id"], error=error)
                    report["relink_failed" if error else "relinked"] += 1
        referenced = (
            "EXISTS (SELECT 1 FROM artifact_versions v WHERE v.artifact_id=b.scope AND ("
            " v.output_manifest @> jsonb_build_array(jsonb_build_object('storage_key', b.storage_key))"
            " OR v.output_manifest @> jsonb_build_array(jsonb_build_object('blob_sha256', b.blob_sha256))"
            " OR v.output_manifest @> jsonb_build_array(jsonb_build_object('sha256', b.blob_sha256))"
            " OR v.source_manifest_blob IN (b.storage_key, b.blob_sha256)))")
        marked = await self._conn.fetch(
            "UPDATE artifact_blobs b SET orphan_marked_at=now() WHERE b.scope<>'fabric' AND b.verified_at IS NOT NULL "
            "AND b.orphan_marked_at IS NULL AND b.created_at < now() - ($1 * interval '1 second') AND NOT "
            f"{referenced} RETURNING b.scope", float(orphan_ttl_seconds))
        report["orphans_marked"] = len(marked)
        cleared = await self._conn.fetch(
            f"UPDATE artifact_blobs b SET orphan_marked_at=NULL WHERE b.orphan_marked_at IS NOT NULL AND {referenced} "
            "RETURNING b.scope")
        report["orphans_cleared"] = len(cleared)
        report["legacy_holds_normalised"] = len(await self._conn.fetch(
            "UPDATE artifacts a SET legal_hold=true WHERE a.lifecycle='tombstoned' AND a.purge_after IS NULL "
            f"AND NOT a.legal_hold AND {_NOT_PURGED} RETURNING a.artifact_id"))
        report["uploads_expired"] = await self.expire_uploads()
        report["purged"] = len(await self.purge_due())
        return report

    async def list_orphan_blobs(self, *, limit: int = 100) -> list[dict]:
        rows = await self._conn.fetch(
            "SELECT scope AS artifact_id, blob_sha256, size_bytes, orphan_marked_at FROM artifact_blobs "
            "WHERE orphan_marked_at IS NOT NULL ORDER BY orphan_marked_at LIMIT $1", int(limit))
        return [self._jsonable(dict(row)) for row in rows]


__all__ = ["ArtifactLifecycleMixin", "DEFAULT_HOLD_DAYS", "MAX_BULK"]

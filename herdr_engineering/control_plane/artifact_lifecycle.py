"""Artifact lifecycle (archive / restore / tombstone / purge) and two-phase publish reconciliation.

Deletion policy: no registry row is ever deleted (a trigger forbids it for versions). ``archive`` hides and is reversible.
``tombstone`` records a reason, starts a hold window (``purge_after``) and is restorable until bytes are purged. ``purge``
is a system operation: it deletes object bytes only when no other live reference to the same content-addressed key
exists, then marks the versions ``purged``; hashes, provenance and the audit trail remain. Every step is an
append-only ``artifact_lifecycle_events`` row and is idempotent by ``request_key``.

Reconciliation repairs the window between "version committed" and "graph edges emitted", marks verified blobs that no
version references (never deleting them), expires stale upload sessions and runs due purges.
"""
from __future__ import annotations

import inspect
import json
from collections.abc import Callable
from typing import Any

from .artifact_members import is_member_principal

DEFAULT_HOLD_DAYS = 30.0
_TRANSITIONS = {
    "archive": ({"active"}, "archived"),
    "restore": ({"archived", "tombstoned"}, "active"),
    "tombstone": ({"active", "archived", "retracted"}, "tombstoned"),
}


class ArtifactLifecycleMixin:
    _conn: Any
    _rf: Any

    async def _lifecycle(self, action: str, artifact_id: str, *, actor: str, reason: str = "",
                         request_key: str | None = None, hold_days: float | None = DEFAULT_HOLD_DAYS) -> dict:
        from .artifacts import ArtifactError, _transaction
        if not actor:
            raise ArtifactError("actor is required")
        reason = (reason or "").strip()[:1000]
        if action == "tombstone" and not reason:
            raise ArtifactError("a tombstone needs a reason")
        allowed_from, to_state = _TRANSITIONS[action]
        async with _transaction(self._conn):
            row = await self._conn.fetchrow(
                "SELECT artifact_id, lifecycle FROM artifacts WHERE artifact_id=$1 FOR UPDATE", artifact_id)
            if row is None:
                raise ArtifactError(f"no artifact {artifact_id}")
            if request_key:
                prior = await self._conn.fetchrow(
                    "SELECT action, to_state FROM artifact_lifecycle_events WHERE artifact_id=$1 AND request_key=$2",
                    artifact_id, request_key)
                if prior:
                    if prior["action"] != action:
                        raise ArtifactError("request key was reused for a different lifecycle action")
                    return {"artifact_id": artifact_id, "lifecycle": prior["to_state"], "replayed": True}
            current = row["lifecycle"]
            if current not in allowed_from:
                raise ArtifactError(f"cannot {action} an artifact that is {current}")
            if action == "restore" and current == "tombstoned" and await self._conn.fetchrow(
                    "SELECT 1 AS x FROM artifact_versions WHERE artifact_id=$1 AND purged_at IS NOT NULL LIMIT 1",
                    artifact_id):
                raise ArtifactError("artifact bytes were already purged and cannot be restored")
            if action == "archive":
                await self._conn.execute(
                    "UPDATE artifacts SET lifecycle='archived', archived_at=now() WHERE artifact_id=$1", artifact_id)
            elif action == "restore":
                await self._conn.execute(
                    "UPDATE artifacts SET lifecycle='active', archived_at=NULL, tombstoned_at=NULL, purge_after=NULL "
                    "WHERE artifact_id=$1", artifact_id)
            else:
                days = None if hold_days is None else float(max(0.0, hold_days))
                await self._conn.execute(
                    "UPDATE artifacts SET lifecycle='tombstoned', tombstoned_at=now(), "
                    "purge_after=CASE WHEN $2::float8 IS NULL THEN NULL ELSE now() + ($2::float8 * interval '1 day') END "
                    "WHERE artifact_id=$1", artifact_id, days)
            await self._conn.execute(
                "INSERT INTO artifact_lifecycle_events (artifact_id, action, from_state, to_state, actor, reason, "
                "request_key) VALUES ($1,$2,$3,$4,$5,$6,$7)",
                artifact_id, action, current, to_state, actor, reason, request_key)
        return {"artifact_id": artifact_id, "lifecycle": to_state, "replayed": False}

    async def archive_artifact(self, artifact_id: str, *, actor: str, reason: str = "",
                               request_key: str | None = None) -> dict:
        return await self._lifecycle("archive", artifact_id, actor=actor, reason=reason, request_key=request_key)

    async def restore_artifact(self, artifact_id: str, *, actor: str, reason: str = "",
                               request_key: str | None = None) -> dict:
        return await self._lifecycle("restore", artifact_id, actor=actor, reason=reason, request_key=request_key)

    async def tombstone_artifact(self, artifact_id: str, *, actor: str, reason: str,
                                 request_key: str | None = None, legal_hold: bool = False,
                                 hold_days: float = DEFAULT_HOLD_DAYS) -> dict:
        """``legal_hold`` keeps bytes indefinitely (no purge date) until an operator sets one."""
        return await self._lifecycle("tombstone", artifact_id, actor=actor, reason=reason, request_key=request_key,
                                     hold_days=None if legal_hold else hold_days)

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

    async def _key_shared(self, artifact_id: str, key: str) -> bool:
        """True when a live artifact other than this one still references the content-addressed key."""
        digest = self._digest_of(key)
        row = await self._conn.fetchrow(
            "SELECT 1 AS x FROM artifact_versions v JOIN artifacts a ON a.artifact_id=v.artifact_id "
            "WHERE a.artifact_id<>$1 AND a.lifecycle<>'tombstoned' AND v.purged_at IS NULL AND ("
            " v.output_manifest @> jsonb_build_array(jsonb_build_object('storage_key', $2::text))"
            " OR v.output_manifest @> jsonb_build_array(jsonb_build_object('blob_sha256', $3::text))"
            " OR v.output_manifest @> jsonb_build_array(jsonb_build_object('sha256', $3::text))"
            " OR v.source_manifest_blob IN ($2::text, $3::text)) LIMIT 1", artifact_id, key, digest)
        if row:
            return True
        row = await self._conn.fetchrow(
            "SELECT 1 AS x FROM artifact_blobs b JOIN artifacts a ON a.artifact_id=b.scope "
            "WHERE b.scope<>$1 AND b.storage_key=$2 AND a.lifecycle<>'tombstoned' LIMIT 1", artifact_id, key)
        if row:
            return True
        row = await self._conn.fetchrow(
            "SELECT 1 AS x FROM artifacts a WHERE a.artifact_id<>$1 AND a.storage_key=$2 "
            "AND a.lifecycle<>'tombstoned' LIMIT 1", artifact_id, key)
        return row is not None

    async def purge_due(self, *, limit: int = 20) -> list[dict]:
        """Purge tombstoned artifacts whose hold has expired. Idempotent; returns one record per artifact."""
        from .artifacts import _transaction
        rows = await self._conn.fetch(
            "SELECT a.artifact_id FROM artifacts a WHERE a.lifecycle='tombstoned' AND a.purge_after IS NOT NULL "
            "AND a.purge_after <= now() AND (EXISTS (SELECT 1 FROM artifact_versions v WHERE "
            "v.artifact_id=a.artifact_id AND v.purged_at IS NULL) OR EXISTS (SELECT 1 FROM artifact_blobs b "
            "WHERE b.scope=a.artifact_id)) ORDER BY a.purge_after LIMIT $1", int(limit))
        results = []
        for row in rows:
            artifact_id = row["artifact_id"]
            async with _transaction(self._conn):
                await self._lock_owner_usage(artifact_id)
                locked = await self._conn.fetchrow(
                    "SELECT lifecycle FROM artifacts WHERE artifact_id=$1 FOR UPDATE", artifact_id)
                if locked is None or locked["lifecycle"] != "tombstoned":
                    continue
                outcome = await self._purge_locked(artifact_id, actor="system", reason="hold window elapsed")
            results.append({"artifact_id": artifact_id, **outcome})
        return results

    async def _lock_owner_usage(self, artifact_id: str) -> None:
        """A member owner's usage row is locked BEFORE the artifact row (publish takes them in that order), so the
        ``settle`` at the end of a purge cannot deadlock against a publish."""
        owner = await self._conn.fetchval("SELECT created_by FROM artifacts WHERE artifact_id=$1", artifact_id)
        if is_member_principal(owner):
            from .artifact_member_quota import _lock_usage
            await _lock_usage(self._conn, owner)

    async def _purge_locked(self, artifact_id: str, *, actor: str, reason: str, strict: bool = False) -> dict:
        """Delete the unshared bytes of a tombstoned artifact and mark its versions purged. The caller holds the
        artifact row lock inside a transaction. ``strict`` (operator purge) refuses to mark anything purged when an
        object delete failed, so a failure is never recorded as a purge. The non-strict (scheduled) path records a failed
        delete in ``retained_shared`` and still marks the versions purged: those keys are not retried (known gap). A member-owned artifact also has its
        recipient data anonymised and its owner's usage settled in the same transaction."""
        from .artifacts import ArtifactError
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
        deleted, retained, failed = [], [], []
        for key in sorted(keys):
            if not key.startswith("sha256/"):
                digest = self._digest_of(key)
                key = f"sha256/{digest[:2]}/{digest[2:4]}/{digest}" if len(digest) == 64 else key
            if not key.startswith("sha256/"):
                continue
            if await self._key_shared(artifact_id, key):
                retained.append(key)
                continue
            try:
                self._rf.delete(key)
                deleted.append(key)
            except Exception:                       # noqa: BLE001 - recorded in the outcome; not retried
                retained.append(key)
                failed.append(key)
        if failed and strict:
            raise ArtifactError(f"{len(failed)} object(s) could not be deleted; nothing was marked purged")
        await self._conn.execute(
            "UPDATE artifact_versions SET state='purged', purged_at=now() "
            "WHERE artifact_id=$1 AND purged_at IS NULL", artifact_id)
        await self._conn.execute("DELETE FROM artifact_blobs WHERE scope=$1", artifact_id)
        await self._conn.execute(
            "INSERT INTO artifact_lifecycle_events (artifact_id, action, from_state, to_state, actor, reason, "
            "detail) VALUES ($1,'purge','tombstoned','tombstoned',$2,$3,$4::jsonb)",
            artifact_id, actor, reason, json.dumps({"deleted": deleted, "retained_shared": retained}))
        owner = await self._conn.fetchval("SELECT created_by FROM artifacts WHERE artifact_id=$1", artifact_id)
        if is_member_principal(owner):
            await self._anonymise_member_data(artifact_id)
            from .artifact_member_quota import settle
            await settle(self._conn, owner)
        return {"deleted": deleted, "retained_shared": retained}

    async def _anonymise_member_data(self, artifact_id: str) -> None:
        """Per-member data tied to a purged artifact: the recipients and titles of its links, the free-text detail of
        its member audit rows and its access grants. The rows (who did what, when) remain; nothing identifying does."""
        await self._conn.execute(
            "UPDATE artifact_shares SET allow_emails='{}', allow_groups='{}', title=NULL WHERE artifact_id=$1",
            artifact_id)
        await self._conn.execute(
            "UPDATE artifact_member_events SET detail='{}'::jsonb WHERE artifact_id=$1", artifact_id)
        await self._conn.execute("DELETE FROM artifact_acl WHERE artifact_id=$1", artifact_id)
        # Personal data collected through the artifact's links: comment text and author identity, the viewers recorded
        # on opened/denied events (the rows stay, so the audit trail of what happened does), staged upload names, and
        # the artifact's own title and filename.
        await self._conn.execute(
            "UPDATE artifact_annotations SET body='[removed]', author_identity='[removed]', author_email='[removed]',"
            " file_name='[removed]', resolved_by=NULL WHERE artifact_id=$1", artifact_id)
        await self._conn.execute(
            "UPDATE artifact_share_events SET actor='[removed]', detail='{}'::jsonb WHERE kind IN ('opened','denied')"
            " AND share_id IN (SELECT share_id FROM artifact_shares WHERE artifact_id=$1)", artifact_id)
        await self._conn.execute("UPDATE artifact_collab_uploads SET name='[removed]' WHERE artifact_id=$1", artifact_id)
        await self._conn.execute(
            "UPDATE artifacts SET display_name=NULL, original_filename='[removed]' WHERE artifact_id=$1", artifact_id)

    async def purge_artifact(self, artifact_id: str, *, actor: str, reason: str = "operator purge") -> dict:
        """Operator purge, now: tombstone a retracted artifact (no hold window) and purge its bytes at once, ignoring
        the hold. The state afterwards is the one ``purge_due`` leaves: lifecycle stays ``tombstoned``, versions are
        ``purged``, blobs are deleted. Only a ``retracted`` artifact, or a ``tombstoned`` one with bytes left, can be
        purged; every share must already be revoked. Returns ``{"artifact_id", "deleted", "retained_shared"}``."""
        from .artifacts import ArtifactError, _transaction
        if not actor:
            raise ArtifactError("actor is required")
        async with _transaction(self._conn):
            await self._lock_owner_usage(artifact_id)
            row = await self._conn.fetchrow(
                "SELECT lifecycle FROM artifacts WHERE artifact_id=$1 FOR UPDATE", artifact_id)
            if row is None:
                raise ArtifactError(f"no artifact {artifact_id}")
            lifecycle = row["lifecycle"]
            if lifecycle not in ("retracted", "tombstoned"):
                raise ArtifactError(f"cannot purge an artifact that is {lifecycle}; retract it first")
            if lifecycle == "tombstoned" and not await self._conn.fetchval(
                    "SELECT EXISTS (SELECT 1 FROM artifact_versions WHERE artifact_id=$1 AND purged_at IS NULL)"
                    " OR EXISTS (SELECT 1 FROM artifact_blobs WHERE scope=$1)", artifact_id):
                raise ArtifactError("artifact bytes were already purged")
            # No purge date on a tombstone is a legal hold. TODO(artifact-delete merge): use
            # ``legal_hold OR purge_after IS NULL`` once that branch's column exists here.
            if lifecycle == "tombstoned" and await self._conn.fetchval(
                    "SELECT purge_after IS NULL FROM artifacts WHERE artifact_id=$1", artifact_id):
                raise ArtifactError("cannot purge an artifact under legal hold; lift the hold first")
            if await self._conn.fetchval(
                    "SELECT EXISTS (SELECT 1 FROM artifact_shares WHERE artifact_id=$1 AND state<>'revoked')",
                    artifact_id):
                raise ArtifactError("revoke the artifact's share links before purging")
            if lifecycle == "retracted":
                await self._conn.execute(
                    "UPDATE artifacts SET lifecycle='tombstoned', tombstoned_at=now(), purge_after=now() "
                    "WHERE artifact_id=$1", artifact_id)
                await self._conn.execute(
                    "INSERT INTO artifact_lifecycle_events (artifact_id, action, from_state, to_state, actor, reason) "
                    "VALUES ($1,'tombstone','retracted','tombstoned',$2,$3)", artifact_id, actor, reason)
            outcome = await self._purge_locked(artifact_id, actor=actor, reason=reason, strict=True)
        return {"artifact_id": artifact_id, **outcome}

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
        report["uploads_expired"] = await self.expire_uploads()
        report["purged"] = len(await self.purge_due())
        return report

    async def list_orphan_blobs(self, *, limit: int = 100) -> list[dict]:
        rows = await self._conn.fetch(
            "SELECT scope AS artifact_id, blob_sha256, size_bytes, orphan_marked_at FROM artifact_blobs "
            "WHERE orphan_marked_at IS NOT NULL ORDER BY orphan_marked_at LIMIT $1", int(limit))
        return [self._jsonable(dict(row)) for row in rows]


__all__ = ["ArtifactLifecycleMixin", "DEFAULT_HOLD_DAYS"]

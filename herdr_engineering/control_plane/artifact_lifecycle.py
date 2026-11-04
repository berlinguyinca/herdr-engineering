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

DEFAULT_HOLD_DAYS = 30.0
MAX_BULK = 200
MAX_REASON = 1000
DELETE_MODES = ("tombstone", "purge_now")
_NOT_PURGED = ("NOT EXISTS (SELECT 1 FROM artifact_lifecycle_events pe WHERE pe.artifact_id=a.artifact_id "
               "AND pe.action='purge')")
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
                if current == "tombstoned" and await self._conn.fetchval(
                        "SELECT from_state FROM artifact_lifecycle_events WHERE artifact_id=$1 AND action='tombstone' "
                        "ORDER BY event_id DESC LIMIT 1", artifact_id) == "retracted":
                    to_state = "retracted"          # a hidden artifact must not reappear because it was tombstoned
                await self._conn.execute(
                    "UPDATE artifacts SET lifecycle=$2, archived_at=NULL, tombstoned_at=NULL, purge_after=NULL "
                    "WHERE artifact_id=$1", artifact_id, to_state)
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
            f"SELECT 1 AS x FROM artifact_versions v JOIN artifacts a ON a.artifact_id=v.artifact_id "
            f"WHERE a.artifact_id<>$1 AND {_NOT_PURGED} AND v.purged_at IS NULL AND ("
            " v.output_manifest @> jsonb_build_array(jsonb_build_object('storage_key', $2::text))"
            " OR v.output_manifest @> jsonb_build_array(jsonb_build_object('blob_sha256', $3::text))"
            " OR v.output_manifest @> jsonb_build_array(jsonb_build_object('sha256', $3::text))"
            " OR v.source_manifest_blob IN ($2::text, $3::text)) LIMIT 1", artifact_id, key, digest)
        if row:
            return True
        row = await self._conn.fetchrow(
            "SELECT 1 AS x FROM artifact_blobs b JOIN artifacts a ON a.artifact_id=b.scope "
            f"WHERE b.scope<>$1 AND b.storage_key=$2 AND {_NOT_PURGED} LIMIT 1", artifact_id, key)
        if row:
            return True
        row = await self._conn.fetchrow(
            f"SELECT 1 AS x FROM artifacts a WHERE a.artifact_id<>$1 AND a.storage_key=$2 "
            f"AND {_NOT_PURGED} LIMIT 1", artifact_id, key)
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
                locked = await self._conn.fetchrow(
                    "SELECT lifecycle FROM artifacts WHERE artifact_id=$1 FOR UPDATE", artifact_id)
                if locked is None or locked["lifecycle"] != "tombstoned":
                    continue
                results.append(await self._purge_locked(artifact_id, actor="system", reason="hold window elapsed"))
        return results

    async def _purge_locked(self, artifact_id: str, *, actor: str, reason: str, request_key: str | None = None,
                            extra: dict | None = None) -> dict:
        """Delete the unshared bytes of a tombstoned artifact. The caller holds the artifact row lock in a transaction."""
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
        deleted, retained = [], []
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
            except Exception:                           # noqa: BLE001 - recorded, retried next sweep
                retained.append(key)
        await self._conn.execute(
            "UPDATE artifact_versions SET state='purged', purged_at=now() WHERE artifact_id=$1 AND purged_at IS NULL",
            artifact_id)
        await self._conn.execute("DELETE FROM artifact_blobs WHERE scope=$1", artifact_id)
        await self._conn.execute(
            "INSERT INTO artifact_lifecycle_events (artifact_id, action, from_state, to_state, actor, reason, "
            "request_key, detail) VALUES ($1,'purge','tombstoned','tombstoned',$2,$3,$4,$5::jsonb)",
            artifact_id, actor, reason, request_key,
            json.dumps({"deleted": deleted, "retained_shared": retained, **(extra or {})}))
        return {"artifact_id": artifact_id, "deleted": deleted, "retained_shared": retained}

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

    async def purge_now(self, artifact_id: str, *, actor: str, reason: str, request_key: str | None = None,
                        override_legal_hold: bool = False) -> dict:
        """Tombstone (when needed) and purge immediately, in one transaction. Hashes, provenance and audit stay.

        ``result`` is ``deleted``, ``already_purged``, ``not_found`` or ``refused`` (``reason`` is then a fixed code:
        ``legal_hold`` for a tombstone without a purge date, unless ``override_legal_hold``). A repeated
        ``request_key`` is answered from the audit trail and changes nothing."""
        from .artifacts import _transaction
        reason = self._delete_args(actor, reason)
        out = {"artifact_id": artifact_id, "result": "not_found", "reason": None, "replayed": False,
               "deleted": [], "retained_shared": []}
        async with _transaction(self._conn):
            row = await self._conn.fetchrow(
                "SELECT lifecycle, purge_after FROM artifacts WHERE artifact_id=$1 FOR UPDATE", artifact_id)
            if row is None:
                return out
            prior = await self._conn.fetchrow(
                "SELECT request_key FROM artifact_lifecycle_events WHERE artifact_id=$1 AND action='purge' "
                "ORDER BY (request_key IS NOT DISTINCT FROM $2) DESC, event_id LIMIT 1", artifact_id, request_key)
            if prior is not None:
                return {**out, "result": "already_purged",
                        "replayed": bool(request_key) and prior["request_key"] == request_key}
            held = row["lifecycle"] == "tombstoned" and row["purge_after"] is None
            if held and not override_legal_hold:
                return {**out, "result": "refused", "reason": "legal_hold"}
            if row["lifecycle"] != "tombstoned":
                await self._lifecycle("tombstone", artifact_id, actor=actor, reason=reason, hold_days=0.0)
            purged = await self._purge_locked(
                artifact_id, actor=actor, reason=reason, request_key=request_key,
                extra={"legal_hold_overridden": True} if held else None)
        return {**out, "result": "deleted", "deleted": purged["deleted"], "retained_shared": purged["retained_shared"]}

    async def delete_many(self, artifact_ids: list[str], *, mode: str, actor: str, reason: str,
                          request_key: str | None = None, hold_days: float = DEFAULT_HOLD_DAYS,
                          override_legal_hold: bool = False) -> list[dict]:
        """``tombstone`` or ``purge_now`` every distinct id (at most ``MAX_BULK``); one result per id, in order."""
        from .artifacts import ArtifactError
        if mode not in DELETE_MODES:
            raise ArtifactError("mode must be tombstone or purge_now")
        reason = self._delete_args(actor, reason)
        ids = self._bulk_ids(artifact_ids)
        results = []
        for artifact_id in ids:
            if mode == "purge_now":
                results.append(await self.purge_now(
                    artifact_id, actor=actor, reason=reason, request_key=request_key,
                    override_legal_hold=override_legal_hold))
            else:
                results.append(await self._tombstone_result(artifact_id, actor, reason, request_key, hold_days))
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
                                hold_days: float) -> dict:
        from .artifacts import ArtifactError
        out = {"artifact_id": artifact_id, "result": "tombstoned", "reason": None, "replayed": False,
               "unchanged": False}
        row = await self._conn.fetchrow("SELECT lifecycle FROM artifacts WHERE artifact_id=$1", artifact_id)
        if row is None:
            return {**out, "result": "not_found"}
        if row["lifecycle"] == "tombstoned" and not await self._conn.fetchrow(
                "SELECT 1 AS x FROM artifact_lifecycle_events WHERE artifact_id=$1 AND request_key=$2",
                artifact_id, request_key):
            purged = await self._conn.fetchrow(
                "SELECT 1 AS x FROM artifact_lifecycle_events WHERE artifact_id=$1 AND action='purge'", artifact_id)
            return {**out, "result": "already_purged" if purged else "tombstoned", "unchanged": True}
        try:
            done = await self.tombstone_artifact(artifact_id, actor=actor, reason=reason, request_key=request_key,
                                                 hold_days=hold_days)
        except ArtifactError:
            return {**out, "result": "refused", "reason": "not_allowed"}
        return {**out, "replayed": done["replayed"]}

    async def restore_many(self, artifact_ids: list[str], *, actor: str, request_key: str | None = None) -> list[dict]:
        """Restore tombstoned artifacts whose bytes are not purged; one result per distinct id (at most ``MAX_BULK``)."""
        from .artifacts import ArtifactError
        results = []
        for artifact_id in self._bulk_ids(artifact_ids):
            out = {"artifact_id": artifact_id, "result": "restored", "reason": None, "replayed": False}
            row = await self._conn.fetchrow("SELECT lifecycle FROM artifacts WHERE artifact_id=$1", artifact_id)
            prior = row is not None and request_key and await self._conn.fetchrow(
                "SELECT 1 AS x FROM artifact_lifecycle_events WHERE artifact_id=$1 AND request_key=$2 "
                "AND action='restore'", artifact_id, request_key)
            if row is None:
                out["result"] = "not_found"
            elif prior:
                out["replayed"] = True
            elif row["lifecycle"] != "tombstoned":
                out.update(result="refused", reason="not_tombstoned")
            else:
                try:
                    await self.restore_artifact(artifact_id, actor=actor, reason="restored by owner",
                                                request_key=request_key)
                except ArtifactError:
                    out.update(result="refused", reason="purged")
            results.append(out)
        return results

    async def list_tombstoned(self, *, limit: int = MAX_BULK) -> list[dict]:
        """Tombstoned artifacts (newest first) with their hold, for the owner's restore view."""
        rows = await self._conn.fetch(
            "SELECT a.artifact_id, a.original_filename, a.artifact_type, a.tombstoned_at, a.purge_after, "
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
        report["uploads_expired"] = await self.expire_uploads()
        report["purged"] = len(await self.purge_due())
        return report

    async def list_orphan_blobs(self, *, limit: int = 100) -> list[dict]:
        rows = await self._conn.fetch(
            "SELECT scope AS artifact_id, blob_sha256, size_bytes, orphan_marked_at FROM artifact_blobs "
            "WHERE orphan_marked_at IS NOT NULL ORDER BY orphan_marked_at LIMIT $1", int(limit))
        return [self._jsonable(dict(row)) for row in rows]


__all__ = ["ArtifactLifecycleMixin", "DEFAULT_HOLD_DAYS", "MAX_BULK"]

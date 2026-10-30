"""Artifact model + bindings + materialization (Spec §56-74)."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from . import ids
from .artifact_context import ArtifactContextMixin
from .artifact_lifecycle import ArtifactLifecycleMixin
from .artifact_provenance import ProvenanceError, context_links, normalize_origin
from .artifact_uploads import ArtifactUploadMixin
from .rustfs import IntegrityError, RustFSClient

__all__ = ["ArtifactStore", "ArtifactError", "ArtifactConflict", "IntegrityError"]


class ArtifactError(Exception):
    pass


class ArtifactConflict(ArtifactError):
    """The caller attempted to commit against a stale logical artifact head."""

    def __init__(self, artifact_id: str, current_version_id: str | None):
        self.artifact_id = artifact_id
        self.current_version_id = current_version_id
        super().__init__(
            f"artifact {artifact_id} changed; current version is {current_version_id or 'none'}"
        )


@asynccontextmanager
async def _transaction(conn: Any):
    transaction = getattr(conn, "transaction", None)
    if transaction is None:
        yield conn
        return
    async with transaction():
        yield conn


class ArtifactStore(ArtifactContextMixin, ArtifactUploadMixin, ArtifactLifecycleMixin):
    def __init__(self, conn: Any, rustfs: RustFSClient):
        self._conn = conn
        self._rf = rustfs

    async def register_logical(self, *, original_filename: str,
                               artifact_type: str = "other",
                               created_by: str | None = None,
                               mime_type: str | None = None,
                               workspace_id: str | None = None) -> dict:
        """Create a durable logical artifact without creating a mutable blob."""
        aid = ids.new_id("artifact")
        await self._conn.execute(
            "INSERT INTO artifacts (artifact_id, original_filename, mime_type, "
            "artifact_type, created_by, workspace_id, status, lifecycle) "
            "VALUES ($1,$2,$3,$4,$5,$6,'draft','active')",
            aid, original_filename, mime_type, artifact_type, created_by, workspace_id,
        )
        return {
            "artifact_id": aid,
            "original_filename": original_filename,
            "mime_type": mime_type,
            "artifact_type": artifact_type,
            "created_by": created_by,
            "workspace_id": workspace_id,
            "current_version_id": None,
            "status": "draft",
            "lifecycle": "active",
        }

    async def commit_version(self, artifact_id: str, *,
                             base_version_id: str | None,
                             source_manifest_blob: str | None,
                             output_manifest: list[dict],
                             provenance: dict | None = None,
                             created_by: str | None = None,
                             agent_id: str | None = None,
                             host_id: str | None = None,
                             session_id: str | None = None,
                             mission_id: str | None = None,
                             job_id: str | None = None,
                             idempotency_key: str | None = None,
                             report_descriptor: dict | None = None,
                             validation: dict | None = None,
                             origin: dict | None = None) -> dict:
        """Commit one immutable version using compare-and-swap on the head.

        ``origin`` carries P03 provenance (room/task/issue/project, session, agent, model, prompt/skill revision,
        commit, inputs). It is validated and redacted here and committed with the version in one transaction, together
        with the lookup bindings and the publish-ledger row that reconciliation repairs.
        """
        try:
            origin_columns = normalize_origin(origin)
        except ProvenanceError as exc:
            raise ArtifactError(str(exc)) from exc
        if report_descriptor is not None:
            from .artifact_report_descriptor import validate_report_descriptor
            report_descriptor = validate_report_descriptor(report_descriptor, output_manifest)
        validation = {} if validation is None else validation
        if not isinstance(validation, dict):
            raise ArtifactError("validation must be an object")
        report_json = json.dumps(report_descriptor, sort_keys=True) if report_descriptor is not None else None
        validation_json = json.dumps(validation, sort_keys=True, allow_nan=False)
        output_json = json.dumps(output_manifest, sort_keys=True, separators=(",", ":"))
        provenance_json = json.dumps(provenance or {}, sort_keys=True, separators=(",", ":"))
        request_body = {"artifact_id": artifact_id, "base_version_id": base_version_id,
                        "source_manifest_blob": source_manifest_blob,
                        "output_manifest": output_manifest, "provenance": provenance or {}}
        if report_descriptor is not None:
            request_body["report_descriptor"] = report_descriptor
        if validation:
            request_body["validation"] = validation
        if origin:
            request_body["origin"] = origin
        request_sha = hashlib.sha256(
            json.dumps(request_body,
                       sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        principal = created_by or "unknown"
        if not isinstance(output_manifest, list):
            raise ArtifactError("output_manifest must be a list")
        for output in output_manifest:
            if not isinstance(output, dict):
                raise ArtifactError("each output manifest entry must be an object")
            reference = output.get("storage_key") or output.get("blob_sha256")
            if not reference:
                raise ArtifactError("each output manifest entry needs a blob reference")
            if not isinstance(reference, str):
                raise ArtifactError("output blob reference must be a string")
            digests = [output[field] for field in ("sha256", "blob_sha256")
                       if output.get(field) is not None]
            if any(not isinstance(value, str) or not value for value in digests):
                raise ArtifactError("output blob digest must be a nonempty string")
            reference_digest = reference.rsplit("/", 1)[-1]
            if len(reference_digest) == 64 and all(char in "0123456789abcdef" for char in reference_digest):
                digests.append(reference_digest)
            if len(set(digests)) > 1:
                raise ArtifactError("conflicting output blob digest/reference metadata")
        await self._validate_blob_reference(source_manifest_blob)
        for output in output_manifest:
            await self._validate_blob_reference(
                output.get("storage_key") or output.get("blob_sha256"))
        if report_descriptor is None:
            # Legacy unscoped blobs remain compatible, but an artifact-scoped
            # upload cannot become public by attaching it to a non-report target.
            if source_manifest_blob is not None:
                await self._validate_existing_blob_scope(artifact_id, source_manifest_blob)
            for output in output_manifest:
                await self._validate_existing_blob_scope(artifact_id,
                    output.get("storage_key") or output.get("blob_sha256"),
                    sha256=output.get("sha256") or output.get("blob_sha256"),
                    size_bytes=output.get("size_bytes"), media_type=output.get("media_type"))
        if report_descriptor is not None:
            # Every report output/source needs the same artifact scope. Otherwise
            # an editor could omit a private blob from descriptor.assets, attach
            # it as an extra output, and later share their own stolen-content version.
            for output in output_manifest:
                await self._report_scoped_blob(artifact_id,
                    output.get("storage_key") or output.get("blob_sha256"),
                    sha256=output.get("sha256") or output.get("blob_sha256"),
                    size_bytes=output.get("size_bytes"), media_type=output.get("media_type"))
            if source_manifest_blob is not None:
                await self._report_scoped_blob(artifact_id, source_manifest_blob)
            for asset in report_descriptor["assets"]:
                blob = await self._conn.fetchrow(
                    "SELECT storage_key, size_bytes, media_type FROM artifact_blobs "
                    "WHERE scope=$1 AND blob_sha256=$2 AND verified_at IS NOT NULL",
                    artifact_id, asset["sha256"])
                if not blob:
                    raise ArtifactError("report asset must be uploaded in this artifact scope")
                if int(blob["size_bytes"]) != asset["size_bytes"] or blob["media_type"] != asset["media_type"]:
                    raise ArtifactError("report asset metadata differs from its scoped upload")
                matching = next(output for output in output_manifest
                                if output.get("name") == asset["name"] and output.get("sha256") == asset["sha256"])
                if (matching.get("storage_key") or matching.get("blob_sha256")) not in {
                        blob["storage_key"], asset["sha256"]}:
                    raise ArtifactError("report asset storage reference differs from its scoped upload")
        async with _transaction(self._conn):
            if idempotency_key:
                prior = await self._conn.fetchrow(
                    "SELECT version_id, request_sha256 FROM artifact_idempotency "
                    "WHERE principal_id=$1 AND idempotency_key=$2",
                    principal, idempotency_key,
                )
                if prior:
                    if prior["request_sha256"] != request_sha:
                        raise ArtifactError("idempotency key was reused for a different request")
                    existing = await self.get_version(prior["version_id"])
                    if existing is not None:
                        return existing
            artifact = await self._conn.fetchrow(
                "SELECT artifact_id, current_version_id, artifact_type FROM artifacts "
                "WHERE artifact_id=$1 FOR UPDATE", artifact_id,
            )
            if artifact is None:
                raise ArtifactError(f"no artifact {artifact_id}")
            if artifact.get("artifact_type") == "report" and report_descriptor is None:
                raise ArtifactError("report artifact versions require a report descriptor")
            if idempotency_key:
                # The artifact row lock serializes retries for the same logical
                # artifact. Re-read the key after acquiring it so a concurrent
                # identical request returns the winner instead of conflicting.
                prior = await self._conn.fetchrow(
                    "SELECT version_id, request_sha256 FROM artifact_idempotency "
                    "WHERE principal_id=$1 AND idempotency_key=$2",
                    principal, idempotency_key,
                )
                if prior:
                    if prior["request_sha256"] != request_sha:
                        raise ArtifactError("idempotency key was reused for a different request")
                    existing = await self.get_version(prior["version_id"])
                    if existing is not None:
                        return existing
            current = artifact.get("current_version_id")
            if current != base_version_id:
                raise ArtifactConflict(artifact_id, current)
            number_row = await self._conn.fetchrow(
                "SELECT COALESCE(MAX(version_number), 0) + 1 AS next_number "
                "FROM artifact_versions WHERE artifact_id=$1", artifact_id,
            )
            version_number = int(number_row["next_number"])
            version_id = ids.new_id("version")
            await self._conn.execute(
                "INSERT INTO artifact_versions (version_id, artifact_id, version_number, "
                "parent_version_id, base_version_id, state, source_manifest_blob, "
                "output_manifest, provenance, created_by, agent_id, host_id, session_id, "
                "mission_id, job_id, committed_at, report_descriptor, validation, origin_room_id, origin_task_id, "
                "origin_issue_ref, origin_project_id, origin_session_ref, origin_agent_ref, model_id, "
                "prompt_revision, skill_revision, commit_sha, redacted_inputs) VALUES "
                "($1,$2,$3,$4,$5,'ready',$6,$7::jsonb,$8::jsonb,$9,$10,$11,$12,$13,$14,now(),$15::jsonb,$16::jsonb,"
                "$17,$18,$19,$20,$21,$22,$23,$24,$25,$26,$27::jsonb)",
                version_id, artifact_id, version_number, base_version_id, base_version_id,
                source_manifest_blob, output_json, provenance_json, created_by, agent_id,
                host_id, session_id, mission_id, job_id, report_json, validation_json,
                origin_columns["origin_room_id"], origin_columns["origin_task_id"],
                origin_columns["origin_issue_ref"], origin_columns["origin_project_id"],
                origin_columns["origin_session_ref"], origin_columns["origin_agent_ref"],
                origin_columns["model_id"], origin_columns["prompt_revision"], origin_columns["skill_revision"],
                origin_columns["commit_sha"], json.dumps(origin_columns["redacted_inputs"]),
            )
            links = context_links(origin_columns)
            if any(links.values()):
                await self._insert_link(
                    artifact_id, created_by=created_by, task_id=links["task_id"], project_id=links["project_id"],
                    issue_ref=links["issue_ref"], room_id=links["room_id"], link_kind="origin",
                    version_id=version_id, source_event_id=f"art-{version_id}-links")
            await self._conn.execute(
                "INSERT INTO artifact_publish_ledger (version_id, artifact_id, source_event_id) VALUES ($1,$2,$3)",
                version_id, artifact_id, f"art-{version_id}")
            await self._conn.execute(
                "UPDATE artifacts SET current_version_id=$1, status='available' "
                "WHERE artifact_id=$2", version_id, artifact_id,
            )
            if report_descriptor is not None:
                await self._conn.execute(
                    "UPDATE artifacts SET artifact_type='report', size_bytes=$1 WHERE artifact_id=$2",
                    sum(output.get("size_bytes", 0) for output in output_manifest), artifact_id)
            if idempotency_key:
                await self._conn.execute(
                    "INSERT INTO artifact_idempotency "
                    "(principal_id, idempotency_key, request_sha256, version_id) "
                    "VALUES ($1,$2,$3,$4)",
                    principal, idempotency_key, request_sha, version_id,
                )
            await self._conn.execute(
                "INSERT INTO artifact_audit "
                "(artifact_id, version_id, action, principal_id, host_id, session_id) "
                "VALUES ($1,$2,'version_committed',$3,$4,$5)",
                artifact_id, version_id, created_by, host_id, session_id,
            )
        return {
            "version_id": version_id,
            "artifact_id": artifact_id,
            "version_number": version_number,
            "parent_version_id": base_version_id,
            "base_version_id": base_version_id,
            "state": "ready",
            "source_manifest_blob": source_manifest_blob,
            "output_manifest": output_manifest,
            "provenance": provenance or {},
            "created_by": created_by,
            "agent_id": agent_id,
            "host_id": host_id,
            "session_id": session_id,
            "mission_id": mission_id,
            "job_id": job_id,
            "report_descriptor": report_descriptor,
            "validation": validation,
            **{key: value for key, value in origin_columns.items() if value not in (None, [])},
        }

    async def report_permission(self, artifact_id: str, principal_id: str, permission: str) -> bool:
        """Private report operations use durable owner/grant authority, not a guessed context."""
        if not principal_id or permission not in {"read", "edit", "review", "share", "admin"}:
            return False
        artifact = await self._conn.fetchrow(
            "SELECT created_by FROM artifacts WHERE artifact_id=$1", artifact_id)
        if artifact is None:
            return False
        if artifact.get("created_by") == principal_id:
            return True
        grant = await self._conn.fetchrow(
            "SELECT permissions FROM artifact_acl WHERE artifact_id=$1 AND principal_id=$2 "
            "AND (expires_at IS NULL OR expires_at > now())", artifact_id, principal_id)
        if not grant:
            return False
        permissions = grant["permissions"]
        if isinstance(permissions, str):
            permissions = json.loads(permissions)
        return permission in permissions or "admin" in permissions

    async def _validate_existing_blob_scope(self, artifact_id, reference, **metadata):
        # Inspect the actual reference, never the caller's optional SHA metadata:
        # a forged digest must not hide the scope of the requested storage key.
        digest = reference.rsplit("/", 1)[-1] if isinstance(reference, str) else ""
        scoped = await self._conn.fetchrow(
            "SELECT scope FROM artifact_blobs WHERE scope <> 'fabric' "
            "AND (storage_key=$1 OR blob_sha256=$2) LIMIT 1", reference, digest)
        if scoped:
            await self._report_scoped_blob(artifact_id, reference, **metadata)

    async def _report_scoped_blob(self, artifact_id, reference, *, sha256=None,
                                  size_bytes=None, media_type=None):
        digest = sha256 or (reference.rsplit("/", 1)[-1] if isinstance(reference, str) else "")
        if (not isinstance(digest, str) or len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)):
            raise ArtifactError("report references require a scoped uploaded SHA-256 blob")
        key = f"sha256/{digest[:2]}/{digest[2:4]}/{digest}"
        blob = await self._conn.fetchrow(
            "SELECT storage_key, size_bytes, media_type FROM artifact_blobs "
            "WHERE scope=$1 AND blob_sha256=$2 AND verified_at IS NOT NULL", artifact_id, digest)
        if not blob or reference not in {key, digest} or blob["storage_key"] != key:
            raise ArtifactError("report reference must be uploaded in this artifact scope")
        if size_bytes is not None and size_bytes != blob["size_bytes"]:
            raise ArtifactError("report output size differs from its scoped blob")
        if media_type is not None and media_type != blob["media_type"]:
            raise ArtifactError("report output media type differs from its scoped blob")
        return blob

    async def verify_share_outputs(self, artifact_id: str, version_id: str) -> int:
        """Re-run the commit-time scope rules on a version's outputs before they are copied into a share bundle.

        Report versions need every output scoped to this artifact (``_report_scoped_blob``); other versions may use
        legacy unscoped blobs but never one registered to another artifact (``_validate_existing_blob_scope``).
        Returns the number of outputs checked; raises ``ArtifactError`` otherwise.
        """
        version = await self.get_version(version_id, artifact_id)
        if version is None:
            raise ArtifactError("no artifact version")
        outputs = version.get("output_manifest") or []
        check = self._report_scoped_blob if version.get("report_descriptor") else self._validate_existing_blob_scope
        for output in outputs:
            if not isinstance(output, dict):
                raise ArtifactError("each output manifest entry must be an object")
            await check(artifact_id, output.get("storage_key") or output.get("blob_sha256"),
                        sha256=output.get("sha256") or output.get("blob_sha256"),
                        size_bytes=output.get("size_bytes"), media_type=output.get("media_type"))
        return len(outputs)

    async def register_report_blob(self, artifact_id: str, *, storage_key: str, sha256: str,
                                   size_bytes: int, media_type: str) -> dict:
        """Record bytes already verified by the scoped HTTP/worker upload boundary."""
        if (not isinstance(sha256, str) or len(sha256) != 64
                or any(character not in "0123456789abcdef" for character in sha256)
                or isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or size_bytes < 0
                or not isinstance(media_type, str) or not media_type or len(media_type) > 255):
            raise ArtifactError("invalid report blob metadata")
        expected = f"sha256/{sha256[:2]}/{sha256[2:4]}/{sha256}"
        if storage_key != expected:
            raise ArtifactError("report blob key must match its content digest")
        await self._validate_blob_reference(storage_key)
        previous = await self._conn.fetchrow(
            "SELECT storage_key, size_bytes, media_type FROM artifact_blobs "
            "WHERE scope=$1 AND blob_sha256=$2", artifact_id, sha256)
        if previous and (previous["storage_key"] != storage_key or previous["size_bytes"] != size_bytes
                         or previous["media_type"] != media_type):
            raise ArtifactError("immutable scoped blob metadata conflict")
        await self._conn.execute(
            "INSERT INTO artifact_blobs (scope, blob_sha256, storage_key, size_bytes, media_type, verified_at) "
            "VALUES ($1,$2,$3,$4,$5,now()) ON CONFLICT (scope, blob_sha256) DO NOTHING",
            artifact_id, sha256, storage_key, size_bytes, media_type)
        return {"storage_key": storage_key, "sha256": sha256, "size_bytes": size_bytes,
                "media_type": media_type}

    async def get_report(self, artifact_id: str, version_id: str) -> dict:
        from .artifact_report_state import ArtifactReportState
        version = await self.get_version(version_id, artifact_id)
        if version is None or not version.get("report_descriptor"):
            raise ArtifactError("no artifact report version")
        state = ArtifactReportState(self)
        feedback = await state.list_feedback(artifact_id, version_id, limit=10)
        feedback_count = await state.feedback_count(artifact_id, version_id)
        return {**await state.inspect(artifact_id, version_id),
                "report_descriptor": version["report_descriptor"],
                "validation": version.get("validation") or {},
                "assets": version["report_descriptor"].get("assets", []),
                "feedback": feedback, "feedback_count": feedback_count,
                "feedback_has_more": len(feedback) < feedback_count}

    async def get_report_descriptor(self, artifact_id: str, version_id: str) -> dict:
        version = await self.get_version(version_id, artifact_id)
        if not version or not version.get("report_descriptor"):
            raise ArtifactError("no artifact report version")
        return version["report_descriptor"]

    async def get_report_diff(self, artifact_id: str, version_id: str, base_version_id: str) -> dict:
        from .artifact_report_descriptor import report_diff
        current = await self.get_version(version_id, artifact_id)
        base = await self.get_version(base_version_id, artifact_id)
        if not current or not base or not current.get("report_descriptor") or not base.get("report_descriptor"):
            raise ArtifactError("no artifact report comparison")
        return {"artifact_id": artifact_id, "version_id": version_id, "base_version_id": base_version_id,
                **report_diff(base["report_descriptor"], current["report_descriptor"])}

    async def record_report_check(self, artifact_id: str, version_id: str, **values) -> dict:
        from .artifact_report_state import ArtifactReportState
        return await ArtifactReportState(self).record_check(artifact_id, version_id, **values)

    async def add_report_feedback(self, artifact_id: str, version_id: str, **values) -> dict:
        from .artifact_report_state import ArtifactReportState
        return await ArtifactReportState(self).add_feedback(artifact_id, version_id, **values)

    async def list_report_feedback(self, artifact_id: str, version_id: str, **pagination) -> list[dict]:
        from .artifact_report_state import ArtifactReportState
        return await ArtifactReportState(self).list_feedback(artifact_id, version_id, **pagination)

    async def count_report_feedback(self, artifact_id: str, version_id: str) -> int:
        from .artifact_report_state import ArtifactReportState
        return await ArtifactReportState(self).feedback_count(artifact_id, version_id)

    async def list_version_summaries(self, artifact_id: str) -> list[dict]:
        rows = await self._conn.fetch(
            "SELECT version_id, artifact_id, version_number, parent_version_id, state, created_at, "
            "committed_at, (report_descriptor IS NOT NULL) AS is_report FROM artifact_versions "
            "WHERE artifact_id=$1 ORDER BY version_number DESC", artifact_id)
        return [self._row(row) for row in rows]

    async def get_report_job(self, artifact_id: str, job_id: str) -> dict | None:
        row = await self._conn.fetchrow(
            "SELECT * FROM artifact_build_jobs WHERE artifact_id=$1 AND job_id=$2 AND profile='report'",
            artifact_id, job_id)
        return self._row(row)

    async def cancel_report_job(self, artifact_id: str, job_id: str) -> dict | None:
        row = await self._conn.fetchrow(
            "UPDATE artifact_build_jobs SET status='cancelled', completed_at=now() "
            "WHERE artifact_id=$1 AND job_id=$2 AND profile='report' AND status='queued' RETURNING *",
            artifact_id, job_id)
        return self._row(row)

    async def get_report_asset(self, artifact_id: str, version_id: str, asset_id: str) -> dict:
        version = await self.get_version(version_id, artifact_id)
        if not version or not version.get("report_descriptor"):
            raise ArtifactError("no artifact report version")
        asset = next((value for value in version["report_descriptor"]["assets"] if value["id"] == asset_id), None)
        if asset is None:
            raise ArtifactError("no artifact report asset")
        blob = await self._conn.fetchrow(
            "SELECT storage_key, size_bytes, media_type FROM artifact_blobs "
            "WHERE scope=$1 AND blob_sha256=$2 AND verified_at IS NOT NULL", artifact_id, asset["sha256"])
        if not blob or blob["size_bytes"] != asset["size_bytes"] or blob["media_type"] != asset["media_type"]:
            raise ArtifactError("no artifact verified report asset")
        return {**asset, "storage_key": blob["storage_key"]}

    async def _validate_blob_reference(self, reference: str | None) -> None:
        if not reference:
            return
        if isinstance(reference, str) and len(reference) == 64 and all(
                char in "0123456789abcdef" for char in reference):
            reference = f"sha256/{reference[:2]}/{reference[2:4]}/{reference}"
        head = getattr(self._rf, "head", None)
        if head is None or not head(reference):
            raise ArtifactError(f"artifact blob is not verified: {reference}")

    @staticmethod
    def _jsonable(value: Any) -> Any:
        if isinstance(value, (datetime, date)):
            return value.isoformat()
        if isinstance(value, dict):
            return {key: ArtifactStore._jsonable(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [ArtifactStore._jsonable(item) for item in value]
        return value

    @staticmethod
    def _row(row: Any) -> dict | None:
        if row is None:
            return None
        value = dict(row) if not isinstance(row, dict) else row
        normalized = ArtifactStore._jsonable(value)
        for key in ("output_manifest", "provenance", "validation", "report_descriptor", "payload", "redacted_inputs",
                    "request", "permissions", "findings"):
            candidate = normalized.get(key)
            if isinstance(candidate, str):
                try:
                    normalized[key] = json.loads(candidate)
                except (TypeError, ValueError):
                    pass
        return normalized

    async def get_version(self, version_id: str,
                          artifact_id: str | None = None) -> dict | None:
        if artifact_id is None:
            row = await self._conn.fetchrow(
                "SELECT * FROM artifact_versions WHERE version_id=$1", version_id)
        else:
            row = await self._conn.fetchrow(
                "SELECT * FROM artifact_versions "
                "WHERE version_id=$1 AND artifact_id=$2", version_id, artifact_id)
        return self._row(row)

    async def list_versions(self, artifact_id: str) -> list[dict]:
        rows = await self._conn.fetch(
            "SELECT * FROM artifact_versions WHERE artifact_id=$1 "
            "ORDER BY version_number DESC", artifact_id)
        return [self._row(row) for row in rows]

    async def list_records(self, *, principal_id: str, query: str | None = None,
                           artifact_type: str | None = None, status: str | None = None,
                           limit: int = 200, offset: int = 0) -> list[dict]:
        clauses = [
            "(a.created_by=$1 OR EXISTS (SELECT 1 FROM artifact_acl acl "
            "WHERE acl.artifact_id=a.artifact_id AND acl.principal_id=$1 "
            "AND (acl.expires_at IS NULL OR acl.expires_at > now()) "
            "AND (acl.permissions ? 'read' OR acl.permissions ? 'admin')))"
        ]
        args: list = [principal_id]
        if query:
            args.append(f"%{query}%")
            clauses.append(f"(a.artifact_id ILIKE ${len(args)} OR a.original_filename ILIKE ${len(args)})")
        if artifact_type:
            args.append(artifact_type)
            clauses.append(f"a.artifact_type=${len(args)}")
        if status:
            args.append(status)
            clauses.append(f"a.status=${len(args)}")
        args.extend([max(1, min(500, int(limit))), max(0, int(offset))])
        rows = await self._conn.fetch(
            "SELECT a.* FROM artifacts a WHERE " + " AND ".join(clauses) +
            f" ORDER BY a.created_at DESC LIMIT ${len(args)-1} OFFSET ${len(args)}", *args)
        return [self._row(row) for row in rows]

    async def get_record(self, artifact_id: str, *, principal_id: str) -> dict | None:
        row = await self._conn.fetchrow(
            "SELECT a.* FROM artifacts a WHERE a.artifact_id=$1 AND "
            "(a.created_by=$2 OR EXISTS (SELECT 1 FROM artifact_acl acl "
            "WHERE acl.artifact_id=a.artifact_id AND acl.principal_id=$2 "
            "AND (acl.expires_at IS NULL OR acl.expires_at > now()) "
            "AND (acl.permissions ? 'read' OR acl.permissions ? 'admin'))) ",
            artifact_id, principal_id)
        return self._row(row)

    async def materialize_version(self, version_id: str, *, host_id: str,
                                  session_id: str, target_dir: str | Path) -> dict:
        """Atomically materialize verified immutable outputs into one workspace."""
        if not host_id or not session_id:
            raise ArtifactError("host_id and session_id are required for materialization")
        version = await self.get_version(version_id)
        if version is None:
            raise ArtifactError(f"version not found: {version_id}")
        outputs = version.get("output_manifest") or []
        root = Path(target_dir).expanduser()
        root.mkdir(parents=True, exist_ok=True)
        root = root.resolve()
        files = []
        for output in outputs:
            name = output.get("name") if isinstance(output, dict) else None
            key = output.get("storage_key") if isinstance(output, dict) else None
            expected = (output.get("sha256") or output.get("blob_sha256")
                        if isinstance(output, dict) else None)
            if not isinstance(name, str) or not name or not isinstance(key, str) or not expected:
                raise ArtifactError("output manifest is missing materialization fields")
            relative = Path(name)
            if relative.is_absolute() or ".." in relative.parts:
                raise ArtifactError(f"unsafe output path: {name}")
            destination = (root / relative).resolve()
            if root != destination and root not in destination.parents:
                raise ArtifactError(f"unsafe output path: {name}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            if any(parent.is_symlink() for parent in [root, *destination.parent.parents]
                   if parent.exists() and root in parent.parents):
                raise ArtifactError(f"unsafe output path: {name}")
            data = self._rf.get(key, expected)
            fd, temporary = tempfile.mkstemp(prefix=".herdr-materialize-",
                                              dir=str(destination.parent))
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, destination)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            files.append(str(relative))
        await self._conn.execute(
            "INSERT INTO artifact_materializations "
            "(artifact_id, host_id, session_id, local_path, status, sha256_verified) "
            "VALUES ($1,$2,$3,$4,'materialized',true)",
            version["artifact_id"], host_id, session_id, str(root))
        return {"version_id": version_id, "target_dir": str(root), "files": files,
                "sha256_verified": True}

    @staticmethod
    def _sql_timestamp(value):
        """Normalize known timestamptz fields at the persistence boundary."""
        if value is None or isinstance(value, datetime):
            return value
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value, tz=UTC)
        if isinstance(value, str):
            return datetime.fromisoformat(value)
        raise ValueError("timestamp must be epoch seconds or a datetime")

    async def record_review_event(self, action: str, review: dict) -> None:
        if action == "request":
            await self._conn.execute(
                "INSERT INTO artifact_reviews "
                "(review_id, artifact_id, version_id, requested_by, state) "
                "VALUES ($1,$2,$3,$4,'requested') ON CONFLICT (review_id) DO NOTHING",
                review["review_id"], review["artifact_id"], review["version_id"],
                review["requested_by"])
        elif action == "complete":
            await self._conn.execute(
                "UPDATE artifact_reviews SET reviewer=$1, state=$2, findings=$3::jsonb, "
                "completed_at=now() WHERE review_id=$4",
                review["reviewer"], review["state"], json.dumps(review["comments"]),
                review["review_id"])
        else:
            raise ArtifactError(f"unknown review event: {action}")

    async def record_job_event(self, action: str, job: dict) -> None:
        """Apply a state transition without replacing a newer worker snapshot."""
        targets = {"created": "queued", "claimed": "running", "completed": "completed",
                   "failed": "failed", "cancelled": "cancelled"}
        if action not in targets or job.get("status") != targets[action]:
            raise ArtifactError(f"invalid artifact job event: {action}")
        if action == "created":
            changed = await self._conn.execute(
                "INSERT INTO artifact_build_jobs "
                "(job_id, artifact_id, base_version_id, profile, request, status, requested_by, created_at) "
                "VALUES ($1,$2,$3,$4,$5::jsonb,'queued',$6,COALESCE($7,now())) "
                "ON CONFLICT (job_id) DO NOTHING",
                job["job_id"], job["artifact_id"], job.get("base_version_id"), job["profile"],
                json.dumps(job["request"]), job["requested_by"],
                self._sql_timestamp(job.get("created_at")))
        elif action == "claimed":
            changed = await self._conn.execute(
                "UPDATE artifact_build_jobs SET status='running', assigned_host_id=$1, started_at=$2 "
                "WHERE job_id=$3 AND status='queued'",
                job.get("assigned_host_id"), self._sql_timestamp(job.get("started_at")), job["job_id"])
        elif action == "completed":
            changed = await self._conn.execute(
                "UPDATE artifact_build_jobs SET status='completed', result_version_id=$1, completed_at=$2 "
                "WHERE job_id=$3 AND status='running'",
                job.get("result_version_id"), self._sql_timestamp(job.get("completed_at")), job["job_id"])
        elif action == "failed":
            error = job.get("error")
            changed = await self._conn.execute(
                "UPDATE artifact_build_jobs SET status='failed', error=$1::jsonb, completed_at=$2 "
                "WHERE job_id=$3 AND status IN ('queued','running')",
                json.dumps(error if isinstance(error, dict) else {"message": error}),
                self._sql_timestamp(job.get("completed_at")), job["job_id"])
        else:
            changed = await self._conn.execute(
                "UPDATE artifact_build_jobs SET status='cancelled', completed_at=$1 "
                "WHERE job_id=$2 AND status IN ('queued','running')",
                self._sql_timestamp(job.get("completed_at")), job["job_id"])
        if changed not in {"INSERT 0 1", "UPDATE 1"}:
            raise ArtifactError(f"artifact job state conflict: {action} {job['job_id']}")

    async def record_acl_event(self, action: str, grant: dict) -> None:
        if action == "grant":
            await self._conn.execute(
                "INSERT INTO artifact_acl "
                "(artifact_id, principal_id, permissions, expires_at, granted_by) "
                "VALUES ($1,$2,$3::jsonb,$4,$5) "
                "ON CONFLICT (artifact_id, principal_id) DO UPDATE SET "
                "permissions=EXCLUDED.permissions, expires_at=EXCLUDED.expires_at, "
                "granted_by=EXCLUDED.granted_by",
                grant["artifact_id"], grant["principal_id"],
                json.dumps(grant["permissions"]), self._sql_timestamp(grant.get("expires_at")),
                grant.get("granted_by", "control-plane"))
        elif action == "revoke":
            await self._conn.execute(
                "DELETE FROM artifact_acl WHERE artifact_id=$1 AND principal_id=$2",
                grant["artifact_id"], grant["principal_id"])
        else:
            raise ArtifactError(f"unknown ACL event: {action}")

    async def record_publication_event(self, action: str, publication: dict) -> None:
        if action == "requested":
            await self._conn.execute(
                "INSERT INTO publications "
                "(publication_id, resource_id, pinned_version_id, requested_by, audience, "
                "token_hash, provider_id, desired_state, expires_at) VALUES "
                "($1,$2,$3,$4,$5,$6,$7,'requested',to_timestamp($8)) "
                "ON CONFLICT (publication_id) DO NOTHING",
                publication["publication_id"], publication["resource_id"],
                publication["pinned_version_id"], publication["requested_by"],
                publication["audience"], publication["token_hash"],
                publication.get("provider_id", "private_only"), publication["expires_at"])
        elif action == "approved":
            await self._conn.execute(
                "UPDATE publications SET approved_by=$1, desired_state='active', "
                "observed_state='active', endpoint=$2, approved_at=now() WHERE publication_id=$3",
                publication.get("approved_by"), publication.get("endpoint"),
                publication["publication_id"])
        elif action == "revoked":
            await self._conn.execute(
                "UPDATE publications SET desired_state='revoked', revoked_at=now(), "
                "provider_error=$1 WHERE publication_id=$2",
                publication.get("provider_error"), publication["publication_id"])
        elif action == "failed":
            await self._conn.execute(
                "UPDATE publications SET desired_state='failed', provider_error=$1 "
                "WHERE publication_id=$2",
                publication.get("provider_error"), publication["publication_id"])
        else:
            raise ArtifactError(f"unknown publication event: {action}")

    async def record_retraction(self, artifact_id: str, version_id: str | None = None, reason: str = "") -> None:
        """Durable half of the lineage retraction (``artifact_scope.ArtifactGraphGate``): an artifact becomes
        ``lifecycle='retracted'``, a version ``state='retracted'``. Nothing is deleted; both columns are free text."""
        if version_id is None:
            changed = await self._conn.execute(
                "UPDATE artifacts SET lifecycle='retracted' WHERE artifact_id=$1", artifact_id)
        else:
            changed = await self._conn.execute(
                "UPDATE artifact_versions SET state='retracted' WHERE version_id=$1 AND artifact_id=$2",
                version_id, artifact_id)
        if changed != "UPDATE 1":
            raise ArtifactError("no such artifact or version to retract")

    async def load_retractions(self) -> list[dict]:
        rows = await self._conn.fetch(
            "SELECT artifact_id, NULL::text AS version_id FROM artifacts WHERE lifecycle='retracted' "
            "UNION ALL SELECT artifact_id, version_id FROM artifact_versions WHERE state='retracted'")
        return [self._row(row) for row in rows]

    async def load_acl(self) -> list[dict]:
        rows = await self._conn.fetch(
            "SELECT artifact_id, principal_id, permissions, expires_at, granted_by "
            "FROM artifact_acl")
        return [self._row(row) for row in rows]

    async def load_reviews(self) -> list[dict]:
        rows = await self._conn.fetch("SELECT * FROM artifact_reviews")
        reviews = [self._row(row) for row in rows]
        for review in reviews:
            # Persistence calls these findings; the review manager uses comments.
            review["comments"] = review.get("findings") or []
        return reviews

    async def get_job(self, job_id: str) -> dict | None:
        """Read current worker state rather than a startup manager snapshot."""
        row = await self._conn.fetchrow(
            "SELECT * FROM artifact_build_jobs WHERE job_id=$1", job_id)
        return self._row(row)

    async def load_jobs(self) -> list[dict]:
        rows = await self._conn.fetch("SELECT * FROM artifact_build_jobs")
        return [self._row(row) for row in rows]

    async def ensure_binding_context(self, *, mission: dict | None = None,
                                     session: dict | None = None) -> None:
        """Materialize server-observed records needed by artifact binding FKs.

        Callers resolve these records from the trusted repo projection, never
        from upload fields. Existing durable rows remain authoritative.
        """
        if mission is None and session is None:
            return
        mission_key = (mission.get("mission_key") or mission.get("mission_id")) if mission else None
        session_mission = ((session.get("mission_key") or session.get("mission_id"))
                           if session else None)
        if mission is not None and (not mission_key or not mission.get("title")):
            raise ArtifactError("observed mission identity and title are required")
        if session is not None and not session.get("session_id"):
            raise ArtifactError("observed session identity is required")
        if mission_key and session_mission and mission_key != session_mission:
            raise ArtifactError("observed mission and session binding conflict")
        async with _transaction(self._conn):
            if mission is not None:
                await self._conn.execute(
                    "INSERT INTO missions (mission_id, title, purpose, repository, branch, worktree, "
                    "owner, stage, status, created_at, updated_at, completed_at, cwd) "
                    "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,COALESCE($10,now()),COALESCE($11,now()),$12,$13) "
                    "ON CONFLICT (mission_id) DO NOTHING",
                    mission_key, mission["title"], mission.get("purpose"), mission.get("repository"),
                    mission.get("branch"), mission.get("worktree"), mission.get("owner"),
                    mission.get("stage") or "unknown", mission.get("status") or "unknown",
                    self._sql_timestamp(mission.get("created_at")),
                    self._sql_timestamp(mission.get("updated_at")),
                    self._sql_timestamp(mission.get("completed_at")), mission.get("cwd"))
            if session is not None:
                # A session's observed host ID alone does not prove a hosts row.
                # Preserve that provenance in payload without fabricating a host.
                # A mission may also lag its session in the projection: reference
                # only an existing SQL row and retain the observed ID in payload.
                await self._conn.execute(
                    "INSERT INTO sessions (session_id, mission_id, title, purpose, agent_role, model, "
                    "status, stage, started_at, last_activity, ended_at, payload, cwd, branch) "
                    "VALUES ($1,(SELECT mission_id FROM missions WHERE mission_id=$2),"
                    "$3,$4,$5,$6,$7,$8,COALESCE($9,now()),$10,$11,$12::jsonb,$13,$14) "
                    "ON CONFLICT (session_id) DO NOTHING",
                    session["session_id"], session_mission, session.get("title"), session.get("purpose"),
                    session.get("agent_role"), session.get("model"), session.get("status") or "unknown",
                    session.get("stage"),
                    self._sql_timestamp(session.get("started_at") or session.get("created_at")),
                    self._sql_timestamp(session.get("last_activity") or session.get("updated_at")),
                    self._sql_timestamp(session.get("ended_at")),
                    json.dumps(self._jsonable(session)), session.get("cwd"), session.get("branch"))

    async def register(self, *, original_filename: str, mime_type: str,
                       data: bytes, artifact_type: str = "other",
                       created_by: str | None = None,
                       mission_id: str | None = None,
                       session_id: str | None = None,
                       retention_class: str = "temporary") -> str:
        digest = hashlib.sha256(data).hexdigest()
        key = f"sha256/{digest[:2]}/{digest[2:4]}/{digest}"
        self._rf.put(data)
        aid = ids.new_id("artifact")
        await self._conn.execute(
            "INSERT INTO artifacts (artifact_id, original_filename, mime_type, "
            " artifact_type, created_by, size_bytes, sha256, storage_backend, "
            " storage_key, status, retention_class) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,'rustfs',$8,'available',$9)",
            aid, original_filename, mime_type, artifact_type, created_by,
            len(data), digest, key, retention_class)
        if mission_id or session_id:
            await self.attach(aid, mission_id=mission_id,
                              session_id=session_id, created_by=created_by)
        return aid

    async def get(self, artifact_id: str) -> dict | None:
        return await self._conn.fetchrow(
            "SELECT * FROM artifacts WHERE artifact_id=$1", artifact_id)

    async def download(self, artifact_id: str) -> bytes:
        row = await self.get(artifact_id)
        if not row:
            raise ArtifactError(f"no artifact {artifact_id}")
        return self._rf.get(row["storage_key"], row["sha256"])

    async def attach(self, artifact_id: str, *, mission_id=None,
                     session_id=None, stage=None, created_by=None,
                     workspace_id=None, room_id=None, issue_ref=None,
                     commit_sha=None) -> int:
        await self._conn.execute(
            "INSERT INTO artifact_bindings (artifact_id, mission_id, session_id, "
            " stage, created_by, workspace_id, room_id, issue_ref, commit_sha) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)",
            artifact_id, mission_id, session_id, stage, created_by,
            workspace_id, room_id, issue_ref, commit_sha)
        return 1

    async def materialize(self, artifact_id: str, host_id: str,
                          session_id=None, local_path=None,
                          verified=False) -> int:
        await self._conn.execute(
            "INSERT INTO artifact_materializations (artifact_id, host_id, "
            " session_id, local_path, status, sha256_verified) "
            "VALUES ($1,$2,$3,$4,'materialized',$5)",
            artifact_id, host_id, session_id, local_path, verified)
        return 1

    async def mark_available(self, artifact_id: str) -> None:
        await self._conn.execute(
            "UPDATE artifacts SET status='available' WHERE artifact_id=$1",
            artifact_id)

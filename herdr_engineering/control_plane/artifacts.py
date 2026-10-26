"""Artifact model + bindings + materialization (Spec §56-74)."""
from __future__ import annotations

import hashlib
import json
from contextlib import asynccontextmanager
from datetime import date, datetime
from typing import Any

from . import ids
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


class ArtifactStore:
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
                             idempotency_key: str | None = None) -> dict:
        """Commit one immutable version using compare-and-swap on the head."""
        output_json = json.dumps(output_manifest, sort_keys=True, separators=(",", ":"))
        provenance_json = json.dumps(provenance or {}, sort_keys=True, separators=(",", ":"))
        request_sha = hashlib.sha256(
            json.dumps({"artifact_id": artifact_id, "base_version_id": base_version_id,
                        "source_manifest_blob": source_manifest_blob,
                        "output_manifest": output_manifest, "provenance": provenance or {}},
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
        await self._validate_blob_reference(source_manifest_blob)
        for output in output_manifest:
            await self._validate_blob_reference(
                output.get("storage_key") or output.get("blob_sha256"))
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
                "SELECT artifact_id, current_version_id FROM artifacts "
                "WHERE artifact_id=$1 FOR UPDATE", artifact_id,
            )
            if artifact is None:
                raise ArtifactError(f"no artifact {artifact_id}")
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
                "mission_id, job_id, committed_at) VALUES "
                "($1,$2,$3,$4,$5,'ready',$6,$7::jsonb,$8::jsonb,$9,$10,$11,$12,$13,$14,now())",
                version_id, artifact_id, version_number, base_version_id, base_version_id,
                source_manifest_blob, output_json, provenance_json, created_by, agent_id,
                host_id, session_id, mission_id, job_id,
            )
            await self._conn.execute(
                "UPDATE artifacts SET current_version_id=$1, status='available' "
                "WHERE artifact_id=$2", version_id, artifact_id,
            )
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
        }

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
        return ArtifactStore._jsonable(value)

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
                     session_id=None, stage=None, created_by=None) -> int:
        await self._conn.execute(
            "INSERT INTO artifact_bindings (artifact_id, mission_id, session_id, "
            " stage, created_by) VALUES ($1,$2,$3,$4,$5)",
            artifact_id, mission_id, session_id, stage, created_by)
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

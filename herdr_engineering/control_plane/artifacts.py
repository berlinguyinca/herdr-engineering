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
        normalized = ArtifactStore._jsonable(value)
        for key in ("output_manifest", "provenance", "validation", "payload",
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

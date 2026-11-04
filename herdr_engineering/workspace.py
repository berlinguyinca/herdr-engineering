"""Distributed workspace manifests and single-writer preparation checks."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath


@dataclass(frozen=True)
class WorkspaceManifest:
    workspace_id: str
    repo_url: str
    revision: str
    branch: str
    worktree_id: str
    task_id: str
    artifact_refs: tuple[str, ...]
    environment_digest: str
    owner: str
    acl: tuple[str, ...]

    def to_dict(self):
        return asdict(self) | {"artifact_refs": list(self.artifact_refs), "acl": list(self.acl)}


class WorkspacePreparationError(RuntimeError):
    pass


class WorkspaceManifestStore:
    """Small authoritative manifest store; replacement requires identical identity."""

    def __init__(self, repo=None):
        self.repo = repo
        self._rows: dict[str, dict] = {}
        self._prepared: dict[tuple[str, str], dict] = {}

    def _load(self, workspace_id):
        if self.repo is None:
            return
        for event in self.repo.stream("workspace_manifest", workspace_id):
            payload = event.get("payload") or {}
            if isinstance(payload, str):
                payload = json.loads(payload)
            if event.get("event_type") == "WorkspaceManifestSaved":
                self._rows[workspace_id] = copy.deepcopy(payload)
            if event.get("event_type") == "WorkspacePrepared":
                self._prepared[(workspace_id, payload["host_uid"])] = copy.deepcopy(payload)

    def _persist(self, workspace_id, kind, payload):
        if self.repo is not None:
            self.repo.record_event("workspace_manifest", workspace_id, kind, payload,
                                   require_durable=True, publish=False)

    def save(self, manifest: WorkspaceManifest) -> dict:
        row = manifest.to_dict()
        self._load(manifest.workspace_id)
        previous = self._rows.get(manifest.workspace_id)
        if previous is not None and previous != row:
            raise WorkspacePreparationError("workspace manifest identity conflict")
        if previous is None:
            self._persist(manifest.workspace_id, "WorkspaceManifestSaved", row)
        self._rows[manifest.workspace_id] = copy.deepcopy(row)
        return copy.deepcopy(row)

    def get(self, workspace_id: str) -> dict | None:
        self._load(workspace_id)
        row = self._rows.get(workspace_id)
        return copy.deepcopy(row) if row else None

    def mark_prepared(self, manifest, *, host_uid, path, revision):
        if (not host_uid or not isinstance(path, str) or not path.startswith("/")
                or ".." in PurePosixPath(path).parts or any(char in path for char in "\x00\r\n")):
            raise WorkspacePreparationError("prepared destination path is invalid")
        if revision != manifest.revision:
            raise WorkspacePreparationError("prepared workspace revision mismatch")
        self.save(manifest)
        row = {"workspace_id": manifest.workspace_id, "host_uid": host_uid,
               "owner": manifest.owner, "path": path, "revision": revision, "ready": True}
        self._persist(manifest.workspace_id, "WorkspacePrepared", row)
        self._prepared[(manifest.workspace_id, host_uid)] = copy.deepcopy(row)
        return row

    def resolve(self, workspace_id, host_uid):
        self._load(workspace_id)
        return copy.deepcopy(self._prepared.get((workspace_id, host_uid)))


class WorkspacePreparer:
    def __init__(self, manifests: WorkspaceManifestStore):
        self.manifests = manifests

    def prepare(self, manifest: WorkspaceManifest, *, preflight: dict, create_worktree,
                host_uid=None) -> dict:
        if not preflight.get("ready"):
            raise WorkspacePreparationError(
                "workspace destination is not ready: " + ",".join(preflight.get("reason_codes", ())))
        stored = self.manifests.save(manifest)
        result = create_worktree(stored)
        if host_uid is not None:
            if not isinstance(result, dict):
                raise WorkspacePreparationError("native preparation did not return destination evidence")
            self.manifests.mark_prepared(manifest, host_uid=host_uid,
                                         path=result.get("path"), revision=result.get("revision"))
        return {"workspace": stored, "worktree": result}

    def prepare_native(self, manifest, *, repository_path, destination_path, host_uid,
                       native_adapter, materialize_artifact=None):
        """Host-side preparation: inspect Git, call native Herdr, then verify Git."""
        repository = Path(repository_path).resolve(strict=True)
        destination = Path(destination_path).absolute()
        if destination.exists() or destination.is_symlink():
            raise WorkspacePreparationError("destination already exists; reconcile instead of overwriting")
        if not re.fullmatch(r"[a-fA-F0-9]{40}|[a-fA-F0-9]{64}", manifest.revision):
            raise WorkspacePreparationError("workspace requires an exact full commit revision")
        git_environment = {key: value for key, value in os.environ.items()
                           if not key.startswith("GIT_")}
        def git(cwd, *args):
            result = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True,
                                    text=True, timeout=30, env=git_environment)
            if result.returncode:
                raise WorkspacePreparationError("Git workspace verification failed")
            return result.stdout.strip()
        if git(repository, "remote", "get-url", "origin") != manifest.repo_url:
            raise WorkspacePreparationError("repository origin does not match manifest")
        if git(repository, "status", "--porcelain", "--untracked-files=all"):
            raise WorkspacePreparationError("dirty source requires an explicit snapshot")
        revision = git(repository, "rev-parse", "--verify", manifest.revision + "^{commit}")
        if revision.lower() != manifest.revision.lower():
            raise WorkspacePreparationError("source revision does not match manifest")
        git(repository, "check-ref-format", "--branch", manifest.branch)
        if manifest.artifact_refs and materialize_artifact is None:
            raise WorkspacePreparationError("artifact materializer is required")
        free = shutil.disk_usage(repository).free / 1024 ** 3
        if free < 20:
            raise WorkspacePreparationError("destination disk headroom is insufficient")
        self.manifests.save(manifest)
        native_adapter.create_worktree(str(destination), manifest.branch,
                                       cwd=str(repository), revision=manifest.revision)
        if git(destination, "rev-parse", "HEAD").lower() != manifest.revision.lower():
            raise WorkspacePreparationError("native destination revision mismatch")
        if git(destination, "branch", "--show-current") != manifest.branch:
            raise WorkspacePreparationError("native destination branch mismatch")
        for artifact in manifest.artifact_refs:
            materialize_artifact(artifact, destination)
        return self.manifests.mark_prepared(manifest, host_uid=host_uid,
                                            path=str(destination), revision=manifest.revision)


def preflight_workspace(*, repo_url: str, revision: str, branch: str, worktree_id: str,
                        tool_version: str, expected_tool_version: str | None,
                        disk_free_gib: float, required_disk_gib: float,
                        credentials_scoped: bool, dirty: bool = False) -> dict:
    reasons = []
    if not repo_url or not revision or not branch or not worktree_id:
        reasons.append("WORKSPACE_IDENTITY_MISSING")
    if expected_tool_version and tool_version != expected_tool_version:
        reasons.append("TOOL_VERSION_MISMATCH")
    if disk_free_gib < required_disk_gib:
        reasons.append("DISK_LOW")
    if not credentials_scoped:
        reasons.append("CREDENTIAL_SCOPE_INVALID")
    if dirty:
        reasons.append("DIRTY_WORKTREE_REQUIRES_EXPLICIT_SNAPSHOT")
    return {"ready": not reasons, "reason_codes": reasons}


def artifact_digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def registry_materializer(adapter, *, authorize, owner_id, host_uid, session_id):
    """Reuse the durable artifact registry's private checksum-verified transfer."""
    def materialize(version_id, destination):
        if not isinstance(version_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", version_id):
            raise WorkspacePreparationError("artifact reference must identify an immutable version")
        version = adapter("get_version", version_id)
        if not isinstance(version, dict) or not authorize(version.get("artifact_id"), owner_id, "read"):
            raise WorkspacePreparationError("artifact access denied")
        # Artifacts cannot overwrite the checked-out source tree.
        target = Path(destination) / ".herdr-artifacts" / artifact_digest(version_id.encode())
        receipt = adapter("materialize_version", version_id, host_id=host_uid,
                          session_id=session_id, target_dir=str(target))
        if not isinstance(receipt, dict) or receipt.get("sha256_verified") is not True:
            raise WorkspacePreparationError("artifact checksum verification was not acknowledged")
        return receipt
    return materialize


"""Distributed workspace manifests and single-writer preparation checks."""
from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass


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

    def __init__(self):
        self._rows: dict[str, dict] = {}

    def save(self, manifest: WorkspaceManifest) -> dict:
        row = manifest.to_dict()
        previous = self._rows.get(manifest.workspace_id)
        if previous is not None and previous != row:
            raise WorkspacePreparationError("workspace manifest identity conflict")
        self._rows[manifest.workspace_id] = row
        return dict(row)

    def get(self, workspace_id: str) -> dict | None:
        row = self._rows.get(workspace_id)
        return dict(row) if row else None


class WorkspacePreparer:
    def __init__(self, manifests: WorkspaceManifestStore):
        self.manifests = manifests

    def prepare(self, manifest: WorkspaceManifest, *, preflight: dict, create_worktree) -> dict:
        if not preflight.get("ready"):
            raise WorkspacePreparationError(
                "workspace destination is not ready: " + ",".join(preflight.get("reason_codes", ())))
        stored = self.manifests.save(manifest)
        result = create_worktree(stored)
        return {"workspace": stored, "worktree": result}


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


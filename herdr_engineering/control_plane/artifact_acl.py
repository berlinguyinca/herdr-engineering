"""Artifact-scoped ACL policy used before catalog aggregation or delivery."""
from __future__ import annotations

import time
from collections.abc import Callable


class ArtifactAcl:
    def __init__(self, *, now: Callable[[], float] = time.time, on_change=None):
        self.now = now
        self.on_change = on_change
        self._grants: dict[tuple[str, str], dict] = {}

    def grant(self, artifact_id: str, principal_id: str, permissions: set[str],
              *, expires_at: float | None = None) -> dict:
        allowed = {"read", "edit", "review", "share", "admin"}
        if not artifact_id or not principal_id or not permissions <= allowed:
            raise ValueError("invalid artifact ACL grant")
        grant = {"artifact_id": artifact_id, "principal_id": principal_id,
                 "permissions": sorted(permissions), "expires_at": expires_at}
        self._grants[(artifact_id, principal_id)] = grant
        if self.on_change:
            self.on_change("grant", dict(grant))
        return dict(grant)

    def revoke(self, artifact_id: str, principal_id: str) -> None:
        self._grants.pop((artifact_id, principal_id), None)
        if self.on_change:
            self.on_change("revoke", {"artifact_id": artifact_id, "principal_id": principal_id})

    def can(self, artifact_id: str, principal_id: str, permission: str) -> bool:
        grant = self._grants.get((artifact_id, principal_id))
        if not grant:
            return False
        expires_at = grant["expires_at"]
        if expires_at is not None and expires_at <= self.now():
            self._grants.pop((artifact_id, principal_id), None)
            return False
        return permission in grant["permissions"] or "admin" in grant["permissions"]

    def filter_visible(self, artifacts: list[dict], principal_id: str) -> list[dict]:
        return [artifact for artifact in artifacts
                if self.can(artifact.get("artifact_id", ""), principal_id, "read")]

    def has_grants(self, artifact_id: str) -> bool:
        return any(aid == artifact_id for aid, _ in self._grants)

    def load(self, grants: list[dict]) -> None:
        for grant in grants:
            self._grants[(grant["artifact_id"], grant["principal_id"])] = {
                "artifact_id": grant["artifact_id"],
                "principal_id": grant["principal_id"],
                "permissions": list(grant.get("permissions") or []),
                "expires_at": grant.get("expires_at"),
            }


"""Artifact-scoped ACL policy used before catalog aggregation or delivery."""
from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime

COLLAB_PREFIXES = ("collab:", "collab-invite:")
# ``collab:user:<id>`` are WEB room-member grants (artifact_room_access), not fleet-agent collaborators.
ROOM_USER_PREFIX = "collab:user:"


def is_agent_collaborator(principal_id: str) -> bool:
    """True for the principals only the collaborators route may write (agent grants and their invites)."""
    return principal_id.startswith(COLLAB_PREFIXES) and not principal_id.startswith(ROOM_USER_PREFIX)


def normalize_expiry(value) -> float | None:
    """Epoch seconds from a number, a datetime or an ISO-8601 string (what the store returns); None stays None."""
    if value is None or isinstance(value, bool):
        if value is None:
            return None
        raise ValueError("expires_at must be a number or null")
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("expires_at must be a number or ISO-8601 timestamp") from None
    if isinstance(value, datetime):
        return (value if value.tzinfo else value.replace(tzinfo=UTC)).timestamp()
    raise ValueError("expires_at must be a number or null")


class ArtifactAcl:
    def __init__(self, *, now: Callable[[], float] = time.time, on_change=None):
        self.now = now
        self.on_change = on_change
        self._lock = threading.RLock()          # request threads and the room-sync thread share the maps
        self._grants: dict[tuple[str, str], dict] = {}
        self._holders: dict[str, set] = {}      # artifact id -> principals with a stored grant (read-time gate index)

    def grant(self, artifact_id: str, principal_id: str, permissions: set[str],
              *, expires_at: float | None = None, granted_by: str | None = None) -> dict:
        if is_agent_collaborator(principal_id):
            raise ValueError("collaborator grants use the collaborators route")
        allowed = {"read", "edit", "review", "share", "admin"}
        if not artifact_id or not principal_id or not permissions <= allowed:
            raise ValueError("invalid artifact ACL grant")
        grant = {"artifact_id": artifact_id, "principal_id": principal_id,
                 "permissions": sorted(permissions), "expires_at": normalize_expiry(expires_at)}
        if granted_by is not None:          # who wrote it; room-derived grants say "room:<id>" so they can be revoked as a set
            grant["granted_by"] = granted_by
        with self._lock:
            self._grants[(artifact_id, principal_id)] = grant
            self._holders.setdefault(artifact_id, set()).add(principal_id)
        if self.on_change:
            self.on_change("grant", dict(grant))
        return dict(grant)

    def revoke(self, artifact_id: str, principal_id: str) -> None:
        with self._lock:
            self._grants.pop((artifact_id, principal_id), None)
            self._holders.get(artifact_id, set()).discard(principal_id)
        if self.on_change:
            self.on_change("revoke", {"artifact_id": artifact_id, "principal_id": principal_id})

    def can(self, artifact_id: str, principal_id: str, permission: str) -> bool:
        with self._lock:
            grant = self._grants.get((artifact_id, principal_id))
            if not grant:
                return False
            expires_at = grant["expires_at"]
            if expires_at is not None and expires_at <= self.now():
                self._grants.pop((artifact_id, principal_id), None)
                self._holders.get(artifact_id, set()).discard(principal_id)
                return False
            return permission in grant["permissions"] or "admin" in grant["permissions"]

    def filter_visible(self, artifacts: list[dict], principal_id: str) -> list[dict]:
        return [artifact for artifact in artifacts
                if self.can(artifact.get("artifact_id", ""), principal_id, "read")]

    def principals_with(self, artifact_id: str, permission: str) -> list[str]:
        """Principal ids holding a live ``permission`` on the artifact (expired grants are dropped on the way)."""
        with self._lock:
            return [pid for pid in sorted(self._holders.get(artifact_id, ())) if self.can(artifact_id, pid, permission)]

    def grants_by(self, granted_by: str) -> list[dict]:
        """Stored grants written by ``granted_by`` (copies), for revoking a source's grants as a set."""
        with self._lock:
            return [dict(g) for g in self._grants.values() if g.get("granted_by") == granted_by]

    def held(self, artifact_id: str, principal_id: str) -> dict | None:
        """The live stored grant (a copy) whatever its source, or None. Unlike ``can`` it does not decide a permission."""
        with self._lock:
            grant = self._grants.get((artifact_id, principal_id))
            if not grant or (grant["expires_at"] is not None and grant["expires_at"] <= self.now()):
                return None
            return dict(grant)

    def grants_with_source_prefix(self, prefix: str) -> list[dict]:
        """Stored grants whose ``granted_by`` starts with ``prefix`` (copies), e.g. every ``room:`` grant."""
        with self._lock:
            return [dict(g) for g in self._grants.values() if str(g.get("granted_by") or "").startswith(prefix)]

    def has_grants(self, artifact_id: str) -> bool:
        with self._lock:
            return any(aid == artifact_id for aid, _ in self._grants)

    def load(self, grants: list[dict]) -> None:
        with self._lock:
            for grant in grants:
                if is_agent_collaborator(str(grant.get("principal_id") or "")):
                    continue
                self._holders.setdefault(grant["artifact_id"], set()).add(grant["principal_id"])
                self._grants[(grant["artifact_id"], grant["principal_id"])] = {
                    "artifact_id": grant["artifact_id"],
                    "principal_id": grant["principal_id"],
                    "permissions": list(grant.get("permissions") or []),
                    "expires_at": normalize_expiry(grant.get("expires_at")),
                    **({"granted_by": grant["granted_by"]} if grant.get("granted_by") else {}),
                }

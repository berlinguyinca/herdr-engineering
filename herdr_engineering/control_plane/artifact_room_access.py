"""Room-derived read access to artifacts (unified workspace P03, design/ARTIFACT_INTEGRATION.md).

People in a room can read the artifacts linked to that room. This is not a second access check: it is written as ordinary
registry read grants, so the one ACL (``artifact_acl``) still decides, and revoking is a delete. A grant is
``collab:user:<id>`` -> ``read`` on the artifact, ``granted_by="room:<room id>"``, expiring when the person's room
membership does. The grants of a room are re-synced when its membership changes, when an artifact is linked to it, and
at startup, so a person who leaves loses access and a person who joins gains it.

A collab identity never equals an API-key principal and never gets more than ``read``: room membership grants no edit,
share, review or admin right, and no shell or external-account access.
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable

log = logging.getLogger(__name__)

PREFIX = "collab:user:"


def principal_for(user_id: str) -> str:
    return PREFIX + user_id


def source_for(room_id: str) -> str:
    return "room:" + room_id


_FOREVER = float("inf")


def _room_may_replace(other: dict, expires_at: float | None) -> bool:
    """May a room write over the live grant ``other`` (held by a different source)? Only another room's grant, and only
    to lengthen it: a direct grant (any other ``granted_by``) is never overwritten, and a room never shortens or narrows
    what another source gave (one grant row holds one source, so the first holder keeps it)."""
    if not str(other.get("granted_by") or "").startswith("room:"):
        return False
    return (expires_at if expires_at is not None else _FOREVER) > (
        other["expires_at"] if other["expires_at"] is not None else _FOREVER)


class RoomArtifactAccess:
    """Keep ``artifact_acl`` in step with room membership.

    ``call(method, *args, **kwargs)`` reaches the registry (``server._artifact_call`` shape); ``readers(room_id)`` returns
    ``{user_id: expires_at | None}``; ``acl`` is the ``ArtifactAcl``.
    """

    def __init__(self, acl, call: Callable, readers: Callable[[str], dict], *, now: Callable[[], float]) -> None:
        self.acl, self.call, self.readers, self.now = acl, call, readers, now
        self._dirty: set[str] = set()
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = False
        self._thread: threading.Thread | None = None

    # ---- the sync itself (synchronous, testable)
    def sync_room(self, room_id: str) -> dict:
        wanted_artifacts = self.call("room_artifact_ids", room_id)
        return self._apply(room_id, set(wanted_artifacts), self.readers(room_id))

    def sync_artifact(self, artifact_id: str, *, resync: bool = False) -> dict:
        """After a link: every room this artifact is linked to, but only for this artifact."""
        rooms = {link["room_id"] for link in self.call("list_links", artifact_id) if link.get("room_id")}
        out = {"granted": 0, "revoked": 0}
        for room_id in rooms:
            result = self._apply(room_id, {artifact_id}, self.readers(room_id), only={artifact_id}, resync=resync)
            out["granted"] += result["granted"]
            out["revoked"] += result["revoked"]
        return out

    def _apply(self, room_id: str, artifacts: set[str], readers: dict, *, only: set[str] | None = None,
               resync: bool = False) -> dict:
        source = source_for(room_id)
        want = {(a, principal_for(u)): exp for a in artifacts for u, exp in readers.items()}
        have = {(g["artifact_id"], g["principal_id"]): g for g in self.acl.grants_by(source)
                if only is None or g["artifact_id"] in only}
        granted = revoked = 0
        for key, expires_at in want.items():
            current = have.get(key)
            if current is None:
                other = self.acl.held(*key)
                if other is not None and not _room_may_replace(other, expires_at):
                    continue                    # a grant of another source stays as it is (R-RA2); it is not ours to revoke
            if current is None or current.get("expires_at") != expires_at or current["permissions"] != ["read"]:
                self.acl.grant(key[0], key[1], {"read"}, expires_at=expires_at, granted_by=source)
                granted += 1
        gone = have.keys() - want.keys()
        for key in gone:
            self.acl.revoke(*key)
            revoked += 1
        if gone and not resync:
            # One grant per (artifact, person) holds one source. If another room still has this person, give it back to
            # that room now rather than at its next change.
            for artifact_id in {a for a, _ in gone}:
                self.sync_artifact(artifact_id, resync=True)
        return {"granted": granted, "revoked": revoked}

    def unlink(self, artifact_id: str, room_id: str) -> int:
        """After the room's link to an artifact was removed: revoke that room's grants on it. A person another room still
        links the artifact to gets the grant back from that room (``_apply`` re-syncs on any revoke)."""
        return self._apply(room_id, set(), self.readers(room_id), only={artifact_id})["revoked"]

    def revoke_all_room_grants(self) -> int:
        """Remove every ``room:*``-sourced grant (after ``room_artifact_access`` was turned off). Grants of any other
        source are never touched. Idempotent: a second call finds nothing. Returns how many were removed."""
        grants = self.acl.grants_with_source_prefix("room:")
        for g in grants:
            self.acl.revoke(g["artifact_id"], g["principal_id"])
        return len(grants)

    # ---- triggers
    def notify(self, room_id: str) -> None:
        """Cheap and non-blocking (called under the collab lock): queue the room for the worker."""
        with self._lock:
            self._dirty.add(room_id)
        self._wake.set()

    def drain(self) -> int:
        with self._lock:
            rooms, self._dirty = self._dirty, set()
        for room_id in sorted(rooms):
            try:
                self.sync_room(room_id)
            except Exception:                       # noqa: BLE001 - retried on the next change or restart
                log.exception("room artifact grants for %s were not synced", room_id)
                with self._lock:
                    self._dirty.add(room_id)
        return len(rooms)

    def start(self, room_ids: Callable[[], list[str]]) -> None:
        """Sync every room once, then follow changes on a daemon thread."""
        for room_id in room_ids():
            self.notify(room_id)

        def run():
            while not self._stop:
                self._wake.wait(30)
                self._wake.clear()
                if self._stop:
                    return
                self.drain()
        self._thread = threading.Thread(target=run, name="room-artifact-access", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop = True
        self._wake.set()


async def revoke_room_grants_pg(conn, *, apply: bool) -> dict:
    """Remove every ``room:*``-sourced row of ``artifact_acl`` (the maintenance step after ``room_artifact_access`` was
    turned off; the grants it wrote stay until removed). Counts first and deletes nothing unless ``apply``. Rows of any
    other ``granted_by`` are never matched. Idempotent. A running control plane keeps its in-memory ACL until restart, so
    run it with the feature off and restart afterwards (docs/artifacts/room-access-maintenance.md)."""
    found = int(await conn.fetchval("SELECT count(*) FROM artifact_acl WHERE granted_by LIKE 'room:%'"))
    removed = 0
    if apply and found:
        removed = len(await conn.fetch("DELETE FROM artifact_acl WHERE granted_by LIKE 'room:%' RETURNING 1"))
    return {"found": found, "removed": removed, "applied": bool(apply)}

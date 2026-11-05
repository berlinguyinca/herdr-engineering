"""Operator controls for member-owned artifacts (plan 5, Task 9): ``/api/v1/artifact-members...``.

Every route needs the ``operator`` role.  The member path segment is a Cognito user id (or ``member:<id>``) validated
as a member principal; ``links`` is reserved for the cross-member link listing, so a user literally named ``links`` is
addressed as ``member:links``.  Nothing here leaks beyond operator context: an unknown member, artifact or share is a
plain 404.  Readers exclude ``tombstoned`` artifacts; purge is the one writer allowed to meet one, to finish a purge.
"""
from __future__ import annotations

import logging
from typing import Any, Final
from urllib.parse import parse_qs, urlsplit

from .artifact_members import MemberStore, _positive, is_member_principal, member_principal
from .artifact_share_api import ShareHTTPError, _call, _jsonable, _share_body, revoke_share
from .artifact_shares import AUDIENCES, ShareError
from .artifacts import ArtifactError

log = logging.getLogger("herdr.control_plane.artifact_members_api")

LIMIT_FIELDS: Final = ("max_artifact_bytes", "max_member_bytes", "max_artifacts")
SHARE_STATES: Final = ("pending", "live", "revoked")
LINK_COLUMNS: Final = ("s.share_id, s.artifact_id, s.version_id, s.audience, s.state, s.expires_at, s.allow_emails,"
                       " s.allow_groups, s.created_at, s.created_by, s.approved_by, s.revoked_at, s.title")
MAX_LINKS: Final = 500


class MemberAdmin:
    """The database half of the operator controls (one asyncpg connection, behind a ``SyncArtifactAdapter``)."""

    def __init__(self, conn: Any, *, defaults: dict | None = None) -> None:
        self.conn = conn
        self.members = MemberStore(conn, defaults=defaults)

    async def list_members(self) -> list[dict]:
        return _jsonable(await self.members.list_members())

    async def known(self, principal: str) -> bool:
        return bool(await self.conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM artifact_member_usage WHERE member_principal=$1)"
            " OR EXISTS (SELECT 1 FROM artifacts WHERE created_by=$1)", principal))

    async def show(self, principal: str) -> dict | None:
        if not await self.known(principal):
            return None
        usage = await self.members.usage(principal)
        artifacts = await self.conn.fetch(
            "SELECT a.artifact_id, COALESCE(a.display_name, a.original_filename) AS title, a.lifecycle,"
            " a.current_version_id, a.created_at,"
            " COALESCE((SELECT sum(b.size_bytes) FROM artifact_blobs b WHERE b.scope=a.artifact_id), 0)::bigint AS bytes"
            " FROM artifacts a WHERE a.created_by=$1 AND a.lifecycle <> 'tombstoned'"
            " ORDER BY a.created_at DESC, a.artifact_id LIMIT $2", principal, MAX_LINKS)
        return _jsonable({"member": principal, "usage": usage, "artifacts": [dict(r) for r in artifacts],
                          "links": await self.links(principal=principal)})

    async def links(self, *, principal: str | None = None, audience: str | None = None,
                    state: str | None = None) -> list[dict]:
        """Member-created links (newest first), on artifacts that are not tombstoned."""
        rows = await self.conn.fetch(
            f"SELECT {LINK_COLUMNS} FROM artifact_shares s JOIN artifacts a ON a.artifact_id=s.artifact_id"
            " WHERE s.created_by LIKE 'member:%' AND a.lifecycle <> 'tombstoned'"
            " AND ($1::text IS NULL OR s.created_by=$1) AND ($2::text IS NULL OR s.audience=$2)"
            " AND ($3::text IS NULL OR s.state=$3) ORDER BY s.created_at DESC, s.share_id LIMIT $4",
            principal, audience, state, MAX_LINKS)
        return [_link(r) for r in rows]

    async def set_limits(self, principal: str, changes: dict, actor: str) -> dict:
        return _jsonable(await self.members.set_limits(principal, actor=actor, **changes))

    async def set_sharing(self, principal: str, enabled: bool, actor: str) -> dict:
        return _jsonable(await self.members.set_limits(principal, sharing_disabled=not enabled, actor=actor))

    async def open_shares(self, *, principal: str | None = None, artifact_id: str | None = None) -> list[dict]:
        """Share rows (with token hash, for the revoke path) that are still pending or live."""
        rows = await self.conn.fetch(
            "SELECT share_id, token_hash FROM artifact_shares WHERE state <> 'revoked'"
            " AND ($1::text IS NULL OR created_by=$1) AND ($2::text IS NULL OR artifact_id=$2)"
            " ORDER BY created_at, share_id", principal, artifact_id)
        return [dict(r) for r in rows]

    async def share_of(self, principal: str, share_id: str) -> dict | None:
        row = await self.conn.fetchrow(
            "SELECT share_id, token_hash FROM artifact_shares WHERE share_id=$1 AND created_by=$2",
            share_id, principal)
        return dict(row) if row else None

    async def purgeable(self, principal: str, artifact_id: str) -> dict | None:
        """The member's artifact for purge: any lifecycle (the caller decides), tombstoned included."""
        row = await self.conn.fetchrow(
            "SELECT artifact_id, lifecycle, purge_after FROM artifacts WHERE artifact_id=$1 AND created_by=$2",
            artifact_id, principal)
        return dict(row) if row else None

    async def record_purge(self, principal: str, artifact_id: str, actor: str, detail: dict) -> None:
        await self.members.record(principal, "artifact_purge", actor={"kind": "operator", "id": actor},
                                  artifact_id=artifact_id, outcome="ok", detail=detail)


def _link(row: Any) -> dict:
    value = _jsonable(dict(row))
    emails, groups = value.pop("allow_emails", None) or [], value.pop("allow_groups", None) or []
    value["recipients"] = [*emails, *groups]
    return value


def _principal(segment: str) -> str:
    """``alice`` or ``member:alice`` to the member principal; ``ValueError`` for anything else."""
    return member_principal(segment[len("member:"):] if segment.startswith("member:") else segment)


def parse_limits(body: dict) -> dict:
    """The limit changes of a ``limits`` request: positive ints or None, rejecting bools, floats and huge values."""
    if not body:
        raise ValueError("give at least one of: " + ", ".join(LIMIT_FIELDS))
    return {name: _positive(name, body[name]) for name in LIMIT_FIELDS if name in body}


def _revoke_all(server, rows: list[dict], actor: str) -> list[str]:
    done = []
    for row in rows:
        revoke_share(server, row, actor)
        done.append(row["share_id"])
    return done


def route_members(handler, server, parts, resource) -> bool:
    """Handle ``/api/v1/artifact-members...``; false for any other path."""
    if resource != "artifact-members":
        return False
    if handler._require("operator"):
        return True
    method = handler.command
    kind = _classify(method, parts)
    if kind is None:
        handler._deny(404, "not found")
        return True
    admin = getattr(server, "member_admin", None)
    if (admin is None or getattr(server, "share_store", None) is None or getattr(server, "shares_root", None) is None
            or server.artifact_store is None):
        handler._deny(503, "member controls are not enabled")
        return True
    origin = handler.headers.get("Origin")
    if method == "POST" and origin and (urlsplit(origin).netloc != handler.headers.get("Host")
                                         or urlsplit(origin).scheme not in {"http", "https"}):
        handler._deny(403, "cross-origin member mutation refused")
        return True
    actor = handler._resource_principal()
    try:
        _dispatch(handler, server, admin, kind, parts, actor)
    except ShareHTTPError as exc:
        handler._json({"error": exc.message}, exc.status)
    except ShareError as exc:
        handler._json({"error": str(exc)}, 404 if exc.code == "not_found" else 400)
    except ArtifactError as exc:
        message = str(exc)
        if message.startswith("no artifact"):
            handler._json({"error": "not found"}, 404)
        elif message.startswith("artifact bytes were already") and kind == "purge":
            handler._json({"artifact_id": parts[6], "purged": True, "already_purged": True, "deleted": 0,
                           "retained_shared": 0, "revoked_shares": []})
        elif message.startswith(("cannot purge", "artifact bytes were already", "revoke the artifact")):
            handler._json({"error": message}, 409)
        else:
            raise
    return True


def _classify(method: str, parts: list[str]) -> str | None:
    n = len(parts)
    if method == "GET":
        if n == 4:
            return "list"
        return ("links" if parts[4] == "links" else "show") if n == 5 else None
    if method == "POST" and n == 6 and parts[5] in {"limits", "sharing"}:
        return parts[5]
    if method == "POST" and n == 8 and (parts[5], parts[7]) in {("shares", "revoke"), ("artifacts", "purge")}:
        return "revoke" if parts[5] == "shares" else "purge"
    return None


def _dispatch(handler, server, admin, kind: str, parts: list[str], actor: str) -> None:
    if kind == "list":
        handler._json({"members": _call(admin, "list_members")})
        return
    if kind == "links":
        query = parse_qs(urlsplit(handler.path).query)
        audience = _choice(query, "audience", AUDIENCES)
        state = _choice(query, "state", SHARE_STATES)
        handler._json({"links": _call(admin, "links", audience=audience, state=state)})
        return
    try:
        principal = _principal(parts[4])
    except ValueError:
        handler._json({"error": "not found"}, 404)
        return
    if kind == "show":
        shown = _call(admin, "show", principal)
        handler._json(shown if shown is not None else {"error": "not found"}, 200 if shown is not None else 404)
        return
    body = _share_body(handler, _allowed(kind))               # read and validate the body before any lookup answers
    if kind == "limits":
        changes = parse_limits(body)
        if not _call(admin, "known", principal):
            raise ShareHTTPError(404, "not found")
        handler._json({"member": principal, "usage": _call(admin, "set_limits", principal, changes, actor)})
    elif kind == "sharing":
        enabled = body.get("enabled")
        if not isinstance(enabled, bool):
            raise ValueError('"enabled" must be true or false')
        if not _call(admin, "known", principal):
            raise ShareHTTPError(404, "not found")
        usage = _call(admin, "set_sharing", principal, enabled, actor)
        revoked = [] if enabled else _revoke_all(server, _call(admin, "open_shares", principal=principal), actor)
        handler._json({"member": principal, "usage": usage, "revoked_shares": revoked})
    elif kind == "revoke":
        share = _call(admin, "share_of", principal, parts[6])
        if share is None:
            raise ShareHTTPError(404, "not found")
        full = _call(server.share_store, "get", share["share_id"]) or share
        handler._json({"share": _jsonable(revoke_share(server, full, actor))})
    else:
        _purge(handler, server, admin, principal, parts[6], body, actor)


def _allowed(kind: str) -> set[str]:
    return {"limits": set(LIMIT_FIELDS), "sharing": {"enabled"}, "revoke": set(), "purge": {"confirm"}}[kind]


def _choice(query: dict, name: str, allowed) -> str | None:
    values = query.get(name)
    if values is None:
        return None
    if len(values) != 1 or values[0] not in allowed:
        raise ValueError(f"{name} must be one of: " + ", ".join(allowed))
    return values[0]


def _purge(handler, server, admin, principal: str, artifact_id: str, body: dict, actor: str) -> None:
    if body.get("confirm") != artifact_id or not isinstance(body.get("confirm"), str):
        raise ValueError('purge requires {"confirm": "<artifact_id>"} naming exactly this artifact')
    artifact = _call(admin, "purgeable", principal, artifact_id)
    if artifact is None:
        raise ShareHTTPError(404, "not found")
    if artifact["lifecycle"] not in ("retracted", "tombstoned"):
        raise ShareHTTPError(409, f"cannot purge an artifact that is {artifact['lifecycle']}; it must be deleted first")
    if artifact["lifecycle"] == "tombstoned" and artifact["purge_after"] is None:    # a hold: refuse before revoking
        raise ShareHTTPError(409, "cannot purge an artifact under legal hold; lift the hold first")
    revoked = _revoke_all(server, _call(admin, "open_shares", artifact_id=artifact_id), actor)
    result = _call(server.artifact_store, "purge_artifact", artifact_id, actor=actor)
    try:
        _call(admin, "record_purge", principal, artifact_id, actor,
              {"deleted": len(result["deleted"]), "retained_shared": len(result["retained_shared"]),
               "revoked_shares": revoked})
    except Exception as exc:    # noqa: BLE001 - the purge is done and logged on the artifact; the member row is secondary
        log.error("purge of %s done, member audit row not written (%s)", artifact_id, type(exc).__name__)
    handler._json({"artifact_id": artifact_id, "purged": True, "deleted": len(result["deleted"]),
                   "retained_shared": len(result["retained_shared"]), "revoked_shares": revoked})


__all__ = ["MemberAdmin", "is_member_principal", "parse_limits", "route_members"]

"""Owner API for artifact collaborators, collaboration activity and version diffs.

Routes (``admin`` on the artifact, except ``diff`` which needs ``read``; every denial is the same non-disclosing 404):

* ``GET  /api/v1/artifacts/{id}/collaborators``                       grants, pending invite grants, writability
* ``POST /api/v1/artifacts/{id}/collaborators``                       ``{agent_id, permission, expires_in_days}``
* ``POST /api/v1/artifacts/{id}/collaborators/{agent_id}/revoke``
* ``POST /api/v1/artifacts/{id}/collaborators/invite-grant``          ``{invite_id, permission, expires_in_days}``
* ``GET  /api/v1/artifacts/{id}/collab-activity``                     conflicts and rejected commits
* ``GET  /api/v1/artifacts/{id}/versions/{vid}/diff``                 display diff against the first parent

Grants are the durable ``collab:`` ACL rows; the web's in-memory ``server.artifact_acl`` is never touched. Without
``control_plane.collab_artifacts.enabled`` (or without the grant store) every route answers 503 before any store call.
The diff route without a ``base_version_id`` query parameter is this module's; with one it stays the report diff.
"""
from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

from .artifact_report_api import _access

AGENT_ID = re.compile(r"[A-Za-z0-9_.:-]{1,80}")
INVITE_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
DEFAULT_DAYS, MAX_DAYS = 30, 90
PERMISSIONS = ("read", "write")
LEGACY = "legacy artifact: set an owner first"


def _plain(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def _body(handler, allowed: set[str], *, optional: bool = False) -> dict:
    """The strictly validated JSON object body (``optional``: an empty body means ``{}``)."""
    if optional and not int(handler.headers.get("Content-Length") or 0):
        return {}
    if handler.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise ValueError("collaborator requests require application/json")
    value = handler._read_json()
    if not isinstance(value, dict) or set(value) - allowed:
        raise ValueError("invalid collaborator request fields; allowed: " + (", ".join(sorted(allowed)) or "none"))
    return value


def _text(value, pattern: re.Pattern, name: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ValueError(f"{name} must match {pattern.pattern}")
    return value


def _permission(body: dict) -> str:
    permission = body.get("permission")
    if permission not in PERMISSIONS:        # a list or dict is simply not one of the two strings
        raise ValueError("permission must be 'read' or 'write'")
    return permission


def _expiry(body: dict) -> datetime:
    days = body.get("expires_in_days", DEFAULT_DAYS)
    if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= MAX_DAYS:
        raise ValueError(f"expires_in_days must be an integer from 1 to {MAX_DAYS}")
    return datetime.now(UTC) + timedelta(days=days)


def _owned_writable(handler, server, artifact_id: str, permission: str) -> bool:
    """False after answering 409 (legacy artifact, or write on a non-writable one); 404 cannot happen after _access."""
    state = handler._artifact_call("collab_state", artifact_id)
    if state is None:
        handler._deny(404, "not found")
        return False
    if not state["created_by"]:
        handler._json({"error": LEGACY}, 409)
        return False
    if permission == "write" and not state["writable"]:
        handler._json({"error": state["writable_reason"], "writable_reason": state["writable_reason"]}, 409)
        return False
    return True


def route_collab(handler, server, parts, resource) -> bool:
    """Handle the collaborator routes above; false for any other path."""
    if resource != "artifacts" or len(parts) < 6:
        return False
    method, tail = handler.command, parts[5:]
    if tail == ["collaborators"]:
        kind, allowed = "collaborators", {"GET", "POST"}
    elif tail == ["collaborators", "invite-grant"]:
        kind, allowed = "invite-grant", {"POST"}
    elif len(tail) == 3 and tail[0] == "collaborators" and tail[2] == "revoke":
        kind, allowed = "revoke", {"POST"}
    elif tail == ["collab-activity"]:
        kind, allowed = "activity", {"GET"}
    elif len(tail) == 3 and tail[0] == "versions" and tail[2] == "diff":
        if "base_version_id" in parse_qs(urlsplit(handler.path).query):
            return False                      # the report diff keeps its own route
        kind, allowed = "diff", {"GET"}
    else:
        return False
    if method not in allowed:
        handler._deny(404, "not found")
        return True
    if (not getattr(server, "collab_artifacts_enabled", False) or getattr(server, "collab_grant_store", None) is None
            or server.artifact_store is None):
        handler._deny(503, "collaboration is not enabled")
        return True
    origin = handler.headers.get("Origin")
    if method == "POST" and origin and (urlsplit(origin).netloc != handler.headers.get("Host")
                                         or urlsplit(origin).scheme not in {"http", "https"}):
        handler._deny(403, "cross-origin collaborator mutation refused")
        return True
    artifact_id = parts[4]
    if not _access(handler, artifact_id, "read" if kind == "diff" else "admin"):
        handler._deny(404, "not found")
        return True
    grants = server.collab_grant_store
    if kind == "collaborators" and method == "GET":
        state = handler._artifact_call("collab_state", artifact_id) or {}
        handler._json(_plain({"writable": bool(state.get("writable")),
                              "writable_reason": state.get("writable_reason"),
                              "collaborators": grants("collaborators", artifact_id),
                              "pending_invites": grants("pending_invite_grants", artifact_id)}))
    elif kind == "collaborators":
        body = _body(handler, {"agent_id", "permission", "expires_in_days"})
        agent_id = _text(body.get("agent_id"), AGENT_ID, "agent_id")
        permission, expires_at = _permission(body), _expiry(body)
        if _owned_writable(handler, server, artifact_id, permission):
            row = grants("grant", artifact_id, agent_id, permission, expires_at=expires_at,
                         granted_by=handler._resource_principal())
            handler._json(_plain(row), 201)
    elif kind == "revoke":
        _body(handler, set(), optional=True)
        agent_id = _text(tail[1], AGENT_ID, "agent_id")
        handler._json({"agent_id": agent_id, "revoked": bool(grants("revoke", artifact_id, agent_id))})
    elif kind == "invite-grant":
        body = _body(handler, {"invite_id", "permission", "expires_in_days"})
        invite_id = _text(body.get("invite_id"), INVITE_ID, "invite_id")
        permission, expires_at = _permission(body), _expiry(body)
        if _owned_writable(handler, server, artifact_id, permission):
            row = grants("add_invite_grant", invite_id, artifact_id, permission, expires_at=expires_at,
                         granted_by=handler._resource_principal())
            handler._json(_plain(row), 201)
    elif kind == "activity":
        handler._json(_plain({"attempts": grants("attempts", artifact_id)}))
    else:
        result = handler._artifact_call("version_text_diff", artifact_id, tail[1])
        if result is None:
            handler._deny(404, "not found")
        else:
            handler._json(result)
    return True

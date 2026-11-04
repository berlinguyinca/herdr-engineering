"""Member identity, usage accounting and audit for member-owned artifacts (plan 5, Task 1).

A member is a signed-in collab user (or an agent owned by one) acting as principal ``member:<user_id>``.  Storage is not
capped by default: limits are optional, ``None`` meaning unlimited (or "inherit the configured default" in a per-member
override row).  Accounting always runs.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

import asyncpg

from .collab_cognito import _USER_RE

MEMBER_PREFIX: Final = "member:"
_LIMIT_FIELDS: Final = ("max_artifact_bytes", "max_member_bytes", "max_artifacts")
_UNSET: Final[Any] = object()


def member_principal(user_id: str) -> str:
    """The one derivation of a member principal; ``ValueError`` for anything Cognito could not have issued."""
    if not isinstance(user_id, str) or not _USER_RE.match(user_id):
        raise ValueError("invalid member user id")
    return MEMBER_PREFIX + user_id


def is_member_principal(p: str | None) -> bool:
    return isinstance(p, str) and p.startswith(MEMBER_PREFIX) and _USER_RE.match(p[len(MEMBER_PREFIX):]) is not None


def member_owner_key(principal: str) -> str:
    """Staging / view key: ``m_`` plus 24 hex chars of sha256(principal) (26 characters)."""
    return "m_" + hashlib.sha256(principal.encode("utf-8")).hexdigest()[:24]


@dataclass(frozen=True)
class MemberCheck:
    ok: bool
    principal: str | None
    reason: str | None


def _refuse(reason: str) -> MemberCheck:
    return MemberCheck(False, None, reason)


def member_for_actor(actor: Mapping[str, Any], *, agents: Mapping[str, Mapping[str, Any]], group: str,
                     directory: Mapping[str, Mapping[str, Any]]) -> MemberCheck:
    """Resolve an intent actor (``{"kind": "agent", "agent_id"}`` or ``{"kind": "user", "user_id"}``) to a member.

    ``agents`` is the agents feed (``{agent_id: {"owner", "status", "external"}}``); ``directory`` maps a user id to
    ``{"status": "approved"|"disabled"|..., "groups": [...]}``.  A user actor skips the agent checks.
    """
    if actor.get("kind") == "agent":
        agent = agents.get(str(actor.get("agent_id")))
        if agent is None:
            return _refuse("unknown_agent")
        if agent.get("external") is not False:  # fail closed: a feed entry without the flag is not admitted
            return _refuse("external_agent")
        if agent.get("status") != "active":
            return _refuse("agent_inactive")
        user_id = agent.get("owner") or agent.get("owner_user_id")
    elif actor.get("kind") == "user":
        user_id = actor.get("user_id")
    else:
        return _refuse("unknown_user")
    entry = directory.get(user_id) if isinstance(user_id, str) else None
    if entry is None:
        return _refuse("unknown_user")
    groups = entry.get("groups")
    if not isinstance(groups, (list, tuple, set, frozenset)) or group not in groups:
        return _refuse("not_in_group")
    if entry.get("status") != "approved":
        return _refuse("user_disabled")
    try:
        return MemberCheck(True, member_principal(user_id), None)
    except ValueError:
        return _refuse("unknown_user")


@dataclass(frozen=True)
class MemberLimits:
    max_artifact_bytes: int | None = None
    max_member_bytes: int | None = None
    max_artifacts: int | None = None

    @staticmethod
    def effective(row: Mapping[str, Any] | None, defaults: Mapping[str, Any] | None) -> MemberLimits:
        """Per-member override when set, else the configured default, else unlimited (``None``)."""
        row, defaults = row or {}, defaults or {}
        picked = {f: (row.get(f) if row.get(f) is not None else defaults.get(f)) for f in _LIMIT_FIELDS}
        return MemberLimits(**picked)


_MAX_LIMIT: Final = {"max_artifact_bytes": 2**63 - 1, "max_member_bytes": 2**63 - 1, "max_artifacts": 2**31 - 1}


def _positive(name: str, value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= _MAX_LIMIT[name]:
        raise ValueError(f"{name} must be a positive integer within range, or None")
    return value


class MemberStore:
    def __init__(self, conn: asyncpg.Connection, *, defaults: Mapping[str, Any] | None = None) -> None:
        self.conn = conn
        self.defaults = dict(defaults or {})

    async def usage(self, principal: str) -> dict[str, Any]:
        if not is_member_principal(principal):
            raise ValueError("not a member principal")
        await self.conn.execute(
            "INSERT INTO artifact_member_usage (member_principal) VALUES ($1) ON CONFLICT DO NOTHING", principal)
        row = dict(await self.conn.fetchrow("SELECT * FROM artifact_member_usage WHERE member_principal=$1", principal))
        counts = await self.conn.fetchrow(
            "SELECT (SELECT count(*) FROM artifacts WHERE created_by=$1) AS artifacts,"
            " (SELECT count(*) FROM artifact_versions v JOIN artifacts a USING (artifact_id) WHERE a.created_by=$1)"
            " AS versions", principal)
        links = await self.conn.fetch(
            "SELECT audience, count(*) AS n FROM artifact_shares WHERE created_by=$1 AND state='live'"
            " GROUP BY audience", principal)
        by_audience = {"lab": 0, "public": 0} | {r["audience"]: r["n"] for r in links}
        limits = MemberLimits.effective(row, self.defaults)
        return {**row, "limits": {f: getattr(limits, f) for f in _LIMIT_FIELDS},
                "artifact_count": counts["artifacts"], "version_count": counts["versions"],
                "live_links": by_audience}

    async def set_limits(self, principal: str, *, max_artifact_bytes: Any = _UNSET, max_member_bytes: Any = _UNSET,
                         max_artifacts: Any = _UNSET, sharing_disabled: bool | None = None,
                         actor: str) -> dict[str, Any]:
        """``None`` clears an override; leaving an argument out leaves it unchanged."""
        given = {"max_artifact_bytes": max_artifact_bytes, "max_member_bytes": max_member_bytes,
                 "max_artifacts": max_artifacts}
        changes = {k: _positive(k, v) for k, v in given.items() if v is not _UNSET}
        if sharing_disabled is not None:
            changes["sharing_disabled"] = bool(sharing_disabled)
        async with self.conn.transaction():
            await self.usage(principal)
            for column, value in changes.items():  # column names come from the fixed set above
                await self.conn.execute(
                    f"UPDATE artifact_member_usage SET {column}=$2, updated_at=now() WHERE member_principal=$1",
                    principal, value)
            await self.record(principal, "set_limits", actor={"kind": "operator", "id": actor}, outcome="ok",
                              detail={"changes": changes})
        return await self.usage(principal)

    async def record(self, principal: str, action: str, *, actor: Mapping[str, Any], artifact_id: str | None = None,
                     share_id: str | None = None, audience: str | None = None, outcome: str,
                     code: str | None = None, detail: Mapping[str, Any] | None = None) -> None:
        await self.conn.execute(
            "INSERT INTO artifact_member_events (member_principal, actor, action, artifact_id, share_id, audience,"
            " outcome, code, detail) VALUES ($1,$2::jsonb,$3,$4,$5,$6,$7,$8,$9::jsonb)",
            principal, json.dumps(dict(actor)), action, artifact_id, share_id, audience, outcome, code,
            json.dumps(dict(detail or {})))

    async def list_members(self) -> list[dict[str, Any]]:
        rows = await self.conn.fetch("SELECT member_principal FROM artifact_member_usage ORDER BY member_principal")
        return [await self.usage(r["member_principal"]) for r in rows]

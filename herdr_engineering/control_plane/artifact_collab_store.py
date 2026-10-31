"""Collaborator grants, invite grants, the intent ledger and verified uploads (migration 0045).

Grants live in ``artifact_acl`` under the principal ``collab:<agent_id>``.  ``conn`` is an asyncpg connection (or any
object with the same ``fetch``/``fetchrow``/``fetchval``/``execute`` coroutines).  An *actor* is any object with
``kind`` ('agent' or 'user') and ``id`` attributes.

The database does NOT enforce idempotency-key uniqueness or the per-agent daily quota: the applier (Task 5a) must
serialize work per key and per agent (for example with a Postgres advisory lock) around the check-then-record steps.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Literal

PREFIX = "collab:"
MAX_GRANT_DAYS = 90
_PERMS = {"read": ["read"], "write": ["read", "edit"]}


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


_MAX_EPOCH = 32503680000   # 3000-01-01 UTC: larger numbers are epoch milliseconds, which would disable a cap


def _moment(value) -> datetime:
    """A datetime from a datetime, epoch seconds or an ISO-8601 string; ValueError for anything implausible."""
    if isinstance(value, bool):
        raise ValueError("not a timestamp: bool")
    if isinstance(value, datetime):
        moment = _aware(value)
    elif isinstance(value, (int, float)):
        if not 0 <= value <= _MAX_EPOCH:
            raise ValueError(f"epoch seconds out of range: {value!r}")
        moment = datetime.fromtimestamp(value, UTC)
    else:
        moment = _aware(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
    if moment.year > 3000:
        raise ValueError("timestamp beyond year 3000")
    return moment


def _check_grant(permission: str, expires_at: datetime) -> datetime:
    if permission not in _PERMS:
        raise ValueError(f"invalid collaborator permission: {permission!r}")
    expires_at = _aware(expires_at)
    if expires_at > datetime.now(UTC) + timedelta(days=MAX_GRANT_DAYS):
        raise ValueError(f"collaborator grants expire within {MAX_GRANT_DAYS} days")
    return expires_at


def _permission(raw) -> str:
    perms = json.loads(raw) if isinstance(raw, str) else raw
    return "write" if "edit" in perms else "read"


def _grant_row(row) -> dict:
    return {"artifact_id": row["artifact_id"], "agent_id": row["principal_id"][len(PREFIX):],
            "permission": _permission(row["permissions"]), "expires_at": row["expires_at"]}


def _names(raw) -> list:
    if raw is None:
        return []
    return json.loads(raw) if isinstance(raw, str) else list(raw)


class InviteGrantConflict(Exception):
    """The invite id is already registered for a different artifact, permission or grantor (nothing is disclosed)."""


class CollabGrantStore:
    def __init__(self, conn):
        self.conn = conn

    # -- grants -----------------------------------------------------------------------------------------------
    async def grant(self, artifact_id: str, agent_id: str, permission: Literal["read", "write"], *,
                    expires_at: datetime, granted_by: str) -> dict:
        expires_at = _check_grant(permission, expires_at)
        await self._upsert_acl(artifact_id, agent_id, permission, expires_at, granted_by, merge=False)
        return {"artifact_id": artifact_id, "agent_id": agent_id, "permission": permission, "expires_at": expires_at}

    async def _upsert_acl(self, artifact_id: str, agent_id: str, permission: str, expires_at: datetime,
                          granted_by: str, *, merge: bool) -> None:
        """Write the ``collab:<agent_id>`` row.  ``merge=False`` replaces it (an owner's explicit decision, which may
        narrow or shorten a grant).  ``merge=True`` never downgrades a live row: the stronger permission set and the
        later expiry win, the expiry still capped at 90 days from now.  An expired row counts as absent."""
        perms = json.dumps(_PERMS[permission])
        if not merge:
            sql = ("INSERT INTO artifact_acl (artifact_id, principal_id, permissions, expires_at, granted_by) "
                   "VALUES ($1,$2,$3::jsonb,$4,$5) ON CONFLICT (artifact_id, principal_id) DO UPDATE SET "
                   "permissions = EXCLUDED.permissions, expires_at = EXCLUDED.expires_at, "
                   "granted_by = EXCLUDED.granted_by")
        else:
            sql = ("INSERT INTO artifact_acl (artifact_id, principal_id, permissions, expires_at, granted_by) "
                   "VALUES ($1,$2,$3::jsonb,$4,$5) ON CONFLICT (artifact_id, principal_id) DO UPDATE SET "
                   "permissions = CASE WHEN EXCLUDED.permissions ? 'edit' "
                   "OR (artifact_acl.expires_at > now() AND artifact_acl.permissions ? 'edit') "
                   "THEN '[\"read\",\"edit\"]'::jsonb ELSE '[\"read\"]'::jsonb END, "
                   "expires_at = LEAST(GREATEST(EXCLUDED.expires_at, CASE WHEN artifact_acl.expires_at > now() "
                   "THEN artifact_acl.expires_at END), now() + interval '2160 hours'), "
                   "granted_by = EXCLUDED.granted_by")
        await self.conn.execute(sql, artifact_id, PREFIX + agent_id, perms, expires_at, granted_by)

    async def revoke(self, artifact_id: str, agent_id: str) -> bool:
        deleted = await self.conn.fetch(
            "DELETE FROM artifact_acl WHERE artifact_id=$1 AND principal_id=$2 RETURNING 1",
            artifact_id, PREFIX + agent_id)
        return bool(deleted)

    async def revoke_write_grants(self, artifact_id: str) -> int:
        rows = await self.conn.fetch(
            "DELETE FROM artifact_acl WHERE artifact_id=$1 AND left(principal_id, $2) = $3 "
            "AND permissions ? 'edit' RETURNING 1", artifact_id, len(PREFIX), PREFIX)
        return len(rows)

    async def revoke_all_grants(self, artifact_id: str) -> int:
        rows = await self.conn.fetch(
            "DELETE FROM artifact_acl WHERE artifact_id=$1 AND left(principal_id, $2) = $3 RETURNING 1",
            artifact_id, len(PREFIX), PREFIX)
        return len(rows)

    async def live_grants(self) -> list[dict]:
        rows = await self.conn.fetch(
            "SELECT artifact_id, principal_id, permissions, expires_at FROM artifact_acl "
            "WHERE left(principal_id, $1) = $2 AND expires_at > now() ORDER BY artifact_id, principal_id",
            len(PREFIX), PREFIX)
        return [_grant_row(r) for r in rows]

    async def grant_for(self, artifact_id: str, agent_id: str) -> dict | None:
        row = await self.conn.fetchrow(
            "SELECT artifact_id, principal_id, permissions, expires_at FROM artifact_acl "
            "WHERE artifact_id=$1 AND principal_id=$2", artifact_id, PREFIX + agent_id)
        return _grant_row(row) if row else None

    # -- invite grants ----------------------------------------------------------------------------------------
    async def add_invite_grant(self, invite_id: str, artifact_id: str, permission: Literal["read", "write"], *,
                               expires_at: datetime, granted_by: str) -> dict:
        expires_at = _check_grant(permission, expires_at)
        inserted = await self.conn.fetchval(
            "INSERT INTO artifact_collab_invite_grants (invite_id, artifact_id, permission, expires_at, granted_by) "
            "VALUES ($1,$2,$3,$4,$5) ON CONFLICT (invite_id) DO NOTHING RETURNING 1",
            invite_id, artifact_id, permission, expires_at, granted_by)
        if inserted:
            return {"invite_id": invite_id, "artifact_id": artifact_id, "permission": permission,
                    "expires_at": expires_at, "granted_by": granted_by}
        # invite_id is a global key: a repeat is fine only when it is exactly the grant already stored
        row = await self.conn.fetchrow(
            "SELECT invite_id, artifact_id, permission, expires_at, granted_by FROM artifact_collab_invite_grants "
            "WHERE invite_id=$1", invite_id)
        if row is None or (row["artifact_id"], row["permission"], row["granted_by"]) != (
                artifact_id, permission, granted_by):
            raise InviteGrantConflict("invite already registered")
        return dict(row)

    async def pending_invite_grants(self, artifact_id: str) -> list[dict]:
        rows = await self.conn.fetch(
            "SELECT invite_id, artifact_id, permission, expires_at, granted_by, created_at "
            "FROM artifact_collab_invite_grants WHERE artifact_id=$1 AND resolved_at IS NULL ORDER BY created_at, "
            "invite_id", artifact_id)
        return [dict(r) for r in rows]

    async def resolve_invite_grants(self, join_to_agent: dict[str, dict]) -> int:
        """Turn each pending invite grant whose invite joined into a real grant; return how many resolved.

        ``join_to_agent`` maps invite id -> ``{agent_id | id, hard_expiry}``.  Already resolved rows are skipped.
        Each invite is claimed and granted in one transaction, so a failure leaves it unresolved for a retry.  An
        invite whose own expiry has passed is marked resolved with no grant.  The grant never downgrades one the
        agent already holds: the stronger permission and the later expiry (capped at 90 days from now) win.
        """
        if not join_to_agent:
            return 0
        rows = await self.conn.fetch(
            "SELECT invite_id, artifact_id, permission, expires_at, granted_by FROM artifact_collab_invite_grants "
            "WHERE resolved_at IS NULL AND invite_id = ANY($1::text[]) ORDER BY invite_id", list(join_to_agent))
        resolved = 0
        for row in rows:
            agent = join_to_agent[row["invite_id"]]
            agent_id = agent.get("agent_id") or agent.get("id")
            if not agent_id:
                continue
            expires_at = row["expires_at"]
            if agent.get("hard_expiry") is not None:
                expires_at = min(expires_at, _moment(agent["hard_expiry"]))
            async with self.conn.transaction():
                claimed = await self.conn.fetchval(
                    "UPDATE artifact_collab_invite_grants SET resolved_agent_id=$2, resolved_at=now() "
                    "WHERE invite_id=$1 AND resolved_at IS NULL RETURNING 1", row["invite_id"], agent_id)
                if not claimed or row["expires_at"] <= datetime.now(UTC) or expires_at <= datetime.now(
                        UTC):
                    continue        # lost the race, or the invite lapsed: resolved without a grant
                await self._upsert_acl(row["artifact_id"], agent_id, row["permission"], expires_at,
                                       row["granted_by"], merge=True)
                resolved += 1
        return resolved

    # -- intent ledger ----------------------------------------------------------------------------------------
    async def record_received(self, ticket_id: str, kind: str, actor, artifact_id: str | None,
                              base_version_id: str | None, idempotency_key: str | None, request_sha256: str | None,
                              bytes: int) -> dict | None:  # noqa: A002 - the plan's name
        inserted = await self.conn.fetchval(
            "INSERT INTO artifact_collab_intents (ticket_id, kind, actor_kind, actor_id, artifact_id, "
            "base_version_id, idempotency_key, request_sha256, bytes, outcome) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,'received') ON CONFLICT (ticket_id) DO NOTHING RETURNING 1",
            ticket_id, kind, actor.kind, actor.id, artifact_id, base_version_id, idempotency_key, request_sha256,
            bytes)
        if inserted:
            return None
        row = await self.conn.fetchrow("SELECT * FROM artifact_collab_intents WHERE ticket_id=$1", ticket_id)
        return dict(row) if row else None

    async def record_outcome(self, ticket_id: str, outcome: str, *, version_id: str | None = None,
                             attempt_id: str | None = None, reason: str | None = None,
                             bytes: int | None = None) -> bool:  # noqa: A002 - matches record_received
        """Decide a ticket once.  True if this call changed the row; False if it was already decided or is unknown.

        ``bytes`` (when given) replaces the row's inline-byte count in the same update: a ticket settled by replaying
        an earlier one stored nothing new and is set to 0, so the quota never counts its text twice."""
        decided = await self.conn.fetchval(
            "UPDATE artifact_collab_intents SET outcome=$2, version_id=$3, attempt_id=$4, reason=$5, "
            "bytes=COALESCE($6, bytes), decided_at=now() WHERE ticket_id=$1 AND outcome='received' RETURNING 1",
            ticket_id, outcome, version_id, attempt_id, reason, bytes)
        return bool(decided)

    async def decided_for_key(self, actor, kind: str, idempotency_key: str,
                              outcomes: frozenset[str]) -> dict | None:
        row = await self.conn.fetchrow(
            "SELECT * FROM artifact_collab_intents WHERE actor_kind=$1 AND actor_id=$2 AND kind=$3 "
            "AND idempotency_key=$4 AND outcome = ANY($5::text[]) ORDER BY received_at LIMIT 1",
            actor.kind, actor.id, kind, idempotency_key, sorted(outcomes))
        return dict(row) if row else None

    async def bytes_last_24h(self, agent_id: str, *, exclude_ticket: str | None = None) -> int:
        inline = await self.conn.fetchval(
            "SELECT COALESCE(sum(bytes), 0) FROM artifact_collab_intents WHERE kind='commit' AND actor_kind='agent' "
            "AND actor_id=$1 AND outcome <> 'rejected' AND received_at > now() - interval '24 hours' "
            "AND ticket_id IS DISTINCT FROM $2", agent_id, exclude_ticket)
        staged = await self.conn.fetchval(
            "SELECT COALESCE(sum(size_bytes), 0) FROM artifact_collab_uploads WHERE owner_key=$1 AND charged "
            "AND verified_at > now() - interval '24 hours'", agent_id)
        return int(inline) + int(staged)

    # -- verified uploads -------------------------------------------------------------------------------------
    async def verified_upload(self, owner_key: str, upload_id: str) -> dict | None:
        row = await self.conn.fetchrow(
            "SELECT * FROM artifact_collab_uploads WHERE owner_key=$1 AND upload_id=$2", owner_key, upload_id)
        return dict(row) if row else None

    async def record_verified_upload(self, owner_key: str, upload_id: str, *, artifact_id: str, name: str,
                                     sha256: str, size_bytes: int, storage_key: str, media_type: str,
                                     charged: bool) -> dict:
        await self.conn.execute(
            "INSERT INTO artifact_collab_uploads (owner_key, upload_id, artifact_id, name, sha256, size_bytes, "
            "storage_key, media_type, charged) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9) "
            "ON CONFLICT (owner_key, upload_id) DO NOTHING",
            owner_key, upload_id, artifact_id, name, sha256, size_bytes, storage_key, media_type, charged)
        return await self.verified_upload(owner_key, upload_id)

    # -- activity ---------------------------------------------------------------------------------------------
    async def collaborators(self, artifact_id: str) -> list[dict]:
        grants = await self.conn.fetch(
            "SELECT artifact_id, principal_id, permissions, expires_at FROM artifact_acl "
            "WHERE artifact_id=$1 AND left(principal_id, $2) = $3 ORDER BY principal_id",
            artifact_id, len(PREFIX), PREFIX)
        stats = {r["actor_id"]: r for r in await self.conn.fetch(
            "SELECT actor_id, max(received_at) AS last_used_at, "
            "count(*) FILTER (WHERE outcome IN ('created','merged')) AS commits, "
            "count(*) FILTER (WHERE outcome='conflict') AS conflicts, "
            "count(*) FILTER (WHERE outcome='rejected') AS rejected "
            "FROM artifact_collab_intents WHERE artifact_id=$1 AND kind='commit' AND actor_kind='agent' "
            "GROUP BY actor_id", artifact_id)}
        out = []
        for row in grants:
            item = _grant_row(row)
            stat = stats.get(item["agent_id"])
            item.update(last_used_at=stat["last_used_at"] if stat else None,
                        commits=stat["commits"] if stat else 0, conflicts=stat["conflicts"] if stat else 0,
                        rejected=stat["rejected"] if stat else 0)
            out.append(item)
        return out

    async def attempts(self, artifact_id: str, *, limit: int = 100) -> list[dict]:
        rows = await self.conn.fetch(
            "SELECT i.*, (SELECT a.conflict_names FROM artifact_collab_attempts a WHERE a.ticket_id = i.ticket_id "
            "ORDER BY a.created_at DESC LIMIT 1) AS conflict_names FROM artifact_collab_intents i "
            "WHERE i.artifact_id=$1 AND i.outcome IN ('conflict','rejected') ORDER BY i.received_at DESC, "
            "i.ticket_id LIMIT $2", artifact_id, limit)
        return [{**dict(r), "conflict_names": _names(r["conflict_names"])} for r in rows]

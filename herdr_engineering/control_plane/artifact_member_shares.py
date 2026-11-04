"""Member share handlers (plan 5, Task 7): a member creates, approves and revokes lab and public share links.

Applied on the control-plane worker beside the Task-4 handlers (``register_member_intents`` attaches them; this module is
imported there, never by collab-public):

* ``member.share_create {version_id, audience, allow_groups?, allow_emails?, expires_in_days?, entry?, title?}``: a
  ``pending`` share and its dry-run summary.  The artifact id is in the envelope.
* ``member.share_approve {share_id, confirm: true}``: builds the frozen bundle (the operator ``approve_share``), marks the
  share live and leaves the URL in a secret result file (``take_secret``, read once, swept after ``SECRET_TTL``).  The
  URL is in no result, event, view or log line.
* ``member.share_revoke {share_id}`` and ``member.share_events {share_id}``: for the share's creator or the owner of the
  artifact.

The authority is the ``ShareStore`` (grant check at create and again at ``mark_live``, member audience rules, no
``collab:`` creators); the handlers add the member gate, ``sharing_disabled``, the directory-derived audience (the
member's own groups and the approved members' emails) and the owner-only revoke.  Anything the member may not touch is
the same ``not_found``, whether or not it exists.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from .artifact_member_intents import (
    SECRETS_DIR,
    MemberRuntime,
    _actor_json,
    _member,
    _rejected,
    _Who,
    load_directory,
    revoke_share,
    sweep_secrets,
)
from .collab_bridge_files import BridgeError, Intent, link_new_beneath

if TYPE_CHECKING:
    from .collab_intents import IntentContext

log = logging.getLogger(__name__)

SUMMARY_FILES = 50
SECRET_DIR_MODE = 0o700


class _LoopCall:
    """``store.sync_call`` for the operator share code, which is synchronous: runs the store's coroutine on the worker's
    event loop from the thread ``approve_share`` / ``_plan`` run in.  The connection stays lane-exclusive."""

    def __init__(self, loop: asyncio.AbstractEventLoop, target: Any):
        self._loop, self._target = loop, target

    def __call__(self, method: str, *args: Any, **kwargs: Any) -> Any:
        return asyncio.run_coroutine_threadsafe(getattr(self._target, method)(*args, **kwargs), self._loop).result()


class _Server:
    """The slice of the API server ``_plan`` and ``approve_share`` read, backed by this ticket's connection."""

    def __init__(self, ctx: IntentContext, rt: MemberRuntime):
        from .artifact_shares import ShareStore
        loop = asyncio.get_running_loop()
        self.share_settings = rt.share_settings
        self.shares_root = rt.shares_root
        self.share_store = _LoopCall(loop, ShareStore(ctx.conn))
        self.artifact_store = _LoopCall(loop, ctx.store)
        self.rustfs = ctx.store.rustfs


async def _touch(ctx: IntentContext, who: _Who) -> None:
    await ctx.conn.execute("UPDATE artifact_member_usage SET last_used_at=now() WHERE member_principal=$1",
                           who.principal)


async def _refuse(ctx: IntentContext, who: _Who, action: str, reason: str, intent: Intent, *, share_id: str | None = None,
                  audience: str | None = None, audit: bool = True) -> dict:
    """A rejection the member gets, audited (unless the store already wrote the event) and handed to the view hook."""
    from .artifact_members import MemberStore
    if audit:
        await MemberStore(ctx.conn).record(
            who.principal, action, actor=_actor_json(who.actor), artifact_id=intent.artifact_id, share_id=share_id,
            audience=audience, outcome="refused", code=reason)
    return {**_rejected(reason), "_principal": who.principal}


async def _gate(ctx: IntentContext, intent: Intent, action: str, *, spends: bool) -> tuple[_Who | None, dict | None]:
    """The admitted member, or the rejection to return.  ``spends`` actions (create, approve) honour
    ``sharing_disabled``; revoking and reading events stay available so a member can always clean up."""
    from .artifact_members import MemberStore
    who, refusal = await _member(ctx, action)
    if refusal is not None:
        return None, refusal
    assert who is not None
    if who.rt.share_settings is None or who.rt.shares_root is None:
        return None, await _refuse(ctx, who, action, "sharing_unavailable", intent)
    if spends:
        usage = await MemberStore(ctx.conn, defaults=who.rt.defaults).usage(who.principal)
        if usage["sharing_disabled"]:
            return None, await _refuse(ctx, who, action, "sharing_disabled", intent)
    return who, None


async def _may_share_active(conn: Any, artifact_id: str | None, principal: str) -> bool:
    """The principal owns the artifact or holds a live ``share``/``admin`` grant on it, and it is not deleted."""
    from .artifact_shares import _may_share
    if artifact_id is None:
        return False
    lifecycle = await conn.fetchval("SELECT lifecycle FROM artifacts WHERE artifact_id=$1", artifact_id)
    return lifecycle == "active" and await _may_share(conn, artifact_id, principal)


def _http_reason(exc: Any) -> str:
    from .collab_intents import REASON_MAX
    return "not_found" if exc.status == 404 else str(exc.message)[:REASON_MAX] or "refused"


def _capped(summary: dict) -> dict:
    files = summary.get("files")
    if isinstance(files, list) and len(files) > SUMMARY_FILES:
        return {**summary, "files": files[:SUMMARY_FILES], "files_truncated": True}
    return summary


# ----------------------------------------------------------------------------- create

def _audience_pool(directory: dict[str, dict[str, Any]], user_id: str, rt: MemberRuntime) -> tuple[frozenset, frozenset]:
    """``(groups, emails)`` this member may put on a lab share: the groups it belongs to, and the lower-cased emails of
    the approved members of the directory."""
    mine = directory.get(user_id) or {}
    groups = frozenset(g for g in (mine.get("groups") or [rt.group]) if isinstance(g, str))
    emails = frozenset(e["email"].strip().lower() for e in directory.values()
                       if e.get("status") == "approved" and isinstance(e.get("email"), str) and e["email"].strip())
    return groups, emails


async def handle_share_create(ctx: IntentContext, intent: Intent) -> dict:
    from .artifact_share_api import ShareHTTPError, _jsonable, _plan, _summary
    from .artifact_shares import ShareError, ShareStore
    from .collab_intents import REASON_MAX
    who, refusal = await _gate(ctx, intent, "share_create", spends=True)
    if refusal is not None:
        return refusal
    assert who is not None
    p, aid, rt = intent.payload, intent.artifact_id, who.rt
    if not await _may_share_active(ctx.conn, aid, who.principal):
        return await _refuse(ctx, who, "share_create", "not_found", intent, audience=p.audience)
    directory = await asyncio.to_thread(load_directory, rt)
    if directory is None:
        return _rejected("directory_stale")
    groups, emails = _audience_pool(directory, who.principal[len("member:"):], rt)
    if p.audience == "lab" and not set(p.allow_groups or ()) <= groups:    # exactly a group of the member: no trimming
        return await _refuse(ctx, who, "share_create", "member_audience_refused", intent, audience=p.audience)
    days = p.expires_in_days
    if days is None and p.audience == "public":
        days = rt.share_settings["default_public_days"]
    expires_at = datetime.now(UTC) + timedelta(days=days) if days is not None else None
    server = _Server(ctx, rt)
    try:
        files, chosen, warnings = await asyncio.to_thread(_plan, server, aid, p.version_id, p.entry, who.principal)
    except ShareHTTPError as exc:
        return await _refuse(ctx, who, "share_create", _http_reason(exc), intent, audience=p.audience)
    try:
        row = await ShareStore(ctx.conn).create(
            artifact_id=aid, version_id=p.version_id, audience=p.audience,
            allow_emails=None if p.allow_emails is None else list(p.allow_emails),
            allow_groups=None if p.allow_groups is None else list(p.allow_groups), expires_at=expires_at,
            entry=p.entry, title=p.title, created_by=who.principal, member_groups=groups, member_emails=emails)
    except ShareError as exc:           # the store audited the refusal itself
        return {**_rejected(exc.code or str(exc)[:REASON_MAX] or "refused"), "_principal": who.principal}
    await _touch(ctx, who)
    summary = _capped(_summary(files, chosen, warnings, p.audience, row.get("expires_at")))
    return {"result": "created", "share_id": row["share_id"], "audience": p.audience, "summary": _jsonable(summary),
            "_principal": who.principal}


# ----------------------------------------------------------------------------- approve

async def handle_share_approve(ctx: IntentContext, intent: Intent) -> dict:
    from .artifact_share_api import ShareHTTPError, approve_share
    from .artifact_shares import ShareStore
    who, refusal = await _gate(ctx, intent, "share_approve", spends=True)
    if refusal is not None:
        return refusal
    assert who is not None
    rt, aid, share_id = who.rt, intent.artifact_id, intent.payload.share_id
    share = await ShareStore(ctx.conn).get(share_id)
    if share is None or share["artifact_id"] != aid or share["created_by"] != who.principal \
            or not await _may_share_active(ctx.conn, aid, who.principal):
        return await _refuse(ctx, who, "share_approve", "not_found", intent, share_id=share_id)
    if share["state"] == "live":        # a replay or a second call: the URL was shown once, never again
        return {"result": "already_approved", "share_id": share_id, "audience": share["audience"],
                "_principal": who.principal}
    if share["state"] != "pending":
        return await _refuse(ctx, who, "share_approve", "not_pending", intent, share_id=share_id,
                             audience=share["audience"])
    await asyncio.to_thread(sweep_secrets, rt)
    try:
        url, summary = await asyncio.to_thread(approve_share, _Server(ctx, rt), share, approved_by=who.principal)
    except ShareHTTPError as exc:
        # mark_live's own refusals (404, 410) are audited by the store; the rest are ours to record
        return await _refuse(ctx, who, "share_approve", _http_reason(exc) if exc.status != 410 else "expired", intent,
                             share_id=share_id, audience=share["audience"], audit=exc.status not in (404, 410))
    secret = json.dumps({"ticket": intent.ticket, "principal": who.principal, "url": url,
                         "at": rt.clock()}).encode("utf-8")
    try:
        await asyncio.to_thread(link_new_beneath, rt.outbox, [SECRETS_DIR], intent.ticket + ".json", secret,
                                mode=rt.secret_mode, create_dirs=True, dir_mode=SECRET_DIR_MODE)
    except (BridgeError, OSError):      # a live link nobody can receive is worse than none
        log.error("member share %s: the secret result could not be written; revoking", share_id)
        await revoke_share(ctx.conn, rt, share_id, actor="secret-unavailable")
        return await _refuse(ctx, who, "share_approve", "secret_unavailable", intent, share_id=share_id,
                             audience=share["audience"])
    await _touch(ctx, who)
    return {"result": "live", "share_id": share_id, "audience": share["audience"], "secret_result": True,
            "summary": _capped(summary), "_principal": who.principal}


# ----------------------------------------------------------------------------- revoke and events

async def _creator_or_owner(ctx: IntentContext, intent: Intent, who: _Who) -> dict | None:
    """The share row when the member created it or owns its artifact; None otherwise (and when it does not exist)."""
    from .artifact_member_intents import _own
    from .artifact_shares import ShareStore
    share = await ShareStore(ctx.conn).get(intent.payload.share_id)
    if share is None or share["artifact_id"] != intent.artifact_id:
        return None
    if share["created_by"] == who.principal or await _own(ctx.conn, intent.artifact_id, who.principal) is not None:
        return share
    return None


async def handle_share_revoke(ctx: IntentContext, intent: Intent) -> dict:
    who, refusal = await _gate(ctx, intent, "share_revoke", spends=False)
    if refusal is not None:
        return refusal
    assert who is not None
    share = await _creator_or_owner(ctx, intent, who)
    if share is None:
        return await _refuse(ctx, who, "share_revoke", "not_found", intent, share_id=intent.payload.share_id)
    await revoke_share(ctx.conn, who.rt, share["share_id"], actor=who.principal)
    return {"result": "revoked", "share_id": share["share_id"], "audience": share["audience"],
            "_principal": who.principal}


async def handle_share_events(ctx: IntentContext, intent: Intent) -> dict:
    from .artifact_shares import ShareStore
    from .collab_intents import MAX_SHARE_EVENTS
    who, refusal = await _gate(ctx, intent, "share_events", spends=False)
    if refusal is not None:
        return refusal
    assert who is not None
    share = await _creator_or_owner(ctx, intent, who)
    if share is None:
        return await _refuse(ctx, who, "share_events", "not_found", intent, share_id=intent.payload.share_id)
    rows = await ShareStore(ctx.conn).events(share["share_id"], limit=MAX_SHARE_EVENTS)
    events = [{"kind": r["kind"], "at": r["at"].isoformat(), "actor": r["actor"]} for r in rows]
    return {"result": "listed", "share_id": share["share_id"], "events": events, "_principal": who.principal}

"""Member intents: a signed-in member (or an agent owned by one) creates, publishes and deletes its own artifacts.

Three kinds, applied by the plan-3 ``CollabIntentApplier`` on the control-plane worker:

* ``member.artifact_create {title, filename, profile}``
* ``member.publish {base_version_id, files: [{name, upload_id}], message, entry?}`` (artifact id in the envelope)
* ``member.artifact_delete {confirm: true}`` (artifact id in the envelope)

Light on purpose, like ``annotation_intents``: ``collab-public`` imports this module (through ``collab_artifacts``) so
the validators are registered there and ``append_intent`` can check a payload, while the worker imports it for the
handlers. asyncpg, the stores and ``collab_intents`` are imported inside the functions that need them. The kinds are
registered at import time, and registering twice raises, so every process imports this module once.

Every handler starts with ``_member``: the feature is enabled, ``member-directory.json`` (written by the control plane,
see ``MemberRuntime``) is fresh (``FEED_MAX_AGE``), and ``member_for_actor`` admits the actor. A member only ever acts on
what it created (``_own``): anything else, absent or someone else's, is the same ``not_found``.

Results carry stable codes (``directory_stale``, ``members_disabled``, ``not_found``, ``deleted``, a quota code, a
``member_for_actor`` reason); upload failures keep the plan-3 texts (``upload not found``, ``upload failed
verification``). No payload text is ever echoed.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import mimetypes
import os
import re
import stat
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .collab_bridge_files import (
    FEED_MAX_AGE,
    MAX_MESSAGE,
    TICKET_RE,
    UPLOAD_RE,
    Actor,
    BridgeError,
    BridgeLimits,
    Intent,
    IntentError,
    _check_id,
    _check_text,
    atomic_write_json,
    ensure_dir,
    list_beneath,
    read_json_beneath,
    read_staged_meta,
    register_intent_kind,
    remove_tree_beneath,
    stream_new_beneath,
    tree_entry_cap,
    unlink_beneath,
)
from .share_bundle import check_names, normalise_name

if TYPE_CHECKING:
    from .collab_intents import CollabIntentApplier, IntentContext

log = logging.getLogger(__name__)

KIND_CREATE = "member.artifact_create"
KIND_PUBLISH = "member.publish"
KIND_DELETE = "member.artifact_delete"
KIND_LIBRARY_ACCEPT = "library_accept"
KIND_LIBRARY_DISCARD = "library_discard"
KIND_LIBRARY_FOLDER_CREATE = "library_folder_create"
KIND_LIBRARY_FOLDER_RENAME = "library_folder_rename"
KIND_LIBRARY_FOLDER_DELETE = "library_folder_delete"
KIND_LIBRARY_FILE = "library_file"
KIND_LIBRARY_UNFILE = "library_unfile"
KIND_LIBRARY_CLAIM = "library_claim"
KIND_SHARE_CREATE = "member.share_create"
KIND_SHARE_APPROVE = "member.share_approve"
KIND_SHARE_REVOKE = "member.share_revoke"
KIND_SHARE_EVENTS = "member.share_events"
SECRETS_DIR = "secrets"          # <outbox>/secrets/<ticket>.json: a share URL, read once by its creator's service
SECRET_TTL = 60.0                # seconds an unread secret survives
SECRET_MAX_BYTES = 4096
MEMBER_DIRECTORY = "member-directory.json"
MEMBER_VIEWS_DIR = "member-views"
DIRECTORY_MAX_BYTES = 16 * 1024 * 1024
PROFILES = frozenset({"web", "document"})
MAX_TITLE = 200
VIEW_MAX_ARTIFACTS = 500
VIEW_MAX_VERSIONS = 50
VIEW_MAX_LINKS = 200
VIEW_MAX_SHARED = 500
LIBRARY_VIEW_MAX = 2000
VIEW_INTERVAL = 60.0
MEDIA_TYPES = {
    ".html": "text/html", ".pdf": "application/pdf", ".parquet": "application/vnd.apache.parquet",
    ".css": "text/css", ".js": "application/javascript", ".png": "image/png", ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg", ".svg": "image/svg+xml", ".json": "application/json",
}
ENTRY_EXTENSIONS = (".html", ".pdf")
_UPLOAD = re.compile(UPLOAD_RE)
_TICKET_FIELDS = frozenset({"title", "filename", "profile"})
_PUBLISH_KEYS = frozenset({"base_version_id", "files", "message", "entry"})
_PUBLISH_REQUIRED = frozenset({"base_version_id", "files", "message"})
_FILE_KEYS = frozenset({"name", "upload_id"})


@dataclass(frozen=True)
class MemberRuntime:
    """What the worker hands the handlers through ``context_extras["members"]``.

    ``outbox`` holds ``member-directory.json`` (``{"group", "updated", "users": {user_id: {"status", "groups"?}}}``,
    ``groups`` defaulting to ``[group]``) and ``staging/``; ``mirror_root`` receives ``member-views/``; ``shares_root``
    (the share bundles) is None when sharing is not wired, in which case only rows are revoked; ``defaults`` are the
    configured limit defaults (``max_artifact_bytes``, ``max_member_bytes``, ``max_artifacts``).
    """
    outbox: Path
    mirror_root: Path
    group: str
    enabled: bool
    defaults: Mapping[str, Any]
    shares_root: Path | None
    clock: Callable[[], float]
    removed_grace: float = 3600.0      # seconds a member must be continuously listed ``removed`` before anything is revoked
    share_settings: Mapping[str, Any] | None = None   # ``share_settings_config`` plus ``member_share_limits``; None = off
    secret_mode: int = 0o600           # file mode of a secret result when ``secret_gid`` is None
    secret_gid: int | None = None      # group of collab-public: the secret dir becomes 0770 and the file 0640 in it
    rustfs: Any = None                 # blob client the library content pool is filled from (None: no content is mirrored)


# ----------------------------------------------------------------------------- payloads

@dataclass(frozen=True)
class CreatePayload:
    title: str
    filename: str
    profile: str


@dataclass(frozen=True)
class PublishFile:
    name: str
    upload_id: str


@dataclass(frozen=True)
class PublishPayload:
    base_version_id: str | None
    files: tuple[PublishFile, ...]
    message: str
    entry: str | None


@dataclass(frozen=True)
class DeletePayload:
    confirm: bool


def _bad(what: str) -> IntentError:
    return IntentError(f"invalid {what} payload")


def validate_create_payload(raw: dict, limits: BridgeLimits) -> CreatePayload:
    if not isinstance(raw, dict) or set(raw) != _TICKET_FIELDS:
        raise _bad("member.artifact_create")
    title, filename, profile = raw["title"], raw["filename"], raw["profile"]
    if not isinstance(title, str) or not 1 <= len(title) <= MAX_TITLE or any(ord(c) < 0x20 or ord(c) == 0x7F
                                                                            for c in title):
        raise _bad("member.artifact_create")
    _check_text(title)
    name = normalise_name(filename)
    if "/" in name or profile not in PROFILES:
        raise _bad("member.artifact_create")
    return CreatePayload(title, name, profile)


def validate_publish_payload(raw: dict, limits: BridgeLimits) -> PublishPayload:
    if not isinstance(raw, dict) or not _PUBLISH_REQUIRED <= set(raw) <= _PUBLISH_KEYS:
        raise _bad("member.publish")
    base = raw["base_version_id"]
    if base is not None:
        _check_id(base, "base version id")
    files = raw["files"]
    if not isinstance(files, list) or not 1 <= len(files) <= limits.max_changes_per_commit:
        raise _bad("member.publish")
    listed = []
    for item in files:
        if not isinstance(item, dict) or set(item) != _FILE_KEYS:
            raise _bad("member.publish")
        upload_id = item["upload_id"]
        if not isinstance(upload_id, str) or not _UPLOAD.match(upload_id):
            raise _bad("member.publish")
        listed.append(PublishFile(normalise_name(item["name"]), upload_id))
    names = check_names([f.name for f in listed])          # duplicates, file/directory clashes
    message = raw["message"]
    if not isinstance(message, str) or len(message) > MAX_MESSAGE:
        raise _bad("member.publish")
    _check_text(message)
    entry = raw.get("entry")
    if entry is not None:
        entry = normalise_name(entry)
        if entry not in names:
            raise _bad("member.publish")
    return PublishPayload(base, tuple(listed), message, entry)


def validate_delete_payload(raw: dict, limits: BridgeLimits) -> DeletePayload:
    if not isinstance(raw, dict) or set(raw) != {"confirm"} or raw["confirm"] is not True:
        raise _bad("member.artifact_delete")
    return DeletePayload(True)


_SHARE_ID = re.compile(r"share_[0-9a-f]{24}\Z")
_AUDIENCES = ("lab", "public")
_MAX_ALLOW = 50
_SHARE_CREATE_KEYS = frozenset({"version_id", "audience", "allow_groups", "allow_emails", "expires_in_days", "entry",
                                "title"})


@dataclass(frozen=True)
class ShareCreatePayload:
    version_id: str
    audience: str
    allow_groups: tuple[str, ...] | None
    allow_emails: tuple[str, ...] | None
    expires_in_days: int | None
    entry: str | None
    title: str | None


@dataclass(frozen=True)
class ShareRefPayload:
    share_id: str


def _allow_list(raw: object, what: str) -> tuple[str, ...] | None:
    """Entries are kept EXACTLY as sent (no stripping): the worker compares them with the member's own audience."""
    if raw is None:
        return None
    if not isinstance(raw, list) or len(raw) > _MAX_ALLOW:
        raise _bad(what)
    for item in raw:
        if not isinstance(item, str) or not 1 <= len(item) <= 320 or any(ord(c) < 0x20 or ord(c) == 0x7F for c in item):
            raise _bad(what)
    return tuple(raw)


def validate_share_create_payload(raw: dict, limits: BridgeLimits) -> ShareCreatePayload:
    what = KIND_SHARE_CREATE
    if not isinstance(raw, dict) or not {"version_id", "audience"} <= set(raw) <= _SHARE_CREATE_KEYS:
        raise _bad(what)
    _check_id(raw["version_id"], "version id")
    if raw["audience"] not in _AUDIENCES:
        raise _bad(what)
    days = raw.get("expires_in_days")
    if days is not None and (isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 3650):
        raise _bad(what)
    entry, title = raw.get("entry"), raw.get("title")
    if entry is not None:
        entry = normalise_name(entry)
    if title is not None:
        if not isinstance(title, str) or len(title) > 240:
            raise _bad(what)
        _check_text(title)
    return ShareCreatePayload(raw["version_id"], raw["audience"], _allow_list(raw.get("allow_groups"), what),
                              _allow_list(raw.get("allow_emails"), what), days, entry, title)


def _share_ref(kind: str, extra: frozenset[str] = frozenset()):
    def validate(raw: dict, limits: BridgeLimits) -> ShareRefPayload:
        if not isinstance(raw, dict) or set(raw) != {"share_id"} | extra or not isinstance(raw["share_id"], str) \
                or not _SHARE_ID.match(raw["share_id"]) or (extra and raw.get("confirm") is not True):
            raise _bad(kind)
        return ShareRefPayload(raw["share_id"])
    return validate


_BOTH = frozenset({"agent", "user"})
register_intent_kind(KIND_SHARE_CREATE, validate_share_create_payload, actors=_BOTH, requires_artifact=True)
register_intent_kind(KIND_SHARE_APPROVE, _share_ref(KIND_SHARE_APPROVE, frozenset({"confirm"})), actors=_BOTH,
                     requires_artifact=True)
register_intent_kind(KIND_SHARE_REVOKE, _share_ref(KIND_SHARE_REVOKE), actors=_BOTH, requires_artifact=True)
register_intent_kind(KIND_SHARE_EVENTS, _share_ref(KIND_SHARE_EVENTS), actors=_BOTH, requires_artifact=True)
register_intent_kind(KIND_CREATE, validate_create_payload, actors=_BOTH, requires_artifact=False)
register_intent_kind(KIND_PUBLISH, validate_publish_payload, actors=_BOTH, requires_artifact=True)
register_intent_kind(KIND_DELETE, validate_delete_payload, actors=_BOTH, requires_artifact=True)


def _validate_empty(kind: str):
    def validate(raw: dict, limits: BridgeLimits) -> dict:
        if raw != {}:
            raise _bad(kind)
        return {}
    return validate


# Reviewing agent pushes is a human decision: users only, and never on the agent tool menu.
register_intent_kind(KIND_LIBRARY_ACCEPT, _validate_empty(KIND_LIBRARY_ACCEPT), actors=frozenset({"user"}),
                     requires_artifact=True)
register_intent_kind(KIND_LIBRARY_DISCARD, _validate_empty(KIND_LIBRARY_DISCARD), actors=frozenset({"user"}),
                     requires_artifact=True)


_FOLDER_ID = re.compile(r"fld_[0-9a-f]{24}\Z")


def _folder_payload(kind: str, keys: frozenset[str]):
    """A payload of exactly ``keys`` (``folder_id``, ``name``, ``share_id``), checked; the plain dict."""
    def validate(raw: dict, limits: BridgeLimits) -> dict:
        if not isinstance(raw, dict) or set(raw) != keys:
            raise _bad(kind)
        if "folder_id" in keys and (not isinstance(raw["folder_id"], str) or not _FOLDER_ID.match(raw["folder_id"])):
            raise _bad(kind)
        if "name" in keys and (not isinstance(raw["name"], str) or not 1 <= len(raw["name"]) <= 400):
            raise _bad(kind)
        if "share_id" in keys and (not isinstance(raw["share_id"], str) or not _SHARE_ID.match(raw["share_id"])):
            raise _bad(kind)
        return dict(raw)
    return validate


# The library is a person's own filing: users only, never on the agent tool menu.
_USER = frozenset({"user"})
for _kind, _keys, _needs_artifact in (
        (KIND_LIBRARY_FOLDER_CREATE, frozenset({"name"}), False),
        (KIND_LIBRARY_FOLDER_RENAME, frozenset({"folder_id", "name"}), False),
        (KIND_LIBRARY_FOLDER_DELETE, frozenset({"folder_id"}), False),
        (KIND_LIBRARY_FILE, frozenset({"folder_id"}), True),
        (KIND_LIBRARY_UNFILE, frozenset({"folder_id"}), True),
        (KIND_LIBRARY_CLAIM, frozenset({"share_id"}), False)):
    register_intent_kind(_kind, _folder_payload(_kind, _keys), actors=_USER, requires_artifact=_needs_artifact)


# ----------------------------------------------------------------------------- the member directory

def _finite(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def load_directory(rt: MemberRuntime) -> dict[str, dict[str, Any]] | None:
    """``{user_id: {"status", "groups"}}`` from a fresh ``member-directory.json`` for ``rt.group``, else None.

    None (the callers fail closed: ``directory_stale``) when the file is missing, unreadable, for another group, has a
    non-finite ``updated``, or ``updated`` is more than ``FEED_MAX_AGE`` old or in the future. Blocking."""
    doc = read_json_beneath(rt.outbox, [MEMBER_DIRECTORY], max_bytes=DIRECTORY_MAX_BYTES)
    if doc is None or doc.get("group") != rt.group or not isinstance(doc.get("users"), dict):
        return None
    updated, now = doc.get("updated"), rt.clock()
    if not _finite(updated) or abs(now - updated) > FEED_MAX_AGE:
        return None
    out: dict[str, dict[str, Any]] = {}
    for user_id, entry in doc["users"].items():
        if not isinstance(user_id, str) or not isinstance(entry, dict):
            continue
        groups = entry.get("groups")
        out[user_id] = {"status": entry.get("status"),
                        "groups": groups if isinstance(groups, list) else [rt.group]}
        if isinstance(entry.get("display_name"), str):
            out[user_id]["display_name"] = entry["display_name"]
        if isinstance(entry.get("email"), str):
            out[user_id]["email"] = entry["email"]
        if "removed_since" in entry:
            out[user_id]["removed_since"] = entry["removed_since"]
    return out


# ----------------------------------------------------------------------------- the member check

@dataclass(frozen=True)
class _Who:
    principal: str
    owner_key: str
    rt: MemberRuntime
    actor: Actor
    limits_row: Mapping[str, Any]


def _rejected(reason: str) -> dict:
    return {"result": "rejected", "reason": reason}


def _actor_json(actor: Actor) -> dict:
    return {"kind": actor.kind, "id": actor.id}


def _principal_hint(ctx: IntentContext) -> str | None:
    from .artifact_members import member_principal
    raw = ctx.actor.id if ctx.actor.kind == "user" else (ctx.agent or {}).get("owner")
    try:
        return member_principal(raw)
    except ValueError:
        return None


async def _member(ctx: IntentContext, action: str) -> tuple[_Who | None, dict | None]:
    """The acting member, or the rejection to return. A refusal for a derivable member is audited."""
    from .artifact_members import MemberStore, member_for_actor, member_owner_key
    rt = ctx.extras.get("members")
    if not isinstance(rt, MemberRuntime) or not rt.enabled:
        return None, _rejected("members_disabled")
    directory = await asyncio.to_thread(load_directory, rt)
    if directory is None:
        return None, _rejected("directory_stale")
    if ctx.actor.kind == "agent":
        actor_map = {"kind": "agent", "agent_id": ctx.actor.id}
        agents = {ctx.actor.id: ctx.agent} if ctx.agent is not None else {}
    else:
        actor_map, agents = {"kind": "user", "user_id": ctx.actor.id}, {}
    check = member_for_actor(actor_map, agents=agents, group=rt.group, directory=directory)
    store = MemberStore(ctx.conn, defaults=rt.defaults)
    if not check.ok or check.principal is None:
        hint = _principal_hint(ctx)
        if hint is not None:
            await store.record(hint, action, actor=_actor_json(ctx.actor), outcome="refused", code=check.reason)
        return None, _rejected(check.reason or "refused")
    usage = await store.usage(check.principal)
    return _Who(check.principal, member_owner_key(check.principal), rt, ctx.actor, usage["limits"]), None


async def _limits(ctx: IntentContext, who: _Who):
    from .artifact_members import MemberStore
    usage = await MemberStore(ctx.conn, defaults=who.rt.defaults).usage(who.principal)
    from .artifact_members import MemberLimits
    return MemberLimits(**usage["limits"])


async def _own(conn: Any, artifact_id: str | None, principal: str) -> Mapping[str, Any] | None:
    """The artifact if ``principal`` created it and it is not purged, else None (``not_found``, non-disclosing)."""
    from .artifact_member_quota import _NOT_PURGED
    if artifact_id is None:
        return None
    return await conn.fetchrow(
        "SELECT a.artifact_id, a.lifecycle, a.current_version_id FROM artifacts a "
        f"WHERE a.artifact_id=$1 AND a.created_by=$2 AND a.lifecycle <> 'tombstoned' AND {_NOT_PURGED}",
        artifact_id, principal)


async def _conclude(ctx: IntentContext, who: _Who, action: str, out: dict, *, artifact_id: str | None) -> dict:
    """Audit the outcome, stamp ``last_used_at`` and hand the principal to the after-hook."""
    from .artifact_members import MemberStore
    ok = out.get("result") in ("created", "deleted", "accepted", "renamed", "filed", "unfiled", "claimed")
    await MemberStore(ctx.conn).record(
        who.principal, action, actor=_actor_json(who.actor), artifact_id=artifact_id,
        outcome="ok" if ok else "refused", code=None if ok else str(out.get("reason") or out.get("result")))
    await ctx.conn.execute("UPDATE artifact_member_usage SET last_used_at=now() WHERE member_principal=$1",
                           who.principal)
    return {**out, "_principal": who.principal}


# ----------------------------------------------------------------------------- create

async def handle_create(ctx: IntentContext, intent: Intent) -> dict:
    from .artifact_library import (
        PENDING_CAP,
        PENDING_CAP_CODE,
        PENDING_CAP_MESSAGE,
        count_pending_on,
    )
    from .artifact_member_quota import QuotaExceeded, check_new_artifact
    who, refusal = await _member(ctx, "artifact_create")
    if refusal is not None:
        return refusal
    payload: CreatePayload = intent.payload
    key = intent.idempotency_key or intent.ticket
    digest = hashlib.sha256(json.dumps([payload.title, payload.filename, payload.profile]).encode()).hexdigest()
    async with ctx.conn.transaction():
        prior = await ctx.conn.fetchrow(          # keyed by (member, key): the same whichever actor sends it
            "SELECT request_sha256, artifact_id FROM artifact_member_creates"
            " WHERE member_principal=$1 AND idempotency_key=$2", who.principal, key)
        if prior is not None:                     # after the admission check above, so a removed member gets no replay
            out = ({"result": "created", "artifact_id": prior["artifact_id"]} if prior["request_sha256"] == digest
                   else _rejected("idempotency_conflict"))
            return await _conclude(ctx, who, "artifact_create", out, artifact_id=out.get("artifact_id"))
        try:
            await check_new_artifact(ctx.conn, who.principal, await _limits(ctx, who))
        except QuotaExceeded as exc:
            out = _rejected(exc.code)
        else:
            agent_push = who.actor.kind == "agent"
            if agent_push:             # the usage row is already locked by the quota check: counting here is race-free
                if await count_pending_on(ctx.conn, who.principal) >= PENDING_CAP:
                    out = {**_rejected(PENDING_CAP_CODE), "message": PENDING_CAP_MESSAGE}
                    return await _conclude(ctx, who, "artifact_create", out, artifact_id=None)
            made = await ctx.store.register_logical(original_filename=payload.filename, artifact_type="other",
                                                    created_by=who.principal)
            aid = made["artifact_id"]
            if agent_push:
                await ctx.conn.execute("UPDATE artifacts SET intake='pending' WHERE artifact_id=$1", aid)
            await ctx.conn.execute(
                "UPDATE artifacts SET display_name=$2, payload=$3::jsonb WHERE artifact_id=$1", aid, payload.title,
                json.dumps({"profile": payload.profile}))
            await ctx.conn.execute(
                "INSERT INTO artifact_member_creates (member_principal, idempotency_key, request_sha256, artifact_id)"
                " VALUES ($1,$2,$3,$4)", who.principal, key, digest, aid)
            out = {"result": "created", "artifact_id": aid}
        return await _conclude(ctx, who, "artifact_create", out, artifact_id=out.get("artifact_id"))


# ----------------------------------------------------------------------------- publish

def _media_type(name: str) -> str | None:
    return MEDIA_TYPES.get(os.path.splitext(name)[1].lower())


async def handle_publish(ctx: IntentContext, intent: Intent) -> dict:
    who, refusal = await _member(ctx, "publish")
    if refusal is not None:
        return refusal
    out = await _publish(ctx, intent, who)
    return await _conclude(ctx, who, "publish", out, artifact_id=intent.artifact_id)


async def _publish(ctx: IntentContext, intent: Intent, who: _Who) -> dict:
    from .artifact_member_quota import QuotaExceeded, check_publish, settle
    from .artifacts import ArtifactConflict, ArtifactError
    from .collab_intents import REASON_MAX, REASON_UPLOAD_NOT_FOUND, UploadRefused
    aid, payload = intent.artifact_id, intent.payload
    row = await _own(ctx.conn, aid, who.principal)
    if row is None:
        return _rejected("not_found")
    if row["lifecycle"] != "active":
        return _rejected("deleted")
    names = [f.name for f in payload.files]
    if any(_media_type(n) is None for n in names):
        return _rejected("media_type_not_allowed")
    if not any(n.lower().endswith(ENTRY_EXTENSIONS) for n in names):
        return _rejected("no_html_or_pdf")
    declared: list[dict] = []
    unverified = 0
    for file in payload.files:                                  # uploads are looked up under THIS member's key only
        verified = await ctx.grants.verified_upload(who.owner_key, file.upload_id)
        if verified is not None:
            if verified["artifact_id"] != aid or verified["name"] != file.name:
                return _rejected(REASON_UPLOAD_NOT_FOUND)
            declared.append({"size_bytes": int(verified["size_bytes"])})
            continue
        meta = await asyncio.to_thread(read_staged_meta, who.rt.outbox, who.owner_key, file.upload_id, ctx.limits)
        if meta is None or meta.artifact_id != aid or meta.name != file.name:
            return _rejected(REASON_UPLOAD_NOT_FOUND)
        declared.append({"size_bytes": meta.size_bytes})
        unverified += meta.size_bytes
    limits = await _limits(ctx, who)
    async with ctx.conn.transaction():                          # stage 1: the declared sizes against the limits
        try:
            await check_publish(ctx.conn, who.principal, limits, new_bytes=unverified, output_manifest=declared)
        except QuotaExceeded as exc:
            return _rejected(exc.code)
    entries = []
    for file in payload.files:                                  # stage 2: verified bytes; durable rows even if stage 3 fails
        try:
            entries.append(await ctx.assemble(who.owner_key, file.upload_id, artifact_id=aid, name=file.name,
                                              charge=False, media_type=_media_type(file.name)))
        except UploadRefused as exc:
            return _rejected(exc.reason)
    async with ctx.conn.transaction():                          # the assembled blobs are durable: count them now, so a
        await settle(ctx.conn, who.principal)                   # conflict or refusal below cannot leave bytes_used stale
    key = "member-publish:" + (intent.idempotency_key or intent.ticket)
    provenance = {"via": "member_mcp" if who.actor.kind == "agent" else "member_web",
                  "actor": _actor_json(who.actor), "message": payload.message}
    if payload.entry is not None:
        provenance["entry"] = payload.entry
    async with ctx.conn.transaction():                          # stage 3: recheck, commit and settle together
        row = await ctx.conn.fetchrow("SELECT lifecycle FROM artifacts WHERE artifact_id=$1 FOR UPDATE", aid)
        if row is None or row["lifecycle"] != "active":
            return _rejected("deleted")
        try:
            await check_publish(ctx.conn, who.principal, limits, new_bytes=0, output_manifest=entries)
            version = await ctx.store.commit_version(
                aid, base_version_id=payload.base_version_id, source_manifest_blob=None, output_manifest=entries,
                provenance=provenance, created_by=who.principal, idempotency_key=key)
        except ArtifactConflict as exc:
            return {"result": "conflict", "head_version_id": exc.current_version_id}
        except QuotaExceeded as exc:
            return _rejected(exc.code)
        except ArtifactError as exc:
            return _rejected(str(exc)[:REASON_MAX] or "commit refused")
        await settle(ctx.conn, who.principal)
    return {"result": "created", "version_id": version["version_id"], "version_number": int(version["version_number"])}


# ----------------------------------------------------------------------------- delete

async def revoke_share(conn: Any, rt: MemberRuntime, share_id: str, *, actor: str) -> None:
    """Revoke one share: its served state first (the next lookup finds nothing), then the row, then the bundle."""
    await revoke_share_at(conn, rt.shares_root, share_id, actor=actor)


async def revoke_share_at(conn: Any, root: Path | None, share_id: str, *, actor: str) -> None:
    """``revoke_share`` for a caller that has the share bundle directory but no ``MemberRuntime`` (the dashboard)."""
    from .artifact_shares import ShareStore
    from .share_bundle import remove_bundle, remove_state
    store = ShareStore(conn)
    seen = await store.get(share_id)
    if seen is None:
        return
    if root is not None and seen.get("token_hash"):
        await asyncio.to_thread(remove_state, root, seen["token_hash"])
    await store.mark_revoked(share_id, actor=actor)
    latest = await store.get(share_id) or {}
    if root is not None:
        if latest.get("token_hash") and latest["token_hash"] != seen.get("token_hash"):
            await asyncio.to_thread(remove_state, root, latest["token_hash"])
        await asyncio.to_thread(remove_bundle, root, share_id)


def _remove_member_staging(rt: MemberRuntime, owner_key: str, artifact_id: str, limits: BridgeLimits) -> int:
    """Remove this member's staged uploads of ``artifact_id``; blocking. Dirs without a readable meta are left to the
    TTL sweep."""
    listing = list_beneath(rt.outbox, ["staging", owner_key], max_entries=100_000)
    removed = 0
    for upload_id, st in listing or []:
        if not stat.S_ISDIR(st.st_mode):
            continue
        meta = read_staged_meta(rt.outbox, owner_key, upload_id, limits)
        if meta is not None and meta.artifact_id == artifact_id:
            try:
                removed += bool(remove_tree_beneath(rt.outbox, ["staging", owner_key, upload_id],
                                                    max_entries=tree_entry_cap(limits)))
            except (BridgeError, OSError):
                log.warning("member delete: staging %s could not be removed", upload_id)
    return removed


async def handle_delete(ctx: IntentContext, intent: Intent) -> dict:
    who, refusal = await _member(ctx, "artifact_delete")
    if refusal is not None:
        return refusal
    return await _delete_own(ctx, who, intent.artifact_id, "artifact_delete")


async def delete_member_rows(conn: Any, store: Any, shares_root: Path | None, principal: str, aid: str | None) -> bool:
    """The member delete, in one transaction: retract the artifact, revoke every share of it and settle the quota.
    False when ``principal`` does not own a live ``aid`` (nothing is changed).  The one implementation behind a
    member's delete/discard intents and the dashboard's discard for a key mapped to that member."""
    from .artifact_member_quota import settle
    from .artifact_shares import ShareStore
    async with conn.transaction():
        row = await _own(conn, aid, principal)
        if row is None:
            return False
        if row["lifecycle"] == "active":
            await store.record_retraction(aid, reason="member delete")
        for share in await ShareStore(conn).list_for_artifact(aid):     # any creator, any audience
            if share["state"] != "revoked":
                await revoke_share_at(conn, shares_root, share["share_id"], actor=f"member-delete:{principal}")
        await settle(conn, principal)
    return True


async def _delete_own(ctx: IntentContext, who: _Who, aid: str | None, action: str) -> dict:
    if not await delete_member_rows(ctx.conn, ctx.store, who.rt.shares_root, who.principal, aid):
        return await _conclude(ctx, who, action, _rejected("not_found"), artifact_id=aid)
    await asyncio.to_thread(_remove_member_staging, who.rt, who.owner_key, aid, ctx.limits)
    return await _conclude(ctx, who, action, {"result": "deleted"}, artifact_id=aid)


# ----------------------------------------------------------------------------- library: accept or discard a push

async def handle_library_accept(ctx: IntentContext, intent: Intent) -> dict:
    from .artifact_library import LibraryError, accept_pending_on
    who, refusal = await _member(ctx, "library_accept")
    if refusal is not None:
        return refusal
    try:
        async with ctx.conn.transaction():
            await accept_pending_on(ctx.conn, who.principal, intent.artifact_id)
    except LibraryError:
        out = _rejected("not_found")
    else:
        out = {"result": "accepted"}
    return await _conclude(ctx, who, "library_accept", out, artifact_id=intent.artifact_id)


async def handle_library_discard(ctx: IntentContext, intent: Intent) -> dict:
    from .artifact_library import LibraryError, require_pending_on
    who, refusal = await _member(ctx, "library_discard")
    if refusal is not None:
        return refusal
    try:
        await require_pending_on(ctx.conn, who.principal, intent.artifact_id)
    except LibraryError:
        out = _rejected("not_found")
        return await _conclude(ctx, who, "library_discard", out, artifact_id=intent.artifact_id)
    return await _delete_own(ctx, who, intent.artifact_id, "library_discard")


# ----------------------------------------------------------------------------- library: folders, filing, claims

class _OneConn:
    """Lets ``LibraryStore`` run on the worker's own connection: ``acquire()`` hands that connection back."""
    def __init__(self, conn: Any):
        self._conn = conn

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)

    def acquire(self):
        import contextlib

        @contextlib.asynccontextmanager
        async def held():
            yield self._conn
        return held()


def _library_handler(action: str, run: Callable[..., Any], result: str):
    """A user-only library intent: ``run(store, member_id, principal, intent)`` returns extra result fields.
    Results carry no ids (the view has them). A ``LibraryError`` becomes a rejection with its code (``not_found`` for another member's folder, as for a missing one)."""
    async def handle(ctx: IntentContext, intent: Intent) -> dict:
        from .artifact_library import LibraryError, LibraryStore
        who, refusal = await _member(ctx, action)
        if refusal is not None:
            return refusal
        store = LibraryStore(_OneConn(ctx.conn))
        try:
            async with ctx.conn.transaction():
                extra = await run(store, who.principal, intent)
        except LibraryError as exc:
            out = _rejected(exc.code)
        else:
            out = {"result": result, **extra}
        return await _conclude(ctx, who, action, out, artifact_id=intent.artifact_id)
    return handle


def _folder_json(folder: Any) -> dict:
    return {"folder_id": folder.folder_id, "name": folder.name, "item_count": folder.item_count}


async def _folder_create(store: Any, principal: str, intent: Intent) -> dict:
    """Create-or-get by name, so a replayed intent (or a double click) yields the same folder.  This deliberately
    differs from ``LibraryStore.create_folder`` (409 ``duplicate_name``): replays must be idempotent.  The member's lane is
    serial, so looking first cannot race; an insert that failed would abort the transaction."""
    name = intent.payload["name"]
    if not any(f.name.lower() == name.strip().lower() for f in await store.list_folders(principal)):
        await store.create_folder(principal, name)
    return {}


async def _folder_rename(store: Any, principal: str, intent: Intent) -> dict:
    await store.rename_folder(principal, intent.payload["folder_id"], intent.payload["name"])
    return {}


async def _folder_delete(store: Any, principal: str, intent: Intent) -> dict:
    await store.delete_folder(principal, intent.payload["folder_id"])
    return {}


async def _file(store: Any, principal: str, intent: Intent) -> dict:
    await store.file_artifact(principal, intent.payload["folder_id"], intent.artifact_id)
    return {}


async def _unfile(store: Any, principal: str, intent: Intent) -> dict:
    await store.unfile_artifact(principal, intent.payload["folder_id"], intent.artifact_id)
    return {}


async def _claim(store: Any, principal: str, intent: Intent) -> dict:
    await store.record_claim(principal.removeprefix("member:"), intent.payload["share_id"])
    return {}


LIBRARY_HANDLERS = (
    (KIND_LIBRARY_FOLDER_CREATE, _library_handler("library_folder_create", _folder_create, "created")),
    (KIND_LIBRARY_FOLDER_RENAME, _library_handler("library_folder_rename", _folder_rename, "renamed")),
    (KIND_LIBRARY_FOLDER_DELETE, _library_handler("library_folder_delete", _folder_delete, "deleted")),
    (KIND_LIBRARY_FILE, _library_handler("library_file", _file, "filed")),
    (KIND_LIBRARY_UNFILE, _library_handler("library_unfile", _unfile, "unfiled")),
    (KIND_LIBRARY_CLAIM, _library_handler("library_claim", _claim, "claimed")),
)


# ----------------------------------------------------------------------------- the member view

def _iso(value: object) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else None


def _json(value: object) -> Any:
    return json.loads(value) if isinstance(value, str) else value


async def _member_identity(principal: str, rt: MemberRuntime | None) -> tuple[str, frozenset[str]]:
    """The member's (email, groups) from the worker's member directory; empty when it cannot be known (no runtime,
    stale or missing directory, unknown member). Nothing is guessed."""
    from .artifact_members import member_principal
    if rt is None:
        return "", frozenset()
    directory = await asyncio.to_thread(load_directory, rt)
    for user_id, entry in (directory or {}).items():
        try:
            if member_principal(user_id) != principal:
                continue
        except ValueError:
            continue
        email, groups = entry.get("email"), entry.get("groups")
        return (email if isinstance(email, str) else ""), frozenset(g for g in groups or [] if isinstance(g, str))
    return "", frozenset()


def _item_json(item: Any) -> dict:
    return {"artifact_id": item.artifact_id, "title": item.title, "sharer": item.sharer,
            "shared_at": _iso(item.shared_at), "source": item.source, "status": item.status,
            "expires_at": _iso(item.expires_at), "share_id": item.share_id, "folder_ids": list(item.folder_ids),
            "artifact_type": item.artifact_type, "size_bytes": item.size_bytes}


LIBRARY_CONTENT_DIR = "library-content"          # <mirror>/library-content/<sha256>: the shared, gated blob pool
LIB_MAX_FILES = 200
LIB_MAX_FILE_BYTES = 25 * 1024 * 1024
LIB_MAX_ITEM_BYTES = 100 * 1024 * 1024
POOL_GC_MIN_AGE = 600.0
POOL_TICK_FILL_BYTES = 200 * 1024 * 1024          # new pool bytes fetched per publish tick; the rest waits for the next
POOL_FILE_MODE, POOL_DIR_MODE = 0o640, 0o750      # with the collab-public group (``MemberRuntime.secret_gid``)
POOL_FILE_MODE_OPEN, POOL_DIR_MODE_OPEN = 0o644, 0o755   # no group known: world-readable, the mirror's own modes
_SHA_HEX = re.compile(r"[0-9a-f]{64}\Z")


def safe_content_path(path: object) -> bool:
    """A relative, normalised path: no empty, dot or dotdot component, no NUL, no backslash, not absolute."""
    return (isinstance(path, str) and 0 < len(path) <= 512 and "\0" not in path and "\\" not in path
            and all(part not in ("", ".", "..") for part in path.split("/")))


def _touch_pooled(root: Path, sha: str, perms: tuple[int, int, int | None] | None = None) -> bool:
    """Blocking: refresh the mtime of a pooled blob (no-follow) so the GC age guard protects it, and bring its mode and
    group (and the directory's) to ``perms``; False when absent."""
    try:
        dfd = os.open(root / LIBRARY_CONTENT_DIR, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return False
    try:
        fd = os.open(sha, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dfd)
    except OSError:
        os.close(dfd)
        return False
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return False
        os.utime(fd)
        if perms is not None and perms[2] is not None:
            try:
                os.fchown(fd, -1, perms[2])
                os.fchmod(fd, perms[0])
                os.fchown(dfd, -1, perms[2])
                os.fchmod(dfd, perms[1])
            except OSError as exc:
                log.warning("library content pool: mode/group not repaired (%s)", type(exc).__name__)
        return True
    except OSError:
        return False
    finally:
        os.close(fd)
        os.close(dfd)


def _pool_perms(rt: MemberRuntime | None) -> tuple[int, int, int | None]:
    """(file mode, dir mode, gid): 0640/0750 in the collab-public group, else 0644/0755 with a warning."""
    gid = getattr(rt, "secret_gid", None)
    if gid is None:
        log.warning("library content pool: no collab-public group (members.secrets_gid / HERDR_COLLAB_CERT_UID) is "
                    "known; pool files are world-readable (0644)")
        return POOL_FILE_MODE_OPEN, POOL_DIR_MODE_OPEN, None
    return POOL_FILE_MODE, POOL_DIR_MODE, gid


def _fetch_into_pool(rustfs: Any, root: Path, entry: dict, perms: tuple[int, int, int | None] | None = None,
                     budget: list[int] | None = None) -> bool:
    """Blocking: put one blob into the pool unless it is there (digest verified while streaming; never replaced).

    A blob already pooled is touched (the GC age guard then protects it) and costs nothing; a new one is refused,
    to be fetched next tick, once ``budget[0]`` (bytes left this tick) cannot cover it."""
    sha, size = entry["sha256"], entry["size"]
    if _touch_pooled(root, sha, perms):
        return True
    if budget is not None and budget[0] < size:
        return False
    key = entry["storage_key"]
    if isinstance(key, str) and _SHA_HEX.match(key):
        key = f"sha256/{key[:2]}/{key[2:4]}/{key}"
    fmode, dmode, gid = perms or (POOL_FILE_MODE_OPEN, POOL_DIR_MODE_OPEN, None)
    try:
        try:
            ok = stream_new_beneath(root, [LIBRARY_CONTENT_DIR], sha, rustfs.iter_get(key, sha), max_bytes=size,
                                    mode=fmode, dir_mode=dmode, gid=gid)
        except PermissionError:      # the group cannot be set (not root, not a member of it): readable, but say so
            log.warning("library content pool: cannot set the collab-public group; storing world-readable")
            ok = stream_new_beneath(root, [LIBRARY_CONTENT_DIR], sha, rustfs.iter_get(key, sha), max_bytes=size,
                                    mode=POOL_FILE_MODE_OPEN, dir_mode=POOL_DIR_MODE_OPEN)
        if ok and budget is not None:
            budget[0] -= size
        return ok
    except Exception as exc:                                   # noqa: BLE001 - one blob never stops the view
        log.warning("library content blob not pooled (%s)", type(exc).__name__)
        return False


async def _attach_files(conn: Any, items: list[dict], rustfs: Any, root: Path, rt: MemberRuntime | None = None,
                        budget: list[int] | None = None) -> set[str]:
    """Give every active or pending item its files and pool their bytes; the pooled shas.

    Owned, ACL-assigned and pending items list the head version; a row with a ``share_id`` (opened, or assigned by a
    lab share) the version its share pinned.

    Anything else (revoked, expired, no head, over a cap, bytes unavailable) gets ``files: []``; an item over a cap is
    also marked ``too_large`` (the owner is told, nobody else).  Access ends when the view is rewritten."""
    for item in items:
        item["files"] = []
    wanted = [i["artifact_id"] for i in items if i["status"] in ("active", "pending")]
    if not wanted or rustfs is None:
        return set()
    rows = await conn.fetch(
        "SELECT a.artifact_id, v.output_manifest FROM artifacts a JOIN artifact_versions v"
        " ON v.version_id=a.current_version_id WHERE a.artifact_id = ANY($1::text[])"
        " AND a.lifecycle NOT IN ('retracted','tombstoned') AND v.state <> 'retracted'", wanted)
    heads = {r["artifact_id"]: _json(r["output_manifest"]) for r in rows}
    claimed = [i["share_id"] for i in items if i["status"] in ("active", "pending") and i.get("share_id")]
    pinned: dict[str, Any] = {}
    if claimed:
        for r in await conn.fetch(
                "SELECT s.share_id, v.output_manifest FROM artifact_shares s"
                " JOIN artifact_versions v ON v.version_id=s.version_id AND v.artifact_id=s.artifact_id"
                " JOIN artifacts a ON a.artifact_id=s.artifact_id WHERE s.share_id = ANY($1::text[])"
                " AND a.lifecycle NOT IN ('retracted','tombstoned') AND v.state <> 'retracted'", claimed):
            pinned[r["share_id"]] = _json(r["output_manifest"])
    perms = _pool_perms(rt)
    pooled: set[str] = set()
    for item in items:
        # a row that came through a share (opened, or assigned by a lab share) shows only the version the share pinned
        manifest = pinned.get(item["share_id"]) if item.get("share_id") else heads.get(item["artifact_id"])
        if item["status"] not in ("active", "pending") or not isinstance(manifest, list):
            continue
        entries: list[dict] = []
        for m in manifest:
            if not isinstance(m, dict):
                continue
            sha = m.get("sha256") or m.get("blob_sha256")
            size = m.get("size_bytes")
            if (safe_content_path(m.get("name")) and isinstance(sha, str) and _SHA_HEX.match(sha)
                    and isinstance(size, int) and not isinstance(size, bool) and size >= 0):
                entries.append({"path": m["name"], "sha256": sha, "size": size,
                                "content_type": m.get("media_type") if isinstance(m.get("media_type"), str) else None,
                                "storage_key": m.get("storage_key") or m.get("blob_sha256") or sha})
        if (len(entries) > LIB_MAX_FILES or any(e["size"] > LIB_MAX_FILE_BYTES for e in entries)
                or sum(e["size"] for e in entries) > LIB_MAX_ITEM_BYTES):
            item["too_large"] = True
            continue
        ok = True
        for e in entries:
            ok = ok and await asyncio.to_thread(_fetch_into_pool, rustfs, root, e, perms, budget)
        if not ok:
            continue
        item["files"] = [{"path": e["path"], "sha256": e["sha256"], "size": e["size"],
                          "content_type": e["content_type"] or mimetypes.guess_type(e["path"])[0]
                          or "application/octet-stream"} for e in entries]
        pooled.update(e["sha256"] for e in entries)
    return pooled


def sweep_pool(root: Path, referenced: set[str], now: float) -> int:
    """Blocking: delete pool blobs no view references, skipping files younger than ``POOL_GC_MIN_AGE``."""
    try:
        listing = list_beneath(root, [LIBRARY_CONTENT_DIR], max_entries=1_000_000)
    except BridgeError:
        return 0
    removed = 0
    for name, st in listing or []:
        if stat.S_ISREG(st.st_mode) and name not in referenced and now - st.st_mtime >= POOL_GC_MIN_AGE:
            removed += bool(unlink_beneath(root, [LIBRARY_CONTENT_DIR], name))
    return removed


async def _library_view(conn: Any, principal: str, rt: MemberRuntime | None, rustfs: Any = None,
                        root: Path | None = None, budget: list[int] | None = None) -> tuple[dict, set[str]]:
    """``({items, folders}, pooled shas)``: the feed (own pending included) newest first, at most ``LIBRARY_VIEW_MAX``."""
    from .artifact_library import DEFAULT_SOURCES, MAX_PAGE, LibraryStore
    store = LibraryStore(_OneConn(conn))
    email, groups = await _member_identity(principal, rt)   # an empty/stale directory fails closed: fewer items
    member_id = principal.removeprefix("member:")
    sources = frozenset({*DEFAULT_SOURCES, "pending"})
    items: list[dict] = []
    while len(items) < LIBRARY_VIEW_MAX:
        page = await store.feed(member_id, email, groups, sources=sources,
                                limit=min(MAX_PAGE, LIBRARY_VIEW_MAX - len(items)), offset=len(items))
        items.extend(_item_json(i) for i in page.items)
        if page.next_offset is None or not page.items:
            break
    # the original filename (searchable by the agent tool), only while the item is readable: same rule as the SQL search
    names = {r["artifact_id"]: r["original_filename"] or "" for r in await conn.fetch(
        "SELECT artifact_id, original_filename FROM artifacts WHERE artifact_id = ANY($1::text[])",
        [i["artifact_id"] for i in items])}
    for item in items:
        item["filename"] = names.get(item["artifact_id"], "") if item["status"] in ("active", "pending") else ""
    pooled = await _attach_files(conn, items, rustfs, root, rt, budget) if root is not None else set()
    return {"items": items, "folders": [_folder_json(f) for f in await store.list_folders(principal)]}, pooled


async def write_member_view(conn: Any, principal: str, root: Path, *, defaults: Mapping[str, Any] | None = None,
                            rt: MemberRuntime | None = None, rustfs: Any = None,
                            budget: list[int] | None = None) -> set[str]:
    """Write ``<root>/member-views/<member_owner_key>.json``: the member's usage, artifacts, shared-with-me and links."""
    from .artifact_member_quota import _NOT_PURGED
    from .artifact_members import MemberStore, member_owner_key
    usage = await MemberStore(conn, defaults=defaults).usage(principal)
    artifacts = await conn.fetch(
        "SELECT a.artifact_id, COALESCE(a.display_name, a.original_filename) AS title, a.lifecycle,"
        " a.current_version_id, v.version_number AS head_number,"
        " COALESCE((SELECT sum(b.size_bytes) FROM artifact_blobs b WHERE b.scope=a.artifact_id), 0)::bigint AS bytes"
        " FROM artifacts a LEFT JOIN artifact_versions v ON v.version_id=a.current_version_id"
        f" WHERE a.created_by=$1 AND a.lifecycle <> 'tombstoned' AND {_NOT_PURGED}"      # a tombstone is not a reader's
        " ORDER BY a.created_at DESC, a.artifact_id LIMIT $2",
        principal, VIEW_MAX_ARTIFACTS)
    versions: dict[str, list[dict]] = {}
    for v in await conn.fetch(
            "SELECT version_id, artifact_id, version_number, committed_at, provenance, parent_version_ids"
            " FROM artifact_versions WHERE artifact_id = ANY($1::text[]) ORDER BY artifact_id, version_number DESC",
            [a["artifact_id"] for a in artifacts]):
        mine = versions.setdefault(v["artifact_id"], [])
        if len(mine) < VIEW_MAX_VERSIONS:
            mine.append({"version_id": v["version_id"], "version_number": v["version_number"],
                         "created_at": _iso(v["committed_at"]),
                         "message": (_json(v["provenance"]) or {}).get("message"),
                         "parent_version_ids": list(v["parent_version_ids"] or [])})
    shared = await conn.fetch(
        "SELECT a.artifact_id, COALESCE(a.display_name, a.original_filename) AS title, a.current_version_id,"
        " v.version_number AS head_number, acl.permissions FROM artifact_acl acl"
        " JOIN artifacts a ON a.artifact_id=acl.artifact_id"
        " LEFT JOIN artifact_versions v ON v.version_id=a.current_version_id"
        " WHERE acl.principal_id=$1 AND (acl.expires_at IS NULL OR acl.expires_at > now())"
        " AND a.created_by IS DISTINCT FROM $1 AND a.lifecycle='active'"
        " AND (acl.permissions ? 'read' OR acl.permissions ? 'admin' OR acl.permissions ? 'share')"
        " ORDER BY a.artifact_id LIMIT $2", principal, VIEW_MAX_SHARED)
    links = await conn.fetch(      # the member's own links, and every link on an artifact it owns (it may revoke those)
        "SELECT s.share_id, s.artifact_id, s.version_id, s.audience, s.state, s.expires_at, s.allow_emails,"
        " s.allow_groups, s.created_at, (s.created_by = $1) AS mine FROM artifact_shares s"
        " WHERE (s.created_by=$1 OR s.artifact_id = ANY($3::text[]))"
        " AND NOT EXISTS (SELECT 1 FROM artifacts t WHERE t.artifact_id=s.artifact_id AND t.lifecycle='tombstoned')"
        " ORDER BY s.created_at DESC, s.share_id LIMIT $2",
        principal, VIEW_MAX_LINKS, [a["artifact_id"] for a in artifacts])
    root = Path(root)
    from .collab_bridge_files import DIR_MODE
    ensure_dir(root, mode=DIR_MODE, public_mode=True)
    library, pooled = await _library_view(conn, principal, rt, rustfs if rustfs is not None else getattr(rt, "rustfs", None), root, budget)
    doc = {
        "principal": principal, "updated": __import__("time").time(),
        "usage": {"bytes_used": usage["bytes_used"], "bytes_retracted": usage["bytes_retracted"],
                  "artifact_count": usage["artifact_count"], "version_count": usage["version_count"],
                  "limits": usage["limits"], "sharing_disabled": usage["sharing_disabled"]},
        "artifacts": [{"artifact_id": a["artifact_id"], "title": a["title"], "lifecycle": a["lifecycle"],
                       "head_version_id": a["current_version_id"], "head_version_number": a["head_number"],
                       "bytes": a["bytes"], "versions": versions.get(a["artifact_id"], [])} for a in artifacts],
        "shared_with_me": [{"artifact_id": s["artifact_id"], "title": s["title"],
                            "head_version_id": s["current_version_id"], "head_version_number": s["head_number"],
                            "permissions": _json(s["permissions"])} for s in shared],
        "links": [{"share_id": s["share_id"], "artifact_id": s["artifact_id"], "version_id": s["version_id"],
                   "audience": s["audience"], "state": s["state"], "expires_at": _iso(s["expires_at"]),
                   "recipients": [*(s["allow_emails"] or []), *(s["allow_groups"] or [])],
                   "created_at": _iso(s["created_at"]), "mine": s["mine"]} for s in links],
        "library": library,
    }

    def write() -> None:
        atomic_write_json(root / MEMBER_VIEWS_DIR, member_owner_key(principal) + ".json", doc)
    await asyncio.to_thread(write)
    return pooled


async def _after(ctx: IntentContext, intent: Intent, out: dict, private: dict) -> None:
    """Rewrite the acting member's view once the outcome is durable."""
    principal, rt = private.get("_principal"), ctx.extras.get("members")
    if isinstance(principal, str) and isinstance(rt, MemberRuntime):
        await write_member_view(ctx.conn, principal, rt.mirror_root, defaults=rt.defaults, rt=rt,
                                rustfs=getattr(ctx.store, "rustfs", None))


# ----------------------------------------------------------------------------- secret results

def take_secret(outbox: Path, ticket: str, principal: str) -> str | None:
    """Read and delete the share URL the worker left for ``ticket``; None when there is none for ``principal``.

    Read once: the reader that wins the unlink gets the URL, any other (or later) reader gets None.  A file that names
    another principal is left alone.  Blocking; collab-public calls it, the worker never does."""
    if not isinstance(ticket, str) or not re.match(TICKET_RE, ticket):
        return None
    name = ticket + ".json"
    doc = read_json_beneath(outbox, [SECRETS_DIR, name], max_bytes=SECRET_MAX_BYTES)
    if doc is None or doc.get("ticket") != ticket or doc.get("principal") != principal:
        return None
    url = doc.get("url")
    if not isinstance(url, str) or not url or not unlink_beneath(outbox, [SECRETS_DIR], name):
        return None
    return url


def sweep_secrets(rt: MemberRuntime) -> int:
    """Delete the secret files that stayed unread for ``SECRET_TTL`` seconds (or cannot be read); returns the count.
    Blocking."""
    try:
        listing = list_beneath(rt.outbox, [SECRETS_DIR], max_entries=100_000)
    except BridgeError:
        return 0
    removed = 0
    now = rt.clock()
    for name, st in listing or []:
        if not stat.S_ISREG(st.st_mode):
            continue
        doc = read_json_beneath(rt.outbox, [SECRETS_DIR, name], max_bytes=SECRET_MAX_BYTES)
        at = doc.get("at") if doc else None
        if _finite(at) and not now - at > SECRET_TTL:
            continue
        removed += bool(unlink_beneath(rt.outbox, [SECRETS_DIR], name))
    return removed


# ----------------------------------------------------------------------------- publisher, sweep, staging

async def sweep_disabled_members(conn: Any, rt: MemberRuntime) -> int:
    """Revoke the pending and live shares of members that are disabled, removed from the group, or have sharing off.

    Sharing switched off by the operator is positive evidence by itself and is acted on even without a directory.
    The directory-based reasons need a fresh directory: a stale or missing file, a member who is absent from it and
    one marked ``unknown`` prove nothing, so nothing is revoked for them."""
    from .artifact_members import positively_out
    from .artifact_shares import _may_share
    directory = await asyncio.to_thread(load_directory, rt)
    rows = await conn.fetch(
        "SELECT s.share_id, s.artifact_id, s.created_by, a.created_by AS owner,"
        " COALESCE(u.sharing_disabled, false) AS off FROM artifact_shares s"
        " LEFT JOIN artifact_member_usage u ON u.member_principal=s.created_by"
        " LEFT JOIN artifacts a ON a.artifact_id=s.artifact_id"
        " WHERE s.state IN ('pending','live') AND s.created_by LIKE 'member:%'")

    def out(principal: str | None) -> bool:                       # absent or "unknown" proves nothing
        if directory is None or not isinstance(principal, str) or not principal.startswith("member:"):
            return False
        return positively_out(directory.get(principal[len("member:"):]), rt.group, now=rt.clock(),
                              grace=rt.removed_grace)

    revoked = 0
    for r in rows:
        creator, owner = r["created_by"], r["owner"]
        # the creator's own standing, the artifact owner's (a collaborator's link must not outlive its owner), and
        # the creator's grant (a link authorised by a share grant must not outlive the grant): all positive evidence
        gone = r["off"] or out(creator) or (owner != creator and out(owner))
        if not gone and owner != creator:
            gone = not await _may_share(conn, r["artifact_id"], creator)
        if not gone:
            continue
        try:
            await revoke_share(conn, rt, r["share_id"], actor="member-disabled")
            revoked += 1
        except Exception as exc:
            log.error("member sweep: share %s not revoked (%s)", r["share_id"], type(exc).__name__)
    return revoked


async def _lab_share_recipients(conn: Any, rt: MemberRuntime) -> set[str]:
    """Approved directory members that match a live lab share (email or group): they need a view before any intent."""
    from .artifact_members import member_principal
    shares = await conn.fetch("SELECT allow_emails, allow_groups FROM artifact_shares WHERE state='live'"
                              " AND audience='lab' AND (expires_at IS NULL OR expires_at > now())")
    if not shares:
        return set()
    emails = {e.strip().lower() for r in shares for e in r["allow_emails"] or [] if isinstance(e, str)}
    groups = {g for r in shares for g in r["allow_groups"] or [] if isinstance(g, str)}
    directory = await asyncio.to_thread(load_directory, rt) or {}       # a stale directory fails closed: no extra views
    found: set[str] = set()
    for user_id, entry in directory.items():
        if entry.get("status") != "approved":
            continue
        email = entry.get("email")
        held = {g for g in entry.get("groups") or [] if isinstance(g, str)}
        if (isinstance(email, str) and email.strip().lower() in emails) or held & groups:
            try:
                found.add(member_principal(user_id))
            except ValueError:
                continue
    return found


async def publish_member_views(conn: Any, rt: MemberRuntime) -> int:
    """Rewrite the view of every known member, then run ``sweep_disabled_members``; returns the views written."""
    rows = await conn.fetch(
        "SELECT created_by AS p FROM artifacts WHERE created_by LIKE 'member:%'"
        " UNION SELECT member_principal FROM artifact_member_usage"
        " UNION SELECT principal_id FROM artifact_acl WHERE principal_id LIKE 'member:%'"
        " UNION SELECT 'member:' || member_id FROM artifact_share_claims"      # a member who only opens links
        " UNION SELECT owner FROM artifact_library_folders WHERE owner LIKE 'member:%'")
    principals = {r["p"] for r in rows} | await _lab_share_recipients(conn, rt)
    written, failed = 0, False
    referenced: set[str] = set()
    budget = [POOL_TICK_FILL_BYTES]
    for principal in sorted(principals):
        try:
            referenced |= await write_member_view(conn, principal, rt.mirror_root, defaults=rt.defaults, rt=rt,
                                                   budget=budget)
            written += 1
        except Exception as exc:
            failed = True
            log.error("member view not written (%s)", type(exc).__name__)
    if rt.rustfs is not None and failed:      # a view that failed still lists its blobs: collect nothing then
        log.warning("library content pool: garbage collection skipped, a member view was not written this tick")
    elif rt.rustfs is not None:
        await asyncio.to_thread(sweep_pool, rt.mirror_root, referenced, time.time())
    await sweep_disabled_members(conn, rt)
    await asyncio.to_thread(sweep_secrets, rt)
    return written


def staging_keep(rt: MemberRuntime) -> Callable[[str, Any], Any]:
    """The ``keep`` predicate for ``m_`` staging dirs: dropped only for a member the directory positively lists as
    disabled or removed from the group.

    A stale or missing directory, an unlisted member and an ``unknown`` one prove nothing (True: only the TTL
    applies)."""
    cache: dict[str, Any] = {"at": None, "stat": None, "by_key": None}   # one read and hashing pass per sweep

    def stamp() -> tuple[int, int] | None:
        try:
            st = os.stat(rt.outbox / MEMBER_DIRECTORY)
        except OSError:
            return None
        return st.st_mtime_ns, st.st_size

    async def keep(owner_key: str, _meta: Any) -> bool:
        from .artifact_members import member_owner_key, member_principal, positively_out
        if not rt.enabled:
            return True
        now = rt.clock()
        seen = await asyncio.to_thread(stamp)
        if cache["at"] is None or seen != cache["stat"] or not 0 <= now - cache["at"] < 5.0:
            directory = await asyncio.to_thread(load_directory, rt)
            by_key: dict[str, dict[str, Any]] | None = None
            if directory is not None:
                by_key = {}
                for user_id, entry in directory.items():
                    try:
                        by_key[member_owner_key(member_principal(user_id))] = entry
                    except ValueError:
                        continue
            cache["at"], cache["stat"], cache["by_key"] = now, seen, by_key
        by_key = cache["by_key"]
        if by_key is None:
            return True
        return not positively_out(by_key.get(owner_key), rt.group, now=now, grace=rt.removed_grace)
    return keep


def _lane(intent: Intent, agent: Mapping[str, Any] | None) -> str:
    """One lane per member, whichever of its actors sends the intent."""
    owner = agent.get("owner") if agent else None
    return "member:" + (owner if isinstance(owner, str) and owner else intent.actor.id)


def register_member_intents(applier: CollabIntentApplier) -> None:
    """Attach the handlers and the ``m_`` staging rule; the runtime comes from the applier's ``context_extras``."""
    for kind, handler, outcomes in ((KIND_CREATE, handle_create, "created"), (KIND_PUBLISH, handle_publish, "created"),
                                    (KIND_DELETE, handle_delete, "deleted"),
                                    (KIND_LIBRARY_ACCEPT, handle_library_accept, "accepted"),
                                    (KIND_LIBRARY_DISCARD, handle_library_discard, "deleted")):
        # No ``replay_outcomes``: the applier would replay a stored result before the handler runs, i.e. before the
        # directory/admission check. The handlers are idempotent themselves, behind ``_member``.
        applier.register(kind, handler, lane_key=_lane, after=_after)
    from . import (
        artifact_member_shares as shares,  # the worker only: collab-public never imports the handlers
    )
    for kind, handler in LIBRARY_HANDLERS:
        applier.register(kind, handler, lane_key=_lane, after=_after)
    for kind, handler in ((KIND_SHARE_CREATE, shares.handle_share_create), (KIND_SHARE_APPROVE, shares.handle_share_approve),
                          (KIND_SHARE_REVOKE, shares.handle_share_revoke), (KIND_SHARE_EVENTS, shares.handle_share_events)):
        applier.register(kind, handler, lane_key=_lane, after=_after)
    rt = applier.extras.get("members")
    if isinstance(rt, MemberRuntime):
        applier.register_staging_owner("m_", staging_keep(rt))

"""Herdr Collaboration Network (HCN) P1: authenticated rooms, scoped agents, audit.

Spec: docs/specs/herdr-collaboration-network-2026-10-21/ (see audit.md, ADR-0007).

Design in one paragraph: every state change is an immutable ``collab`` event
persisted through the existing durable sink (``fabric_events``) and folded into
in-memory state. Events never enter ``ControlPlaneRepo``'s event streams, so the
global SSE feed, ``/activity`` and the generic events endpoint cannot leak them.
Identity comes only from the credential on the request; authority for an agent
is ``grant ∩ owner's room role ∩ agent ceiling`` evaluated on every request,
and non-members always get 404 so room ids cannot be enumerated. Peer message
text is untrusted data: it is stored and returned, never executed, never routed
to a terminal.
"""
from __future__ import annotations

import bisect
import copy
import hashlib
import ipaddress
import json
import math
import os
import re
import secrets
import threading
import time
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.parse import urlparse

from ..assistant_packages import ManifestError, agent_package_record
from . import collab_models, ids
from .redact import redact_text

SCHEMA_VERSION = 1
ENTITY = "collab"
STREAM_ID = "log"
ORG_ID = "default"  # single-organization deployment (spec: no federation in v1)

# "task" is the human right to give an agent work; the agent-side rights are "instruct"
# (give work to other agents) and "execute" (receive work). The room owner approves them per agent.
ROLE_ACTIONS = {
    "owner": frozenset({"read", "post", "invite", "grant_agent", "task", "instruct", "execute", "spawn_helpers"}),
    "contributor": frozenset({"read", "post", "grant_agent", "task", "instruct", "execute", "spawn_helpers"}),
    "viewer": frozenset({"read"}),
    "guest": frozenset({"read"}),
}
INVITABLE_ROLES = frozenset({"contributor", "viewer", "guest"})
MAX_USERS = 2000                 # people ever registered (approval mode)
MAX_PENDING_USERS = 200          # waiting for approval: beyond this, new sign-ins are refused until some are decided
USER_STATUSES = frozenset({"pending", "approved", "denied", "disabled", "kicked"})
USER_ACTIONS = {"approve": "approved", "deny": "denied", "disable": "disabled", "enable": "approved", "kick": "kicked"}


class ApprovalPolicy:
    """Who may use the network once approval mode is on: anyone with a pool account may sign in and
    ask; nothing works until an approver or the operator approves. ``auto_groups`` (e.g. local.dev) are
    approved on sight; ``approver_groups`` (e.g. Administrators) are too, and may approve others."""

    def __init__(self, auto_groups=(), approver_groups=()):
        self.auto_groups = frozenset(auto_groups)
        self.approver_groups = frozenset(approver_groups)
AGENT_ACTIONS = frozenset({"read", "post", "instruct", "execute", "spawn_helpers"})  # an agent can never invite/grant/administer
PROJECT_ROLES = frozenset({"manager", "contributor"})

MAX_TEXT = 8000
MAX_BODY = 64 * 1024
_PANE_LINK = re.compile(r"pane:[a-z0-9][a-z0-9._-]{0,62}:[A-Za-z0-9][A-Za-z0-9:_.-]{0,39}")
_KEY_RE = re.compile(r"[A-Za-z0-9._:\-]{1,128}\Z")
_MESSAGE_FIELDS = frozenset({"text", "idempotency_key", "in_reply_to"})
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")   # C0, DEL and C1 (8-bit CSI)
_NAME_BAD = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")
_EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s]{2,}$")
INVITE_TTL = 7 * 24 * 3600.0
MAX_OPEN_INVITES = 50
_FEED_TYPES = frozenset({"message", "member_added", "member_removed",
                         "agent_granted", "agent_revoked", "agent_grant_revoked", "agent_renamed",
                         "task_created", "task_updated",
                         "helper_requested", "helper_claimed", "helper_started", "helper_finished",
                         "helper_cancelled", "helper_expired"})
MAX_TASK_TEXT = 4000
TASK_TTL = 24 * 3600.0           # a task nobody finished in a day is expired
MAX_TASK_DEPTH = 3               # human -> agent -> agent -> agent
TASK_RATE = 20                   # new tasks per creator per 10 minutes
MAX_OPEN_TASKS_PER_AGENT = 10
MAX_APPROVAL_PROPOSALS_PER_OWNER = 100
APPROVAL_PROPOSAL_RATE = 20       # per requester per 10 minutes
MAX_APPROVAL_SUMMARY = 500
MAX_APPROVAL_DETAILS = 8 * 1024
MAX_ASSISTANT_PACKAGES_PER_OWNER_ROOM = 100
ASSISTANT_PACKAGE_RATE = 20       # immutable definitions per owner per 10 minutes
ASSISTANT_RUNTIME_ATTEST_TTL = 300.0
ASSISTANT_RUNTIME_LEASE = 120.0
MAX_ASSISTANT_RUNTIME_HOSTS = 50
MAX_ASSISTANT_RUNTIMES_PER_OWNER = 20
ASSISTANT_RUNTIME_RATE = 12       # activation requests per owner per hour
_RUNTIME_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_RUNTIME_VERSION = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\Z")
_RUNTIME_LIVE = frozenset({"claimed", "starting", "ready"})
_RUNTIME_TERMINAL = frozenset({"failed", "cancelled"})
MAX_PENDING_REQUESTS = 120       # open access requests per room (one family of agents fits)
SPAWN_FAMILIES = ("claude-code", "codex", "pi", "agent")   # what the host runner knows how to start
PROFILE_LIMIT_DEFAULTS = {"live_per_agent": 3, "depth": 2, "per_hour": 20}   # a room profile may only lower these
MAX_PROFILE_HOSTS = 50
MAX_PROFILE_SERVICES = 20
MAX_PROFILE_NAMES = 50
_PROFILE_HOST = re.compile(r"(?:[a-z0-9.-]+|\[[0-9A-Fa-f:.]{2,45}\]):\d{1,5}", re.ASCII)   # name, IPv4 or [IPv6]
_EGRESS_HOST = re.compile(r"[a-z0-9]([a-z0-9.-]*[a-z0-9])?", re.ASCII)
_NUMERIC_LABEL = re.compile(r"[0-9]+|0x[0-9a-f]*", re.ASCII)   # a last label like this makes a name an IPv4 address
_PROFILE_SECRET_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", re.ASCII)   # a NAME that refers to a host file, never a value
_PROFILE_SERVICE_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,31}", re.ASCII)
_PROFILE_HEADER = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,63}", re.ASCII)
_PROFILE_BAD_HEADERS = frozenset({"host", "content-length", "transfer-encoding", "connection", "upgrade",
                                  "proxy-authorization", "te", "trailer"})
_PROFILE_KEYS = frozenset({"allowed_families", "allowed_models", "tools_allowed", "permanent_hosts", "services",
                           "ssh_secret_names", "spend_tokens_per_day", "limits"})
# The model endpoint each harness family must reach; merged into the host allowlist of rooms that allow that family.
FAMILY_ENDPOINTS: dict[str, list[str]] = {
    "claude-code": ["api.anthropic.com:443"],
    "codex": ["api.openai.com:443"],
    "pi": ["llm.metabolomics.us:443"],
}
MAX_OPEN_EGRESS = 60             # open host-access requests per room
EGRESS_OPEN_TTL = 86400.0        # an unanswered request stops counting (and deduping) after this
EGRESS_DENY_WINDOW = 3600.0      # a denied host cannot be re-requested for this long
EGRESS_DEFAULT_TTL = 86400.0
EGRESS_KEEP = 7 * 86400.0        # decided rows stay listed this long past their decision / expiry, then are pruned
EGRESS_MAX_TTL = 30 * 86400.0
MAX_SPAWN_AGENTS = 20            # agents one fleet start may place on the owner's hosts
MAX_OPEN_SPAWNS = 3              # requests waiting for, or held by, the runner
SPAWN_STALE = 900.0              # a claimed start with no result after this long is shown as stalled
FLEET_ONLINE = 60.0              # a host the runner has not mentioned for this long is offline
_FLEET_HOST = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")
MAX_JOIN_AGENTS = 50             # agents one join token / bulk call may create
JOIN_TOKEN_MAX_TTL = 24 * 3600.0
REQUEST_TTL = 7 * 24 * 3600.0
_HANDLE_BAD = re.compile(r"[^a-z0-9\-]")
# Helper agents: an agent asks for a short-lived helper; the host runner starts it in a sandbox.
HELPER_ROLES = ("reviewer", "debugger", "worker", "vision", "successor")
HELPER_TTL = 7200.0              # a helper (and its credential) lives at most this long
HELPERS_GLOBAL_LIVE = 24         # live helpers across every room and person
HELPERS_PER_OWNER_LIVE = 6       # live helpers of one person's agents, across rooms (so one person cannot hold them all)
HELPER_CLAIM_WINDOW = 600.0      # a request no runner claimed in this long expires and frees its slot
MAX_SUCCESSOR_CHAIN = 5          # successors of successors, in a row
MAX_HELPER_TASK = 8000
MAX_HELPER_RESULT = 32000
_HELPER_LIVE = frozenset({"requested", "claimed", "running"})
_HELPER_FIELDS = frozenset({"room_id", "role", "task", "family", "model"})   # helper_spawn's wait is MCP-side
_HELPER_IGNORED = frozenset({"parent", "parent_agent_id"})   # the parent is the caller, never what the body says
HELPER_ERRORS = frozenset({"failed", "timeout", "cancelled", "sandbox-error", "profile-changed"})   # audited codes
_HELPER_BAD = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\u2028\u2029]")   # all control chars but \t \n


class CollabError(Exception):
    def __init__(self, status: int, message: str, reason: str | None = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.reason = reason          # a stable machine-readable code, for callers that branch on it


# control_plane.collab.summary_fix: the operator's standing room and requester agent that start a
# text-only helper when a summary's owning agent cannot be woken (summary_fix.helper_fallback).
SUMMARY_FIX_KEYS = frozenset({"room_id", "requester_agent_id"})
_SUMMARY_FIX_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_SUMMARY_FIX_OP = re.compile(r"^summary-fix-[0-9a-f]{40}-[0-9]{1,2}$")
MAX_SUMMARY_FIX_LIST = 50


def summary_fix_config(value: Any) -> dict | None:
    """Validate ``control_plane.collab.summary_fix``; None when absent. Ids only, never a credential."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("control_plane.collab.summary_fix must be a mapping with room_id and requester_agent_id")
    unknown = sorted(str(k) for k in set(value) - SUMMARY_FIX_KEYS)
    if unknown:
        raise ValueError("control_plane.collab.summary_fix: unknown keys: " + ", ".join(unknown))
    for key in sorted(SUMMARY_FIX_KEYS):
        v = value.get(key)
        if not isinstance(v, str) or not _SUMMARY_FIX_ID.match(v):
            raise ValueError(f"control_plane.collab.summary_fix.{key} must be a collaboration id")
    return {k: value[k] for k in SUMMARY_FIX_KEYS}


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class UserAuthenticator(Protocol):
    """Resolves a human bearer token to a user id. Replace with OIDC/MFA later."""

    def verify(self, token: str) -> tuple[str, float | None] | None: ...
    def user_for(self, token: str) -> str | None: ...
    def exists(self, user_id: str) -> bool: ...


class StaticTokenAuthenticator:
    """Pilot authenticator: user_id -> sha256(token), compared in constant time."""

    def __init__(self, users: dict[str, str]):
        self._users = dict(users)
        self._disabled: set[str] = set()

    def verify(self, token: str) -> tuple[str, float | None] | None:
        uid = self.user_for(token)
        return (uid, None) if uid else None

    def user_for(self, token: str) -> str | None:
        digest = _digest(token)
        found = None
        for uid, expected in self._users.items():  # no early exit: constant time
            if secrets.compare_digest(digest, expected):
                found = uid
        return None if found in self._disabled else found

    def exists(self, user_id: str) -> bool:
        return user_id in self._users and user_id not in self._disabled

    def disable(self, user_id: str) -> None:
        self._disabled.add(user_id)


class Principal:
    __slots__ = ("kind", "id", "owner", "digest", "exp", "status", "approver", "admin")

    def __init__(self, kind: str, id: str, owner: str, digest: str | None = None,
                 exp: float | None = None):
        self.kind, self.id, self.owner, self.digest, self.exp = kind, id, owner, digest, exp
        self.status = "approved"       # approval mode sets this for people; agents are judged by their owner
        self.approver = False          # may approve or deny people waiting
        self.admin = False             # the operator on the tailnet-only listener: may also disable and remove

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.id}"

    @property
    def is_agent(self) -> bool:
        return self.kind == "agent"


class Stream:
    """Returned by dispatch for an SSE response; the HTTP layer pumps it."""

    def __init__(self, service: CollabService, principal: Principal, room_id: str, cursor: int):
        self.service, self.principal, self.room_id, self.cursor = service, principal, room_id, cursor


class FileEventLog:
    """Append-only JSON-lines event log for a collab-only deployment (no database).

    Same sink contract as the durable Postgres sink: ``record_required`` must have
    reached stable storage before it returns, or raise. A failed write is rolled back
    (truncated to the previous end) so a torn line can never sit in front of the next
    event; if even the rollback fails the log refuses all further writes (fail closed).
    """

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._broken = False
        self._fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)

    def load(self) -> list[dict]:
        rows = []
        with open(self.path, "rb") as fh:
            raw = fh.read()
        lines = raw.split(b"\n")
        tail = lines.pop()          # text after the last newline: only ever a torn write
        for line in lines:
            if line.strip():
                rows.append(json.loads(line))
        if tail.strip():
            # An unacknowledged partial append from a crash: it was never applied, so drop it.
            os.truncate(self.path, len(raw) - len(tail))
        return rows

    def record_required(self, event: dict) -> None:
        data = (json.dumps(event, separators=(",", ":")) + "\n").encode("utf-8")
        with self._lock:
            if self._broken:
                raise OSError("collaboration log is in a failed state; restart required")
            start = os.fstat(self._fd).st_size
            try:
                view = memoryview(data)
                while view:
                    view = view[os.write(self._fd, view):]
                os.fsync(self._fd)
            except BaseException:
                try:
                    os.ftruncate(self._fd, start)
                    os.fsync(self._fd)
                except OSError:
                    self._broken = True
                raise

    record = record_required


class CollabService:
    def __init__(self, authenticator: UserAuthenticator, *, sink: Any = None,
                 now=None, enrollment_ttl: float = 600.0, credential_ttl: float = 12 * 3600.0,
                 stream_poll_seconds: float = 1.0, message_limit: int = 60,
                 max_streams_per_principal: int = 5, public_config: dict | None = None,
                 remote_credential_ttl: float = 30 * 24 * 3600.0,
                 max_agents_per_user: int = 60, max_projects_per_user: int = 20,
                 max_rooms_per_project: int = 50, max_members_per_room: int = 100,
                 max_log_events: int = 200_000, guest_ttl: float = 7 * 24 * 3600.0,
                 default_grant_ttl: float = 30 * 24 * 3600.0,
                 approval: ApprovalPolicy | None = None, summary_fix: dict | None = None):
        self.auth = authenticator
        try:
            self.summary_fix = summary_fix_config(summary_fix)
        except ValueError as exc:
            raise CollabError(400, str(exc)) from None
        self.approval = approval
        self._users: dict[str, dict] = {}       # approval mode: user id -> {status, since, decided_by, decided_at, reason}
        self._sink = sink
        self._now = now or time.time
        self.enrollment_ttl = enrollment_ttl
        self.credential_ttl = credential_ttl
        self.remote_credential_ttl = remote_credential_ttl  # agents that connect over HTTP MCP
        self.stream_poll_seconds = stream_poll_seconds
        self.message_limit = message_limit
        self.max_agents_per_user = max_agents_per_user
        self.max_projects_per_user = max_projects_per_user
        self.max_rooms_per_project = max_rooms_per_project
        self.max_members_per_room = max_members_per_room
        self.max_log_events = max_log_events
        self.guest_ttl = guest_ttl
        self.default_grant_ttl = default_grant_ttl
        self.max_streams = max_streams_per_principal
        self._lock = threading.RLock()
        self._cv = threading.Condition(self._lock)    # notified on every commit: waiters never poll
        self._log: list[dict] = []
        self._seq = 0
        self._projects: dict[str, dict] = {}
        self._rooms: dict[str, dict] = {}
        self._agents: dict[str, dict] = {}
        self._by_cred: dict[str, str] = {}      # credential digest -> agent id
        self._agent_projects: dict[str, set] = defaultdict(set)  # projects an agent was ever granted into
        self._pending: dict[str, dict] = {}     # enrollment code digest -> record
        self._idem: dict[tuple, tuple] = {}     # (principal, room, key) -> (fingerprint, seq)
        self._hits: dict[str, list[float]] = defaultdict(list)
        self._streams: dict[str, int] = defaultdict(int)
        self._denied_at: dict[tuple, float] = {}
        self.public_config = dict(public_config or {})
        self._email_user: dict[str, str] = {}   # email -> user id (linked at sign-in)
        self._user_email: dict[str, str] = {}
        self._invites: dict[str, dict] = {}
        self._tasks: dict[str, dict] = {}
        self._tasks_by_target: dict[str, list[str]] = defaultdict(list)
        self._approval_proposals: dict[str, dict] = {}
        self._approval_proposal_idem: dict[tuple[str, str, str], tuple[str, str]] = {}
        self._assistant_packages: dict[tuple[str, str, str], dict] = {}
        self._assistant_package_versions: dict[tuple[str, str, str, str], str] = {}
        self._assistant_runtime_hosts: dict[str, dict] = {}
        self._assistant_runtimes: dict[str, dict] = {}
        self._assistant_runtime_idem: dict[tuple[str, str, str], tuple[str, str]] = {}
        self._requests: dict[str, dict] = {}                 # open agent-access requests
        # Indexes kept only by _apply (so hydrate rebuilds them): they replace whole-state scans.
        self._agents_by_owner: dict[str, set] = defaultdict(set)
        self._pending_by_owner: dict[str, set] = defaultdict(set)
        self._requests_by_room: dict[str, set] = defaultdict(set)
        self._requests_by_agent: dict[str, set] = defaultdict(set)
        self._rooms_by_agent: dict[str, set] = defaultdict(set)
        self._housekept = 0.0
        self._room_seq: dict[str, int] = {}                  # room id -> sequence of its latest change
        self._egress: dict[str, dict] = {}                   # request id -> host-access request
        self._fleet_flags: dict[str, bool] = {}              # host -> may it receive automatic agents (default yes)
        self._fleet_seen: dict[str, dict] = {}               # host -> what the host runner last reported (memory only)
        self._spawns: dict[str, dict] = {}                   # spawn id -> request
        self._helpers: dict[str, dict] = {}                  # helper id -> helper request
        self._helper_by_agent: dict[str, dict] = {}          # helper's own agent id -> its request
        self._helper_text: dict[str, dict] = {}              # helper id -> task text waiting for its request event
        self._helper_by_op: dict[str, dict] = {}             # summary-fix operation id -> its helper (replay safety)
        self._helpers_paused = False                         # the panic switch
        self._catalogue: list[dict] = []                     # normalized model catalogue the runner posted
        self._helper_minted: set[str] = set()                # agents a claim minted (a helper agent, linked or not)
        self._state_waiters: dict[str, int] = defaultdict(int)
        self._joins: dict[str, dict] = {}                    # token digest -> record
        self._joins_by_id: dict[str, str] = {}               # join id -> digest
        self._by_room: dict[str, list] = defaultdict(list)   # room id -> feed events, in sequence order
        self._msg_room: dict[str, str] = {}                  # message event id -> room id
        self.stats = defaultdict(int)

    # ------------------------------------------------------------ plumbing
    @property
    def durable(self) -> bool:
        return callable(getattr(self._sink, "record_required", None))

    def _iso(self, t: float | None = None) -> str:
        return datetime.fromtimestamp(self._now() if t is None else t, UTC).isoformat()

    def _commit(self, event_type: str, payload: dict) -> dict:
        """Persist first, then apply: a failed write never changes state."""
        with self._lock:
            if len(self._log) >= self.max_log_events:
                self.stats["log_full"] += 1
                raise CollabError(503, "collaboration log is full; ask the operator")
            now = self._iso()
            payload = {"org_id": ORG_ID, "correlation_id": payload.pop("correlation_id", None)
                       or ids.new_id("collab"), "created_at": now, **payload}
            ev = {"event_id": ids.new_id("event"), "schema_version": SCHEMA_VERSION,
                  "source_timestamp": now, "ingest_timestamp": now,
                  "entity_type": ENTITY, "entity_id": STREAM_ID,
                  "mission_id": None, "session_id": None,
                  "sequence": self._seq + 1, "event_type": event_type, "payload": payload}
            if self._sink is not None:
                try:
                    (self._sink.record_required if self.durable else self._sink.record)(ev)
                except Exception as exc:
                    self.stats["persist_failures"] += 1
                    raise CollabError(503, "collaboration log unavailable") from exc
            self._seq += 1
            self._log.append(ev)
            self._apply(ev)
            self._touch_rooms(payload, ev["sequence"])
            self._cv.notify_all()
            return ev

    def _touch_rooms(self, p: dict, seq: int) -> None:
        """Remember which rooms an event changed, so a room's long-poll wakes only for its own news."""
        if p.get("room_notify") is False:
            return
        rooms = set()
        if isinstance(p.get("room_id"), str):
            rooms.add(p["room_id"])
        rooms.update(r for r in (p.get("room_ids") or ()) if isinstance(r, str))
        if isinstance(p.get("agent_id"), str):
            rooms |= self._rooms_by_agent.get(p["agent_id"], set())
        for r in rooms:
            self._room_seq[r] = seq

    @property
    def seq(self) -> int:
        return self._seq

    def wait_for_change(self, seen_seq: int, timeout: float) -> None:
        """Block until something is committed after `seen_seq` (or the timeout): replaces polling."""
        with self._cv:
            if self._seq == seen_seq and timeout > 0:
                self._cv.wait(timeout)

    def hydrate(self, rows: list[dict]) -> int:
        """Rebuild state from persisted collab events (restart / durable reload)."""
        with self._lock:
            parsed = []
            for r in rows:
                if r.get("entity_type") != ENTITY:
                    continue
                p = r.get("payload") or {}
                if isinstance(p, str):
                    try:
                        p = json.loads(p)
                    except ValueError:
                        continue
                parsed.append({**r, "payload": p, "sequence": int(r["sequence"])})
            parsed.sort(key=lambda e: e["sequence"])
            expected = self._seq + 1
            for ev in parsed:
                # A gap or duplicate means the log is not the one this service wrote
                # (e.g. started empty over existing rows). Replaying it could drop a
                # revocation, so fail closed instead of guessing.
                if ev["sequence"] != expected:
                    raise ValueError(
                        f"collab log is not contiguous: expected sequence {expected}, "
                        f"found {ev['sequence']}")
                expected += 1
                self._seq = ev["sequence"]
                self._log.append(ev)
                self._apply(ev)
            # helper_task is always committed right before its helper_requested (two commits under one lock):
            # a text still waiting here belongs to a request whose second commit failed. Drop it.
            self._helper_text.clear()
            known = {ev["payload"].get(k) for ev in parsed
                     for k in ("actor_user_id", "subject_user_id", "user_id", "owner_user_id")}
            mark = getattr(self.auth, "mark_known", None)
            if mark:
                mark({u for u in known if isinstance(u, str)})
            if self.approval is not None:
                # Everyone who was already using the network when approval was switched on keeps working;
                # only people who show up from now on wait. (Recomputed on every start, never logged.)
                for u in known:
                    if isinstance(u, str) and u not in self._users:
                        self._users[u] = {"status": "approved", "since": None, "decided_by": "grandfathered",
                                          "decided_at": None, "reason": None}
            return len(parsed)

    # --------------------------------------------------------- state fold
    def _apply(self, ev: dict) -> None:
        t, p, seq = ev["event_type"], ev["payload"], ev["sequence"]
        if t == "project_created":
            self._projects[p["project_id"]] = {"name": p["name"], "members": {p["actor_user_id"]: "manager"}}
        elif t == "project_member_added":
            self._projects[p["project_id"]]["members"][p["subject_user_id"]] = p["role"]
        elif t == "room_created":
            self._rooms[p["room_id"]] = {
                "id": p["room_id"], "project_id": p["project_id"], "name": p["name"], "created_by": p["actor_user_id"],
                "members": {p["actor_user_id"]: {"role": "owner", "expires_at": None, "joined_seq": seq}},
                "grants": {}}
        elif t == "room_profile_set":
            self._rooms[p["room_id"]]["profile"] = p["profile"]
        elif t == "user_registered":
            self._users[p["user_id"]] = {"status": p["status"], "since": p["at"], "decided_by": p.get("decided_by"),
                                         "decided_at": p["at"] if p["status"] != "pending" else None, "reason": None}
        elif t == "user_decided":
            u = self._users.setdefault(p["user_id"], {"since": p["at"]})
            u.update(status=p["status"], decided_by=p["decided_by"], decided_at=p["at"], reason=p.get("reason"))
        elif t == "member_added":
            self._rooms[p["room_id"]]["members"][p["subject_user_id"]] = {
                "role": p["role"], "expires_at": p.get("expires_at"), "joined_seq": seq}
        elif t == "member_removed":
            room = self._rooms[p["room_id"]]
            room["members"].pop(p["subject_user_id"], None)
            # Agents of a removed member lose their grants for good: re-adding the person
            # must not silently revive access they never re-approved.
            for aid in [a for a in room["grants"] if self._agents.get(a, {}).get("owner") == p["subject_user_id"]]:
                room["grants"].pop(aid, None)
                self._rooms_by_agent[aid].discard(p["room_id"])
            for rid in self._requests_by_room.get(p["room_id"], ()):   # their pending requests die with their membership
                rq = self._requests[rid]
                if rq["requester"] == p["subject_user_id"]:
                    rq["open"] = False
        elif t == "enrollment_started":
            self._pending[p["code_digest"]] = {
                "owner": p["actor_user_id"], "device_id": p["device_id"],
                "harness_type": p["harness_type"], "expires_at": p["expires_at_epoch"], "used": False}
            self._pending_by_owner[p["actor_user_id"]].add(p["code_digest"])
        elif t == "agent_enrolled":
            if p["code_digest"] in self._pending:
                self._pending[p["code_digest"]]["used"] = True
            self._agents[p["agent_id"]] = {
                "owner": p["owner_user_id"], "device_id": p["device_id"], "harness_type": p["harness_type"],
                "instance_id": p.get("instance_id"), "status": "active",
                "name": p.get("name") or _default_handle(p["harness_type"], p["agent_id"]),
                "parent": p.get("parent_agent_id"), "join": p.get("join_digest"),
                "cred_digest": p["secret_digest"], "expires_at": p["expires_at_epoch"]}
            self._by_cred[p["secret_digest"]] = p["agent_id"]
            self._agents_by_owner[p["owner_user_id"]].add(p["agent_id"])
            if p.get("action") == "helper.agent.create":
                self._helper_minted.add(p["agent_id"])
            if p.get("join_digest") in self._joins:
                self._joins[p["join_digest"]]["uses"] += 1
        elif t == "join_token_created":
            self._joins[p["join_digest"]] = {
                "id": p["join_id"], "owner": p["actor_user_id"], "family": p["harness_type"],
                "prefix": p["name_prefix"], "max_uses": p["max_uses"], "uses": 0,
                "expires_at": p["expires_at_epoch"], "room_id": p.get("room_id"),
                "actions": list(p.get("actions") or []), "revoked": False, "created_at": p["created_at"],
                "parent": p.get("parent_agent_id")}
            self._joins_by_id[p["join_id"]] = p["join_digest"]
        elif t == "join_token_revoked":
            if p["join_id"] in self._joins_by_id:
                self._joins[self._joins_by_id[p["join_id"]]]["revoked"] = True
        elif t == "agent_linked":
            self._agents[p["agent_id"]]["instance_id"] = p.get("instance_id")
        elif t == "agent_renamed":
            self._agents[p["agent_id"]]["name"] = p["name"]
        elif t == "credential_rotated":
            a = self._agents[p["agent_id"]]
            self._by_cred.pop(a["cred_digest"], None)
            a.update(cred_digest=p["secret_digest"], expires_at=p["expires_at_epoch"])
            self._by_cred[p["secret_digest"]] = p["agent_id"]
        elif t == "agent_revoked":
            self._agents[p["agent_id"]]["status"] = "revoked"
            for rid in self._requests_by_agent.get(p["agent_id"], ()):
                self._requests[rid]["open"] = False
        elif t in ("agent_grant_revoked", "agent_grant_expired"):
            self._rooms[p["room_id"]]["grants"].pop(p["agent_id"], None)
            self._rooms_by_agent[p["agent_id"]].discard(p["room_id"])
        elif t == "agent_access_requested":
            for rid in self._requests_by_room.get(p["room_id"], ()):       # one open request per agent per room
                rq = self._requests[rid]
                if rq["open"] and rq["agent_id"] == p["agent_id"]:
                    rq["open"] = False
            self._requests_by_room[p["room_id"]].add(p["request_id"])
            self._requests_by_agent[p["agent_id"]].add(p["request_id"])
            self._requests[p["request_id"]] = {
                "room_id": p["room_id"], "agent_id": p["agent_id"], "actions": list(p["actions"]),
                "requester": p["actor_user_id"], "created_at": p["created_at"],
                "created_epoch": p["created_epoch"], "open": True}
        elif t == "agent_request_closed":
            if p["request_id"] in self._requests:
                self._requests[p["request_id"]]["open"] = False
        elif t == "task_created":
            self._tasks[p["task_id"]] = {
                "room_id": p["room_id"], "target": p["target_agent_id"], "text": p["text"],
                "by_kind": p["created_by_kind"], "by_id": p["created_by_id"], "by_owner": p["actor_user_id"],
                "root": p.get("root_user_id") or p["actor_user_id"],
                "status": "pending", "result": None, "depth": p["depth"], "parent": p.get("parent_task_id"),
                "created_at": p["created_at"], "created_epoch": p["created_epoch"],
                "updated_at": p["created_at"], "seq": seq}
            self._tasks_by_target[p["target_agent_id"]].append(p["task_id"])
            if p.get("idempotency_key") and p.get("fingerprint") and p.get("requester_key"):
                self._idem[(p["requester_key"], p["room_id"],
                            "task:" + p["idempotency_key"])] = (
                    p["fingerprint"], p["task_id"])
        elif t == "task_updated":
            tk = self._tasks.get(p["task_id"])
            if tk:
                tk.update(status=p["status"], result=p.get("result"), updated_at=p["created_at"])
        elif t == "approval_proposal_created":
            requester_key = f"{p['requester_kind']}:{p['requester_id']}"
            self._approval_proposals[p["proposal_id"]] = {
                "proposal_id": p["proposal_id"], "room_id": p["room_id"],
                "task_id": p["task_id"], "kind": p["kind"], "summary": p["summary"],
                "details": p["details"], "state": "pending", "owner": p["actor_user_id"],
                "requester_kind": p["requester_kind"], "requester_id": p["requester_id"],
                "created_at": p["created_at"], "sequence": ev["sequence"],
            }
            self._approval_proposal_idem[(requester_key, p["room_id"], p["idempotency_key"])] = (
                p["fingerprint"], p["proposal_id"])
        elif t == "assistant_package_registered":
            package = copy.deepcopy(p["package"])
            key = (p["room_id"], p["actor_user_id"], package["package_id"])
            version_key = (p["room_id"], p["actor_user_id"],
                           package["manifest"]["name"], package["manifest"]["version"])
            self._assistant_packages[key] = package
            self._assistant_package_versions[version_key] = package["package_id"]
        elif t == "assistant_runtime_host_attested":
            record = copy.deepcopy(p["attestation"])
            record["used"] = False
            self._assistant_runtime_hosts[record["host_id"]] = record
        elif t == "assistant_runtime_requested":
            runtime = {
                "runtime_id": p["runtime_id"], "room_id": p["room_id"],
                "project_id": p["project_id"], "owner": p["actor_user_id"],
                "package_id": p["package_id"], "package_digest": p["package_digest"],
                "host_id": p["host_id"], "name": p["name"],
                "selected_model": p["selected_model"], "attestation_nonce": p["attestation_nonce"],
                "access": ["execute", "read"], "status": "queued", "agent_id": None,
                "device_id": None, "claim_digest": None, "lease_expires_at": None,
                "checkpoint": None, "launch_attestation": None, "error_code": None,
                "cancel_requested": False, "cancel_reason": None,
                "created_at": p["created_at"], "updated_at": p["created_at"],
                "created_epoch": p["created_epoch"], "updated_epoch": p["created_epoch"],
                "attestation": copy.deepcopy(p["attestation"]),
            }
            self._assistant_runtimes[p["runtime_id"]] = runtime
            self._assistant_runtime_idem[(p["actor_user_id"], p["room_id"], p["idempotency_key"])] = (
                p["fingerprint"], p["runtime_id"])
        elif t == "assistant_runtime_claimed":
            runtime = self._assistant_runtimes[p["runtime_id"]]
            runtime.update(status="claimed", agent_id=p["agent_id"], device_id=p["device_id"],
                           claim_digest=p["claim_digest"], lease_expires_at=p["lease_expires_at"],
                           updated_at=p["created_at"], updated_epoch=p["claimed_epoch"])
            host = self._assistant_runtime_hosts.get(runtime["host_id"])
            if host and host["nonce"] == runtime["attestation_nonce"]:
                host["used"] = True
            self._agents[p["agent_id"]] = {
                "owner": runtime["owner"], "device_id": p["device_id"], "harness_type": "pi",
                "instance_id": None, "status": "active", "name": runtime["name"],
                "parent": None, "join": None, "cred_digest": p["secret_digest"],
                "expires_at": p["credential_expires_at"],
            }
            self._by_cred[p["secret_digest"]] = p["agent_id"]
            self._agents_by_owner[runtime["owner"]].add(p["agent_id"])
            self._rooms[runtime["room_id"]]["grants"][p["agent_id"]] = {
                "actions": ["execute", "read"], "expires_at": p["lease_expires_at"],
                "granted_seq": seq,
            }
            self._rooms_by_agent[p["agent_id"]].add(runtime["room_id"])
            self._agent_projects[p["agent_id"]].add(runtime["project_id"])
        elif t in ("assistant_runtime_heartbeat", "assistant_runtime_reported"):
            runtime = self._assistant_runtimes[p["runtime_id"]]
            runtime.update(checkpoint=copy.deepcopy(p["checkpoint"]),
                           lease_expires_at=p.get("lease_expires_at", runtime["lease_expires_at"]),
                           updated_at=p["created_at"], updated_epoch=p["reported_epoch"])
            aid = runtime["agent_id"]
            grant = self._rooms[runtime["room_id"]]["grants"].get(aid)
            if grant and p.get("lease_expires_at") is not None:
                grant["expires_at"] = p["lease_expires_at"]
            if t == "assistant_runtime_reported":
                runtime.update(status=p["status"], error_code=p.get("error_code"),
                               launch_attestation=copy.deepcopy(p.get("launch_attestation")))
                if p.get("instance_id"):
                    self._agents[runtime["agent_id"]]["instance_id"] = p["instance_id"]
                if p["status"] in _RUNTIME_TERMINAL:
                    aid = runtime["agent_id"]
                    if aid and aid in self._agents:
                        self._agents[aid]["status"] = "revoked"
                        self._by_cred.pop(self._agents[aid]["cred_digest"], None)
                        self._rooms[runtime["room_id"]]["grants"].pop(aid, None)
                        self._rooms_by_agent[aid].discard(runtime["room_id"])
        elif t == "assistant_runtime_cancel_requested":
            runtime = self._assistant_runtimes[p["runtime_id"]]
            runtime.update(cancel_requested=True, cancel_reason="owner_requested",
                           updated_at=p["created_at"], updated_epoch=p["requested_epoch"])
            if runtime["status"] == "queued":
                runtime.update(status="cancelled", error_code=None)
            elif runtime["agent_id"]:
                self._rooms[runtime["room_id"]]["grants"].pop(runtime["agent_id"], None)
                self._rooms_by_agent[runtime["agent_id"]].discard(runtime["room_id"])
        elif t == "assistant_runtime_expired":
            runtime = self._assistant_runtimes[p["runtime_id"]]
            runtime.update(status="failed", error_code=p["error_code"],
                           updated_at=p["created_at"], updated_epoch=p["expired_epoch"])
            aid = runtime["agent_id"]
            if aid and aid in self._agents:
                self._agents[aid]["status"] = "revoked"
                self._by_cred.pop(self._agents[aid]["cred_digest"], None)
                self._rooms[runtime["room_id"]]["grants"].pop(aid, None)
                self._rooms_by_agent[aid].discard(runtime["room_id"])
        elif t == "agent_granted":
            self._rooms[p["room_id"]]["grants"][p["agent_id"]] = {
                "actions": list(p["actions"]), "expires_at": p.get("expires_at"), "granted_seq": seq}
            self._agent_projects[p["agent_id"]].add(p["project_id"])
            self._rooms_by_agent[p["agent_id"]].add(p["room_id"])
        elif t == "egress_requested":
            self._egress[p["request_id"]] = {
                "id": p["request_id"], "room_id": p["room_id"], "agent_id": p["agent_id"],
                "host_port": p["host_port"], "state": "open", "created_epoch": p["created_epoch"],
                "decided_epoch": None, "expires_at": None, "decided_by": None}
        elif t == "egress_decided":
            r = self._egress.get(p["request_id"])
            if r is not None:
                r.update(state=p["state"], decided_epoch=p["decided_epoch"], expires_at=p.get("expires_at"),
                         decided_by=p["actor_user_id"])
        elif t == "egress_promoted":
            room = self._rooms.get(p["room_id"])
            if room is not None:
                prof = room.get("profile") or _normalize_profile({})
                if p["host_port"] not in prof["permanent_hosts"]:
                    prof["permanent_hosts"] = [*prof["permanent_hosts"], p["host_port"]]
                room["profile"] = prof
        elif t == "egress_removed":
            room = self._rooms.get(p["room_id"])
            if room is not None and room.get("profile"):
                room["profile"]["permanent_hosts"] = [h for h in room["profile"]["permanent_hosts"]
                                                      if h != p["host_port"]]
            for r in self._egress.values():
                if r["room_id"] == p["room_id"] and r["host_port"] == p["host_port"] and r["state"] == "approved":
                    r.update(state="removed", expires_at=p["removed_epoch"])
        elif t == "fleet_host_set":
            self._fleet_flags[p["host"]] = bool(p["auto_agents"])
        elif t == "fleet_spawn_requested":
            self._spawns[p["spawn_id"]] = {
                "spawn_id": p["spawn_id"], "owner": p["actor_user_id"], "harness_type": p["harness_type"],
                "name_prefix": p["name_prefix"], "count": p["count"], "target_room_id": p.get("target_room_id"),
                "actions": list(p.get("actions") or []), "expires_at": p.get("expires_at"),
                "parent": p.get("parent_agent_id"), "status": "requested", "created_epoch": p["created_epoch"], "claimed_epoch": None, "placements": [], "error": None}
        elif t == "fleet_spawn_claimed":
            if p["spawn_id"] in self._spawns:
                self._spawns[p["spawn_id"]].update(status="claimed", claimed_epoch=p["claimed_epoch"])
        elif t == "fleet_spawn_done":
            if p["spawn_id"] in self._spawns:
                self._spawns[p["spawn_id"]].update(status="done", placements=list(p["placements"]), error=p.get("error"))
        elif t == "helper_task":
            self._helper_text[p["helper_id"]] = {"task": p["task"], "model_reason": p.get("model_reason"),
                                                 "tier": p.get("tier")}
        elif t == "helper_requested":
            extra = self._helper_text.pop(p["helper_id"], {})
            self._helpers[p["helper_id"]] = {
                "helper_id": p["helper_id"], "room_id": p["room_id"], "project_id": p["project_id"],
                "requester_agent_id": p["requester_agent_id"], "owner": p["owner_user_id"],
                "parent_agent_id": p.get("parent_agent_id"), "role": p["role"], "family": p["family"],
                "model": p.get("model"), "model_reason": extra.get("model_reason"), "tier": extra.get("tier"),
                "task": extra.get("task"), "depth": p["depth"], "chain": p["chain"], "status": "requested",
                "created_epoch": p["created_epoch"], "expires_epoch": p["expires_epoch"], "claimed_epoch": None,
                "finished_epoch": None, "helper_agent_id": None, "actions": [], "result": None, "error": None,
                "reason": None, "parent_actions": list(p.get("parent_actions") or []), "error_text": None,
                "operation_id": p.get("operation_id")}
            if p.get("operation_id"):
                self._helper_by_op[p["operation_id"]] = self._helpers[p["helper_id"]]
        elif t == "helper_claimed":
            r = self._helpers.get(p["helper_id"])
            if r is not None:
                r.update(status="claimed", claimed_epoch=p["claimed_epoch"], helper_agent_id=p["helper_agent_id"],
                         actions=list(p["actions"]))
                self._helper_by_agent[p["helper_agent_id"]] = r
        elif t == "helper_started":
            if p["helper_id"] in self._helpers:
                self._helpers[p["helper_id"]]["status"] = "running"
        elif t == "helper_result":
            if p["helper_id"] in self._helpers:
                self._helpers[p["helper_id"]].update(result=p.get("result"), error_text=p.get("error_text"))
        elif t == "helper_finished":
            if p["helper_id"] in self._helpers:
                self._helpers[p["helper_id"]].update(status=p["status"], error=p.get("error"),
                                                     finished_epoch=p["finished_epoch"])
        elif t in ("helper_cancelled", "helper_expired"):
            if p["helper_id"] in self._helpers:
                self._helpers[p["helper_id"]].update(
                    status="cancelled" if t == "helper_cancelled" else "expired", reason=p.get("reason"),
                    finished_epoch=p["finished_epoch"])
        elif t == "helpers_paused":
            self._helpers_paused = bool(p["paused"])
        elif t == "catalogue_set":
            self._catalogue = list(p["entries"])
        elif t == "identity_linked":
            old = self._user_email.get(p["user_id"])
            if old:
                self._email_user.pop(old, None)
            self._user_email[p["user_id"]] = p["email"]
            self._email_user[p["email"]] = p["user_id"]
        elif t == "invite_created":
            self._invites[p["invite_id"]] = {
                "room_id": p["room_id"], "email": p["email"], "role": p["role"],
                "inviter": p["actor_user_id"], "expires_at": p["expires_at_epoch"], "open": True}
        elif t == "invite_closed":
            if p["invite_id"] in self._invites:
                self._invites[p["invite_id"]]["open"] = False
        elif t == "message":
            self._idem[(p["sender_key"], p["room_id"], p["idempotency_key"])] = (p["fingerprint"], seq)
            self._msg_room[ev["event_id"]] = (p["room_id"], seq)
        if t in _FEED_TYPES:
            for rid in {p.get("room_id"), *p.get("room_ids", ())} - {None}:
                self._by_room[rid].append(ev)

    # ------------------------------------------------------ authentication
    def authenticate(self, authorization: str, device: str | None) -> Principal:
        scheme, _, token = (authorization or "").partition(" ")
        token = token.strip()
        if not token:
            self.stats["auth_failures"] += 1
            raise CollabError(401, "unauthorized")
        if scheme == "Bearer":
            claims = getattr(self.auth, "verify_claims", None)
            if claims is not None:
                c = claims(token)
                got, groups = ((c["user"], c["exp"]), c["groups"]) if c else (None, [])
            else:
                got, groups = self.auth.verify(token), []
            if got is None:
                self.stats["auth_failures"] += 1
                raise CollabError(401, "unauthorized")
            pr = Principal("user", got[0], got[0], exp=got[1])
            if self.approval is not None:
                self._admit(pr, set(groups))
            return pr
        if scheme == "Agent":
            digest = _digest(token)
            with self._lock:
                aid = self._by_cred.get(digest)
                agent = self._agents.get(aid) if aid else None
                if agent is None:
                    self.stats["auth_failures"] += 1
                    raise CollabError(401, "unauthorized")
                reason = None
                if agent["status"] != "active":
                    reason = "revoked"
                elif self._now() >= agent["expires_at"]:
                    reason = "expired"
                elif device != agent["device_id"]:
                    reason = "device_mismatch"
                elif not self._ok(agent["owner"]):
                    reason = "owner_disabled"
                if reason:
                    self.stats["auth_failures"] += 1
                    self._audit_denied(Principal("agent", aid, agent["owner"]), "auth.agent", reason,
                                       resource_id=aid, agent_id=aid)
                    raise CollabError(401, "unauthorized")
                return Principal("agent", aid, agent["owner"], digest)
        self.stats["auth_failures"] += 1
        raise CollabError(401, "unauthorized")

    _REFUSED = {"denied": "your request to join was declined", "disabled": "your account is disabled",
                "kicked": "your access was removed"}

    def _admit(self, pr: Principal, groups: set) -> None:
        """Approval mode: record a first sign-in, apply the automatic rules, refuse the refused. Someone
        waiting is let through with status "pending", which dispatch limits to asking for approval."""
        pol, uid = self.approval, pr.id
        auto = bool(groups & (pol.auto_groups | pol.approver_groups))
        with self._lock:
            u = self._users.get(uid)
            if u is None:
                if len(self._users) >= MAX_USERS or (
                        not auto and sum(1 for x in self._users.values() if x["status"] == "pending") >= MAX_PENDING_USERS):
                    self.stats["policy_denied"] += 1
                    raise CollabError(429, "too many people are waiting for approval; ask the operator")
                self._commit("user_registered", {
                    "actor_user_id": uid, "actor_agent_id": None, "user_id": uid, "at": self._iso(),
                    "status": "approved" if auto else "pending", "decided_by": "auto:group" if auto else None,
                    "action": "user.register", "outcome": "allowed", "project_ids": [], "resource_id": uid})
            elif u["status"] == "pending" and auto:
                self._commit("user_decided", {
                    "actor_user_id": uid, "actor_agent_id": None, "user_id": uid, "at": self._iso(),
                    "status": "approved", "decided_by": "auto:group", "reason": None,
                    "action": "user.approve", "outcome": "allowed", "project_ids": [], "resource_id": uid})
            status = self._users[uid]["status"]
        if status in self._REFUSED:
            self.stats["policy_denied"] += 1
            raise CollabError(403, self._REFUSED[status])
        pr.status = status
        pr.approver = bool(groups & pol.approver_groups)

    def _ensure_operator(self, uid: str) -> None:
        """The tailnet-only dashboard owner is always approved: nobody else can lock them out."""
        with self._lock:
            if self._users.get(uid, {}).get("status") != "approved":
                self._commit("user_decided", {
                    "actor_user_id": uid, "actor_agent_id": None, "user_id": uid, "at": self._iso(),
                    "status": "approved", "decided_by": "operator", "reason": None,
                    "action": "user.approve", "outcome": "allowed", "project_ids": [], "resource_id": uid})

    def still_valid(self, pr: Principal) -> bool:
        """Re-check a long-lived stream's principal (revocation / expiry / owner loss)."""
        with self._lock:
            if pr.kind == "user":
                # An open stream ends when its token expires (the client re-opens
                # with a fresh one) or when the user loses access.
                return (pr.exp is None or self._now() < pr.exp) and self._ok(pr.id)
            a = self._agents.get(pr.id)
            return bool(a and a["status"] == "active" and self._now() < a["expires_at"]
                        and self._by_cred.get(a["cred_digest"]) == pr.id
                        and a["cred_digest"] == pr.digest and self._ok(a["owner"]))

    # ---------------------------------------------------------- authorization
    def _approved(self, uid: str) -> bool:
        return self.approval is None or self._users.get(uid, {}).get("status") == "approved"

    def _ok(self, uid: str) -> bool:
        """The person exists and may use the network (signed up, and approved when approval is on)."""
        return self.auth.exists(uid) and self._approved(uid)

    def _member(self, room: dict, uid: str) -> dict | None:
        m = room["members"].get(uid)
        if m is None or (m["expires_at"] is not None and self._now() >= m["expires_at"]):
            return None
        return m

    def _user_projects(self, uid: str) -> list[str]:
        out = []
        for pid, proj in self._projects.items():
            if uid in proj["members"]:
                out.append(pid)
        for room in self._rooms.values():
            m = self._member(room, uid)
            if m and m["role"] != "guest" and room["project_id"] not in out:
                out.append(room["project_id"])
        return out

    def _effective(self, pr: Principal, room: dict) -> tuple[frozenset, int] | None:
        """(allowed actions, first visible sequence) or None when no access at all."""
        if pr.kind == "user":
            m = self._member(room, pr.id)
            return (ROLE_ACTIONS[m["role"]], m["joined_seq"]) if m else None
        agent = self._agents.get(pr.id)
        grant = room["grants"].get(pr.id)
        if not agent or agent["status"] != "active" or not grant:
            return None
        if grant["expires_at"] is not None and self._now() >= grant["expires_at"]:
            return None
        m = self._member(room, agent["owner"])
        if not m or not self._ok(agent["owner"]):
            return None  # owner lost membership: the agent loses it too
        allowed = frozenset(grant["actions"]) & ROLE_ACTIONS[m["role"]] & AGENT_ACTIONS
        h = self._helper_by_agent.get(pr.id)
        if h is not None and h["parent_agent_id"]:
            # a helper never holds more than the agent it works under holds right now (recursively)
            parent = self._agents.get(h["parent_agent_id"])
            peff = self._effective(Principal("agent", h["parent_agent_id"], parent["owner"]), room) if parent else None
            if peff is None:
                return None
            allowed &= peff[0]
        return allowed, max(grant["granted_seq"], m["joined_seq"])

    def _authorize(self, pr: Principal, room_id: str, action: str, audit_action: str) -> tuple[dict, int]:
        room = self._rooms.get(room_id)
        eff = self._effective(pr, room) if room else None
        if eff is None:
            if room:
                self._audit_denied(pr, audit_action, "not_a_member", room_id=room_id,
                                   project_ids=[room["project_id"]])
            self.stats["policy_denied"] += 1
            raise CollabError(404, "not found")  # identical for unknown and forbidden rooms
        allowed, first = eff
        if action not in allowed:
            self._audit_denied(pr, audit_action, "forbidden", room_id=room_id,
                               project_ids=[room["project_id"]])
            self.stats["policy_denied"] += 1
            raise CollabError(403, "forbidden")
        return room, first

    def _audit_denied(self, pr: Principal, action: str, reason: str, *, room_id=None,
                      resource_id=None, project_ids=(), agent_id=None) -> None:
        # Only authenticated principals are logged: anonymous failures are counted,
        # never persisted, so unauthenticated traffic cannot grow the log. A retry
        # loop (e.g. a revoked bridge) is persisted at most once a minute per
        # (principal, action, reason, room); the rest is only counted.
        now = self._now()
        gate = (pr.key, action, reason, room_id)
        if now - self._denied_at.get(gate, -1e18) < 60:
            self.stats["audit_throttled"] += 1
            return
        if len(self._denied_at) > 10_000:
            self._denied_at = {k: t for k, t in self._denied_at.items() if now - t < 60}
        self._denied_at[gate] = now
        try:
            self._commit("audit_denied", self._actor(pr, action=action, outcome="denied",
                                                     reason=reason, room_id=room_id,
                                                     resource_id=resource_id or room_id,
                                                     project_ids=list(project_ids), agent_id=agent_id))
        except CollabError:
            pass

    @staticmethod
    def _actor(pr: Principal, **extra) -> dict:
        return {"actor_user_id": pr.owner, "actor_agent_id": pr.id if pr.is_agent else None, **extra}

    def _require_user(self, pr: Principal) -> None:
        if pr.is_agent:
            self.stats["policy_denied"] += 1
            raise CollabError(403, "forbidden: agents cannot perform this operation")

    # ------------------------------------------------------------ quotas
    def _room_has_space(self, room: dict) -> None:
        if len(room["members"]) >= self.max_members_per_room:
            raise CollabError(429, "room member limit reached")

    def _agent_slots(self, owner: str, extra: int = 1) -> None:
        now = self._now()
        live = sum(1 for aid in self._agents_by_owner.get(owner, ())
                   if (a := self._agents[aid])["status"] == "active" and a["expires_at"] > now)
        waiting = sum(1 for d in self._pending_by_owner.get(owner, ())
                      if not (r := self._pending[d])["used"] and r["expires_at"] > now)
        if live + waiting + extra > self.max_agents_per_user:
            raise CollabError(429, "agent limit reached; revoke one you no longer use")

    # ------------------------------------------------------------ operations
    def create_project(self, pr: Principal, body: dict) -> dict:
        self._require_user(pr)
        name = _name(body.get("name"))
        with self._lock:
            if sum(1 for pj in self._projects.values() if pj["members"].get(pr.id) == "manager") >= self.max_projects_per_user:
                raise CollabError(429, "project limit reached")
            pid = ids.new_id("project")
            self._commit("project_created", self._actor(
                pr, project_id=pid, name=name, action="project.create", outcome="allowed",
                project_ids=[pid], resource_id=pid))
        return {"project_id": pid, "name": name}

    def _project_visible(self, pr: Principal, project_id: str) -> dict:
        proj = self._projects.get(project_id)
        if not proj or project_id not in self._user_projects(pr.id):
            raise CollabError(404, "not found")
        return proj

    def add_project_member(self, pr: Principal, project_id: str, body: dict) -> dict:
        self._require_user(pr)
        with self._lock:
            proj = self._project_visible(pr, project_id)
            if proj["members"].get(pr.id) != "manager":
                raise CollabError(403, "forbidden")
            uid, role = body.get("user_id"), body.get("role")
            if not isinstance(role, str) or role not in PROJECT_ROLES:
                raise CollabError(400, "invalid role")
            if not isinstance(uid, str) or not self._ok(uid):
                raise CollabError(404, "unknown user")
            self._commit("project_member_added", self._actor(
                pr, project_id=project_id, subject_user_id=uid, role=role, action="project.member.add",
                outcome="allowed", project_ids=[project_id], resource_id=project_id))
        return {"project_id": project_id, "user_id": uid, "role": role}

    def create_room(self, pr: Principal, project_id: str, body: dict) -> dict:
        self._require_user(pr)
        name = _name(body.get("name"))
        with self._lock:
            proj = self._project_visible(pr, project_id)
            if pr.id not in proj["members"]:
                raise CollabError(403, "forbidden")
            if sum(1 for r in self._rooms.values() if r["project_id"] == project_id) >= self.max_rooms_per_project:
                raise CollabError(429, "room limit reached for this project")
            rid = ids.new_id("room")
            self._commit("room_created", self._actor(
                pr, project_id=project_id, room_id=rid, name=name, action="room.create",
                outcome="allowed", project_ids=[project_id], resource_id=rid))
        return {"room_id": rid, "project_id": project_id, "name": name}

    def list_rooms(self, pr: Principal) -> dict:
        self._require_user(pr)
        with self._lock:
            out = []
            for rid, room in self._rooms.items():
                m = self._member(room, pr.id)
                if m:
                    out.append({"room_id": rid, "name": room["name"], "role": m["role"],
                                "project_id": room["project_id"] if m["role"] != "guest" else None})
        return {"rooms": out}

    def get_room(self, pr: Principal, room_id: str) -> dict:
        with self._lock:
            room, _ = self._authorize(pr, room_id, "read", "room.read")
            out = {"room_id": room_id, "name": room["name"]}
            if not pr.is_agent:
                m = self._member(room, pr.id)
                out["role"] = m["role"]
                if m["role"] == "owner":
                    out["members"] = [{"user_id": u, "role": v["role"], "email": self._user_email.get(u)}
                                      for u, v in room["members"].items() if self._member(room, u)]
            return out

    def add_member(self, pr: Principal, room_id: str, body: dict) -> dict:
        self._require_user(pr)
        with self._lock:
            room, _ = self._authorize(pr, room_id, "invite", "member.add")
            uid, role = body.get("user_id"), body.get("role")
            if not isinstance(role, str) or role not in INVITABLE_ROLES:
                raise CollabError(400, "invalid role")
            exp = _finite_expiry(body.get("expires_at"), self._now())
            if not isinstance(uid, str) or not self._ok(uid):
                raise CollabError(404, "unknown user")
            if self._member(room, uid):
                raise CollabError(409, "already a member")
            self._room_has_space(room)
            if role == "guest" and exp is None:
                exp = self._now() + self.guest_ttl
            self._commit("member_added", self._actor(
                pr, project_id=room["project_id"], room_id=room_id, subject_user_id=uid, role=role,
                expires_at=exp, action="member.add", outcome="allowed",
                project_ids=[room["project_id"]], resource_id=room_id))
        return {"room_id": room_id, "user_id": uid, "role": role}

    def remove_member(self, pr: Principal, room_id: str, uid: str) -> dict:
        self._require_user(pr)
        with self._lock:
            room, _ = self._authorize(pr, room_id, "invite", "member.remove")
            m = room["members"].get(uid)
            if m is None:
                raise CollabError(404, "not found")
            if m["role"] == "owner":
                raise CollabError(400, "cannot remove an owner")
            self._commit("member_removed", self._actor(
                pr, project_id=room["project_id"], room_id=room_id, subject_user_id=uid,
                action="member.remove", outcome="allowed", project_ids=[room["project_id"]],
                resource_id=room_id))
            self._expire_helpers()                   # their helpers in this room end now and free their slots
        return {"ok": True}

    # -- people: approval, disable, kick
    def _user_row(self, uid: str, u: dict) -> dict:
        rooms = sum(1 for room in self._rooms.values() if uid in room["members"])
        agents = sum(1 for aid in self._agents_by_owner.get(uid, ()) if self._agents[aid]["status"] == "active")
        return {"user_id": uid, "email": self._user_email.get(uid), "status": u["status"], "since": u.get("since"),
                "decided_by": u.get("decided_by"), "decided_at": u.get("decided_at"), "reason": u.get("reason"),
                "rooms": rooms, "agents": agents}

    def list_users(self, pr: Principal, status: str | None = None) -> dict:
        """The operator sees everyone; an approver sees only the people waiting."""
        self._require_user(pr)
        if self.approval is None or not (pr.admin or pr.approver):
            raise CollabError(404, "not found")
        if status is not None and status not in USER_STATUSES:
            raise CollabError(400, "unknown status")
        with self._lock:
            rows = [self._user_row(uid, u) for uid, u in self._users.items()
                    if (status is None or u["status"] == status) and (pr.admin or u["status"] == "pending")]
            counts: dict[str, int] = defaultdict(int)
            for u in self._users.values():
                counts[u["status"]] += 1
        order = {"pending": 0, "denied": 1, "kicked": 2, "disabled": 3, "approved": 4}
        rows.sort(key=lambda r: (order[r["status"]], r["since"] or "", r["user_id"]))
        return {"users": rows[:500], "counts": dict(counts), "can_manage": pr.admin}

    def user_log(self, pr: Principal) -> dict:
        """Who was registered, approved, declined, disabled, enabled or removed, by whom, newest first."""
        self._require_user(pr)
        if self.approval is None or not pr.admin:
            raise CollabError(404, "not found")
        with self._lock:
            rows = []
            for e in reversed(self._log):
                p = e["payload"]
                if e["event_type"] in ("user_registered", "user_decided"):
                    rows.append({"at": p.get("at"), "user_id": p["user_id"], "email": self._user_email.get(p["user_id"]),
                                 "action": p.get("action"), "status": p["status"], "by": p.get("decided_by"),
                                 "reason": p.get("reason")})
                elif e["event_type"] == "helpers_paused":       # the panic switch: who pressed it, when, which way
                    rows.append({"at": p.get("created_at"), "user_id": p.get("actor_user_id"),
                                 "email": self._user_email.get(p.get("actor_user_id")), "action": p.get("action"),
                                 "status": "paused" if p.get("paused") else "resumed", "by": p.get("actor_user_id"),
                                 "reason": "all helpers paused" if p.get("paused") else "helpers resumed"})
        return {"log": rows[:300]}

    def decide_user(self, pr: Principal, uid: str, action: str, body: dict) -> dict:
        """approve / deny (operator or approver, for people waiting); disable / enable / kick (operator).
        Every decision is a logged event with who made it."""
        self._require_user(pr)
        if self.approval is None or not (pr.admin or pr.approver):
            raise CollabError(404, "not found")
        if action not in USER_ACTIONS:
            raise CollabError(404, "not found")
        reason = body.get("reason")
        if reason is not None and (not isinstance(reason, str) or len(reason) > 200 or _NAME_BAD.search(reason)):
            raise CollabError(400, "reason must be a short plain-text note")
        if not isinstance(uid, str) or not self.auth.exists(uid):
            raise CollabError(404, "not found")
        with self._lock:
            u = self._users.get(uid)
            if u is None:
                raise CollabError(404, "not found")
            if uid == pr.id:
                raise CollabError(400, "you cannot do this to yourself")
            if action == "approve" and not self._user_email.get(uid):
                # Nobody is approved blind: the pool lets anyone sign up, and the email is what says who it is.
                raise CollabError(409, "they have not finished signing in yet (no verified email); ask again in a moment")
            if action in ("approve", "deny"):
                if not pr.admin and u["status"] != "pending":
                    raise CollabError(403, "approvers only decide people who are waiting")
            elif not pr.admin:
                raise CollabError(403, "only the operator can do this")
            if action == "enable" and u["status"] != "disabled":
                raise CollabError(409, "that person is not disabled")
            if action in ("disable",) and u["status"] != "approved":
                raise CollabError(409, "only an approved person can be disabled")
            self._throttle("userdecide:" + pr.id, 300, 600.0, "user decision")
            new = USER_ACTIONS[action]
            self._commit("user_decided", self._actor(
                pr, user_id=uid, at=self._iso(), status=new, decided_by=pr.id, reason=reason,
                action=f"user.{action}", outcome="allowed", project_ids=[], resource_id=uid))
            if action == "kick":
                # Out of every room, and every agent they own stops working for good.
                for rid, room in list(self._rooms.items()):
                    m = room["members"].get(uid)
                    if m is not None and m["role"] != "owner":      # an owner's room stays; they simply can't get in
                        self._commit("member_removed", self._actor(
                            pr, project_id=room["project_id"], room_id=rid, subject_user_id=uid,
                            action="member.remove", outcome="allowed", project_ids=[room["project_id"]],
                            resource_id=rid))
                for aid in list(self._agents_by_owner.get(uid, ())):
                    if self._agents[aid]["status"] == "active":
                        rooms = [r for r, room in self._rooms.items() if aid in room["grants"]]
                        self._commit("agent_revoked", self._actor(
                            pr, agent_id=aid, room_ids=rooms, action="agent.revoke", outcome="allowed",
                            project_ids=[], resource_id=aid))
                seen: set = set()
                for aid in list(self._agents_by_owner.get(uid, ())):
                    self._end_agent(self._actor(pr), aid, "owner removed", seen)
            elif action == "disable":
                self._expire_helpers()                # a disabled person's helpers end now
            elif action in ("approve", "enable") and self._user_email.get(uid):
                self._deliver_pending(uid, self._user_email[uid])
            return self._user_row(uid, self._users[uid])

    # -- people: link email at sign-in, invite by email
    def me(self, pr: Principal) -> dict:
        self._require_user(pr)
        out = {"user_id": pr.id, "email": self._user_email.get(pr.id)}
        if self.approval is not None:
            out.update(status=pr.status, approver=pr.approver, admin=pr.admin)
        return out

    def link_identity(self, pr: Principal, body: dict) -> dict:
        """Record the verified email of the signed-in user and activate pending invites."""
        self._require_user(pr)
        verify = getattr(self.auth, "verify_id_token", None)
        token = body.get("id_token")
        email = verify(token, user=pr.id) if verify and isinstance(token, str) else None
        if not email:
            raise CollabError(400, "a valid id_token for this user with a verified email is required")
        with self._lock:
            if self._user_email.get(pr.id) != email:
                self._commit("identity_linked", {"actor_user_id": pr.id, "actor_agent_id": None,
                                                 "user_id": pr.id, "email": email})
            if self._approved(pr.id):                  # invitations wait for approval, then arrive
                self._deliver_pending(pr.id, email)
        out = {"user_id": pr.id, "email": email}
        if self.approval is not None:
            out.update(status=pr.status, approver=pr.approver, admin=pr.admin)
        return out

    def _deliver_pending(self, user: str, email: str) -> None:
        for iid, inv in list(self._invites.items()):
            if not inv["open"] or inv["email"] != email:
                continue
            room = self._rooms.get(inv["room_id"])
            if self._now() >= inv["expires_at"] or room is None:
                self._commit("invite_closed", {"actor_user_id": user, "actor_agent_id": None,
                                               "invite_id": iid, "reason": "expired"})
                continue
            inviter = self._member(room, inv["inviter"])
            if inviter is None or "invite" not in ROLE_ACTIONS[inviter["role"]] or not self._ok(inv["inviter"]):
                # The person who sent it no longer has the authority to add anyone.
                self._commit("invite_closed", {"actor_user_id": user, "actor_agent_id": None,
                                               "invite_id": iid, "reason": "inviter_unavailable"})
                continue
            if not self._member(room, user) and len(room["members"]) < self.max_members_per_room:
                self._commit("member_added", {
                    "actor_user_id": inv["inviter"], "actor_agent_id": None,
                    "project_id": room["project_id"], "room_id": inv["room_id"],
                    "subject_user_id": user, "role": inv["role"],
                    "expires_at": self._now() + self.guest_ttl if inv["role"] == "guest" else None,
                    "action": "member.add", "outcome": "allowed",
                    "project_ids": [room["project_id"]], "resource_id": inv["room_id"]})
            self._commit("invite_closed", {"actor_user_id": user, "actor_agent_id": None,
                                           "invite_id": iid, "reason": "accepted"})

    def invite_by_email(self, pr: Principal, room_id: str, body: dict) -> dict:
        """Invite a person by email. The reply is identical whether they were added now
        or the invitation is pending, so it reveals nothing about who has signed in."""
        self._require_user(pr)
        with self._lock:
            room, _ = self._authorize(pr, room_id, "invite", "member.invite")
            email, role = body.get("email"), body.get("role")
            if (not isinstance(email, str) or len(email) > 320
                    or not _EMAIL_RE.match(email.strip())):
                raise CollabError(400, "a valid email is required")
            email = email.strip().lower()
            if not isinstance(role, str) or role not in INVITABLE_ROLES:
                raise CollabError(400, "invalid role")
            reply = {"status": "invited", "email": email, "role": role}
            user = self._email_user.get(email)
            if user and self._ok(user):
                if self._member(room, user):
                    return reply   # same answer as a pending invite: no signed-in-or-not oracle
                self._commit("member_added", self._actor(
                    pr, project_id=room["project_id"], room_id=room_id, subject_user_id=user,
                    role=role, expires_at=self._now() + self.guest_ttl if role == "guest" else None, action="member.add", outcome="allowed",
                    project_ids=[room["project_id"]], resource_id=room_id))
                return reply
            open_here = [i for i in self._invites.values()
                         if i["open"] and i["room_id"] == room_id and self._now() < i["expires_at"]]
            if any(i["email"] == email for i in open_here):
                return reply  # already pending: idempotent
            if len(open_here) >= MAX_OPEN_INVITES:
                raise CollabError(429, "too many pending invitations for this room")
            self._commit("invite_created", self._actor(
                pr, project_id=room["project_id"], room_id=room_id, invite_id=ids.new_id("grant"),
                email=email, role=role, expires_at_epoch=self._now() + INVITE_TTL,
                action="member.invite", outcome="allowed", project_ids=[room["project_id"]],
                resource_id=room_id))
        return reply

    def list_invites(self, pr: Principal, room_id: str) -> dict:
        self._require_user(pr)
        with self._lock:
            self._authorize(pr, room_id, "invite", "invite.list")
            return {"invites": [
                {"invite_id": i, "email": v["email"], "role": v["role"], "expires_at": v["expires_at"]}
                for i, v in self._invites.items()
                if v["open"] and v["room_id"] == room_id and self._now() < v["expires_at"]]}

    def cancel_invite(self, pr: Principal, room_id: str, invite_id: str) -> dict:
        self._require_user(pr)
        with self._lock:
            room, _ = self._authorize(pr, room_id, "invite", "invite.cancel")
            inv = self._invites.get(invite_id)
            if not inv or inv["room_id"] != room_id or not inv["open"]:
                raise CollabError(404, "not found")
            self._commit("invite_closed", self._actor(
                pr, invite_id=invite_id, reason="cancelled", room_id=room_id,
                action="invite.cancel", outcome="allowed", project_ids=[room["project_id"]],
                resource_id=room_id))
        return {"ok": True}

    # -- agents
    def start_enrollment(self, pr: Principal, body: dict) -> dict:
        self._require_user(pr)
        device, harness = _name(body.get("device_id"), "device_id"), _name(body.get("harness_type"), "harness_type")
        code = "hcn_" + secrets.token_urlsafe(24)
        expires = self._now() + self.enrollment_ttl
        with self._lock:
            self._agent_slots(pr.id)
            self._commit("enrollment_started", self._actor(
                pr, code_digest=_digest(code), device_id=device, harness_type=harness,
                expires_at_epoch=expires, action="agent.enroll.start", outcome="allowed",
                project_ids=[], resource_id=None))
        return {"code": code, "expires_at": expires}

    def complete_enrollment(self, body: dict) -> dict:
        code, device = body.get("code"), body.get("device_id")
        if not isinstance(code, str) or not isinstance(device, str):
            raise CollabError(401, "unauthorized")
        digest = _digest(code)
        with self._lock:
            rec = self._pending.get(digest)
            if (rec is None or rec["used"] or self._now() >= rec["expires_at"]
                    or rec["device_id"] != device or not self._ok(rec["owner"])):
                self.stats["auth_failures"] += 1
                raise CollabError(401, "unauthorized")
            aid, cred = ids.new_id("agent"), "hca_" + secrets.token_urlsafe(32)
            expires = self._now() + self.credential_ttl
            instance = body.get("instance_id")
            self._commit("agent_enrolled", {
                "actor_user_id": rec["owner"], "actor_agent_id": None, "agent_id": aid,
                "owner_user_id": rec["owner"], "code_digest": digest, "device_id": device,
                "harness_type": rec["harness_type"],
                "instance_id": instance if isinstance(instance, str) and len(instance) <= 120
                and not _NAME_BAD.search(instance) else None,
                "secret_digest": _digest(cred), "expires_at_epoch": expires,
                "action": "agent.enroll", "outcome": "allowed",
                "project_ids": [], "resource_id": aid})
        return {"agent_id": aid, "credential": cred, "expires_at": expires}

    def _parent_for(self, owner: str, value: Any) -> str | None:
        """The agent a new agent is started under (its helpers share its herdr tab): one of the owner's own
        live agents, or nothing."""
        if value is None:
            return None
        parent = self._agents.get(value) if isinstance(value, str) else None
        if not parent or parent["status"] != "active" or parent["owner"] != owner:
            raise CollabError(404, "parent agent not found")
        return value

    def connect_agent(self, pr: Principal, body: dict) -> dict:
        """Create an agent for the signed-in user in one step (remote MCP clients).

        The browser session already proves who the owner is, so no enrollment code is
        needed. The credential is shown once, lives ``remote_credential_ttl``, grants
        nothing until the owner adds the agent to a room, and is revocable at any time."""
        self._require_user(pr)
        label = _name(body.get("harness_type", "claude-code"), "harness_type")
        handle = _handle(body["name"]) if body.get("name") is not None else None
        cred = "hca_" + secrets.token_urlsafe(32)
        device = "remote-" + secrets.token_hex(8)
        expires = self._now() + self.remote_credential_ttl
        aid = ids.new_id("agent")
        with self._lock:
            self._agent_slots(pr.id)
            parent = self._parent_for(pr.id, body.get("parent_agent_id"))
            self._commit("agent_enrolled", {
                "actor_user_id": pr.id, "actor_agent_id": None, "agent_id": aid,
                "owner_user_id": pr.id, "code_digest": None, "device_id": device,
                "harness_type": label, "instance_id": None, "name": handle, "parent_agent_id": parent,
                "secret_digest": _digest(cred),
                "expires_at_epoch": expires, "action": "agent.connect", "outcome": "allowed",
                "project_ids": [], "resource_id": aid})
        return {"agent_id": aid, "credential": cred, "device_id": device, "expires_at": expires,
                "name": handle or _default_handle(label, aid)}

    # ------------------------------------------------------------ fleets of agents
    def _agent_names(self, owner: str, prefix: str, count: int) -> list[str]:
        """`prefix-1`, `prefix-2`, ... skipping handles this person's agents already use."""
        taken = {self._agents[a]["name"] for a in self._agents_by_owner.get(owner, ())}
        out, n = [], 1
        while len(out) < count:
            name = f"{prefix}-{n}"
            if name not in taken:
                out.append(name)
            n += 1
        return out

    def _join_args(self, body: dict) -> tuple[str, str, int]:
        family = _name(body.get("harness_type", "claude-code"), "harness_type")
        prefix = _handle(body.get("name_prefix"))
        if len(prefix) > 24:
            raise CollabError(400, "name_prefix must be at most 24 characters")
        count = body.get("count", 1)
        if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= MAX_JOIN_AGENTS:
            raise CollabError(400, f"count must be 1-{MAX_JOIN_AGENTS}")
        return family, prefix, count

    def create_join_token(self, pr: Principal, body: dict) -> dict:
        """A short-lived, limited-use token that lets scripts on other machines register agents for
        this person. It creates NOTHING by itself: each use makes one agent that has no access,
        plus (if a room is named) a pending request that the room owner must approve."""
        self._require_user(pr)
        family, prefix, count = self._join_args(body)
        ttl = body.get("ttl_seconds", 3600)
        if not isinstance(ttl, (int, float)) or isinstance(ttl, bool) or not 60 <= ttl <= JOIN_TOKEN_MAX_TTL:
            raise CollabError(400, "ttl_seconds must be 60-86400")
        room_id, actions = body.get("room_id"), []
        with self._lock:
            if room_id is not None:
                if not isinstance(room_id, str):
                    raise CollabError(400, "invalid room_id")
                self._authorize(pr, room_id, "grant_agent", "join.token")
                actions = _actions(body.get("actions"))
            self._throttle("jointoken:" + pr.id, 10, 3600.0, "join token")
            self._agent_slots(pr.id, count)
            parent = self._parent_for(pr.id, body.get("parent_agent_id"))
            token, jid = "hcj_" + secrets.token_urlsafe(24), ids.new_id("collab")
            expires = self._now() + float(ttl)
            self._commit("join_token_created", self._actor(
                pr, join_id=jid, join_digest=_digest(token), harness_type=family, name_prefix=prefix,
                max_uses=count, expires_at_epoch=expires, room_id=room_id, actions=actions,
                parent_agent_id=parent, action="join.token.create", outcome="allowed", project_ids=[], resource_id=jid))
        return {"join_id": jid, "token": token, "expires_at": expires, "max_uses": count,
                "name_prefix": prefix, "harness_type": family, "room_id": room_id, "actions": actions}

    def list_join_tokens(self, pr: Principal) -> dict:
        self._require_user(pr)
        now = self._now()
        with self._lock:
            return {"tokens": [
                {"join_id": r["id"], "harness_type": r["family"], "name_prefix": r["prefix"],
                 "uses": r["uses"], "max_uses": r["max_uses"], "expires_at": r["expires_at"],
                 "room_id": r["room_id"], "actions": r["actions"],
                 "status": "revoked" if r["revoked"] else ("expired" if now >= r["expires_at"] else (
                     "used up" if r["uses"] >= r["max_uses"] else "active"))}
                for r in self._joins.values() if r["owner"] == pr.id and now - r["expires_at"] < 7 * 86400]}

    def revoke_join_token(self, pr: Principal, join_id: str) -> dict:
        self._require_user(pr)
        with self._lock:
            digest = self._joins_by_id.get(join_id)
            if not digest or self._joins[digest]["owner"] != pr.id:
                raise CollabError(404, "not found")
            if not self._joins[digest]["revoked"]:
                self._commit("join_token_revoked", self._actor(
                    pr, join_id=join_id, action="join.token.revoke", outcome="allowed",
                    project_ids=[], resource_id=join_id))
        return {"ok": True}

    def redeem_join_token(self, body: dict) -> dict:
        """Public, token-authenticated: one use = one new agent for the token's owner."""
        token = body.get("token")
        if not isinstance(token, str) or not token.startswith("hcj_") or len(token) > 80:
            raise CollabError(401, "unauthorized")
        digest = _digest(token)
        instance = body.get("instance_id")
        with self._lock:
            rec = self._joins.get(digest)
            if (rec is None or rec["revoked"] or self._now() >= rec["expires_at"]
                    or rec["uses"] >= rec["max_uses"] or not self._ok(rec["owner"])):
                self.stats["auth_failures"] += 1
                raise CollabError(401, "unauthorized")
            self._throttle("join:" + digest, 120, 60.0, "join")
            owner = rec["owner"]
            self._agent_slots(owner)
            name = self._agent_names(owner, rec["prefix"], 1)[0]
            aid, cred = ids.new_id("agent"), "hca_" + secrets.token_urlsafe(32)
            device = "remote-" + secrets.token_hex(8)
            expires = self._now() + self.remote_credential_ttl
            self._commit("agent_enrolled", {
                "actor_user_id": owner, "actor_agent_id": None, "agent_id": aid, "owner_user_id": owner,
                "code_digest": None, "join_digest": digest, "device_id": device,
                "harness_type": rec["family"], "name": name,
                "parent_agent_id": rec.get("parent") if (self._agents.get(rec.get("parent")) or {}).get("status") == "active" else None,
                "instance_id": instance if isinstance(instance, str) and len(instance) <= 120
                and instance and not _NAME_BAD.search(instance) else None,
                "secret_digest": _digest(cred), "expires_at_epoch": expires,
                "action": "agent.join", "outcome": "allowed", "project_ids": [], "resource_id": aid})
            request_id = self._file_request(owner, rec["room_id"], aid, rec["actions"]) if rec["room_id"] else None
        parent = self._agents.get(rec.get("parent")) if rec.get("parent") else None
        return {"agent_id": aid, "name": name, "credential": cred, "device_id": device, "expires_at": expires,
                "harness_type": rec["family"], "name_prefix": rec["prefix"],
                "parent": {"name": parent["name"], "instance_id": parent.get("instance_id")} if parent else None,
                "room_id": rec["room_id"], "request_id": request_id,
                "status": "pending" if request_id else "no access yet"}

    def link_joined(self, body: dict) -> dict:
        """Public, token-authenticated: the script that just registered an agent says which herdr pane it
        put it in (`pane:HOST:PANE`), so the dashboard can show the agent with its session and keep their
        names equal. Only for agents made by this very token, only while it is valid, and only that format."""
        token, aid, inst = body.get("token"), body.get("agent_id"), body.get("instance_id")
        if not isinstance(token, str) or not token.startswith("hcj_") or len(token) > 80 or not isinstance(aid, str):
            raise CollabError(401, "unauthorized")
        if not isinstance(inst, str) or not _PANE_LINK.fullmatch(inst):
            raise CollabError(400, "instance_id must look like pane:HOST:PANE")
        digest = _digest(token)
        with self._lock:
            rec, agent = self._joins.get(digest), self._agents.get(aid)
            if (rec is None or rec["revoked"] or self._now() >= rec["expires_at"] or not agent
                    or agent.get("join") != digest or agent["status"] != "active"):
                self.stats["auth_failures"] += 1
                raise CollabError(401, "unauthorized")
            self._throttle("joinlink:" + digest, 120, 60.0, "join link")
            self._commit("agent_linked", {"actor_user_id": rec["owner"], "actor_agent_id": None, "agent_id": aid,
                                          "instance_id": inst, "action": "agent.link", "outcome": "allowed",
                                          "project_ids": [], "resource_id": aid})
        return {"agent_id": aid, "instance_id": inst}

    def _file_request(self, owner: str, room_id: str, agent_id: str, actions: list[str]) -> str | None:
        """Ask on a person's behalf (their token was used); None when the room no longer takes it."""
        room = self._rooms.get(room_id)
        m = self._member(room, owner) if room else None
        if not m or "grant_agent" not in ROLE_ACTIONS[m["role"]]:
            return None
        now = self._now()
        open_here = [r for r in map(self._requests.get, self._requests_by_room.get(room_id, ()))
                     if r and r["open"] and now - r["created_epoch"] < REQUEST_TTL
                     and self._agents.get(r["agent_id"], {}).get("status") == "active"]
        if len(open_here) >= MAX_PENDING_REQUESTS:
            return None
        rid = ids.new_id("grant")
        self._commit("agent_access_requested", {
            "actor_user_id": owner, "actor_agent_id": None, "project_id": room["project_id"],
            "room_id": room_id, "agent_id": agent_id, "request_id": rid, "actions": actions,
            "created_epoch": now, "action": "agent.request", "outcome": "allowed",
            "project_ids": [room["project_id"]], "resource_id": agent_id})
        return rid

    def bulk_connect(self, pr: Principal, body: dict) -> dict:
        """Dashboard owner only (the operator listener): create a family of agents in one call. In a
        room the caller owns they are granted directly; otherwise they are requested."""
        self._require_user(pr)
        family, prefix, count = self._join_args(body)
        room_id, actions = body.get("room_id"), []
        with self._lock:
            if room_id is not None:
                if not isinstance(room_id, str):
                    raise CollabError(400, "invalid room_id")
                self._authorize(pr, room_id, "grant_agent", "agent.bulk")
                actions = _actions(body.get("actions"))
            self._throttle("bulk:" + pr.id, 12, 3600.0, "bulk registration")
            self._agent_slots(pr.id, count)
            if room_id is not None:                      # fail before creating anything we could not place
                now = self._now()
                waiting = sum(1 for r in map(self._requests.get, self._requests_by_room.get(room_id, ()))
                              if r and r["open"] and now - r["created_epoch"] < REQUEST_TTL)
                if waiting + count > MAX_PENDING_REQUESTS and self._member(self._rooms[room_id], pr.id)["role"] != "owner":
                    raise CollabError(429, "too many pending agent requests for this room")
            if "expires_at" in body:
                self._grant_expiry(body)
            out = []
            try:
                for name in self._agent_names(pr.id, prefix, count):
                    info = self.connect_agent(pr, {"harness_type": family, "name": name,
                                                   **({"parent_agent_id": body["parent_agent_id"]} if body.get("parent_agent_id") else {})})
                    out.append({**info, "status": None})        # recorded first: credentials are never lost
                    if room_id is not None:
                        out[-1]["status"] = self.grant_agent(pr, room_id, {
                            "agent_id": info["agent_id"], "actions": actions,
                            **({"expires_at": body["expires_at"]} if "expires_at" in body else {})},
                            _bulk=True)["status"]
            except CollabError as exc:
                if not out:
                    raise
                return {"agents": out, "error": exc.message}
        return {"agents": out}

    # ------------------------------------------------------------ fleets started on the owner's own hosts
    # Dashboard owner only (the operator listener; the public site gets a 404). A host-side runner
    # does the actual starting: this service only records what was asked and what happened, so the
    # internet-facing container never gets a way to start anything.
    def _require_host(self, value: Any) -> str:
        if not isinstance(value, str) or not _FLEET_HOST.fullmatch(value):
            raise CollabError(400, "invalid host name")
        return value

    def report_fleet_hosts(self, pr: Principal, body: dict) -> dict:
        """The runner says which hosts it can reach right now (memory only; it re-reports every cycle)."""
        self._require_user(pr)
        hosts = body.get("hosts")
        if not isinstance(hosts, list) or len(hosts) > 50:
            raise CollabError(400, "hosts must be a list of at most 50")
        now, seen = self._now(), {}
        for h in hosts:
            name = self._require_host(h.get("host") if isinstance(h, dict) else None)
            fams = h.get("families")
            seen[name] = {"kind": "local" if h.get("kind") == "local" else "machine", "seen": now,
                          "families": sorted({f for f in fams if f in SPAWN_FAMILIES and f != "agent"})
                          if isinstance(fams, list) else []}
        with self._lock:
            self._fleet_seen = seen
        return {"ok": True}

    def list_fleet_hosts(self, pr: Principal) -> dict:
        self._require_user(pr)
        with self._lock:
            now = self._now()
            names = sorted(set(self._fleet_seen) | set(self._fleet_flags))
            return {"hosts": [{"host": n, "kind": self._fleet_seen.get(n, {}).get("kind", "machine"),
                               "auto_agents": self._fleet_flags.get(n, True),
                               "families": self._fleet_seen.get(n, {}).get("families", []),
                               "online": now - self._fleet_seen.get(n, {}).get("seen", -1e18) < FLEET_ONLINE}
                              for n in names]}

    def set_fleet_host(self, pr: Principal, host: str, body: dict) -> dict:
        """Mark a host "no automatic agents": fleets are never started on it."""
        self._require_user(pr)
        host = self._require_host(host)
        if not isinstance(body.get("auto_agents"), bool):
            raise CollabError(400, "auto_agents must be true or false")
        with self._lock:
            if len(self._fleet_flags) >= 200 and host not in self._fleet_flags:
                raise CollabError(429, "too many hosts")
            self._throttle("fleethost:" + pr.id, 60, 3600.0, "host setting")
            self._commit("fleet_host_set", self._actor(pr, host=host, auto_agents=body["auto_agents"]))
        return {"host": host, "auto_agents": body["auto_agents"]}

    def request_spawn(self, pr: Principal, body: dict) -> dict:
        """Ask for `count` agents to be started on the owner's hosts (round robin, skipping hosts that
        opted out). Nothing runs until the runner claims the request; the room grant, if any, is the
        owner's own choice made here."""
        self._require_user(pr)
        family = _name(body.get("harness_type", "claude-code"), "harness_type")
        if family not in SPAWN_FAMILIES:
            raise CollabError(400, f"harness_type must be one of {', '.join(SPAWN_FAMILIES)}")
        prefix = _handle(body.get("name_prefix"))
        if len(prefix) > 24:
            raise CollabError(400, "name_prefix must be at most 24 characters")
        count = body.get("count", 1)
        if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= MAX_SPAWN_AGENTS:
            raise CollabError(400, f"count must be 1-{MAX_SPAWN_AGENTS}")
        room_id, actions = body.get("room_id"), []
        expires = None
        with self._lock:
            if room_id is not None:
                if not isinstance(room_id, str):
                    raise CollabError(400, "invalid room_id")
                self._authorize(pr, room_id, "grant_agent", "fleet.spawn")
                actions = _actions(body.get("actions"))
            if "expires_at" in body:
                expires = self._grant_expiry(body)
            if sum(1 for r in self._spawns.values() if r["owner"] == pr.id and r["status"] in ("requested", "claimed")
                   and self._now() - r["created_epoch"] < SPAWN_STALE) >= MAX_OPEN_SPAWNS:
                raise CollabError(429, "too many fleet starts in progress")
            self._agent_slots(pr.id, count)
            self._throttle("spawn:" + pr.id, 12, 3600.0, "fleet start")
            parent = self._parent_for(pr.id, body.get("parent_agent_id"))
            sid = ids.new_id("collab")
            self._commit("fleet_spawn_requested", self._actor(
                pr, spawn_id=sid, harness_type=family, name_prefix=prefix, count=count, target_room_id=room_id,
                actions=actions, expires_at=expires, parent_agent_id=parent, created_epoch=self._now()))
            return self._spawn_view(self._spawns[sid])

    def _spawn_view(self, r: dict) -> dict:
        status = r["status"]
        if status in ("requested", "claimed") and self._now() - (r["claimed_epoch"] or r["created_epoch"]) > SPAWN_STALE:
            status = "expired" if status == "requested" else "stalled"      # never taken / never finished: say so
        return {"spawn_id": r["spawn_id"], "status": status, "harness_type": r["harness_type"],
                "name_prefix": r["name_prefix"], "count": r["count"], "room_id": r["target_room_id"],
                "actions": r["actions"], "placements": r["placements"], "error": r["error"],
                "parent_agent_id": r.get("parent"), "created_at": self._iso(r["created_epoch"])}

    def list_spawns(self, pr: Principal, status: str | None = None) -> dict:
        self._require_user(pr)
        with self._lock:
            rows = [self._spawn_view(r) for r in self._spawns.values()
                    if r["owner"] == pr.id and (status is None or r["status"] == status)]
        rows.sort(key=lambda r: r["created_at"], reverse=True)
        return {"spawns": rows[:50]}

    TASK_ACTIVITY_MAX = 40

    def task_activity(self, pr: Principal, body: dict) -> dict:
        """For the dashboard's fabric timelines: recent task activity of the caller's own agents that
        are linked to the given fabric sessions. Ids, names and statuses only: the task text and
        result never leave the collaboration service this way."""
        self._require_user(pr)
        ids = body.get("instance_ids")
        if (not isinstance(ids, list) or len(ids) > 100
                or not all(isinstance(i, str) and 0 < len(i) <= 200 for i in ids)):
            raise CollabError(400, "instance_ids must be a list of at most 100 session ids")
        wanted = set(ids)
        with self._lock:
            mine = {aid: a for aid, a in self._agents.items()
                    if a["owner"] == pr.id and a["status"] == "active" and a.get("instance_id") in wanted}
            rows = []
            if mine:
                for tid, t in self._tasks.items():
                    got = t["target"] in mine
                    sent = t["by_kind"] == "agent" and t["by_id"] in mine
                    if not (got or sent):
                        continue
                    aid = t["target"] if got else t["by_id"]
                    room = self._rooms.get(t["room_id"]) or {}
                    rows.append({"instance_id": mine[aid]["instance_id"], "agent": mine[aid]["name"],
                                 "direction": "received" if got else "delegated", "task_id": tid,
                                 "status": self._task_status(t), "updated_at": t["updated_at"],
                                 "room": room.get("name"), "_seq": t["seq"]})
            rows.sort(key=lambda r: r.pop("_seq"), reverse=True)
        return {"activity": rows[:self.TASK_ACTIVITY_MAX]}

    def claim_spawn(self, pr: Principal, spawn_id: str) -> dict:
        """The runner takes a request exactly once (a second claim is refused, so a restart cannot start it twice)."""
        self._require_user(pr)
        with self._lock:
            r = self._spawns.get(spawn_id)
            if r is None or r["owner"] != pr.id:
                raise CollabError(404, "not found")
            if r["status"] != "requested":
                raise CollabError(409, "already claimed")
            if self._now() - r["created_epoch"] > SPAWN_STALE:
                raise CollabError(409, "expired: ask again")      # do not start something nobody is waiting for
            self._commit("fleet_spawn_claimed", self._actor(pr, spawn_id=spawn_id, claimed_epoch=self._now()))
            return {**self._spawn_view(self._spawns[spawn_id]), "expires_at": r["expires_at"]}

    def finish_spawn(self, pr: Principal, spawn_id: str, body: dict) -> dict:
        self._require_user(pr)
        placed = body.get("placements")
        if not isinstance(placed, list) or len(placed) > MAX_SPAWN_AGENTS:
            raise CollabError(400, "placements must be a list")
        clean = []
        for x in placed:
            if not isinstance(x, dict) or x.get("status") not in ("started", "failed"):
                raise CollabError(400, "invalid placement")
            clean.append({"name": _handle(x.get("name")), "host": self._require_host(x.get("host")),
                          "status": x["status"], "error": str(x.get("error") or "")[:200] or None})
        err = str(body["error"])[:200] if isinstance(body.get("error"), str) and body["error"] else None
        with self._lock:
            r = self._spawns.get(spawn_id)
            if r is None or r["owner"] != pr.id:
                raise CollabError(404, "not found")
            if r["status"] != "claimed":
                raise CollabError(409, "not claimed")
            self._commit("fleet_spawn_done", self._actor(pr, spawn_id=spawn_id, placements=clean, error=err))
            return self._spawn_view(self._spawns[spawn_id])

    def approve_all_requests(self, pr: Principal, room_id: str, body: dict) -> dict:
        """The owner approves several waiting requests at once; each is still capped at what its
        agent asked for."""
        self._require_user(pr)
        ids_ = body.get("request_ids")
        # Exactly the requests the owner was shown: "everything waiting" would also approve ones that
        # arrived after they looked.
        if (not isinstance(ids_, list) or not ids_ or len(ids_) > 200
                or not all(isinstance(i, str) for i in ids_)):
            raise CollabError(400, "request_ids is required: the requests you reviewed")
        wanted_actions = _actions(body["actions"]) if "actions" in body else None
        if "expires_at" in body:
            self._grant_expiry(body)                       # reject a bad date before touching anything
        with self._lock:
            self._authorize(pr, room_id, "invite", "agent.request.decide")
            self._throttle("approveall:" + pr.id, 30, 600.0, "bulk approval")
            approved, skipped, reasons = 0, 0, []
            for rid in ids_:
                try:
                    self.decide_agent_request(pr, room_id, rid, {
                        "decision": "approve",
                        **({"expires_at": body["expires_at"]} if "expires_at" in body else {}),
                        **({"actions": wanted_actions} if wanted_actions is not None else {})})
                    approved += 1
                except CollabError as exc:
                    skipped += 1
                    if exc.message not in reasons and len(reasons) < 5:
                        reasons.append(exc.message)
        return {"approved": approved, "skipped": skipped, "reasons": reasons}

    def list_my_agents(self, pr: Principal) -> dict:
        self._require_user(pr)
        with self._lock:
            out = []
            for aid in self._agents_by_owner.get(pr.id, ()):
                a = self._agents[aid]
                rooms = [{"room_id": rid, "name": self._rooms[rid]["name"], "actions": g["actions"]}
                         for rid in self._rooms_by_agent.get(aid, ()) if (g := self._rooms[rid]["grants"].get(aid))]
                pending = [{"room_id": r["room_id"], "name": self._rooms[r["room_id"]]["name"],
                            "actions": r["actions"]} for r in map(self._requests.get, self._requests_by_agent.get(aid, ()))
                           if r and r["open"] and r["room_id"] in self._rooms
                           and self._now() - r["created_epoch"] < REQUEST_TTL]
                out.append({"agent_id": aid, "name": a["name"], "harness_type": a["harness_type"],
                            "instance_id": a.get("instance_id"),
                            "status": a["status"], "expires_at": a["expires_at"], "rooms": rooms,
                            "pending": pending})
        return {"agents": out}

    def agent_rooms(self, pr: Principal) -> dict:
        """Rooms an agent can currently use (for MCP); never lists rooms it has no access to."""
        with self._lock:
            self._housekeeping()
            out = []
            for rid in (self._rooms_by_agent.get(pr.id, ()) if pr.is_agent else self._rooms):
                room = self._rooms[rid]
                eff = self._effective(pr, room)
                if eff and eff[0]:
                    out.append({"room_id": rid, "name": room["name"], "actions": sorted(eff[0])})
        return {"rooms": out}

    def rotate_credential(self, pr: Principal) -> dict:
        if not pr.is_agent:
            raise CollabError(403, "forbidden")
        cred = "hca_" + secrets.token_urlsafe(32)
        expires = self._now() + self.credential_ttl
        with self._lock:
            helper = self._helper_by_agent.get(pr.id)
            if helper is not None:                       # a helper's credential never outlives the helper
                expires = min(expires, helper["expires_epoch"])
            self._commit("credential_rotated", self._actor(
                pr, agent_id=pr.id, secret_digest=_digest(cred), expires_at_epoch=expires,
                action="agent.rotate", outcome="allowed",
                project_ids=[], resource_id=pr.id))
        return {"agent_id": pr.id, "credential": cred, "expires_at": expires}

    def revoke_agent(self, pr: Principal, agent_id: str) -> dict:
        self._require_user(pr)
        with self._lock:
            agent = self._agents.get(agent_id)
            if not agent or agent["owner"] != pr.id:
                raise CollabError(404, "not found")
            rooms = [r for r, room in self._rooms.items() if agent_id in room["grants"]]
            self._commit("agent_revoked", self._actor(
                pr, agent_id=agent_id, room_ids=rooms, action="agent.revoke", outcome="allowed",
                project_ids=[], resource_id=agent_id))
            self._end_agent(self._actor(pr), agent_id, "revoked", set())      # and every helper under it
        return {"ok": True}

    # ------------------------------------------------------- room profile
    def _profile(self, room_id: str) -> dict:
        """The room's helper policy (defaults when never set). Names of secrets only, never values."""
        room = self._rooms.get(room_id)
        stored = (room or {}).get("profile")
        return copy.deepcopy(stored) if stored else _normalize_profile({})

    def get_room_profile(self, pr: Principal, room_id: str) -> dict:
        """Members see the whole profile; an agent sees names only (no upstream URLs, no host list); a guest
        gets the same 404 as a stranger."""
        with self._lock:
            room, _ = self._authorize(pr, room_id, "read", "profile.get")
            if not pr.is_agent and (self._member(room, pr.id) or {}).get("role") == "guest":
                self._audit_denied(pr, "profile.get", "guest", room_id=room_id, project_ids=[room["project_id"]])
                self.stats["policy_denied"] += 1
                raise CollabError(404, "not found")
            p = self._profile(room_id)
            if pr.is_agent:
                p.pop("permanent_hosts")
                p["services"] = [{"name": sv["name"]} for sv in p["services"]]
            return p

    def set_room_profile(self, pr: Principal, room_id: str, body: dict) -> dict:
        """Owner only. Replaces the whole profile; unmentioned fields return to their defaults."""
        self._require_user(pr)
        with self._lock:
            room = self._rooms.get(room_id)
            m = self._member(room, pr.id) if room else None
            if not m or m["role"] != "owner":       # the same 404 for strangers and for non-owners
                if room:
                    self._audit_denied(pr, "profile.set", "not_owner", room_id=room_id,
                                       project_ids=[room["project_id"]])
                self.stats["policy_denied"] += 1
                raise CollabError(404, "not found")
            profile = _normalize_profile(body)
            self._throttle("profile:" + pr.id, 60, 600.0, "room profile")
            self._commit("room_profile_set", self._actor(
                pr, project_id=room["project_id"], room_id=room_id, profile=profile,
                action="profile.set", outcome="allowed",
                project_ids=[room["project_id"]], resource_id=room_id))
            return self._profile(room_id)

    # ------------------------------------------------------- host-access requests
    # The runner (as the operator) asks on an agent's behalf; only the operator decides. Approvals
    # expire; "promote" copies a host into the room profile's permanent hosts.
    def _require_operator(self, pr: Principal) -> None:
        if pr.is_agent or not pr.admin:        # the same 404 for the room owner and for strangers
            self.stats["policy_denied"] += 1
            raise CollabError(404, "not found")

    @staticmethod
    def _egress_host_port(host: Any, port: Any) -> str:
        """`host:port` in canonical form: a lowercase name or IPv4 address, or a bracketed IPv6 literal
        (`[2001:db8::1]:443`, canonical spelling, no zone id), the form the egress proxy's allow file reads."""
        if (not isinstance(host, str) or isinstance(port, bool) or not isinstance(port, int)
                or not 1 <= port <= 65535 or len(host) > 253):
            raise CollabError(400, "invalid host or port")
        if host.startswith("[") and host.endswith("]") and "%" not in host:
            try:
                return f"[{ipaddress.IPv6Address(host[1:-1])}]:{port}"
            except ValueError:
                pass
        elif _EGRESS_HOST.fullmatch(host):
            return f"{host}:{port}"
        raise CollabError(400, "invalid host or port")

    def _egress_live(self, room_id: str, now: float) -> set[str]:
        return {r["host_port"] for r in self._egress.values()
                if r["room_id"] == room_id and r["state"] == "approved" and r["expires_at"] > now}

    def egress_allowlist(self, room_id: str) -> list[str]:
        """Permanent hosts, unexpired approvals and the model endpoint of each family the room allows (helpers of
        other families never start there, so their endpoints are not opened)."""
        with self._lock:
            profile = self._profile(room_id)
            hosts = set(profile["permanent_hosts"]) | self._egress_live(room_id, self._now())
            for fam in profile["allowed_families"]:
                hosts.update(FAMILY_ENDPOINTS.get(fam, ()))
            return sorted(hosts)

    def egress_entries(self, room_id: str) -> list[dict]:
        """The allowlist with where each entry comes from: "permanent" (the profile; Remove takes it out),
        "approved" (an unexpired approval; Remove ends it) or "model" (a family endpoint; it cannot be removed)."""
        with self._lock:
            profile = self._profile(room_id)
            kinds: dict[str, str] = {}
            for fam in profile["allowed_families"]:
                for hp in FAMILY_ENDPOINTS.get(fam, ()):
                    kinds[hp] = "model"
            for hp in self._egress_live(room_id, self._now()):
                kinds[hp] = "approved"
            for hp in profile["permanent_hosts"]:
                kinds[hp] = "permanent"
            return [{"host_port": hp, "kind": kinds[hp]} for hp in sorted(kinds)]

    def request_egress(self, pr: Principal, room_id: str, agent_id: str, host: Any, port: Any) -> dict:
        self._require_operator(pr)
        hp = self._egress_host_port(host, port)
        if not isinstance(room_id, str):
            raise CollabError(400, "invalid room id")
        if not isinstance(agent_id, str) or not agent_id or len(agent_id) > 128:
            raise CollabError(400, "invalid agent id")
        with self._lock:
            if room_id not in self._rooms:
                raise CollabError(404, "not found")
            self._housekeeping()
            now = self._now()
            if hp in self.egress_allowlist(room_id):
                return {"state": "allowed", "host_port": hp}
            mine = [r for r in self._egress.values() if r["room_id"] == room_id]
            if any(r["host_port"] == hp and r["state"] == "denied"
                   and now - r["decided_epoch"] < EGRESS_DENY_WINDOW for r in mine):
                return {"state": "denied"}
            open_ = [r for r in mine if r["state"] == "open" and now - r["created_epoch"] < EGRESS_OPEN_TTL]
            for r in open_:
                if r["host_port"] == hp:
                    return dict(r)
            if len(open_) >= MAX_OPEN_EGRESS:
                raise CollabError(429, "too many open host-access requests in this room")
            rid = ids.new_id("grant")
            self._commit("egress_requested", self._actor(
                pr, room_id=room_id, request_id=rid, agent_id=agent_id, host_port=hp, created_epoch=now,
                action="egress.request", outcome="allowed", project_ids=[self._rooms[room_id]["project_id"]],
                resource_id=rid))
            return dict(self._egress[rid])

    def list_egress(self, pr: Principal, room_id: str | None = None) -> dict:
        """Operator only (GET fleet/egress): every room's host-access requests."""
        self._require_operator(pr)
        if room_id is not None and not isinstance(room_id, str):
            raise CollabError(400, "invalid room id")
        with self._lock:
            self._housekeeping()
            now = self._now()
            rows = [dict(r) for r in self._egress.values()
                    if (room_id is None or r["room_id"] == room_id)
                    and not (r["state"] == "open" and now - r["created_epoch"] >= EGRESS_OPEN_TTL)]
            for r in rows:                             # so the dashboard can warn before approving one
                r["internal"] = _internal_host(r["host_port"].rsplit(":", 1)[0])
            rows.sort(key=lambda r: (r["created_epoch"], r["id"]))
            # rooms whose owner listed hosts, so the operator can show (and Remove) them once request rows are pruned
            listed = sorted(rid for rid in self._rooms if self._profile(rid)["permanent_hosts"])
            return {"requests": rows, "rooms_with_hosts": listed}

    def decide_egress(self, pr: Principal, request_id: str, decision: Any,
                      ttl_seconds: Any = EGRESS_DEFAULT_TTL) -> dict:
        self._require_operator(pr)
        if not isinstance(request_id, str):
            raise CollabError(400, "invalid request id")
        if decision not in ("approve", "deny"):
            raise CollabError(400, "decision must be approve or deny")
        if (isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, (int, float))
                or not 1 <= ttl_seconds <= EGRESS_MAX_TTL):
            raise CollabError(400, "ttl_seconds must be between 1 second and 30 days")
        with self._lock:
            r = self._egress.get(request_id)
            if r is None:
                raise CollabError(404, "not found")
            if r["state"] != "open":
                raise CollabError(409, "already decided")
            now = self._now()
            if now - r["created_epoch"] >= EGRESS_OPEN_TTL:
                raise CollabError(409, "expired: ask again")
            approve = decision == "approve"
            self._commit("egress_decided", self._actor(
                pr, room_id=r["room_id"], request_id=request_id, state="approved" if approve else "denied",
                decided_epoch=now, expires_at=now + float(ttl_seconds) if approve else None,
                action="egress.decide", outcome="allowed", project_ids=[self._rooms[r["room_id"]]["project_id"]],
                resource_id=request_id))
            return dict(self._egress[request_id])

    def promote_egress(self, pr: Principal, room_id: str, host_port: Any) -> dict:
        self._require_operator(pr)
        hp = self._promote_target(room_id, host_port)
        with self._lock:
            profile = self._profile(room_id)
            if hp not in profile["permanent_hosts"] and len(profile["permanent_hosts"]) >= MAX_PROFILE_HOSTS:
                raise CollabError(400, "too many permanent hosts")
            self._commit("egress_promoted", self._actor(
                pr, room_id=room_id, host_port=hp, action="egress.promote", outcome="allowed",
                project_ids=[self._rooms[room_id]["project_id"]], resource_id=room_id))
            return {"permanent_hosts": self._profile(room_id)["permanent_hosts"]}

    def remove_egress(self, pr: Principal, room_id: str, host_port: Any) -> dict:
        self._require_operator(pr)
        hp = self._promote_target(room_id, host_port)
        with self._lock:
            self._commit("egress_removed", self._actor(
                pr, room_id=room_id, host_port=hp, removed_epoch=self._now(), action="egress.remove",
                outcome="allowed", project_ids=[self._rooms[room_id]["project_id"]], resource_id=room_id))
            return {"allowlist": self.egress_allowlist(room_id)}

    def _promote_target(self, room_id: str, host_port: Any) -> str:
        if not isinstance(room_id, str):
            raise CollabError(400, "invalid room id")
        if not isinstance(host_port, str) or len(host_port) > 260 or not _PROFILE_HOST.fullmatch(host_port):
            raise CollabError(400, "invalid host:port")
        host, port = host_port.rsplit(":", 1)
        if not 1 <= int(port) <= 65535:
            raise CollabError(400, "invalid host:port")
        hp = self._egress_host_port(host, int(port))        # canonical: a.org:0443 is a.org:443
        if room_id not in self._rooms:
            raise CollabError(404, "not found")
        return hp

    # ------------------------------------------------------- helper agents
    # An agent holding "spawn_helpers" in a room asks for a helper; the host runner (as the operator)
    # claims the request, which mints the helper's own agent, and later reports how it ended. The
    # parent is always the calling agent. Ending an agent (revoke, kick, removal, cancel, expiry,
    # pause) ends every helper under it and revokes their credentials.
    def _helper_live(self, r: dict, now: float | None = None) -> bool:
        return r["status"] in _HELPER_LIVE and (self._now() if now is None else now) < r["expires_epoch"]

    def _helper_view(self, r: dict) -> dict:
        status = "expired" if r["status"] in _HELPER_LIVE and not self._helper_live(r) else r["status"]
        return {k: r[k] for k in ("helper_id", "room_id", "role", "family", "model", "model_reason", "tier",
                                  "depth", "chain", "requester_agent_id", "parent_agent_id", "owner",
                                  "helper_agent_id", "created_epoch", "expires_epoch", "error")} | {"status": status}

    @staticmethod
    def _helper_task(value: Any) -> str:
        if not isinstance(value, str) or not value.strip() or len(value) > MAX_HELPER_TASK:
            raise CollabError(400, f"task must be 1-{MAX_HELPER_TASK} characters")
        if _HELPER_BAD.search(value):
            raise CollabError(400, "task may not contain control characters (newline and tab are fine)")
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            raise CollabError(400, "task must be valid text") from None
        return redact_text(value)

    def _helper_refuse(self, pr: Principal, room: dict, status: int, message: str, reason: str):
        self._audit_denied(pr, "helper.request", reason, room_id=room["id"], project_ids=[room["project_id"]])
        self.stats["policy_denied"] += 1
        raise CollabError(status, message)

    def request_helper(self, pr: Principal, body: dict, *, operation_id: str | None = None) -> dict:
        """The calling agent asks for a helper in one of its rooms. Over a limit it is refused, never queued.
        ``operation_id`` is set only by request_summary_fix_helper (never from a request body)."""
        if not pr.is_agent:
            raise CollabError(403, "forbidden: only agents start helpers")
        if not isinstance(body, dict) or set(body) - _HELPER_FIELDS - _HELPER_IGNORED:
            raise CollabError(400, "unknown field: helpers take room_id, role, task, family and model only")
        room_id = body.get("room_id")
        if not isinstance(room_id, str):
            raise CollabError(400, "invalid room_id")
        with self._lock:
            self._expire_helpers()
            room, _ = self._authorize(pr, room_id, "spawn_helpers", "helper.request")
            mine = self._helper_by_agent.get(pr.id)
            if mine is not None and mine["room_id"] != room_id:       # a helper works in its own room only
                self._helper_refuse(pr, room, 404, "not found", "helper_other_room")
            if self._helpers_paused:
                self._helper_refuse(pr, room, 503, "helpers are paused", "paused")
            role = body.get("role")
            if role not in HELPER_ROLES:
                raise CollabError(400, f"role must be one of {', '.join(HELPER_ROLES)}")
            task = self._helper_task(body.get("task"))
            hint = body.get("model")
            if hint is not None and (not isinstance(hint, str) or not _KEY_RE.match(hint)):
                raise CollabError(400, "invalid model")
            profile = self._profile(room_id)
            allowed = profile["allowed_families"]
            family = body.get("family")
            if family is not None and family not in SPAWN_FAMILIES:
                raise CollabError(400, f"family must be one of {', '.join(SPAWN_FAMILIES)}")
            if not allowed:
                self._helper_refuse(pr, room, 403, "helpers are not enabled in this room", "no_families")
            if family is None:
                own = self._agents[pr.id]["harness_type"]
                family = own if own in allowed else allowed[0]
            elif family not in allowed:
                self._helper_refuse(pr, room, 403, "that family is not allowed in this room", "family")
            successor = role == "successor"
            depth = (mine["depth"] if mine else 0) + (0 if successor else 1)
            chain = ((mine["chain"] if mine and mine["role"] == "successor" else 0) + 1) if successor else 0
            parent = (mine["parent_agent_id"] if mine else None) if successor else pr.id
            limits = profile["limits"]
            now = self._now()
            if depth > limits["depth"]:
                self._helper_refuse(pr, room, 429, "helper depth limit reached", "limit_depth")
            if chain > MAX_SUCCESSOR_CHAIN:
                self._helper_refuse(pr, room, 429, "successor chain limit reached", "limit_chain")
            live = [r for r in self._helpers.values() if self._helper_live(r, now)]
            if sum(1 for r in live if r["requester_agent_id"] == pr.id) >= limits["live_per_agent"]:
                self._helper_refuse(pr, room, 429, "live helper limit reached for this agent", "limit_live")
            if sum(1 for r in live if r["owner"] == pr.owner) >= HELPERS_PER_OWNER_LIVE:
                self._helper_refuse(pr, room, 429, "live helper limit reached for this person", "limit_owner")
            if len(live) >= HELPERS_GLOBAL_LIVE:
                self._helper_refuse(pr, room, 429, "global helper limit reached; try again later", "limit_global")
            # every request counts, cancelled or not: asking and cancelling cannot get around it
            if sum(1 for r in self._helpers.values()
                   if r["owner"] == pr.owner and now - r["created_epoch"] < 3600) >= limits["per_hour"]:
                self._helper_refuse(pr, room, 429, "hourly helper limit reached for this person", "limit_hour")
            author = (mine or {}).get("model")
            choice = collab_models.pick(role, task, None, hint, self._catalogue,
                                        profile["allowed_models"], author if isinstance(author, str) else None)
            model = choice.model if isinstance(choice.model, str) else None
            hid = ids.new_id("collab")
            # The text rides in its own event with no "action": the audit log and the room feed never show it.
            self._commit("helper_task", {"actor_user_id": pr.owner, "actor_agent_id": pr.id, "helper_id": hid,
                                         "task": task, "tier": str(choice.tier)[:40],
                                         "model_reason": str(choice.reason)[:200]})
            try:
                self._commit("helper_requested", self._actor(
                    pr, project_id=room["project_id"], room_id=room_id, helper_id=hid, requester_agent_id=pr.id,
                    owner_user_id=pr.owner, parent_agent_id=parent, role=role, family=family, model=model,
                    depth=depth, chain=chain, parent_actions=sorted(self._effective(pr, room)[0]),
                    created_epoch=now, expires_epoch=now + HELPER_TTL, action="helper.request", outcome="allowed",
                    project_ids=[room["project_id"]], resource_id=hid,
                    **({"operation_id": operation_id, "origin": "summary-fix"} if operation_id else {})))
            except CollabError:
                self._helper_text.pop(hid, None)     # never left waiting for a request that was not written
                raise
            return self._helper_view(self._helpers[hid])

    # A summary's owning agent cannot be woken: the dashboard (as the operator) asks for a text-only helper
    # through the one configured room and requester agent. The requester's own grant and the room profile
    # stay authoritative (request_helper re-checks them); this only adds who may ask on its behalf.
    def _summary_fix_requester(self, pr: Principal) -> tuple[Principal, str]:
        cfg = self.summary_fix
        if cfg is None:
            raise CollabError(409, "no summary-fix room is configured (control_plane.collab.summary_fix)",
                              "helper_unavailable")
        room = self._rooms.get(cfg["room_id"])
        agent = self._agents.get(cfg["requester_agent_id"])
        if room is None or agent is None or agent["owner"] != pr.id:
            raise CollabError(409, "the configured summary-fix room or requester agent does not exist or is not "
                                   "the operator's", "helper_misconfigured")
        now = self._now()
        if agent["status"] != "active" or not self._ok(agent["owner"]):
            raise CollabError(409, "the summary-fix requester agent is revoked", "helper_misconfigured")
        if now >= agent["expires_at"]:
            raise CollabError(409, "the summary-fix requester agent's credential expired; connect a new agent and "
                                   "update control_plane.collab.summary_fix", "helper_requester_expired")
        grant = room["grants"].get(cfg["requester_agent_id"])
        if grant is not None and grant["expires_at"] is not None and now >= grant["expires_at"]:
            raise CollabError(409, "the summary-fix requester agent's grant in its room expired; grant spawn_helpers "
                                   "again", "helper_grant_expired")
        # no spawn_helpers at all is request_helper's own 403 below; more than it is refused here
        if grant is not None and "spawn_helpers" in grant["actions"] and set(grant["actions"]) - {"spawn_helpers"}:
            raise CollabError(409, "the summary-fix requester agent may hold only spawn_helpers in its room",
                              "helper_overgranted")
        if self._profile(room["id"])["tools_allowed"]:
            raise CollabError(409, "the summary-fix room lets helpers use tools; turn that off",
                              "helper_tools_allowed")
        return Principal("agent", cfg["requester_agent_id"], agent["owner"]), room["id"]

    def request_summary_fix_helper(self, pr: Principal, body: dict) -> dict:
        """Operator only (POST fleet/summary-fix/helper {operation_id, task}). The same operation id
        returns the helper it already started, whatever became of it: one item and summary generation
        never starts two helpers."""
        self._require_operator(pr)
        if not isinstance(body, dict) or set(body) - {"operation_id", "task"}:
            raise CollabError(400, "summary-fix helpers take operation_id and task only")
        op = body.get("operation_id")
        if not isinstance(op, str) or not _SUMMARY_FIX_OP.match(op):
            raise CollabError(400, "invalid operation_id")
        with self._lock:
            self._expire_helpers()
            done = self._helper_by_op.get(op)
            if done is not None:
                return {**self._helper_view(done), "replayed": True}
            requester, room_id = self._summary_fix_requester(pr)
            try:
                out = self.request_helper(requester, {"room_id": room_id, "role": "worker", "task": body.get("task")},
                                          operation_id=op)
            except CollabError as exc:
                if exc.reason is None:
                    exc.reason = ("helper_limit" if exc.status == 429 else "helpers_paused" if exc.status == 503
                                  else "helper_refused" if exc.status in (403, 404) else None)
                raise
            return {**out, "replayed": False}

    def list_summary_fix_helpers(self, pr: Principal) -> dict:
        """Operator only (GET fleet/summary-fix/helpers): recent summary-fix helpers, finished ones too, with
        their result. The result is the helper's untrusted text; it is shown, never applied."""
        self._require_operator(pr)
        with self._lock:
            self._expire_helpers()
            rows = sorted((r for r in self._helpers.values() if r.get("operation_id")),
                          key=lambda r: (-r["created_epoch"], r["helper_id"]))[:MAX_SUMMARY_FIX_LIST]
            return {"configured": self.summary_fix is not None,
                    "helpers": [{**self._helper_view(r), "operation_id": r["operation_id"], "result": r["result"],
                                 "error_text": r.get("error_text"), "untrusted": True} for r in rows]}

    def _helper_for(self, pr: Principal, helper_id: Any) -> dict:
        r = self._helpers.get(helper_id) if isinstance(helper_id, str) else None
        # an agent: the one that asked, or the one the helper works under (for a successor: its predecessor's parent)
        mine = pr.id in (r["requester_agent_id"], r["parent_agent_id"]) if r and pr.is_agent else bool(r) and r["owner"] == pr.id
        if mine and pr.is_agent:
            # and is still in the helper's room now: an agent taken out of it (or whose owner left) reads nothing
            room = self._rooms.get(r["room_id"])
            mine = room is not None and self._effective(pr, room) is not None
        if not mine:
            raise CollabError(404, "not found")
        return r

    def helper_status(self, pr: Principal, helper_id: str) -> dict:
        with self._lock:
            self._expire_helpers()
            return self._helper_view(self._helper_for(pr, helper_id))

    def list_my_helpers(self, pr: Principal) -> dict:
        """A person's view (GET helpers): the helpers their agents asked for, newest first, no task or result."""
        self._require_user(pr)
        with self._lock:
            self._expire_helpers()
            rows = sorted((r for r in self._helpers.values() if r["owner"] == pr.id),
                          key=lambda r: (-r["created_epoch"], r["helper_id"]))
            return {"helpers": [self._helper_view(r) for r in rows[:200]]}

    def helper_result(self, pr: Principal, helper_id: str) -> dict:
        """The helper's final output: untrusted text, to be read as data."""
        with self._lock:
            self._expire_helpers()
            r = self._helper_for(pr, helper_id)
            return {**self._helper_view(r), "result": r["result"], "error_text": r.get("error_text"), "untrusted": True}

    def cancel_helper(self, pr: Principal, helper_id: str) -> dict:
        with self._lock:
            r = self._helper_for(pr, helper_id)
            self._end_helper(self._actor(pr), r, "cancelled", set())
            return self._helper_view(r)

    def cancel_helper_as_operator(self, pr: Principal, helper_id: str) -> dict:
        """Operator only (POST fleet/helpers/{id}/cancel): any helper, any owner; same effects as the owner's cancel
        (its subtree ends, credentials revoked), audited with the operator as actor."""
        self._require_operator(pr)
        with self._lock:
            self._expire_helpers()
            r = self._helpers.get(helper_id) if isinstance(helper_id, str) else None
            if r is None:
                raise CollabError(404, "not found")
            self._end_helper(self._actor(pr), r, "cancelled by the operator", set())
            return self._helper_view(r)

    def _end_helper(self, actor: dict, r: dict, reason: str, seen: set) -> None:
        if r["status"] in _HELPER_LIVE:
            self._commit("helper_cancelled", {
                **actor, "room_id": r["room_id"], "project_id": r["project_id"], "helper_id": r["helper_id"],
                "role": r["role"], "model": r["model"], "status": "cancelled", "reason": reason,
                "finished_epoch": self._now(), "action": "helper.cancel", "outcome": "allowed",
                "project_ids": [r["project_id"]], "resource_id": r["helper_id"]})
        if r["helper_agent_id"]:
            self._end_agent(actor, r["helper_agent_id"], reason, seen)

    def _end_agent(self, actor: dict, aid: str, reason: str, seen: set) -> None:
        """Revoke an agent (if still active), close its own helper request and end everything under it."""
        if aid in seen:
            return
        seen.add(aid)
        a = self._agents.get(aid)
        if a and a["status"] == "active":
            rooms = [r for r, room in self._rooms.items() if aid in room["grants"]]
            self._commit("agent_revoked", {**actor, "agent_id": aid, "room_ids": rooms, "reason": reason,
                                           "action": "agent.revoke", "outcome": "allowed", "project_ids": [],
                                           "resource_id": aid})
        own = self._helper_by_agent.get(aid)
        if own is not None and own["status"] in _HELPER_LIVE:
            self._end_helper(actor, own, reason, seen)
        self._end_dependents(actor, aid, reason, seen)

    def _end_dependents(self, actor: dict, aid: str, reason: str, seen: set, room: str | None = None) -> None:
        """Helpers under `aid`, and the successors it asked for (any live status). Only a predecessor that
        ended normally (its own helper finished or expired: the hand-over) leaves its successors alone."""
        own = self._helper_by_agent.get(aid)
        handed_over = reason in ("finished", "expired") or (own is not None and own["status"] in ("done", "failed", "expired"))
        for r in list(self._helpers.values()):
            if room is not None and r["room_id"] != room:
                continue
            if r["parent_agent_id"] == aid or (not handed_over and r["requester_agent_id"] == aid
                                               and r["status"] in _HELPER_LIVE):
                self._end_helper(actor, r, reason, seen)

    def _expire_helpers(self) -> None:
        now = self._now()
        actor = {"actor_user_id": None, "actor_agent_id": None}
        try:
            for r in [r for r in self._helpers.values() if r["status"] in _HELPER_LIVE and now >= r["expires_epoch"]]:
                if r["status"] not in _HELPER_LIVE:
                    continue                                 # ended meanwhile, as part of another subtree
                self._commit("helper_expired", {
                    **actor, "room_id": r["room_id"], "project_id": r["project_id"], "helper_id": r["helper_id"],
                    "role": r["role"], "model": r["model"], "status": "expired", "finished_epoch": now,
                    "action": "helper.expire", "outcome": "allowed", "project_ids": [r["project_id"]],
                    "resource_id": r["helper_id"]})
                if r["helper_agent_id"]:
                    self._end_agent(actor, r["helper_agent_id"], "expired", set())
            for r in [r for r in self._helpers.values()
                      if r["status"] == "requested" and now - r["created_epoch"] >= HELPER_CLAIM_WINDOW]:
                self._commit("helper_expired", {
                    **actor, "room_id": r["room_id"], "project_id": r["project_id"], "helper_id": r["helper_id"],
                    "role": r["role"], "model": r["model"], "status": "expired", "reason": "not claimed in time",
                    "finished_epoch": now, "action": "helper.expire", "outcome": "allowed",
                    "project_ids": [r["project_id"]], "resource_id": r["helper_id"]})
            for r in [r for r in self._helpers.values() if r["status"] in _HELPER_LIVE]:
                if r["status"] not in _HELPER_LIVE:
                    continue
                reason = self._helper_lost_reason(r, now)
                if reason:
                    self._end_helper(actor, r, reason, set())
            for aid in [a for a in self._helper_minted
                        if a not in self._helper_by_agent and self._agents[a]["status"] == "active"]:
                self._end_agent(actor, aid, "unlinked helper", set())    # a claim that was cut off half-way
        except CollabError:
            pass                                             # log full: expired helpers are refused anyway

    def _helper_lost_reason(self, r: dict, now: float) -> str | None:
        """Why a live helper can no longer run although nobody ended it explicitly, or None."""
        room = self._rooms.get(r["room_id"])
        if room is None or not self._member(room, r["owner"]) or not self._ok(r["owner"]):
            return "owner lost access"
        for aid in (r["requester_agent_id"], r["parent_agent_id"]):
            a = self._agents.get(aid) if aid else None
            if a is not None and aid not in self._helper_by_agent and now >= a["expires_at"]:
                return "parent expired"
        par = self._agents.get(r["parent_agent_id"]) if r["parent_agent_id"] else None
        if par is not None and par["status"] != "active":
            return "parent ended"
        if par is not None and self._effective(Principal("agent", r["parent_agent_id"], par["owner"]), room) is None:
            return "parent lost access"                  # e.g. its grant in the room lapsed
        return None

    def list_helpers_for_runner(self, pr: Principal) -> dict:
        """Operator only: every live helper (any owner), with its task, and whether the panic switch is on."""
        self._require_operator(pr)
        with self._lock:
            self._expire_helpers()
            rows = sorted((r for r in self._helpers.values() if self._helper_live(r)),
                          key=lambda r: (r["created_epoch"], r["helper_id"]))
            return {"paused": self._helpers_paused, "now": self._now(),
                    "helpers": [{**self._helper_view(r), "task": r["task"], "profile": self._runner_profile(r)}
                                for r in rows]}

    def _runner_profile(self, r: dict) -> dict:
        """What the runner needs of the room profile to start a helper (names only, never values). The
        operator is not a member of most rooms, so it cannot read the profile itself."""
        p = self._profile(r["room_id"])
        return {"allowed_families": p["allowed_families"], "tools_allowed": p["tools_allowed"],
                "services": p["services"], "ssh_secret_names": p["ssh_secret_names"]}

    def claim_helper(self, pr: Principal, helper_id: str) -> dict:
        """Operator only, exactly once: re-checks the parent's grant and the room profile, then mints the
        helper's own agent (owned by the parent's owner, granted at most the parent's actions in the room,
        living no longer than the helper) and returns its credential once."""
        self._require_operator(pr)
        with self._lock:
            self._expire_helpers()
            if self._helpers_paused:
                raise CollabError(503, "helpers are paused")
            r = self._helpers.get(helper_id) if isinstance(helper_id, str) else None
            if r is None:
                raise CollabError(404, "not found")
            view = self._helper_view(r)
            if view["status"] != "requested":
                raise CollabError(409, f"helper is {view['status']}")
            room = self._rooms[r["room_id"]]
            req = self._agents.get(r["requester_agent_id"])
            prev = self._helper_by_agent.get(r["requester_agent_id"])
            allowed = None
            if req and req["status"] == "active":
                eff = self._effective(Principal("agent", r["requester_agent_id"], req["owner"]), room)
                allowed = eff[0] if eff else None
            elif r["role"] == "successor" and prev is not None and prev["status"] in ("done", "failed", "expired"):
                # the predecessor handed over and ended normally: what it had when it asked, as far as its
                # owner's role in the room still allows
                m = self._member(room, r["owner"])
                if m and self._ok(r["owner"]):
                    allowed = frozenset(r["parent_actions"]) & ROLE_ACTIONS[m["role"]] & AGENT_ACTIONS
            parent_ok = r["parent_agent_id"] is None or (
                (self._agents.get(r["parent_agent_id"]) or {}).get("status") == "active")
            if (not allowed or "spawn_helpers" not in allowed or not parent_ok
                    or r["family"] not in self._profile(r["room_id"])["allowed_families"]):
                self._end_helper(self._actor(pr), r, "no longer allowed", set())
                raise CollabError(409, "no longer allowed: the parent's access or the room profile changed")
            owner, now = r["owner"], self._now()
            self._agent_slots(owner)
            taken = {self._agents[a]["name"] for a in self._agents_by_owner.get(owner, ())}
            taken |= {self._agents.get(a, {}).get("name") for a in room["grants"]}
            name = f"{r['role']}-{secrets.token_hex(3)}"
            while name in taken:
                name = f"{r['role']}-{secrets.token_hex(3)}"
            aid, cred, device = ids.new_id("agent"), "hca_" + secrets.token_urlsafe(32), "helper-" + secrets.token_hex(8)
            actions = sorted(allowed)
            grant_exp = (room["grants"].get(r["requester_agent_id"]) or {}).get("expires_at")
            expires = r["expires_epoch"]
            self._commit("agent_enrolled", self._actor(
                pr, agent_id=aid, owner_user_id=owner, code_digest=None, device_id=device,
                harness_type=r["family"], instance_id=None, name=name, parent_agent_id=r["parent_agent_id"],
                secret_digest=_digest(cred), expires_at_epoch=expires, action="helper.agent.create",
                outcome="allowed", project_ids=[r["project_id"]], resource_id=aid))
            self._commit("agent_granted", self._actor(
                pr, project_id=r["project_id"], room_id=r["room_id"], agent_id=aid, actions=actions,
                expires_at=min(expires, grant_exp) if grant_exp is not None else expires,
                action="helper.agent.grant", outcome="allowed", project_ids=[r["project_id"]], resource_id=aid))
            self._commit("helper_claimed", self._actor(
                pr, room_id=r["room_id"], project_id=r["project_id"], helper_id=helper_id, helper_agent_id=aid,
                actions=actions, claimed_epoch=now, role=r["role"], model=r["model"], status="claimed",
                action="helper.claim", outcome="allowed", project_ids=[r["project_id"]], resource_id=helper_id))
            handoff = None
            if r["role"] == "successor":
                handoff = {"from_agent_id": r["requester_agent_id"],
                           "from_helper_id": prev["helper_id"] if prev else None, "text": r["task"]}
                self._hand_over(pr, r, prev)
            par = self._agents.get(r["parent_agent_id"]) if r["parent_agent_id"] else None
            return {**self._helper_view(r), "task": r["task"], "credential": cred, "device_id": device,
                    "now": now, "profile": self._runner_profile(r), "handoff": handoff,
                    "agent_name": name, "actions": actions, "expires_at": expires,
                    "requester": {"agent_id": r["requester_agent_id"], "name": req["name"],
                                  "instance_id": req.get("instance_id")},
                    "parent": {"agent_id": r["parent_agent_id"], "name": par["name"],
                               "instance_id": par.get("instance_id")} if par else None}

    def _hand_over(self, pr: Principal, r: dict, prev: dict | None) -> None:
        """A successor was claimed: the other successors its predecessor asked for are superseded, and a helper
        predecessor ends. Its handoff note (the successor's task) becomes its final result: the stored hand-over
        its tree parent reads with helper_result. A root predecessor is a person's own session, outside any
        runner: it is not ended (revoking it would also cut its other rooms and end the successor)."""
        actor = self._actor(pr)
        for o in list(self._helpers.values()):
            if (o is not r and o["role"] == "successor" and o["status"] == "requested"
                    and o["requester_agent_id"] == r["requester_agent_id"]):
                self._end_helper(actor, o, "superseded", set())
        if prev is None or prev["status"] not in ("claimed", "running"):
            return                                        # it already finished (or ended) on its own
        note = f"[handed over to successor {r['helper_id']}]\n{r['task']}"
        self._commit("helper_result", {**actor, "helper_id": prev["helper_id"], "result": note[:MAX_HELPER_RESULT],
                                       "error_text": None})
        self._commit("helper_finished", {
            **actor, "room_id": prev["room_id"], "project_id": prev["project_id"], "helper_id": prev["helper_id"],
            "role": prev["role"], "model": prev["model"], "status": "done", "finished_epoch": self._now(),
            "error": None, "successor_id": r["helper_id"], "action": "helper.handover", "outcome": "allowed",
            "project_ids": [prev["project_id"]], "resource_id": prev["helper_id"]})
        self._end_agent(actor, prev["helper_agent_id"], "handed over", set())

    def finish_helper(self, pr: Principal, helper_id: str, body: dict) -> dict:
        """Operator only: "running" once started, then "done" or "failed" with the output. A finished
        helper's credential is revoked and anything it started is ended."""
        self._require_operator(pr)
        status = body.get("status") if isinstance(body, dict) else None
        if status not in ("running", "done", "failed"):
            raise CollabError(400, "status must be running, done or failed")
        result, err = body.get("result"), body.get("error")
        if (result is not None and not isinstance(result, str)) or (err is not None and not isinstance(err, str)):
            raise CollabError(400, "result and error must be text")
        with self._lock:
            self._expire_helpers()
            r = self._helpers.get(helper_id) if isinstance(helper_id, str) else None
            if r is None:
                raise CollabError(404, "not found")
            actor = self._actor(pr)
            base = {"room_id": r["room_id"], "project_id": r["project_id"], "helper_id": helper_id,
                    "role": r["role"], "model": r["model"], "outcome": "allowed",
                    "project_ids": [r["project_id"]], "resource_id": helper_id}
            if status == "running":
                if r["status"] != "claimed":
                    raise CollabError(409, f"helper is {self._helper_view(r)['status']}")
                self._commit("helper_started", {**actor, **base, "status": "running", "action": "helper.start"})
                return self._helper_view(r)
            if r["status"] not in ("claimed", "running"):
                raise CollabError(409, f"helper is {self._helper_view(r)['status']}")
            # Free text (output, the runner's error note) only in an event without "action"; audited
            # events carry a fixed error code.
            text = redact_text(_HELPER_BAD.sub("", err)[:200]) if err else None
            if result or text:
                clean = redact_text(_HELPER_BAD.sub("", result)[:MAX_HELPER_RESULT]) if result else None
                self._commit("helper_result", {**actor, "helper_id": helper_id, "result": clean, "error_text": text})
            code = (err if err in HELPER_ERRORS else "failed") if status == "failed" or err else None
            self._commit("helper_finished", {**actor, **base, "status": status, "finished_epoch": self._now(),
                                             "error": code, "action": "helper.finish"})
            self._end_agent(actor, r["helper_agent_id"], "finished", set())
            return self._helper_view(r)

    def set_catalogue(self, pr: Principal, entries: Any) -> dict:
        """The runner posts the normalized model catalogue (operator only); it replaces the old one wholesale."""
        self._require_operator(pr)
        clean = sorted(_catalogue_entries(entries), key=lambda e: e["id"])   # gateway order is not stable
        with self._lock:
            if clean != self._catalogue:          # the runner posts every 10 minutes: log only changes
                # no "action": the list is configuration data, not an audit row
                self._commit("catalogue_set", {"actor_user_id": pr.owner, "entries": clean})
            return {"count": len(clean)}

    def set_helpers_paused(self, pr: Principal, paused: Any) -> dict:
        """The panic switch (operator only): refuse new helpers, end every live one and its credential."""
        self._require_operator(pr)
        if not isinstance(paused, bool):
            raise CollabError(400, "paused must be true or false")
        with self._lock:
            self._commit("helpers_paused", self._actor(
                pr, paused=paused, action="helpers.pause" if paused else "helpers.resume", outcome="allowed",
                project_ids=[], resource_id=None))
            if paused:
                seen: set = set()
                for r in [r for r in self._helpers.values() if r["status"] in _HELPER_LIVE]:
                    self._end_helper(self._actor(pr), r, "paused", seen)
            return {"paused": paused}

    def grant_agent(self, pr: Principal, room_id: str, body: dict, _bulk: bool = False) -> dict:
        """The room owner grants an agent its permissions directly. Anyone else who may bring an
        agent (a contributor) can only REQUEST access; the owner approves a subset of what was
        asked, so no one gets a permission they did not ask for or the owner did not approve.
        Narrowing an existing grant never needs approval."""
        self._require_user(pr)
        with self._lock:
            room, _ = self._authorize(pr, room_id, "grant_agent", "agent.grant")
            agent = self._agents.get(body.get("agent_id")) if isinstance(body.get("agent_id"), str) else None
            if not agent or agent["owner"] != pr.id or agent["status"] != "active":
                raise CollabError(404, "not found")
            self._helper_room_only(body["agent_id"], room_id)
            aid = body["agent_id"]
            actions = _actions(body.get("actions"))
            m = self._member(room, pr.id)
            existing = room["grants"].get(aid)
            self._name_free(room, aid, agent["name"])
            narrowing = bool(existing) and set(actions) <= set(existing["actions"])
            if existing and set(actions) == set(existing["actions"]) and m["role"] != "owner":
                return {"room_id": room_id, "agent_id": aid, "actions": actions, "status": "granted"}   # nothing to do
            if not _bulk:                       # a bulk call charges once, for the whole family
                self._throttle("grant:" + pr.id, 120 if m["role"] == "owner" else 30, 600.0, "agent permission")   # each call writes to the shared log
            if m["role"] == "owner" or narrowing:
                exp = self._grant_expiry(body) if m["role"] == "owner" else existing["expires_at"]
                self._commit("agent_granted", self._actor(
                    pr, project_id=room["project_id"], room_id=room_id, agent_id=aid,
                    actions=actions, expires_at=exp, action="agent.grant", outcome="allowed",
                    project_ids=[room["project_id"]], resource_id=aid))
                return {"room_id": room_id, "agent_id": aid, "actions": actions, "status": "granted"}
            now = self._now()
            open_here = [r for r in map(self._requests.get, self._requests_by_room.get(room_id, ()))
                         if r and r["open"] and now - r["created_epoch"] < REQUEST_TTL
                         and self._agents.get(r["agent_id"], {}).get("status") == "active"]
            if len(open_here) >= MAX_PENDING_REQUESTS:
                raise CollabError(429, "too many pending agent requests for this room")
            rid = ids.new_id("grant")
            self._commit("agent_access_requested", self._actor(
                pr, project_id=room["project_id"], room_id=room_id, agent_id=aid, request_id=rid,
                actions=actions, created_epoch=now, action="agent.request", outcome="allowed",
                project_ids=[room["project_id"]], resource_id=aid))
        return {"room_id": room_id, "agent_id": aid, "actions": actions, "status": "pending", "request_id": rid}

    def _helper_room_only(self, agent_id: str, room_id: str) -> None:
        h = self._helper_by_agent.get(agent_id)
        if (h is not None and h["room_id"] != room_id) or (agent_id in self._helper_minted and h is None):
            self.stats["policy_denied"] += 1
            raise CollabError(403, "a helper works only in its own room")

    def _name_free(self, room: dict, agent_id: str, name: str) -> None:
        """Handles are how people address agents (@name): two agents in one room never share one.
        Only agents that hold a grant own a handle. A pending request does not, so someone else's
        request can't keep the owner from using that name; the clash shows up when it is approved."""
        for other in room["grants"]:
            if other != agent_id and self._agents.get(other, {}).get("name") == name:
                raise CollabError(409, "an agent with that name is already in this room; rename yours")

    def list_agent_requests(self, pr: Principal, room_id: str) -> dict:
        self._require_user(pr)
        with self._lock:
            room, _ = self._authorize(pr, room_id, "invite", "agent.request.list")
            now, out = self._now(), []
            for rid in self._requests_by_room.get(room_id, ()):
                r = self._requests[rid]
                a = self._agents.get(r["agent_id"])
                if (not r["open"] or now - r["created_epoch"] >= REQUEST_TTL
                        or not a or a["status"] != "active" or not self._member(room, a["owner"])):
                    continue
                out.append({"request_id": rid, "agent_id": r["agent_id"], "name": a["name"],
                            "harness_type": a["harness_type"], "owner_user_id": a["owner"],
                            "owner_email": self._user_email.get(a["owner"]), "actions": r["actions"],
                            "created_at": r["created_at"]})
        return {"requests": out}

    def decide_agent_request(self, pr: Principal, room_id: str, request_id: str, body: dict) -> dict:
        self._require_user(pr)
        with self._lock:
            room, _ = self._authorize(pr, room_id, "invite", "agent.request.decide")
            r = self._requests.get(request_id)
            if (not r or not r["open"] or r["room_id"] != room_id
                    or self._now() - r["created_epoch"] >= REQUEST_TTL):
                raise CollabError(404, "not found")
            decision = body.get("decision")
            if decision not in ("approve", "deny"):
                raise CollabError(400, "decision must be approve or deny")
            agent = self._agents.get(r["agent_id"])
            if decision == "approve":
                if not agent or agent["status"] != "active" or not self._member(room, agent["owner"]):
                    raise CollabError(404, "not found")
                self._helper_room_only(r["agent_id"], room_id)
                actions = _actions(body["actions"]) if "actions" in body else list(r["actions"])
                self._name_free(room, r["agent_id"], agent["name"])
                if not set(actions) <= set(r["actions"]):
                    raise CollabError(400, "cannot approve more than the owner of the agent asked for")
                exp = self._grant_expiry(body)
                self._commit("agent_granted", self._actor(
                    pr, project_id=room["project_id"], room_id=room_id, agent_id=r["agent_id"],
                    actions=actions, expires_at=exp, action="agent.approve", outcome="allowed",
                    project_ids=[room["project_id"]], resource_id=r["agent_id"]))
            self._commit("agent_request_closed", self._actor(
                pr, project_id=room["project_id"], room_id=room_id, request_id=request_id,
                reason="approved" if decision == "approve" else "denied", action="agent.request.close",
                outcome="allowed", project_ids=[room["project_id"]], resource_id=request_id))
        return {"ok": True, "decision": decision}

    def remove_agent_from_room(self, pr: Principal, room_id: str, agent_id: str) -> dict:
        """The room owner, or the agent's own owner, takes an agent out of a room."""
        self._require_user(pr)
        with self._lock:
            room = self._rooms.get(room_id)
            m = self._member(room, pr.id) if room else None
            agent = self._agents.get(agent_id)
            if (not room or not m or not agent or agent_id not in room["grants"]
                    or not (m["role"] == "owner" or agent["owner"] == pr.id)):
                raise CollabError(404, "not found")
            self._commit("agent_grant_revoked", self._actor(
                pr, project_id=room["project_id"], room_id=room_id, agent_id=agent_id,
                action="agent.remove", outcome="allowed", project_ids=[room["project_id"]],
                resource_id=agent_id))
            own = self._helper_by_agent.get(agent_id)
            if own and own["room_id"] == room_id:        # a helper lives in one room: out of it, it is over
                self._end_agent(self._actor(pr), agent_id, "removed from room", set())
            else:
                self._end_dependents(self._actor(pr), agent_id, "removed from room", {agent_id}, room=room_id)
        return {"ok": True}

    STATE_WAIT_MAX = 10.0       # below the dashboard proxy's 15 s timeout

    MAX_STATE_WAITERS = 6                  # a tab switch leaves its old poll running for a few seconds

    def room_state(self, pr: Principal, room_id: str, since: int, cursor: int, wait: float) -> dict:
        """One request that replaces five pollers: returns at once when this room changed after
        `since`, otherwise holds (up to `wait` seconds) until it does. Unchanged = a tiny reply."""
        wait = max(0.0, min(float(wait), self.STATE_WAIT_MAX))
        deadline = time.monotonic() + wait
        with self._cv:
            self._authorize(pr, room_id, "read", "room.state")
            if self._state_waiters[pr.key] >= self.MAX_STATE_WAITERS:
                raise CollabError(429, "too many open room views")
            self._state_waiters[pr.key] += 1
        try:
            with self._cv:
                while True:
                    self._authorize(pr, room_id, "read", "room.state")        # access lost while waiting -> 404
                    if not self.still_valid(pr):
                        raise CollabError(401, "unauthorized")
                    cur = self._room_seq.get(room_id, 0)
                    left = deadline - time.monotonic()
                    if since < 0 or cur != since or left <= 0:
                        break
                    self._cv.wait(min(left, 2.0))
                if since >= 0 and cur == since:
                    return {"seq": cur, "changed": False}
            m = self._member(self._rooms[room_id], pr.id) if not pr.is_agent else None
            guest = bool(m and m["role"] == "guest")
            out: dict[str, Any] = {"seq": cur, "changed": True, **self.read_events(pr, room_id, cursor, 200)}
            out["agents"] = [] if guest else self.list_room_agents(pr, room_id)["agents"]
            out["tasks"] = self.list_tasks(pr, room_id)["tasks"]
            out["flow"] = None if guest else self.room_flow(pr, room_id)
            with self._lock:                      # ask quietly: a denied call would log an event and wake every waiter
                eff = self._effective(pr, self._rooms[room_id])
                may_review = not pr.is_agent and bool(eff and "invite" in eff[0])
            out["requests"] = self.list_agent_requests(pr, room_id)["requests"] if may_review else None
            return out
        finally:
            with self._cv:
                self._state_waiters[pr.key] -= 1

    FLOW_TASKS = 60

    def room_flow(self, pr: Principal, room_id: str) -> dict:
        """Who hands work to whom in this room, for the live flow view: people and agents as nodes,
        tasks aggregated into edges. A fabric session id (instance_id) is only ever shown to the
        agent's own owner; everyone else just sees names, families and permissions."""
        with self._lock:
            room, first = self._authorize(pr, room_id, "read", "task.flow")
            if not pr.is_agent and (self._member(room, pr.id) or {}).get("role") == "guest":
                raise CollabError(403, "forbidden")
            self._housekeeping()
            nodes: dict[str, dict] = {}

            def agent_node(aid: str) -> dict:
                key = "a:" + aid
                if key not in nodes:
                    a = self._agents.get(aid, {})
                    g = room["grants"].get(aid)
                    nodes[key] = {
                        "id": key, "kind": "agent", "agent_id": aid, "name": a.get("name", "removed"),
                        "family": a.get("harness_type"), "owner_user_id": a.get("owner"),
                        "owner_email": self._user_email.get(a.get("owner")),
                        "actions": list(g["actions"]) if g else [],
                        "status": "active" if g and a.get("status") == "active" else "removed",
                        "instance_id": a.get("instance_id") if a.get("owner") == pr.id else None,
                        "parent_agent_id": a.get("parent") if a.get("parent") in room["grants"] else None,
                        "group": _herd_group(a.get("name", ""))}
                return nodes[key]

            def user_node(uid: str) -> dict:
                key = "u:" + uid
                if key not in nodes:
                    m = self._member(room, uid)
                    nodes[key] = {"id": key, "kind": "person", "user_id": uid, "name": self._user_email.get(uid) or "…" + uid[-6:],
                                  "role": m["role"] if m else None, "status": "active" if m else "removed"}
                return nodes[key]

            for aid in room["grants"]:
                agent_node(aid)
            edges: dict[tuple, dict] = {}
            rows = sorted(((t["seq"], tid, t) for tid, t in self._tasks.items()
                           if t["room_id"] == room_id and t["seq"] >= first), key=lambda r: r[0], reverse=True)
            for _, tid, t in rows:
                src = (user_node(t["by_id"]) if t["by_kind"] == "user" else agent_node(t["by_id"]))["id"]
                dst = agent_node(t["target"])["id"]
                e = edges.setdefault((src, dst), {"from": src, "to": dst, "counts": defaultdict(int), "last": ""})
                e["counts"][self._task_status(t)] += 1
                e["last"] = max(e["last"], t["updated_at"])
            recent = [{"task_id": tid, "from": ("u:" if t["by_kind"] == "user" else "a:") + t["by_id"],
                       "to": "a:" + t["target"], "status": self._task_status(t), "depth": t["depth"],
                       "parent_task_id": t["parent"], "updated_at": t["updated_at"], "text": t["text"][:140]}
                      for _, tid, t in rows[:self.FLOW_TASKS]]
            spawns = [{"from": "a:" + n["parent_agent_id"], "to": n["id"]} for n in nodes.values()
                      if n["kind"] == "agent" and n.get("parent_agent_id") and "a:" + n["parent_agent_id"] in nodes]
        return {"room_id": room_id, "seq": self._seq, "nodes": list(nodes.values()),
                "edges": [{**e, "counts": dict(e["counts"])} for e in edges.values()],
                "spawns": spawns, "tasks": recent, "untrusted_text": True}

    def rename_agent(self, pr: Principal, agent_id: str, body: dict) -> dict:
        """Give an agent a new @name. The owner (or the dashboard operator) may; the name must stay unique
        among the agent's own rooms and the owner's other live agents, since people address agents by it.
        `source` records who asked ("herdr" when the runner follows a rename made in a herdr pane or tab);
        old @mentions in past messages are plain text and stay as they were."""
        self._require_user(pr)
        name = _handle(body.get("name"))
        source = "herdr" if body.get("source") == "herdr" and pr.admin else "web"
        with self._lock:
            agent = self._agents.get(agent_id)
            if not agent or agent["status"] != "active" or not (agent["owner"] == pr.id or pr.admin):
                raise CollabError(404, "not found")
            if agent["name"] == name:
                return {"agent_id": agent_id, "name": name, "changed": False}
            self._throttle("rename:" + pr.id, 120, 600.0, "agent rename")
            for aid in self._agents_by_owner.get(agent["owner"], ()):
                other = self._agents[aid]
                if aid != agent_id and other["status"] == "active" and other["name"] == name:
                    raise CollabError(409, "you already have an agent with that name")
            for rid in self._rooms_by_agent.get(agent_id, ()):
                if agent_id in self._rooms[rid]["grants"]:
                    self._name_free(self._rooms[rid], agent_id, name)
            rooms = [r for r in self._rooms_by_agent.get(agent_id, ()) if agent_id in self._rooms[r]["grants"]]
            self._commit("agent_renamed", self._actor(
                pr, agent_id=agent_id, name=name, old_name=agent["name"], source=source, room_ids=rooms,
                action="agent.rename", outcome="allowed", project_ids=[], resource_id=agent_id))
        return {"agent_id": agent_id, "name": name, "changed": True}

    def agent_links(self, pr: Principal) -> dict:
        """Operator only: every live agent that is tied to a fabric session or herdr pane, so the host runner
        can keep their names in step with the herdr pane, tab and workspace labels."""
        with self._lock:
            # only the operator's own agents: someone else's agent must not be able to point the runner at a pane
            out = [{"agent_id": aid, "name": a["name"], "harness_type": a["harness_type"], "instance_id": a["instance_id"],
                    "parent_agent_id": a.get("parent")}
                   for aid, a in self._agents.items()
                   if a["status"] == "active" and a.get("instance_id") and a["owner"] == pr.id]
        return {"agents": out[:1000]}

    def link_agent(self, pr: Principal, agent_id: str, body: dict) -> dict:
        """The owner ties one of their agents to a fabric session (or clears it). The id is only a
        label here; the dashboard resolves it against sessions it knows about."""
        self._require_user(pr)
        inst = body.get("instance_id")
        if inst is not None and (not isinstance(inst, str) or not inst or len(inst) > 200
                                 or _NAME_BAD.search(inst)):
            raise CollabError(400, "invalid instance_id")
        with self._lock:
            agent = self._agents.get(agent_id)
            if not agent or agent["owner"] != pr.id or agent["status"] != "active":
                raise CollabError(404, "not found")
            self._throttle("link:" + pr.id, 120, 600.0, "agent link")
            self._commit("agent_linked", self._actor(
                pr, agent_id=agent_id, instance_id=inst, action="agent.link", outcome="allowed",
                project_ids=[], resource_id=agent_id))
        return {"agent_id": agent_id, "instance_id": inst}

    def list_room_agents(self, pr: Principal, room_id: str) -> dict:
        """Capability summaries only: no device ids, hostnames, paths or instance ids."""
        with self._lock:
            room, _ = self._authorize(pr, room_id, "read", "agent.list")
            self._housekeeping()
            # A roster (who owns which agent): never for guests, like the member list.
            if not pr.is_agent and (self._member(room, pr.id) or {}).get("role") == "guest":
                raise CollabError(403, "forbidden")
            out = []
            for aid, g in room["grants"].items():
                a = self._agents.get(aid)
                if a and a["status"] == "active":
                    out.append({"agent_id": aid, "name": a["name"], "harness_type": a["harness_type"],
                                "owner_user_id": a["owner"], "actions": g["actions"]})
        return {"agents": out}

    # -- tasks (work for agents)
    def _task_status(self, t: dict) -> str:
        if t["status"] in ("pending", "accepted") and self._now() - t["created_epoch"] >= TASK_TTL:
            return "expired"
        return t["status"]

    def _task_text(self, value: Any, what: str, required: bool = True) -> str | None:
        if value is None and not required:
            return None
        if not isinstance(value, str):
            raise CollabError(400, f"{what} must be a string")
        clean = _CONTROL.sub("", value)
        if (required and not clean.strip()) or len(clean) > MAX_TASK_TEXT:
            raise CollabError(400, f"{what} must be 1-{MAX_TASK_TEXT} characters")
        return redact_text(clean)

    def _throttle(self, key: str, limit: int, window: float, what: str) -> None:
        now = self._now()
        hits = [t for t in self._hits[key] if now - t < window]
        if len(hits) >= limit:
            self._hits[key] = hits
            self.stats["rate_limited"] += 1
            raise CollabError(429, f"{what} rate limit exceeded")
        hits.append(now)
        self._hits[key] = hits

    def create_task(self, pr: Principal, room_id: str, body: dict) -> dict:
        """A person with the room's "task" right, or an agent the owner approved with "instruct",
        gives work to ONE agent that the owner approved with "execute"."""
        with self._lock:
            room, first = self._authorize(pr, room_id, "instruct" if pr.is_agent else "task", "task.create")
            bad = [k for k in body if k not in ("target_agent_id", "text", "parent_task_id", "idempotency_key")]
            if bad:
                raise CollabError(400, "unknown field")
            tid_ = body.get("target_agent_id")
            target = self._agents.get(tid_) if isinstance(tid_, str) else None
            teff = (self._effective(Principal("agent", tid_, target["owner"]), room) if target else None)
            if not teff or "execute" not in teff[0]:
                raise CollabError(404, "no such agent in this room")     # same answer for every miss
            if pr.is_agent and tid_ == pr.id:
                raise CollabError(400, "an agent cannot give itself work")
            text = self._task_text(body.get("text"), "text")
            ikey = body.get("idempotency_key")
            if ikey is not None and (not isinstance(ikey, str) or not _KEY_RE.match(ikey)):
                raise CollabError(400, "invalid idempotency_key")
            depth, parent, root = 0, body.get("parent_task_id"), pr.owner
            if pr.is_agent:
                # Agents only delegate: every agent-made task traces to a task that someone gave
                # this agent and it accepted, so a chain always starts with a person.
                pt = self._tasks.get(parent) if isinstance(parent, str) else None
                if (not pt or pt["room_id"] != room_id or pt["target"] != pr.id
                        or pt["seq"] < first or self._task_status(pt) != "accepted"
                        or not self._creator_valid(pt, room)):
                    raise CollabError(400, "agents delegate: parent_task_id must be a task you accepted in this room")
                if pt["by_kind"] == "agent" and pt["by_id"] == tid_:
                    raise CollabError(400, "cannot hand a task straight back to the agent that gave it")
                depth, root = pt["depth"] + 1, pt["root"]
                if depth > MAX_TASK_DEPTH:
                    raise CollabError(400, "task chain is too deep")
            elif parent is not None:
                raise CollabError(400, "parent_task_id is for agents delegating a task")
            fingerprint = _digest(json.dumps([room_id, tid_, text, parent]))
            if ikey is not None and (prior := self._idem.get((pr.key, room_id, "task:" + ikey))):
                if prior[0] != fingerprint:
                    raise CollabError(409, "idempotency_key already used for a different task")
                return {"task_id": prior[1], "status": "pending", "replayed": True}
            self._prune()
            now = self._now()
            open_for_target = sum(1 for tid2 in self._tasks_by_target.get(tid_, ())
                                  if self._task_status(self._tasks[tid2]) in ("pending", "accepted"))
            if open_for_target >= MAX_OPEN_TASKS_PER_AGENT:
                raise CollabError(429, "that agent has too many open tasks")
            self._throttle("task:" + pr.key, TASK_RATE, 600.0, "task")
            self._throttle("task-owner:" + pr.owner, TASK_RATE * 2, 600.0, "task")
            task_id = ids.new_id("grant")
            ev = self._commit("task_created", self._actor(
                pr, project_id=room["project_id"], room_id=room_id, task_id=task_id,
                target_agent_id=tid_, text=text, depth=depth, parent_task_id=parent,
                created_by_kind=pr.kind, created_by_id=pr.id, created_epoch=now, root_user_id=root,
                idempotency_key=ikey, fingerprint=fingerprint, requester_key=pr.key,
                visibility_scope="room", action="task.create", outcome="allowed",
                project_ids=[room["project_id"]], resource_id=task_id))
        return {"task_id": task_id, "status": "pending", "event": self._view(ev)}

    # -- task-scoped requests for human approval (requests only; never grants)
    @staticmethod
    def _approval_proposal_json(proposal: dict) -> dict:
        return {
            "proposal_id": proposal["proposal_id"], "room_id": proposal["room_id"],
            "task_id": proposal["task_id"], "kind": proposal["kind"],
            "summary": proposal["summary"],
            "details": json.loads(json.dumps(proposal["details"], ensure_ascii=False)),
            "state": "pending", "created_at": proposal["created_at"],
            "requester": {"type": proposal["requester_kind"], "id": proposal["requester_id"],
                          "owner_user_id": proposal["owner"]},
        }

    def _approval_relationship(self, pr: Principal, room: dict, task: dict) -> bool:
        """A proposal stays inside the user who owns the task's assigned agent."""
        target = self._agents.get(task["target"])
        if not target or target["owner"] != pr.owner:
            return False
        effective = self._effective(Principal("agent", task["target"], target["owner"]), room)
        if not effective or "execute" not in effective[0] or task["seq"] < effective[1]:
            return False
        if pr.is_agent:
            return task["target"] == pr.id
        return self._member(room, pr.id) is not None

    def create_approval_proposal(self, pr: Principal, room_id: str, body: dict) -> tuple[int, dict]:
        """Record a pending request for approval without creating executable authority."""
        with self._lock:
            room, first = self._authorize(pr, room_id, "read", "approval_proposal.create")
            allowed = {"task_id", "kind", "summary", "details", "idempotency_key"}
            if not isinstance(body, dict) or set(body) - allowed:
                raise CollabError(400, "unknown field")
            task_id = body.get("task_id")
            task = self._tasks.get(task_id) if isinstance(task_id, str) else None
            if (not task or task["room_id"] != room_id or task["seq"] < first
                    or self._task_status(task) not in ("pending", "accepted")
                    or not self._approval_relationship(pr, room, task)):
                raise CollabError(404, "not found")
            if pr.is_agent:
                effective = self._effective(pr, room)
                if not effective or "execute" not in effective[0] or task["seq"] < effective[1]:
                    raise CollabError(404, "not found")
            else:
                member = self._member(room, pr.id)
                if not member or not (member["role"] == "owner" or task["root"] == pr.id
                                      or task["by_owner"] == pr.id):
                    raise CollabError(404, "not found")

            kind, summary = body.get("kind"), body.get("summary")
            if not isinstance(kind, str) or not _KEY_RE.fullmatch(kind) or len(kind) > 64:
                raise CollabError(400, "invalid kind")
            if not isinstance(summary, str):
                raise CollabError(400, "summary must be a string")
            summary = redact_text(_CONTROL.sub("", summary)).strip()
            if not summary or len(summary) > MAX_APPROVAL_SUMMARY:
                raise CollabError(400, f"summary must be 1-{MAX_APPROVAL_SUMMARY} characters")
            details = body.get("details")
            if not isinstance(details, dict):
                raise CollabError(400, "details must be an object")
            try:
                details_json = json.dumps(details, ensure_ascii=False, sort_keys=True,
                                          separators=(",", ":"), allow_nan=False)
            except (TypeError, ValueError):
                raise CollabError(400, "details must be a JSON object") from None
            if len(details_json.encode()) > MAX_APPROVAL_DETAILS:
                raise CollabError(400, "details are too large")
            details = json.loads(details_json)

            ikey = body.get("idempotency_key")
            if not isinstance(ikey, str) or not _KEY_RE.fullmatch(ikey):
                raise CollabError(400, "invalid idempotency_key")
            requester_key = pr.key
            fingerprint = _digest(json.dumps(
                [room_id, task_id, kind, summary, details], ensure_ascii=False,
                sort_keys=True, separators=(",", ":")))
            prior = self._approval_proposal_idem.get((requester_key, room_id, ikey))
            if prior:
                if prior[0] != fingerprint:
                    raise CollabError(409, "idempotency_key already used for a different proposal")
                proposal = self._approval_proposals[prior[1]]
                # Replay never bypasses current room, task or agent authority.
                if not self._approval_relationship(pr, room, task):
                    raise CollabError(404, "not found")
                return 200, {"proposal": self._approval_proposal_json(proposal), "replayed": True}

            count = sum(1 for p in self._approval_proposals.values()
                        if p["room_id"] == room_id and p["owner"] == pr.owner)
            if count >= MAX_APPROVAL_PROPOSALS_PER_OWNER:
                raise CollabError(429, "approval proposal limit reached")
            self._throttle("approval-proposal:" + requester_key,
                           APPROVAL_PROPOSAL_RATE, 600.0, "approval proposal")
            proposal_id = ids.new_id("proposal")
            ev = self._commit("approval_proposal_created", self._actor(
                pr, project_id=room["project_id"], room_id=room_id,
                proposal_id=proposal_id, task_id=task_id, kind=kind, summary=summary,
                details=details, requester_kind=pr.kind, requester_id=pr.id,
                idempotency_key=ikey, fingerprint=fingerprint, visibility_scope="owner",
                room_notify=False,
                action="approval_proposal.create", outcome="allowed",
                project_ids=[room["project_id"]], resource_id=proposal_id))
            proposal = self._approval_proposals[proposal_id]
        return 201, {"proposal": self._approval_proposal_json(proposal),
                     "event_id": ev["event_id"]}

    def list_approval_proposals(self, pr: Principal, room_id: str) -> dict:
        with self._lock:
            room, first = self._authorize(pr, room_id, "read", "approval_proposal.list")
            if pr.is_agent:
                effective = self._effective(pr, room)
                if not effective or "execute" not in effective[0]:
                    raise CollabError(404, "not found")
            rows = []
            for proposal in self._approval_proposals.values():
                if (proposal["room_id"] != room_id or proposal["owner"] != pr.owner
                        or proposal["sequence"] < first):
                    continue
                task = self._tasks.get(proposal["task_id"])
                if not task or not self._approval_relationship(pr, room, task):
                    continue
                if pr.is_agent and proposal["requester_id"] != pr.id:
                    continue
                rows.append(proposal)
            rows.sort(key=lambda p: p["sequence"], reverse=True)
        return {"proposals": [self._approval_proposal_json(p) for p in rows]}

    # -- immutable assistant package definitions (definitions only; never authority)
    def _assistant_package_owner(self, pr: Principal, room_id: str, action: str) -> dict:
        self._require_user(pr)
        if not self._ok(pr.id):
            raise CollabError(404, "not found")
        room, _ = self._authorize(pr, room_id, "read", action)
        member = self._member(room, pr.id)
        if not member or member["role"] != "owner":
            self._audit_denied(pr, action, "not_owner", room_id=room_id,
                               project_ids=[room["project_id"]])
            self.stats["policy_denied"] += 1
            raise CollabError(403, "forbidden")
        return room

    @staticmethod
    def _assistant_package_json(package: dict) -> dict:
        return copy.deepcopy(package)

    def create_assistant_package(self, pr: Principal, room_id: str,
                                 body: dict) -> tuple[int, dict]:
        """Register one private immutable definition; this cannot launch or grant capabilities."""
        with self._lock:
            room = self._assistant_package_owner(pr, room_id, "assistant_package.create")
            if not isinstance(body, dict) or set(body) != {"manifest"}:
                raise CollabError(400, "request must contain exactly manifest")
            try:
                record = agent_package_record(body["manifest"])
            except ManifestError:
                raise CollabError(400, "invalid assistant package manifest") from None
            manifest = record["manifest"]
            version_key = (room_id, pr.id, manifest["name"], manifest["version"])
            prior_id = self._assistant_package_versions.get(version_key)
            if prior_id is not None:
                prior = self._assistant_packages[(room_id, pr.id, prior_id)]
                if prior["digest"] != record["digest"]:
                    raise CollabError(409, "assistant package name and version are immutable")
                return 200, {"package": self._assistant_package_json(prior), "replayed": True}
            count = sum(1 for rid, owner, _ in self._assistant_packages
                        if rid == room_id and owner == pr.id)
            if count >= MAX_ASSISTANT_PACKAGES_PER_OWNER_ROOM:
                raise CollabError(429, "assistant package limit reached")
            self._throttle("assistant-package:" + pr.id,
                           ASSISTANT_PACKAGE_RATE, 600.0, "assistant package")
            digest_hex = record["digest"].removeprefix("sha256:")
            package = {
                "package_id": "package_" + digest_hex,
                **record,
                "status": "definition-only",
                "created_at": self._iso(),
            }
            self._commit("assistant_package_registered", self._actor(
                pr, project_id=room["project_id"], room_id=room_id,
                package=copy.deepcopy(package), visibility_scope="owner", room_notify=False,
                action="assistant_package.register", outcome="allowed",
                project_ids=[room["project_id"]], resource_id=room_id))
        return 201, {"package": self._assistant_package_json(package)}

    def list_assistant_packages(self, pr: Principal, room_id: str) -> dict:
        with self._lock:
            self._assistant_package_owner(pr, room_id, "assistant_package.list")
            rows = [package for (rid, owner, _), package in self._assistant_packages.items()
                    if rid == room_id and owner == pr.id]
            rows.sort(key=lambda p: (p["manifest"]["name"], p["manifest"]["version"],
                                     p["package_id"]))
        return {"packages": [self._assistant_package_json(p) for p in rows]}

    def get_assistant_package(self, pr: Principal, room_id: str,
                              package_id: str) -> dict:
        with self._lock:
            self._assistant_package_owner(pr, room_id, "assistant_package.get")
            package = self._assistant_packages.get((room_id, pr.id, package_id))
            if package is None:
                raise CollabError(404, "not found")
        return {"package": self._assistant_package_json(package)}

    # -- attested Pi package activation
    @staticmethod
    def _assistant_runtime_limits(value: Any) -> dict:
        fields = {"cpu_quota_percent", "memory_bytes", "process_count", "wall_time_seconds"}
        if not isinstance(value, dict) or set(value) != fields:
            raise CollabError(400, "invalid runtime limits")
        ranges = {
            "cpu_quota_percent": (10, 800), "memory_bytes": (268_435_456, 17_179_869_184),
            "process_count": (16, 1024), "wall_time_seconds": (30, 86_400),
        }
        out = {}
        for key, (minimum, maximum) in ranges.items():
            got = value.get(key)
            if not isinstance(got, int) or isinstance(got, bool) or not minimum <= got <= maximum:
                raise CollabError(400, "invalid runtime limits")
            out[key] = got
        return out

    @staticmethod
    def _assistant_role_bindings(value: Any, limit: int = 32) -> list[dict]:
        if not isinstance(value, list) or len(value) > limit:
            raise CollabError(400, "invalid role bindings")
        out, seen = [], set()
        for item in value:
            if (not isinstance(item, dict) or set(item) != {"id", "version", "digest"}
                    or not isinstance(item.get("id"), str) or not item["id"].startswith("procedure:")
                    or not _KEY_RE.fullmatch(item["id"])
                    or not isinstance(item.get("version"), str)
                    or not _RUNTIME_VERSION.fullmatch(item["version"])
                    or not isinstance(item.get("digest"), str)
                    or not _RUNTIME_DIGEST.fullmatch(item["digest"])):
                raise CollabError(400, "invalid role binding")
            key = (item["id"], item["version"])
            if key in seen:
                raise CollabError(400, "role bindings must be unique")
            seen.add(key)
            out.append({"id": item["id"], "version": item["version"], "digest": item["digest"]})
        out.sort(key=lambda item: (item["id"], item["version"], item["digest"]))
        return out

    def _normalize_assistant_runtime_attestation(self, body: Any) -> dict:
        fields = {"schema_version", "host_id", "profile", "profile_digest", "runtime",
                  "enforcement", "approved_models", "issued_at", "expires_at", "nonce",
                  "role_bindings"}
        if not isinstance(body, dict) or set(body) != fields or body.get("schema_version") != 1:
            raise CollabError(400, "invalid runtime host attestation")
        host_id = self._require_host(body.get("host_id"))
        profile, profile_digest = body.get("profile"), body.get("profile_digest")
        runtime, enforcement = body.get("runtime"), body.get("enforcement")
        if profile != "task-restricted-v1" or not isinstance(profile_digest, str) \
                or not _RUNTIME_DIGEST.fullmatch(profile_digest):
            raise CollabError(400, "invalid runtime host attestation")
        if (not isinstance(runtime, dict)
                or set(runtime) != {"kind", "artifact_digest", "entrypoint_id"}
                or runtime.get("kind") != "pi" or runtime.get("entrypoint_id") != "pi-fixed-v1"
                or not isinstance(runtime.get("artifact_digest"), str)
                or not _RUNTIME_DIGEST.fullmatch(runtime["artifact_digest"])):
            raise CollabError(400, "invalid runtime host attestation")
        enforcement_fields = {"bubblewrap", "cgroup_v2", "broker_only", "task_only",
                              "no_new_privileges", "limits"}
        if (not isinstance(enforcement, dict) or set(enforcement) != enforcement_fields
                or any(enforcement.get(key) is not True for key in enforcement_fields - {"limits"})):
            raise CollabError(400, "invalid runtime host enforcement")
        limits = self._assistant_runtime_limits(enforcement["limits"])
        models = _profile_strings(body.get("approved_models"), "approved_models", _KEY_RE, 100, 128)
        if not models:
            raise CollabError(400, "approved_models must not be empty")
        issued, expires, now = body.get("issued_at"), body.get("expires_at"), self._now()
        if (not isinstance(issued, (int, float)) or isinstance(issued, bool) or not math.isfinite(issued)
                or not isinstance(expires, (int, float)) or isinstance(expires, bool) or not math.isfinite(expires)
                or issued > now + 5 or now - issued > ASSISTANT_RUNTIME_ATTEST_TTL
                or expires <= now or expires <= issued or expires > now + ASSISTANT_RUNTIME_ATTEST_TTL):
            raise CollabError(400, "invalid runtime host attestation time")
        nonce = body.get("nonce")
        if not isinstance(nonce, str) or not 16 <= len(nonce) <= 128 or not _KEY_RE.fullmatch(nonce):
            raise CollabError(400, "invalid runtime host attestation nonce")
        return {
            "schema_version": 1, "host_id": host_id, "profile": profile,
            "profile_digest": profile_digest, "runtime": copy.deepcopy(runtime),
            "enforcement": {**{key: True for key in enforcement_fields - {"limits"}},
                            "limits": limits},
            "approved_models": sorted(models), "issued_at": float(issued),
            "expires_at": float(expires), "nonce": nonce,
            "role_bindings": self._assistant_role_bindings(body.get("role_bindings")),
        }

    @staticmethod
    def _assistant_runtime_host_json(record: dict, now: float, enabled: bool = True) -> dict:
        status = "stale" if now >= record["expires_at"] else (
            "unavailable" if record.get("used") or not enabled else "ready")
        return {
            "host_id": record["host_id"], "label": record["host_id"], "status": status,
            "attested_at": record["issued_at"], "expires_at": record["expires_at"],
            "supported_runtime_kinds": ["pi"], "sandbox_profiles": [record["profile"]],
            "models": list(record["approved_models"]),
            "role_bindings": copy.deepcopy(record["role_bindings"]),
        }

    def attest_assistant_runtime_host(self, pr: Principal, body: dict) -> tuple[int, dict]:
        self._require_operator(pr)
        attestation = self._normalize_assistant_runtime_attestation(body)
        with self._lock:
            prior = self._assistant_runtime_hosts.get(attestation["host_id"])
            if prior and prior["nonce"] == attestation["nonce"]:
                comparable = {k: v for k, v in prior.items() if k != "used"}
                if comparable != attestation:
                    raise CollabError(409, "attestation nonce already used for different content")
                return 200, {"host": self._assistant_runtime_host_json(
                    prior, self._now(), self._fleet_flags.get(prior["host_id"], True)),
                    "replayed": True}
            if len(self._assistant_runtime_hosts) >= MAX_ASSISTANT_RUNTIME_HOSTS \
                    and attestation["host_id"] not in self._assistant_runtime_hosts:
                raise CollabError(429, "too many runtime hosts")
            self._commit("assistant_runtime_host_attested", self._actor(
                pr, attestation=copy.deepcopy(attestation), action="assistant_runtime_host.attest",
                outcome="allowed", project_ids=[], resource_id=attestation["host_id"]))
            return 201, {"host": self._assistant_runtime_host_json(
                self._assistant_runtime_hosts[attestation["host_id"]], self._now(),
                self._fleet_flags.get(attestation["host_id"], True))}

    def list_assistant_runtime_hosts(self, pr: Principal, room_id: str) -> dict:
        with self._lock:
            self._assistant_package_owner(pr, room_id, "assistant_runtime_host.list")
            now = self._now()
            rows = [self._assistant_runtime_host_json(
                record, now, self._fleet_flags.get(host_id, True))
                for host_id, record in self._assistant_runtime_hosts.items()]
            rows.sort(key=lambda row: row["host_id"])
            return {"hosts": rows}

    @staticmethod
    def _assistant_runtime_json(runtime: dict) -> dict:
        return {
            "runtime_id": runtime["runtime_id"], "package_id": runtime["package_id"],
            "package_digest": runtime["package_digest"], "host_id": runtime["host_id"],
            "name": runtime["name"], "selected_model": runtime["selected_model"],
            "status": runtime["status"], "agent_id": runtime["agent_id"],
            "created_at": runtime["created_at"], "updated_at": runtime["updated_at"],
            "launch_attestation": copy.deepcopy(runtime["launch_attestation"]),
            "cancel_requested": runtime["cancel_requested"], "access": ["execute", "read"],
            "checkpoint": copy.deepcopy(runtime["checkpoint"]),
            "error_code": runtime["error_code"], "lease_expires_at": runtime["lease_expires_at"],
        }

    def _assistant_runtime_package(self, runtime: dict) -> dict | None:
        return self._assistant_packages.get((runtime["room_id"], runtime["owner"], runtime["package_id"]))

    def _assistant_runtime_policy(self, runtime: dict, *, claiming: bool = True) -> tuple[dict, dict]:
        room = self._rooms.get(runtime["room_id"])
        member = self._member(room, runtime["owner"]) if room else None
        package = self._assistant_runtime_package(runtime)
        host = self._assistant_runtime_hosts.get(runtime["host_id"])
        if (not self._ok(runtime["owner"]) or not member or member["role"] != "owner"
                or not package or package["digest"] != runtime["package_digest"]
                or package["manifest"]["runtime"]["kind"] != "pi"
                or self._fleet_flags.get(runtime["host_id"], True) is False):
            raise CollabError(409, "runtime policy changed")
        if claiming:
            if (not host or host["nonce"] != runtime["attestation_nonce"] or host.get("used")
                    or self._now() >= host["expires_at"]):
                raise CollabError(409, "runtime policy changed")
        else:
            host = runtime["attestation"]
            aid = runtime.get("agent_id")
            agent = self._agents.get(aid) if aid else None
            grant = room["grants"].get(aid) if room and aid else None
            if (not agent or agent["status"] != "active" or not grant
                    or sorted(grant["actions"]) != ["execute", "read"]):
                raise CollabError(409, "runtime policy changed")
        manifest = package["manifest"]
        artifact_digest = manifest["runtime"]["artifact"]["digest"]
        profile = manifest["sandbox"]["profile"]
        limits = manifest["sandbox"]["limits"]
        required_roles = self._assistant_role_bindings(manifest["references"]["procedures"], 8)
        room_models = self._profile(runtime["room_id"])["allowed_models"]
        if (host["runtime"]["artifact_digest"] != artifact_digest or host["profile"] != profile
                or host["enforcement"]["limits"] != limits
                or runtime["selected_model"] not in host["approved_models"]
                or runtime["selected_model"] not in manifest["model_policy"]["allowed"]
                or any(role not in host["role_bindings"] for role in required_roles)
                or (room_models is not None and runtime["selected_model"] not in room_models)):
            raise CollabError(409, "runtime package is not supported by this host")
        return package, host

    def create_assistant_runtime(self, pr: Principal, room_id: str,
                                 body: dict) -> tuple[int, dict]:
        fields = {"package_id", "host_id", "name", "idempotency_key", "selected_model"}
        required = fields - {"selected_model"}
        if not isinstance(body, dict) or set(body) - fields or not required <= set(body):
            raise CollabError(400, "invalid assistant runtime request")
        with self._lock:
            room = self._assistant_package_owner(pr, room_id, "assistant_runtime.create")
            self._expire_assistant_runtimes()
            package_id, host_id = body.get("package_id"), body.get("host_id")
            if not isinstance(package_id, str) or not package_id.startswith("package_"):
                raise CollabError(400, "invalid package_id")
            host_id = self._require_host(host_id)
            name, key = _handle(body.get("name")), body.get("idempotency_key")
            if not isinstance(key, str) or not _KEY_RE.fullmatch(key):
                raise CollabError(400, "invalid idempotency_key")
            package = self._assistant_packages.get((room_id, pr.id, package_id))
            if package is None:
                raise CollabError(404, "not found")
            manifest = package["manifest"]
            if manifest["runtime"]["kind"] != "pi":
                raise CollabError(409, "this runtime kind is not launchable")
            model = body.get("selected_model", manifest["model_policy"]["default"])
            if not isinstance(model, str) or model not in manifest["model_policy"]["allowed"]:
                raise CollabError(409, "selected model is not allowed by the package")
            host = self._assistant_runtime_hosts.get(host_id)
            candidate = {"room_id": room_id, "owner": pr.id, "package_id": package_id,
                         "package_digest": package["digest"], "host_id": host_id,
                         "name": name, "selected_model": model}
            fingerprint = _digest(json.dumps(candidate, sort_keys=True, separators=(",", ":")))
            prior = self._assistant_runtime_idem.get((pr.id, room_id, key))
            if prior:
                if prior[0] != fingerprint:
                    raise CollabError(409, "idempotency_key already used for a different runtime")
                return 200, {"runtime": self._assistant_runtime_json(
                    self._assistant_runtimes[prior[1]]), "replayed": True}
            if host is None:
                raise CollabError(409, "runtime host is not attested")
            probe = {**candidate, "attestation_nonce": host["nonce"]}
            probe_runtime = {**probe, "project_id": room["project_id"]}
            self._assistant_runtime_policy(probe_runtime)
            live = [runtime for runtime in self._assistant_runtimes.values()
                    if runtime["owner"] == pr.id and runtime["status"] not in _RUNTIME_TERMINAL]
            if len(live) >= MAX_ASSISTANT_RUNTIMES_PER_OWNER:
                raise CollabError(429, "too many assistant runtimes")
            taken = {a["name"] for aid in self._agents_by_owner.get(pr.id, ())
                     if (a := self._agents[aid])["status"] == "active"}
            taken |= {runtime["name"] for runtime in live}
            if name in taken:
                raise CollabError(409, "agent name is already in use")
            self._throttle("assistant-runtime:" + pr.id,
                           ASSISTANT_RUNTIME_RATE, 3600.0, "assistant runtime")
            runtime_id, now = ids.new_id("collab"), self._now()
            self._commit("assistant_runtime_requested", self._actor(
                pr, runtime_id=runtime_id, room_id=room_id, project_id=room["project_id"],
                package_id=package_id, package_digest=package["digest"], host_id=host_id,
                name=name, selected_model=model, attestation_nonce=host["nonce"],
                attestation=copy.deepcopy(host), idempotency_key=key, fingerprint=fingerprint,
                created_epoch=now, visibility_scope="owner", room_notify=False,
                action="assistant_runtime.request", outcome="allowed",
                project_ids=[room["project_id"]], resource_id=room_id))
            return 202, {"runtime": self._assistant_runtime_json(self._assistant_runtimes[runtime_id])}

    def _expire_assistant_runtimes(self) -> None:
        now = self._now()
        actor = {"actor_user_id": None, "actor_agent_id": None}
        for runtime in list(self._assistant_runtimes.values()):
            error = None
            if runtime["status"] == "queued":
                host = self._assistant_runtime_hosts.get(runtime["host_id"])
                if not host or host["nonce"] != runtime["attestation_nonce"] or now >= host["expires_at"]:
                    error = "host_attestation_expired"
            elif runtime["status"] in _RUNTIME_LIVE and now >= (runtime["lease_expires_at"] or -1):
                error = "runtime_lease_expired"
            if error:
                self._commit("assistant_runtime_expired", {
                    **actor, "runtime_id": runtime["runtime_id"], "room_id": runtime["room_id"],
                    "project_id": runtime["project_id"], "error_code": error, "expired_epoch": now,
                    "visibility_scope": "owner", "room_notify": False,
                    "action": "assistant_runtime.expire", "outcome": "allowed",
                    "project_ids": [runtime["project_id"]], "resource_id": runtime["room_id"]})
                self._fail_assistant_runtime_tasks(actor, runtime, error)

    def _fail_assistant_runtime_tasks(self, actor: dict, runtime: dict, reason: str) -> None:
        aid = runtime.get("agent_id")
        if not aid:
            return
        for task_id in list(self._tasks_by_target.get(aid, ())):
            task = self._tasks[task_id]
            if self._task_status(task) not in {"pending", "accepted"}:
                continue
            self._commit("task_updated", {
                **actor, "project_id": runtime["project_id"], "room_id": runtime["room_id"],
                "task_id": task_id, "status": "failed", "result": f"runtime ended: {reason}"[:4000],
                "target_agent_id": aid, "visibility_scope": "room",
                "action": "assistant_runtime.task.fail", "outcome": "allowed",
                "project_ids": [runtime["project_id"]], "resource_id": task_id,
            })

    def list_assistant_runtimes(self, pr: Principal, room_id: str) -> dict:
        with self._lock:
            self._assistant_package_owner(pr, room_id, "assistant_runtime.list")
            self._expire_assistant_runtimes()
            rows = [runtime for runtime in self._assistant_runtimes.values()
                    if runtime["room_id"] == room_id and runtime["owner"] == pr.id]
            rows.sort(key=lambda runtime: (-runtime["created_epoch"], runtime["runtime_id"]))
            return {"runtimes": [self._assistant_runtime_json(runtime) for runtime in rows]}

    def get_assistant_runtime(self, pr: Principal, room_id: str, runtime_id: str) -> dict:
        with self._lock:
            self._assistant_package_owner(pr, room_id, "assistant_runtime.get")
            self._expire_assistant_runtimes()
            runtime = self._assistant_runtimes.get(runtime_id)
            if not runtime or runtime["room_id"] != room_id or runtime["owner"] != pr.id:
                raise CollabError(404, "not found")
            return {"runtime": self._assistant_runtime_json(runtime)}

    def cancel_assistant_runtime(self, pr: Principal, room_id: str, runtime_id: str,
                                 body: dict) -> dict:
        if body != {}:
            raise CollabError(400, "cancel request must be empty")
        with self._lock:
            room = self._assistant_package_owner(pr, room_id, "assistant_runtime.cancel")
            self._expire_assistant_runtimes()
            runtime = self._assistant_runtimes.get(runtime_id)
            if not runtime or runtime["room_id"] != room_id or runtime["owner"] != pr.id:
                raise CollabError(404, "not found")
            if runtime["status"] in _RUNTIME_TERMINAL or runtime["cancel_requested"]:
                return {"runtime": self._assistant_runtime_json(runtime)}
            self._commit("assistant_runtime_cancel_requested", self._actor(
                pr, runtime_id=runtime_id, room_id=room_id, project_id=room["project_id"],
                requested_epoch=self._now(), visibility_scope="owner", room_notify=False,
                action="assistant_runtime.cancel", outcome="allowed",
                project_ids=[room["project_id"]], resource_id=room_id))
            return {"runtime": self._assistant_runtime_json(runtime)}

    def list_assistant_runtime_queue(self, pr: Principal) -> dict:
        self._require_operator(pr)
        with self._lock:
            self._expire_assistant_runtimes()
            rows = [runtime for runtime in self._assistant_runtimes.values()
                    if runtime["status"] == "queued" and not runtime["cancel_requested"]]
            rows.sort(key=lambda runtime: (runtime["created_epoch"], runtime["runtime_id"]))
            return {"runtimes": [self._assistant_runtime_json(runtime) for runtime in rows]}

    def _fail_assistant_runtime_policy(self, pr: Principal, runtime: dict) -> None:
        self._commit("assistant_runtime_expired", self._actor(
            pr, runtime_id=runtime["runtime_id"], room_id=runtime["room_id"],
            project_id=runtime["project_id"], error_code="runtime_policy_changed",
            expired_epoch=self._now(), visibility_scope="owner", room_notify=False,
            action="assistant_runtime.policy", outcome="denied",
            project_ids=[runtime["project_id"]], resource_id=runtime["room_id"]))

    def claim_assistant_runtime(self, pr: Principal, runtime_id: str, body: dict) -> dict:
        self._require_operator(pr)
        if not isinstance(body, dict) or set(body) != {"host_id", "attestation_nonce"}:
            raise CollabError(400, "invalid runtime claim")
        host_id = self._require_host(body.get("host_id"))
        nonce = body.get("attestation_nonce")
        if not isinstance(nonce, str) or not _KEY_RE.fullmatch(nonce):
            raise CollabError(400, "invalid runtime claim")
        with self._lock:
            self._expire_assistant_runtimes()
            runtime = self._assistant_runtimes.get(runtime_id)
            if not runtime:
                raise CollabError(404, "not found")
            if runtime["status"] != "queued" or runtime["cancel_requested"]:
                raise CollabError(409, "runtime is not claimable")
            if runtime["host_id"] != host_id or runtime["attestation_nonce"] != nonce:
                raise CollabError(409, "runtime host attestation does not match")
            try:
                package, host = self._assistant_runtime_policy(runtime)
            except CollabError:
                self._fail_assistant_runtime_policy(pr, runtime)
                raise CollabError(409, "runtime policy changed") from None
            self._agent_slots(runtime["owner"])
            taken = {self._agents[aid]["name"] for aid in self._agents_by_owner.get(runtime["owner"], ())
                     if self._agents[aid]["status"] == "active"}
            if runtime["name"] in taken:
                self._fail_assistant_runtime_policy(pr, runtime)
                raise CollabError(409, "agent name is already in use")
            now = self._now()
            aid, credential = ids.new_id("agent"), "hca_" + secrets.token_urlsafe(32)
            device, claim_token = "runtime-" + secrets.token_hex(8), "hcr_" + secrets.token_urlsafe(32)
            lease = now + ASSISTANT_RUNTIME_LEASE
            self._commit("assistant_runtime_claimed", self._actor(
                pr, runtime_id=runtime_id, room_id=runtime["room_id"], project_id=runtime["project_id"],
                agent_id=aid, device_id=device, secret_digest=_digest(credential),
                credential_expires_at=now + self.remote_credential_ttl,
                claim_digest=_digest(claim_token), lease_expires_at=lease, claimed_epoch=now,
                visibility_scope="owner", room_notify=False, action="assistant_runtime.claim",
                outcome="allowed", project_ids=[runtime["project_id"]], resource_id=runtime["room_id"]))
            manifest = package["manifest"]
            return {
                "runtime": self._assistant_runtime_json(runtime), "credential": credential,
                "device_id": device, "claim_token": claim_token, "lease_expires_at": lease,
                "package": {"package_id": package["package_id"], "digest": package["digest"],
                            "runtime_artifact_digest": manifest["runtime"]["artifact"]["digest"],
                            "profile": manifest["sandbox"]["profile"],
                            "limits": copy.deepcopy(manifest["sandbox"]["limits"]),
                            "required_role_bindings": self._assistant_role_bindings(
                                manifest["references"]["procedures"], 8)},
                "selected_model": runtime["selected_model"], "actions": ["execute", "read"],
            }

    @staticmethod
    def _assistant_runtime_checkpoint(value: Any) -> dict:
        if (not isinstance(value, dict) or set(value) - {"sequence", "phase", "output_sha256"}
                or not {"sequence", "phase"} <= set(value)):
            raise CollabError(400, "invalid runtime checkpoint")
        sequence, phase, output = value.get("sequence"), value.get("phase"), value.get("output_sha256")
        if (not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1
                or not isinstance(phase, str) or not _KEY_RE.fullmatch(phase)
                or not (output is None or (isinstance(output, str) and _RUNTIME_DIGEST.fullmatch(output)))):
            raise CollabError(400, "invalid runtime checkpoint")
        return {"sequence": sequence, "phase": phase, "output_sha256": output}

    def _assistant_runtime_claim_auth(self, runtime_id: str, body: dict) -> tuple[dict, dict]:
        runtime = self._assistant_runtimes.get(runtime_id)
        if runtime is None:
            raise CollabError(404, "not found")
        claim_token, nonce = body.get("claim_token"), body.get("attestation_nonce")
        if (not isinstance(claim_token, str) or not secrets.compare_digest(
                runtime.get("claim_digest") or "", _digest(claim_token))
                or nonce != runtime["attestation_nonce"]):
            raise CollabError(404, "not found")
        return runtime, self._assistant_runtime_hosts.get(runtime["host_id"], {})

    def heartbeat_assistant_runtime(self, pr: Principal, runtime_id: str, body: dict) -> dict:
        self._require_operator(pr)
        if not isinstance(body, dict) or set(body) != {"claim_token", "attestation_nonce", "checkpoint"}:
            raise CollabError(400, "invalid runtime heartbeat")
        checkpoint = self._assistant_runtime_checkpoint(body.get("checkpoint"))
        with self._lock:
            self._expire_assistant_runtimes()
            runtime, _ = self._assistant_runtime_claim_auth(runtime_id, body)
            if runtime["status"] not in _RUNTIME_LIVE or runtime["cancel_requested"]:
                raise CollabError(409, "runtime cannot heartbeat")
            try:
                self._assistant_runtime_policy(runtime, claiming=False)
            except CollabError:
                self._fail_assistant_runtime_policy(pr, runtime)
                self._fail_assistant_runtime_tasks(self._actor(pr), runtime, "runtime_policy_changed")
                raise CollabError(409, "runtime policy changed") from None
            if runtime["checkpoint"] and checkpoint["sequence"] <= runtime["checkpoint"]["sequence"]:
                raise CollabError(409, "checkpoint sequence must increase")
            now, lease = self._now(), self._now() + ASSISTANT_RUNTIME_LEASE
            self._commit("assistant_runtime_heartbeat", self._actor(
                pr, runtime_id=runtime_id, room_id=runtime["room_id"], project_id=runtime["project_id"],
                checkpoint=checkpoint, lease_expires_at=lease, reported_epoch=now,
                visibility_scope="owner", room_notify=False, action="assistant_runtime.heartbeat",
                outcome="allowed", project_ids=[runtime["project_id"]], resource_id=runtime["room_id"]))
            return {"runtime": self._assistant_runtime_json(runtime)}

    @staticmethod
    def _assistant_runtime_launch_attestation(value: Any) -> dict:
        if (not isinstance(value, dict)
                or set(value) != {"profile_digest", "runtime_artifact_digest", "enforcement",
                                  "role_bindings"}
                or not isinstance(value.get("profile_digest"), str)
                or not _RUNTIME_DIGEST.fullmatch(value["profile_digest"])
                or not isinstance(value.get("runtime_artifact_digest"), str)
                or not _RUNTIME_DIGEST.fullmatch(value["runtime_artifact_digest"])):
            raise CollabError(400, "invalid launch attestation")
        out = copy.deepcopy(value)
        out["role_bindings"] = CollabService._assistant_role_bindings(value["role_bindings"], 8)
        return out

    def report_assistant_runtime(self, pr: Principal, runtime_id: str, body: dict) -> dict:
        self._require_operator(pr)
        allowed = {"claim_token", "attestation_nonce", "status", "checkpoint",
                   "launch_attestation", "instance_id", "error_code"}
        if not isinstance(body, dict) or set(body) - allowed \
                or not {"claim_token", "attestation_nonce", "status", "checkpoint"} <= set(body):
            raise CollabError(400, "invalid runtime report")
        status = body.get("status")
        if status not in {"starting", "ready", "failed", "cancelled"}:
            raise CollabError(400, "invalid runtime status")
        checkpoint = self._assistant_runtime_checkpoint(body.get("checkpoint"))
        with self._lock:
            self._expire_assistant_runtimes()
            runtime, _ = self._assistant_runtime_claim_auth(runtime_id, body)
            if runtime["status"] not in _RUNTIME_LIVE:
                raise CollabError(409, "runtime is terminal")
            if runtime["cancel_requested"] and status not in {"failed", "cancelled"}:
                raise CollabError(409, "runtime cancellation is pending")
            if ((status == "starting" and runtime["status"] != "claimed")
                    or (status == "ready" and runtime["status"] not in {"claimed", "starting"})):
                raise CollabError(409, "invalid runtime status transition")
            if runtime["checkpoint"] and checkpoint["sequence"] <= runtime["checkpoint"]["sequence"]:
                raise CollabError(409, "checkpoint sequence must increase")
            launch, instance_id, error = None, None, None
            if status == "starting":
                if set(body) != {"claim_token", "attestation_nonce", "status", "checkpoint"}:
                    raise CollabError(400, "invalid starting report")
            elif status == "ready":
                if set(body) != {"claim_token", "attestation_nonce", "status", "checkpoint",
                                 "launch_attestation", "instance_id"}:
                    raise CollabError(400, "invalid ready report")
                launch = self._assistant_runtime_launch_attestation(body.get("launch_attestation"))
                if launch != {
                    "profile_digest": runtime["attestation"]["profile_digest"],
                    "runtime_artifact_digest": runtime["attestation"]["runtime"]["artifact_digest"],
                    "enforcement": runtime["attestation"]["enforcement"],
                    "role_bindings": self._assistant_role_bindings(
                        self._assistant_runtime_package(runtime)["manifest"]["references"]["procedures"], 8),
                }:
                    raise CollabError(409, "launch attestation does not match the claim")
                instance_id = body.get("instance_id")
                if not isinstance(instance_id, str) or not _KEY_RE.fullmatch(instance_id):
                    raise CollabError(400, "invalid instance_id")
            elif status == "failed":
                if set(body) != {"claim_token", "attestation_nonce", "status", "checkpoint", "error_code"}:
                    raise CollabError(400, "invalid failed report")
                error = body.get("error_code")
                if not isinstance(error, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,79}", error):
                    raise CollabError(400, "invalid runtime error_code")
            else:
                if set(body) != {"claim_token", "attestation_nonce", "status", "checkpoint"}:
                    raise CollabError(400, "invalid cancelled report")
                if not runtime["cancel_requested"]:
                    raise CollabError(409, "cancellation was not requested")
            if status in {"starting", "ready"}:
                try:
                    self._assistant_runtime_policy(runtime, claiming=False)
                except CollabError:
                    self._fail_assistant_runtime_policy(pr, runtime)
                    self._fail_assistant_runtime_tasks(self._actor(pr), runtime,
                                                       "runtime_policy_changed")
                    raise CollabError(409, "runtime policy changed") from None
            now = self._now()
            self._commit("assistant_runtime_reported", self._actor(
                pr, runtime_id=runtime_id, room_id=runtime["room_id"], project_id=runtime["project_id"],
                status=status, checkpoint=checkpoint, launch_attestation=launch,
                instance_id=instance_id, error_code=error,
                lease_expires_at=now + ASSISTANT_RUNTIME_LEASE if status in _RUNTIME_LIVE else None,
                reported_epoch=now, visibility_scope="owner", room_notify=False,
                action="assistant_runtime.report", outcome="allowed",
                project_ids=[runtime["project_id"]], resource_id=runtime["room_id"]))
            if status in {"failed", "cancelled"}:
                self._fail_assistant_runtime_tasks(self._actor(pr), runtime, status)
            return {"runtime": self._assistant_runtime_json(runtime)}

    def assistant_runtime_cancel_status(self, pr: Principal, runtime_id: str, body: dict) -> dict:
        self._require_operator(pr)
        if not isinstance(body, dict) or set(body) != {"claim_token"}:
            raise CollabError(400, "invalid cancellation poll")
        with self._lock:
            runtime, _ = self._assistant_runtime_claim_auth(runtime_id, {
                "claim_token": body.get("claim_token"),
                "attestation_nonce": self._assistant_runtimes.get(runtime_id, {}).get("attestation_nonce"),
            })
            return {"runtime_id": runtime_id, "cancel_requested": runtime["cancel_requested"],
                    "reason": runtime["cancel_reason"]}

    def _grant_expiry(self, body: dict) -> float:
        """Every agent grant lapses: the owner may pick a date (up to a year), else the default."""
        exp = _finite_expiry(body.get("expires_at"), self._now())
        horizon = self._now() + 366 * 86400
        if exp is None:
            return self._now() + self.default_grant_ttl
        if exp > horizon:
            raise CollabError(400, "expires_at is too far away")
        return exp

    def _housekeeping(self) -> None:
        """Prune, and write down grants that lapsed (they were already refused; this keeps the
        roster and audit honest). Rate-limited: callers are hot paths."""
        now = self._now()
        if now - self._housekept < 30.0:
            return
        self._housekept = now
        self._prune()
        self._expire_helpers()
        for rid, room in list(self._rooms.items()):
            for aid, g in list(room["grants"].items()):
                if g["expires_at"] is not None and now >= g["expires_at"]:
                    try:
                        self._commit("agent_grant_expired", {
                            "actor_user_id": None, "actor_agent_id": None, "room_id": rid, "agent_id": aid,
                            "project_ids": [room["project_id"]], "action": "agent.grant.expire",
                            "outcome": "allowed", "resource_id": aid})
                    except CollabError:
                        return                      # log full / unavailable: the grant is refused anyway

    def _prune(self) -> None:
        """Bound memory: finished tasks and closed requests older than their window drop out of the
        in-memory index (the append-only log keeps the history)."""
        now = self._now()
        self._prune_egress()
        for tid in [i for i, t in self._tasks.items()
                    if self._task_status(t) not in ("pending", "accepted") and now - t["created_epoch"] > 7 * 86400]:
            t = self._tasks.pop(tid)
            idx = self._tasks_by_target.get(t["target"])
            if idx and tid in idx:
                idx.remove(tid)
        for hid in [i for i, r in self._helpers.items()
                    if r["status"] not in _HELPER_LIVE and now - r["created_epoch"] > 7 * 86400]:
            r = self._helpers.pop(hid)
            if r["helper_agent_id"] and self._helper_by_agent.get(r["helper_agent_id"]) is r:
                del self._helper_by_agent[r["helper_agent_id"]]
            if r.get("operation_id") and self._helper_by_op.get(r["operation_id"]) is r:
                del self._helper_by_op[r["operation_id"]]
        for rid in [i for i, r in self._requests.items() if not r["open"] and now - r["created_epoch"] > REQUEST_TTL]:
            r = self._requests.pop(rid)
            self._requests_by_room[r["room_id"]].discard(rid)
            self._requests_by_agent[r["agent_id"]].discard(rid)

    def _prune_egress(self) -> None:
        now = self._now()
        for rid in [i for i, r in self._egress.items()
                    if (r["state"] == "open" and now - r["created_epoch"] >= EGRESS_OPEN_TTL + EGRESS_KEEP)
                    or (r["state"] != "open" and now - (r["expires_at"] or r["decided_epoch"] or 0) >= EGRESS_KEEP
                        and now - r["decided_epoch"] >= EGRESS_KEEP)]:
            del self._egress[rid]

    def _creator_valid(self, t: dict, room: dict) -> bool:
        """A task is only as good as its requester's CURRENT authority."""
        if t["by_kind"] == "user":
            m = self._member(room, t["by_id"])
            return bool(m and "task" in ROLE_ACTIONS[m["role"]] and self._ok(t["by_id"]))
        creator = self._agents.get(t["by_id"])
        if not creator:
            return False
        eff = self._effective(Principal("agent", t["by_id"], creator["owner"]), room)
        return bool(eff and "instruct" in eff[0])

    def _task_json(self, tid: str, t: dict) -> dict:
        a = self._agents.get(t["target"], {})
        return {"task_id": tid, "room_id": t["room_id"], "target_agent_id": t["target"],
                "target_name": a.get("name"), "text": t["text"], "status": self._task_status(t),
                "result": t["result"], "depth": t["depth"], "parent_task_id": t["parent"],
                "requested_by": {"type": t["by_kind"], "id": t["by_id"], "owner_user_id": t["by_owner"]},
                "started_by_user_id": t["root"],
                "created_at": t["created_at"], "updated_at": t["updated_at"]}

    def list_tasks(self, pr: Principal, room_id: str, limit: int = 100) -> dict:
        with self._lock:
            _, first = self._authorize(pr, room_id, "read", "task.list")
            rows = [(t["seq"], tid, t) for tid, t in self._tasks.items()
                    if t["room_id"] == room_id and t["seq"] >= first]
            rows.sort(key=lambda r: r[0], reverse=True)
            out = [dict(self._task_json(tid, t), untrusted=True) for _, tid, t in rows[:limit]]
        return {"tasks": out}

    def get_task(self, pr: Principal, room_id: str, task_id: str) -> dict:
        with self._lock:
            _, first = self._authorize(pr, room_id, "read", "task.get")
            task = self._tasks.get(task_id) if isinstance(task_id, str) else None
            if not task or task["room_id"] != room_id or task["seq"] < first:
                raise CollabError(404, "not found")
            out = dict(self._task_json(task_id, task), untrusted=True)
        return {"task": out}

    def cancel_task(self, pr: Principal, room_id: str, task_id: str) -> dict:
        self._require_user(pr)
        with self._lock:
            room, _ = self._authorize(pr, room_id, "task", "task.cancel")
            t = self._tasks.get(task_id)
            m = self._member(room, pr.id)
            if (not t or t["room_id"] != room_id or not m
                    or not (m["role"] == "owner" or (t["by_kind"] == "user" and t["by_id"] == pr.id)
                            or (t["by_kind"] == "agent" and t["by_owner"] == pr.id))
                    or self._task_status(t) not in ("pending", "accepted")):
                raise CollabError(404, "not found")
            self._commit("task_updated", self._actor(
                pr, project_id=room["project_id"], room_id=room_id, task_id=task_id,
                status="cancelled", result=None, target_agent_id=t["target"], visibility_scope="room",
                action="task.cancel", outcome="allowed", project_ids=[room["project_id"]],
                resource_id=task_id))
        return {"ok": True}

    def task_inbox(self, pr: Principal, limit: int = 20) -> dict:
        """Open tasks addressed to this agent, only where it STILL holds "execute"."""
        if not pr.is_agent:
            raise CollabError(403, "forbidden")
        with self._lock:
            out = []
            for tid in list(self._tasks_by_target.get(pr.id, ())):
                t = self._tasks[tid]
                if self._task_status(t) not in ("pending", "accepted"):
                    continue
                room = self._rooms.get(t["room_id"])
                eff = self._effective(pr, room) if room else None
                # a task made before this agent's current grant never revives; and its requester
                # must still be allowed to ask
                if (not eff or "execute" not in eff[0] or t["seq"] < eff[1]
                        or not self._creator_valid(t, room)):
                    continue
                out.append(dict(self._task_json(tid, t), room_name=room["name"]))
                if len(out) >= limit:
                    break
        return {"tasks": out}

    def update_task(self, pr: Principal, task_id: str, body: dict) -> dict:
        if not pr.is_agent:
            raise CollabError(403, "forbidden")
        with self._lock:
            t = self._tasks.get(task_id) if isinstance(task_id, str) else None
            room = self._rooms.get(t["room_id"]) if t else None
            eff = self._effective(pr, room) if room else None
            if (not t or t["target"] != pr.id or not eff or "execute" not in eff[0] or t["seq"] < eff[1]
                    or not self._creator_valid(t, room)):
                raise CollabError(404, "not found")
            bad = [k for k in body if k not in ("status", "result")]
            status = body.get("status")
            cur = self._task_status(t)
            allowed = {"pending": ("accepted", "failed"), "accepted": ("done", "failed")}.get(cur, ())
            if bad or status not in allowed:
                raise CollabError(400, "invalid status change")
            result = self._task_text(body.get("result"), "result", required=False)
            self._throttle("task-update:" + pr.key, 60, 60.0, "task update")
            self._commit("task_updated", self._actor(
                pr, project_id=room["project_id"], room_id=t["room_id"], task_id=task_id,
                status=status, result=result, target_agent_id=pr.id, visibility_scope="room",
                action="task.update", outcome="allowed", project_ids=[room["project_id"]],
                resource_id=task_id))
        return {"task_id": task_id, "status": status}

    # -- messages
    def post_message(self, pr: Principal, room_id: str, body: dict) -> tuple[int, dict]:
        with self._lock:
            room, first = self._authorize(pr, room_id, "post", "message.post")
            bad = [k for k in body if k not in _MESSAGE_FIELDS]
            if bad:
                forged = any(k.startswith(("actor_", "sender_", "owner_")) or k in
                             {"org_id", "project_id", "room_id", "role", "agent_id", "user_id"} for k in bad)
                if forged:
                    self._audit_denied(pr, "message.post", "forged_identity", room_id=room_id,
                                       project_ids=[room["project_id"]])
                    self.stats["forged_identity"] += 1
                raise CollabError(400, "identity and scope are derived from the credential, "
                                       "not the request" if forged else "unknown field")
            text, key = body.get("text"), body.get("idempotency_key")
            if not isinstance(key, str) or not _KEY_RE.match(key):
                raise CollabError(400, "idempotency_key required (1-128 of A-Za-z0-9._:-)")
            if not isinstance(text, str):
                raise CollabError(400, "text must be a string")
            clean = _CONTROL.sub("", text)
            if not clean.strip() or len(clean) > MAX_TEXT:
                raise CollabError(400, f"text must be 1-{MAX_TEXT} characters")
            reply = body.get("in_reply_to")
            if reply is not None:
                where = self._msg_room.get(reply) if isinstance(reply, str) else None
                # only messages this poster could already see (not from before they joined)
                if where is None or where[0] != room_id or where[1] < first:
                    reply = False
            if reply is False:
                raise CollabError(400, "in_reply_to must reference a message in this room")
            fp = _digest(json.dumps([room_id, clean, reply]))
            prior = self._idem.get((pr.key, room_id, key))
            if prior:
                if prior[0] != fp:
                    raise CollabError(409, "idempotency_key already used for a different message")
                return 200, {"event": self._view(self._log[prior[1] - 1], replayed=True), "replayed": True}
            now = self._now()
            hits = [t for t in self._hits[pr.key] if now - t < 60]
            if len(hits) >= self.message_limit:
                self._hits[pr.key] = hits
                self.stats["rate_limited"] += 1
                raise CollabError(429, "message rate limit exceeded")
            # One budget per human across their own posts and all of their agents, so making
            # more agents never buys more throughput.
            okey = "owner:" + pr.owner
            ohits = [t for t in self._hits[okey] if now - t < 60]
            if len(ohits) >= self.message_limit * 2:
                self._hits[okey] = ohits
                self.stats["rate_limited"] += 1
                raise CollabError(429, "message rate limit exceeded")
            hits.append(now)
            ohits.append(now)
            self._hits[pr.key] = hits
            self._hits[okey] = ohits
            ev = self._commit("message", self._actor(
                pr, project_id=room["project_id"], room_id=room_id, sender_key=pr.key,
                sender_kind=pr.kind, sender_id=pr.id, idempotency_key=key, fingerprint=fp,
                text=redact_text(clean), in_reply_to=reply, trust="untrusted",
                visibility_scope="room", action="message.post", outcome="allowed",
                project_ids=[room["project_id"]], resource_id=room_id))
        self.stats["messages"] += 1
        return 201, {"event": self._view(ev)}

    # -- reads
    def _view(self, ev: dict, **extra) -> dict:
        p, t = ev["payload"], ev["event_type"]
        out = {"sequence": ev["sequence"], "event_id": ev["event_id"], "event_type": t,
               "schema_version": ev["schema_version"], "room_id": p.get("room_id"),
               "project_id": p.get("project_id"), "created_at": p["created_at"],
               "correlation_id": p.get("correlation_id"), "causation_id": p.get("causation_id"),
               "visibility_scope": p.get("visibility_scope", "room")}
        if t == "message":
            out.update(sender={"type": p["sender_kind"], "id": p["sender_id"],
                               "owner_user_id": p["actor_user_id"]},
                       text=p["text"], trust="untrusted", in_reply_to=p.get("in_reply_to"))
        elif t in ("task_created", "task_updated"):
            out.update(actor_user_id=p.get("actor_user_id"), actor_agent_id=p.get("actor_agent_id"),
                       task={"task_id": p["task_id"], "target_agent_id": p.get("target_agent_id"),
                             "status": p.get("status", "pending"), "text": p.get("text"),
                             "result": p.get("result"), "depth": p.get("depth"),
                             "requested_by": {"type": p.get("created_by_kind"), "id": p.get("created_by_id"),
                                              "owner_user_id": p["actor_user_id"]} if t == "task_created" else None},
                       trust="untrusted")
        else:
            out.update(actor_user_id=p.get("actor_user_id"), actor_agent_id=p.get("actor_agent_id"),
                       subject_user_id=p.get("subject_user_id"), role=p.get("role"),
                       agent_id=p.get("agent_id"))
            if t == "agent_renamed":
                out.update(name=p.get("name"), old_name=p.get("old_name"), source=p.get("source"))
            elif t.startswith("helper_"):             # ids, role and model: never the task text or result
                out.update(helper={"helper_id": p.get("helper_id"), "role": p.get("role"), "model": p.get("model"),
                                   "status": p.get("status")})
        out.update(extra)
        return out

    def read_events(self, pr: Principal, room_id: str, cursor: int, limit: int) -> dict:
        with self._lock:
            room, first = self._authorize(pr, room_id, "read", "events.read")
            guest = not pr.is_agent and (self._member(room, pr.id) or {}).get("role") == "guest"
            out, more, nxt = [], False, cursor
            feed = self._by_room.get(room_id, [])
            lo = bisect.bisect_right(feed, max(cursor, first - 1), key=lambda e: e["sequence"])
            for ev in feed[lo:]:
                if guest and (ev["event_type"].startswith("helper_") or (
                        ev["event_type"].startswith("agent_") and ev["payload"].get("agent_id") in self._helper_minted)):
                    nxt = ev["sequence"]              # a guest never sees helpers, but the cursor moves past them
                    continue
                if len(out) >= limit:
                    more = True
                    break
                out.append({k: v for k, v in self._view(ev).items() if k != "project_id"})
                nxt = ev["sequence"]
        return {"events": out, "next_cursor": nxt, "has_more": more}

    def audit(self, pr: Principal, project_id: str, cursor: int, limit: int) -> dict:
        self._require_user(pr)
        with self._lock:
            proj = self._project_visible(pr, project_id)
            if proj["members"].get(pr.id) != "manager":
                raise CollabError(403, "forbidden")
            out, more = [], False
            for ev in self._log:
                p = ev["payload"]
                if ev["sequence"] <= cursor or "action" not in p:
                    continue
                # Agent lifecycle events carry no project: they appear in a project's
                # audit only if that agent has (or had) a grant in one of its rooms.
                aid = p.get("agent_id")
                # the panic switch is global: it ends helpers in every project, so every project's audit shows it
                if not (project_id in p.get("project_ids", ()) or ev["event_type"] == "helpers_paused"
                        or (aid and project_id in self._agent_projects.get(aid, ()))):
                    continue
                if len(out) >= limit:
                    more = True
                    break
                out.append({"sequence": ev["sequence"], "event_type": ev["event_type"],
                            "action": p["action"], "outcome": p["outcome"], "reason": p.get("reason"),
                            "actor_user_id": p.get("actor_user_id"), "actor_agent_id": p.get("actor_agent_id"),
                            "room_id": p.get("room_id"), "resource_id": p.get("resource_id"),
                            "created_at": p["created_at"], "correlation_id": p.get("correlation_id")})
                if ev["event_type"].startswith("helper_"):
                    out[-1]["helper"] = {"helper_id": p.get("helper_id"), "role": p.get("role"), "model": p.get("model")}
                elif ev["event_type"] == "helpers_paused":
                    out[-1]["paused"] = bool(p.get("paused"))
            nxt = out[-1]["sequence"] if out else cursor
        return {"events": out, "next_cursor": nxt, "has_more": more}

    # ------------------------------------------------------------- streaming
    def open_stream(self, pr: Principal, room_id: str, cursor: int) -> Stream:
        with self._lock:
            self._authorize(pr, room_id, "read", "events.stream")
            if (self._streams[pr.key] >= self.max_streams
                    or self._streams["owner:" + pr.owner] >= self.max_streams * 2):
                raise CollabError(429, "too many streams")
        return Stream(self, pr, room_id, cursor)

    def pump(self, stream: Stream, max_seconds: float = 3600.0):
        """Yield frames; re-authorizes on every tick and closes on any loss of access."""
        started = time.monotonic()
        cursor = stream.cursor
        with self._lock:
            okey = "owner:" + stream.principal.owner
            if (self._streams[stream.principal.key] >= self.max_streams
                    or self._streams[okey] >= self.max_streams * 2):
                yield {"type": "closed", "reason": "too_many_streams"}
                return
            self._streams[stream.principal.key] += 1
            self._streams[okey] += 1
        try:
            yield {"type": "connected", "cursor": cursor}
            while time.monotonic() - started < max_seconds:
                try:
                    if not self.still_valid(stream.principal):
                        raise CollabError(401, "revoked")
                    page = self.read_events(stream.principal, stream.room_id, cursor, 100)
                except CollabError:
                    yield {"type": "closed", "reason": "access_revoked"}
                    return
                yield from page["events"]
                cursor = page["next_cursor"]
                if not page["events"]:
                    yield None  # idle tick: the caller may emit a keep-alive
                    time.sleep(self.stream_poll_seconds)
        finally:
            with self._lock:
                self._streams[stream.principal.key] -= 1
                self._streams["owner:" + stream.principal.owner] -= 1

    # --------------------------------------------------------------- routing
    def dispatch(self, method: str, parts: list[str], query: dict, body: dict,
                 authorization: str, device: str | None, operator: str | None = None):
        """parts: path segments after /api/v1/collab. Returns (status, json) or a Stream."""
        try:
            if parts == ["agents", "enrollment", "complete"] and method == "POST":
                return 201, self.complete_enrollment(body)
            if parts == ["agents", "join"] and method == "POST":
                return 201, self.redeem_join_token(body)         # the token IS the authentication
            if parts == ["agents", "join-link"] and method == "POST":
                return 200, self.link_joined(body)
            if parts == ["config"] and method == "GET":
                return 200, self.public_config  # public identifiers only; no auth needed
            if operator is not None:
                # Only the operator listener (tailnet-only, Host-checked) ever passes this: the
                # local owner uses the dashboard without a sign-in, as the same user id they
                # sign in as on the public site. They must still be a current group member.
                if not self.auth.exists(operator):
                    raise CollabError(403, "operator is not enabled for collaboration")
                if self.approval is not None:
                    self._ensure_operator(operator)
                pr = Principal("user", operator, operator)
                pr.admin = pr.approver = True
            else:
                pr = self.authenticate(authorization, device)
            n = len(parts)
            if pr.status != "approved" and parts != ["me"]:
                self.stats["policy_denied"] += 1
                raise CollabError(403, "waiting for approval")
            if parts[:2] == ["admin", "users"]:
                if method == "GET" and parts == ["admin", "users", "log"]:
                    return 200, self.user_log(pr)
                if method == "GET" and n == 2:
                    return 200, self.list_users(pr, str(query["status"]) if "status" in query else None)
                if method == "POST" and n == 4:
                    return 200, self.decide_user(pr, parts[2], parts[3], body)
                raise CollabError(404, "not found")
            if method == "GET":
                if parts == ["me"]:
                    return 200, self.me(pr)
                if parts == ["agents"]:
                    return 200, self.list_my_agents(pr)
                if parts == ["agents", "join-tokens"]:
                    return 200, self.list_join_tokens(pr)
                if parts[:1] == ["helpers"] and not pr.is_agent:     # agents use MCP; a person sees their agents'
                    if n == 1:
                        return 200, self.list_my_helpers(pr)
                    if n == 2:
                        return 200, self.helper_status(pr, parts[1])
                if parts[:1] == ["fleet"]:
                    if operator is None:                  # dashboard owner only
                        raise CollabError(404, "not found")
                    if parts == ["fleet", "hosts"]:
                        return 200, self.list_fleet_hosts(pr)
                    if parts == ["fleet", "agent-links"]:
                        return 200, self.agent_links(pr)
                    if parts == ["fleet", "egress"]:
                        return 200, self.list_egress(pr)
                    if n == 3 and parts[1] == "egress":
                        self._require_operator(pr)
                        with self._lock:
                            if parts[2] not in self._rooms:
                                raise CollabError(404, "not found")
                        return 200, {"allowlist": self.egress_allowlist(parts[2]),
                                     "entries": self.egress_entries(parts[2])}
                    if parts == ["fleet", "helpers"]:
                        return 200, self.list_helpers_for_runner(pr)
                    if parts == ["fleet", "summary-fix", "helpers"]:
                        return 200, self.list_summary_fix_helpers(pr)
                    if parts == ["fleet", "spawns"]:
                        st = str(query["status"]) if "status" in query else None
                        return 200, self.list_spawns(pr, st)
                    if parts == ["fleet", "assistant-runtimes"]:
                        return 200, self.list_assistant_runtime_queue(pr)
                    raise CollabError(404, "not found")
                if parts == ["tasks", "inbox"]:
                    return 200, self.task_inbox(pr)
                if parts == ["rooms"]:
                    return 200, self.list_rooms(pr)
                if parts == ["audit"]:
                    return 200, self.audit(pr, str(query.get("project_id", "")),
                                           _int(query, "cursor", 0), _int(query, "limit", 100, 1, 500))
                if n >= 2 and parts[0] == "rooms":
                    rid = parts[1]
                    if n == 2:
                        return 200, self.get_room(pr, rid)
                    if parts[2:] == ["profile"]:
                        return 200, self.get_room_profile(pr, rid)
                    if parts[2:] == ["agents"]:
                        return 200, self.list_room_agents(pr, rid)
                    if parts[2:] == ["invites"]:
                        return 200, self.list_invites(pr, rid)
                    if parts[2:] == ["agent-requests"]:
                        return 200, self.list_agent_requests(pr, rid)
                    if parts[2:] == ["tasks"]:
                        return 200, self.list_tasks(pr, rid)
                    if n == 4 and parts[2] == "tasks":
                        return 200, self.get_task(pr, rid, parts[3])
                    if parts[2:] == ["approval-proposals"]:
                        return 200, self.list_approval_proposals(pr, rid)
                    if parts[2:] == ["assistant-packages"]:
                        return 200, self.list_assistant_packages(pr, rid)
                    if n == 4 and parts[2] == "assistant-packages":
                        return 200, self.get_assistant_package(pr, rid, parts[3])
                    if parts[2:] == ["assistant-hosts"]:
                        return 200, self.list_assistant_runtime_hosts(pr, rid)
                    if parts[2:] == ["assistant-runtimes"]:
                        return 200, self.list_assistant_runtimes(pr, rid)
                    if n == 4 and parts[2] == "assistant-runtimes":
                        return 200, self.get_assistant_runtime(pr, rid, parts[3])
                    if parts[2:] == ["flow"]:
                        return 200, self.room_flow(pr, rid)
                    if parts[2:] == ["state"]:
                        wait = _int(query, "wait", 0, 0, 10)
                        return 200, self.room_state(pr, rid, _int(query, "since", -1, -1),
                                                    _int(query, "cursor", 0), float(wait))
                    if parts[2:] == ["events"]:
                        return 200, self.read_events(pr, rid, _int(query, "cursor", 0),
                                                     _int(query, "limit", 100, 1, 200))
                    if parts[2:] == ["events", "stream"]:
                        return self.open_stream(pr, rid, _int(query, "cursor", 0))
            elif method in ("PUT", "POST") and n == 3 and parts[0] == "rooms" and parts[2] == "profile":
                return 200, self.set_room_profile(pr, parts[1], body)
            elif method == "POST":
                if parts == ["me"]:
                    return 200, self.link_identity(pr, body)
                if parts == ["agents", "connect"]:
                    return 201, self.connect_agent(pr, body)
                if parts == ["agents", "join-tokens"]:
                    return 201, self.create_join_token(pr, body)
                if n == 4 and parts[:2] == ["agents", "join-tokens"] and parts[3] == "revoke":
                    return 200, self.revoke_join_token(pr, parts[2])
                if n == 3 and parts[0] == "helpers" and parts[2] == "cancel" and not pr.is_agent:
                    return 200, self.cancel_helper(pr, parts[1])
                if parts[:1] == ["fleet"]:
                    if operator is None:                  # dashboard owner only
                        raise CollabError(404, "not found")
                    if parts == ["fleet", "hosts"]:
                        return 200, self.report_fleet_hosts(pr, body)
                    if n == 3 and parts[1] == "hosts":
                        return 200, self.set_fleet_host(pr, parts[2], body)
                    if parts == ["fleet", "egress", "request"]:
                        return 200, self.request_egress(pr, body.get("room_id"), body.get("agent_id"),
                                                        body.get("host"), body.get("port"))
                    if parts == ["fleet", "egress", "promote"]:
                        return 200, self.promote_egress(pr, body.get("room_id"), body.get("host_port"))
                    if parts == ["fleet", "egress", "remove"]:
                        return 200, self.remove_egress(pr, body.get("room_id"), body.get("host_port"))
                    if n == 4 and parts[1] == "egress" and parts[3] == "decide":
                        kw = {"ttl_seconds": body["ttl_seconds"]} if "ttl_seconds" in body else {}
                        return 200, self.decide_egress(pr, parts[2], body.get("decision"), **kw)
                    if parts == ["fleet", "catalogue"]:
                        return 200, self.set_catalogue(pr, body.get("models"))
                    if parts == ["fleet", "helpers", "pause"]:
                        return 200, self.set_helpers_paused(pr, body.get("paused"))
                    if parts == ["fleet", "summary-fix", "helper"]:
                        return 202, self.request_summary_fix_helper(pr, body)
                    if n == 4 and parts[1] == "helpers" and parts[3] == "claim":
                        return 200, self.claim_helper(pr, parts[2])
                    if n == 4 and parts[1] == "helpers" and parts[3] == "cancel":
                        return 200, self.cancel_helper_as_operator(pr, parts[2])
                    if n == 4 and parts[1] == "helpers" and parts[3] == "finish":
                        return 200, self.finish_helper(pr, parts[2], body)
                    if parts == ["fleet", "spawns"]:
                        return 201, self.request_spawn(pr, body)
                    if parts == ["fleet", "assistant-runtime-hosts"]:
                        return self.attest_assistant_runtime_host(pr, body)
                    if parts == ["fleet", "task-activity"]:
                        return 200, self.task_activity(pr, body)
                    if n == 4 and parts[1] == "spawns" and parts[3] == "claim":
                        return 200, self.claim_spawn(pr, parts[2])
                    if n == 4 and parts[1] == "spawns" and parts[3] == "done":
                        return 200, self.finish_spawn(pr, parts[2], body)
                    if n == 4 and parts[1] == "assistant-runtimes" and parts[3] == "claim":
                        return 200, self.claim_assistant_runtime(pr, parts[2], body)
                    if n == 4 and parts[1] == "assistant-runtimes" and parts[3] == "heartbeat":
                        return 200, self.heartbeat_assistant_runtime(pr, parts[2], body)
                    if n == 4 and parts[1] == "assistant-runtimes" and parts[3] == "report":
                        return 200, self.report_assistant_runtime(pr, parts[2], body)
                    if n == 4 and parts[1] == "assistant-runtimes" and parts[3] == "cancel-status":
                        return 200, self.assistant_runtime_cancel_status(pr, parts[2], body)
                    raise CollabError(404, "not found")
                if parts == ["agents", "bulk"]:
                    if operator is None:                  # dashboard owner only: not offered to the public site
                        raise CollabError(404, "not found")
                    return 201, self.bulk_connect(pr, body)
                if parts == ["projects"]:
                    return 201, self.create_project(pr, body)
                if n == 3 and parts[0] == "projects" and parts[2] == "rooms":
                    return 201, self.create_room(pr, parts[1], body)
                if n == 3 and parts[0] == "projects" and parts[2] == "members":
                    return 201, self.add_project_member(pr, parts[1], body)
                if parts == ["agents", "enrollment", "start"]:
                    return 201, self.start_enrollment(pr, body)
                if n == 3 and parts[0] == "tasks" and parts[2] == "update":
                    return 200, self.update_task(pr, parts[1], body)
                if parts == ["agent", "rotate"]:
                    return 200, self.rotate_credential(pr)
                if n == 3 and parts[0] == "agents" and parts[2] == "link":
                    return 200, self.link_agent(pr, parts[1], body)
                if n == 3 and parts[0] == "agents" and parts[2] == "rename":
                    return 200, self.rename_agent(pr, parts[1], body)
                if n == 3 and parts[0] == "agents" and parts[2] == "revoke":
                    return 200, self.revoke_agent(pr, parts[1])
                if n >= 3 and parts[0] == "rooms":
                    rid = parts[1]
                    if parts[2:] == ["members"]:
                        return 201, self.add_member(pr, rid, body)
                    if parts[2:] == ["invites"]:
                        return 201, self.invite_by_email(pr, rid, body)
                    if n == 5 and parts[2] == "invites" and parts[4] == "cancel":
                        return 200, self.cancel_invite(pr, rid, parts[3])
                    if n == 5 and parts[2] == "members" and parts[4] == "remove":
                        return 200, self.remove_member(pr, rid, parts[3])
                    if parts[2:] == ["agents"]:
                        return 201, self.grant_agent(pr, rid, body)
                    if n == 5 and parts[2] == "agents" and parts[4] == "remove":
                        return 200, self.remove_agent_from_room(pr, rid, parts[3])
                    if parts[2:] == ["agent-requests", "approve-all"]:
                        return 200, self.approve_all_requests(pr, rid, body)
                    if n == 5 and parts[2] == "agent-requests" and parts[4] == "decide":
                        return 200, self.decide_agent_request(pr, rid, parts[3], body)
                    if parts[2:] == ["tasks"]:
                        return 201, self.create_task(pr, rid, body)
                    if parts[2:] == ["approval-proposals"]:
                        return self.create_approval_proposal(pr, rid, body)
                    if parts[2:] == ["assistant-packages"]:
                        return self.create_assistant_package(pr, rid, body)
                    if parts[2:] == ["assistant-runtimes"]:
                        return self.create_assistant_runtime(pr, rid, body)
                    if n == 5 and parts[2] == "assistant-runtimes" and parts[4] == "cancel":
                        return 200, self.cancel_assistant_runtime(pr, rid, parts[3], body)
                    if n == 5 and parts[2] == "tasks" and parts[4] == "cancel":
                        return 200, self.cancel_task(pr, rid, parts[3])
                    if parts[2:] == ["messages"]:
                        return self.post_message(pr, rid, body)
            raise CollabError(404, "not found")
        except CollabError as exc:
            return exc.status, {"error": exc.message, **({"reason": exc.reason} if exc.reason else {})}


CATALOGUE_MAX = 500


def _catalogue_entries(entries: Any) -> list[dict]:
    """Validate a posted model catalogue; any bad entry rejects the whole post."""
    if not isinstance(entries, list) or len(entries) > CATALOGUE_MAX:
        raise CollabError(400, f"models must be a list of at most {CATALOGUE_MAX} entries")
    keys = {"id", "family", "vision", "price_out_per_mtok", "context", "local"}

    def num(v: Any, kind: type, cap: float) -> bool:
        return v is None or (isinstance(v, kind) and not isinstance(v, bool) and 0 <= v <= cap)   # NaN fails too

    out, seen = [], set()
    for e in entries:
        if not isinstance(e, dict) or set(e) - keys:
            raise CollabError(400, "invalid catalogue entry")
        mid, fam = e.get("id"), e.get("family")
        if (not isinstance(mid, str) or not 0 < len(mid) <= 128 or not mid.isprintable() or mid in seen
                or not _KEY_RE.fullmatch(mid)
                or not (fam is None or (isinstance(fam, str) and len(fam) <= 64 and fam.isprintable()))
                or not isinstance(e.get("vision", False), bool) or not isinstance(e.get("local", False), bool)
                or not num(e.get("context"), int, 1e9)
                or not (num(e.get("price_out_per_mtok"), float, 1e6) or num(e.get("price_out_per_mtok"), int, 1e6))):
            raise CollabError(400, "invalid catalogue entry")
        seen.add(mid)
        price = e.get("price_out_per_mtok")
        out.append({"id": mid, "family": fam, "vision": e.get("vision", False),
                    "price_out_per_mtok": None if price is None else float(price),
                    "context": e.get("context"), "local": e.get("local", False)})
    return out


def _profile_strings(v: Any, field: str, rx: re.Pattern | None, limit: int, maxlen: int = 120) -> list[str]:
    if not isinstance(v, list) or len(v) > limit:
        raise CollabError(400, f"{field} must be a list of at most {limit}")
    out = []
    for x in v:
        if not isinstance(x, str) or len(x) > maxlen or (rx is not None and not rx.fullmatch(x)):
            raise CollabError(400, f"{field} has an invalid entry")
        if x not in out:
            out.append(x)
    return out


def _internal_host(host: str) -> bool:
    """A name or IP literal that points into the runner host or its network (not globally routable)."""
    h = host.lower().rstrip(".")
    h = h[1:-1] if h.startswith("[") and h.endswith("]") else h
    if h == "localhost" or h.endswith(".localhost") or h.startswith("localhost."):
        return True
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        # a name whose last label is a number is an IPv4 address in another spelling (2130706433, 0x7f.1, 127.1)
        return bool(_NUMERIC_LABEL.fullmatch(h.rsplit(".", 1)[-1]))
    if isinstance(ip, ipaddress.IPv6Address):
        # an IPv4 address carried inside IPv6 (mapped, 6to4, Teredo, NAT64) counts as that address, as in the proxy
        nat64 = ip in ipaddress.ip_network("64:ff9b::/96")
        inner = ip.ipv4_mapped or ip.sixtofour or (ip.teredo[1] if ip.teredo else None) \
            or (ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF) if nat64 else None)
        if inner is not None and _internal_host(str(inner)):
            return True
        if ip.ipv4_mapped:
            ip = ip.ipv4_mapped
    return not ip.is_global or ip.is_multicast or ip.is_unspecified or ip.is_loopback or ip.is_link_local


def _normalize_profile(body: Any) -> dict:
    """Validate a room profile and return it in canonical form. Unknown keys are refused, so a
    secret value has no field to ride in on: services and ssh refer to secrets by NAME only."""
    if not isinstance(body, dict):
        raise CollabError(400, "profile must be a JSON object")
    extra = set(body) - _PROFILE_KEYS
    if extra:
        raise CollabError(400, f"unknown profile field: {sorted(extra)[0][:40]}")
    fams = _profile_strings(body.get("allowed_families", []), "allowed_families", None, len(SPAWN_FAMILIES))
    if any(f not in SPAWN_FAMILIES for f in fams):
        raise CollabError(400, f"allowed_families must be among {', '.join(SPAWN_FAMILIES)}")
    models = body.get("allowed_models")
    if models is not None:
        models = _profile_strings(models, "allowed_models", _KEY_RE, 100)
    tools = body.get("tools_allowed", False)
    if not isinstance(tools, bool):
        raise CollabError(400, "tools_allowed must be true or false")
    hosts = _profile_strings(body.get("permanent_hosts", []), "permanent_hosts", _PROFILE_HOST, MAX_PROFILE_HOSTS, 260)
    if any(not 0 < int(h.rsplit(":", 1)[1]) < 65536 for h in hosts):
        raise CollabError(400, "permanent_hosts has an invalid port")
    # the same host rule and canonical form as host requests, so the operator's Remove matches what was saved
    try:
        hosts = list(dict.fromkeys(CollabService._egress_host_port(h.rsplit(":", 1)[0], int(h.rsplit(":", 1)[1]))
                                   for h in hosts))
    except CollabError:
        raise CollabError(400, "permanent_hosts has an invalid entry") from None
    if any(_internal_host(h.rsplit(":", 1)[0]) for h in hosts):
        # an owner's hosts skip operator approval, and the proxy lets a LISTED internal address through
        raise CollabError(400, "permanent_hosts may not name loopback, private or link-local addresses")
    raw = body.get("services", [])
    if not isinstance(raw, list) or len(raw) > MAX_PROFILE_SERVICES:
        raise CollabError(400, f"services must be a list of at most {MAX_PROFILE_SERVICES}")
    services = []
    for sv in raw:
        if not isinstance(sv, dict) or set(sv) != {"name", "upstream_url", "header", "secret_name"}:
            raise CollabError(400, "a service needs exactly name, upstream_url, header and secret_name")
        name, url, header, sec = sv["name"], sv["upstream_url"], sv["header"], sv["secret_name"]
        if not (isinstance(name, str) and _PROFILE_SERVICE_NAME.fullmatch(name)
                and isinstance(header, str) and _PROFILE_HEADER.fullmatch(header)
                and header.lower() not in _PROFILE_BAD_HEADERS
                and isinstance(sec, str) and _PROFILE_SECRET_NAME.fullmatch(sec)):
            raise CollabError(400, "a service has an invalid name, header or secret_name")
        try:
            u = urlparse(url) if isinstance(url, str) and len(url) <= 2000 and url.isprintable() else None
            ok = bool(u) and u.scheme == "https" and bool(u.hostname) and u.username is None and u.password is None \
                and not u.fragment and not u.query and not u.params and not any(c in url for c in "?#;") \
                and u.port != 0
        except ValueError:
            ok = False
        if not ok:
            raise CollabError(400, "upstream_url must be an https URL without credentials")
        # the proxy sends the bound key from the runner host: the same host rule as host requests, and never a
        # name or address inside the runner host's network (the proxy also checks what the name resolves to)
        try:
            CollabService._egress_host_port(f"[{u.hostname}]" if ":" in u.hostname else u.hostname, u.port or 443)
        except CollabError:
            raise CollabError(400, "upstream_url needs a plain host name, IPv4 address or [IPv6] address") from None
        if _internal_host(u.hostname):
            raise CollabError(400, "upstream_url may not name loopback, private or link-local addresses")
        if any(x["name"] == name for x in services):
            raise CollabError(400, "service names must be unique")
        services.append({"name": name, "upstream_url": url, "header": header, "secret_name": sec})
    ssh = _profile_strings(body.get("ssh_secret_names", []), "ssh_secret_names", _PROFILE_SECRET_NAME, MAX_PROFILE_NAMES)
    if set(ssh) & {sv["secret_name"] for sv in services}:
        raise CollabError(400, "a secret name cannot be both a service's key and an ssh key")
    spend = body.get("spend_tokens_per_day")
    if spend is not None and (not isinstance(spend, int) or isinstance(spend, bool) or not 0 <= spend < 2**53):
        raise CollabError(400, "spend_tokens_per_day must be a non-negative integer or null")
    lim_in = body.get("limits", {})
    if not isinstance(lim_in, dict) or set(lim_in) - set(PROFILE_LIMIT_DEFAULTS):
        raise CollabError(400, "limits may only set live_per_agent, depth and per_hour")
    limits = {}
    for k, default in PROFILE_LIMIT_DEFAULTS.items():
        v = lim_in.get(k, default)
        if not isinstance(v, int) or isinstance(v, bool) or not 1 <= v <= default:
            raise CollabError(400, f"limits.{k} must be an integer from 1 to {default} (a profile may only lower it)")
        limits[k] = v
    return {"allowed_families": fams, "allowed_models": models, "tools_allowed": tools,
            "permanent_hosts": hosts, "services": services, "ssh_secret_names": ssh,
            "spend_tokens_per_day": spend, "limits": limits}


def _finite_expiry(exp: Any, now: float) -> float | None:
    if exp is None:
        return None
    if (not isinstance(exp, (int, float)) or isinstance(exp, bool) or not math.isfinite(exp)
            or exp <= now):
        raise CollabError(400, "invalid expires_at")
    return float(exp)


def _name(value: Any, field: str = "name") -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 120 or _NAME_BAD.search(value):
        raise CollabError(400, f"{field} must be 1-120 printable characters")
    return value.strip()


def _int(query: dict, key: str, default: int, lo: int = 0, hi: int = 2**62) -> int:
    raw = query.get(key)
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise CollabError(400, f"invalid {key}") from None
    return min(hi, max(lo, value))


def _herd_group(name: str) -> str | None:
    """`worker-3` belongs to the herdr workspace `herd-worker`: the name prefix is the fleet."""
    m = re.fullmatch(r"([a-z0-9][a-z0-9-]*?)-\d+", name or "")
    return f"herd-{m.group(1)}" if m else None


def _default_handle(harness: str, agent_id: str) -> str:
    base = _HANDLE_BAD.sub("-", str(harness).lower()).strip("-")[:20] or "agent"
    return f"{base}-{agent_id[-4:].lower()}"


def _handle(value: Any) -> str:
    if (not isinstance(value, str) or not 1 <= len(value) <= 32
            or _HANDLE_BAD.search(value) or value.startswith("-")):
        raise CollabError(400, "name must be 1-32 characters: lowercase letters, digits and hyphens")
    return value


def _actions(value: Any) -> list[str]:
    if (not isinstance(value, list) or not value
            or not all(isinstance(a, str) and a in AGENT_ACTIONS for a in value)):
        raise CollabError(400, "actions must be a non-empty subset of read, post, instruct, execute, spawn_helpers")
    return sorted(set(value))

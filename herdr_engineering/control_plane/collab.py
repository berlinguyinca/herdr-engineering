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

from . import collab_delegation as dlg
from . import collab_external as ext
from . import ids
from .collab_context import ContextMixin
from .collab_workbench import WorkbenchMixin
from .redact import redact_text

SCHEMA_VERSION = 1
ENTITY = "collab"
STREAM_ID = "log"
ORG_ID = "default"  # single-organization deployment (spec: no federation in v1)

# "task" is the human right to give an agent work; the agent-side rights are "instruct"
# (give work to other agents) and "execute" (receive work). The room owner approves them per agent.
ROLE_ACTIONS = {
    "owner": frozenset({"read", "post", "invite", "grant_agent", "task", "instruct", "execute"}),
    "contributor": frozenset({"read", "post", "grant_agent", "task", "instruct", "execute"}),
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
AGENT_ACTIONS = frozenset({"read", "post", "instruct", "execute"})  # an agent can never invite/grant/administer
PROJECT_ROLES = frozenset({"manager", "contributor"})

MAX_TEXT = 8000
MAX_BODY = 64 * 1024
_PANE_LINK = re.compile(r"pane:[a-z0-9][a-z0-9._-]{0,62}:[A-Za-z0-9][A-Za-z0-9:_.-]{0,39}")
_KEY_RE = re.compile(r"[A-Za-z0-9._:\-]{1,128}\Z")
_MESSAGE_FIELDS = frozenset({"text", "idempotency_key", "in_reply_to", "context"})
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")   # C0, DEL and C1 (8-bit CSI)
_NAME_BAD = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")
_EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s]{2,}$")
INVITE_TTL = 7 * 24 * 3600.0
MAX_OPEN_INVITES = 50
_FEED_TYPES = frozenset({"message", "member_added", "member_removed",
                         "agent_granted", "agent_revoked", "agent_grant_revoked", "agent_renamed",
                         "task_created", "task_updated"})
# Room <-> Herdr workspace links. A link is what room members may SEE of a workspace; the
# terminal is a separate, explicit grant that is never implied by room membership.
LINK_VIEW_GRANTS = frozenset({"status", "tabs", "panes"})       # metadata only, never terminal bytes
LINK_GRANTS = LINK_VIEW_GRANTS | {"terminal"}
DEFAULT_LINK_GRANTS = ("status",)                               # minimal by default
MAX_LINKS_PER_ROOM = 50
MAX_MISSIONS_PER_ROOM = 100
MAX_TERMINAL_PRINCIPALS = 20
TERMINAL_ROLES = frozenset({"owner", "contributor"})            # viewers and guests never get a terminal
MAX_TASK_TEXT = 4000
TASK_TTL = 24 * 3600.0           # a task nobody finished in a day is expired
MAX_TASK_DEPTH = 3               # human -> agent -> agent -> agent
TASK_RATE = 20                   # new tasks per creator per 10 minutes
MAX_OPEN_TASKS_PER_AGENT = 10
MAX_PENDING_REQUESTS = 120       # open access requests per room (one family of agents fits)
SPAWN_FAMILIES = ("claude-code", "codex", "pi", "agent")   # what the host runner knows how to start
MAX_SPAWN_AGENTS = 20            # agents one fleet start may place on the owner's hosts
MAX_OPEN_SPAWNS = 3              # requests waiting for, or held by, the runner
SPAWN_STALE = 900.0              # a claimed start with no result after this long is shown as stalled
FLEET_ONLINE = 60.0              # a host the runner has not mentioned for this long is offline
_FLEET_HOST = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")
MAX_JOIN_AGENTS = 50             # agents one join token / bulk call may create
JOIN_TOKEN_MAX_TTL = 24 * 3600.0
REQUEST_TTL = 7 * 24 * 3600.0
_HANDLE_BAD = re.compile(r"[^a-z0-9\-]")


class CollabError(Exception):
    def __init__(self, status: int, message: str, code: str | None = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code            # a stable machine-readable reason for refusals a client can act on


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
    __slots__ = ("kind", "id", "owner", "digest", "exp", "status", "approver", "admin", "external")

    def __init__(self, kind: str, id: str, owner: str, digest: str | None = None,
                 exp: float | None = None):
        self.kind, self.id, self.owner, self.digest, self.exp = kind, id, owner, digest, exp
        self.status = "approved"       # approval mode sets this for people; agents are judged by their owner
        self.approver = False          # may approve or deny people waiting
        self.admin = False             # the operator on the tailnet-only listener: may also disable and remove
        self.external = False          # an agent a coworker enrolled by invite (T10): bound to one room, never a person

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


class CollabService(WorkbenchMixin, ContextMixin):
    def __init__(self, authenticator: UserAuthenticator, *, sink: Any = None,
                 now=None, enrollment_ttl: float = 600.0, credential_ttl: float = 12 * 3600.0,
                 stream_poll_seconds: float = 1.0, message_limit: int = 60,
                 max_streams_per_principal: int = 5, public_config: dict | None = None,
                 remote_credential_ttl: float = 30 * 24 * 3600.0,
                 max_agents_per_user: int = 60, max_projects_per_user: int = 20,
                 max_rooms_per_project: int = 50, max_members_per_room: int = 100,
                 max_log_events: int = 200_000, guest_ttl: float = 7 * 24 * 3600.0,
                 default_grant_ttl: float = 30 * 24 * 3600.0,
                 approval: ApprovalPolicy | None = None, delegation_enabled: bool = False):
        self.auth = authenticator
        # Kill switch for bounded task delegation and artifact proposals (T11), from the fleet YAML
        # (control_plane.collab.delegation_enabled). Off by default: while off, no delegation can be created,
        # answered, proposed to or accepted, and the MCP/A2A adapters do not advertise it.
        self.delegation_enabled = bool(delegation_enabled)
        self.approval = approval
        # The fabric lineage graph, attached by the combined (non collab-only) server. The graph never
        # calls back into this service: membership is handed to it as a per-request snapshot.
        self.graph_source = None      # callable returning the CURRENT projection (a rebuild replaces it)
        # Writes an external agent's lineage through the ingest path (combined server only; None = refused).
        # Called with (envelope, principal); the envelope is built here, never taken from the request.
        self.lineage_sink = None
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
        # lineage replay for a member starts at their join time ("since_join", default) or covers the whole
        # room history ("full"). Operator YAML: control_plane.collab.replay_history.
        self.replay_history = "since_join"
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
        self._proposals: dict[str, dict] = {}                # artifact proposals in quarantine / decided (T11)
        self._proposals_by_task: dict[str, list[str]] = defaultdict(list)
        self._proposals_by_room: dict[str, list[str]] = defaultdict(list)
        self._proposals_by_agent: dict[str, list[str]] = defaultdict(list)
        self._requests: dict[str, dict] = {}                 # open agent-access requests
        # Indexes kept only by _apply (so hydrate rebuilds them): they replace whole-state scans.
        self._agents_by_owner: dict[str, set] = defaultdict(set)
        self._pending_by_owner: dict[str, set] = defaultdict(set)
        self._requests_by_room: dict[str, set] = defaultdict(set)
        self._requests_by_agent: dict[str, set] = defaultdict(set)
        self._rooms_by_agent: dict[str, set] = defaultdict(set)
        self._links: dict[str, dict] = {}                    # link id -> room/workspace link
        self._links_by_room: dict[str, list] = defaultdict(list)
        self._primary: dict[tuple, str] = {}                 # (host, workspace id) -> link id of its primary room
        self._hosts: dict[str, dict] = {}                    # host registry: host -> {owners, gen}
        self._term_gate: dict[tuple, tuple] = {}             # (principal, link) -> (last audited allow, fingerprint); memory only
        self._room_link_seq: dict[str, int] = {}             # room id -> sequence of its latest link change (people only)
        self._missions: dict[tuple, dict] = {}               # (room id, mission id) -> scoped mission link
        self._housekept = 0.0
        self._room_seq: dict[str, int] = {}                  # room id -> sequence of its latest change
        self._fleet_flags: dict[str, bool] = {}              # host -> may it receive automatic agents (default yes)
        self._fleet_seen: dict[str, dict] = {}               # host -> what the host runner last reported (memory only)
        self._spawns: dict[str, dict] = {}                   # spawn id -> request
        self._state_waiters: dict[str, int] = defaultdict(int)
        self._joins: dict[str, dict] = {}                    # token digest -> record
        self._joins_by_id: dict[str, str] = {}               # join id -> digest
        self._externals_by_room: dict[str, list] = defaultdict(list)   # room id -> external agent ids (T10)
        self._presence: dict[str, dict] = {}                 # external agent id -> last heartbeat; memory only, by design
        # Lineage write counters per ("agent"|"sponsor"|"room", id). Each is made durable AHEAD of its value
        # (reserve-ahead rows in the log), so after a restart a count can be too high by up to
        # USAGE_RESERVE_STEP-1 but never too low.
        self._usage: dict[tuple, int] = defaultdict(int)
        self._usage_reserved: dict[tuple, int] = defaultdict(int)
        self.redeem_failures: dict[str, int] = {}      # failed invite redemptions per client address (bounded)
        self._by_room: dict[str, list] = defaultdict(list)   # room id -> feed events, in sequence order
        self._msg_room: dict[str, str] = {}                  # message event id -> room id
        self.stats = defaultdict(int)
        self._init_workbench()
        self._init_context()

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
            self._touch_rooms(payload, ev["sequence"], event_type)
            self._cv.notify_all()
            return ev

    _LINK_EVENTS = frozenset({"workspace_linked", "workspace_unlinked", "workspace_link_updated",
                              "workspace_primary_set", "workspace_primary_cleared",
                              "mission_linked", "mission_unlinked"})

    def _touch_rooms(self, p: dict, seq: int, event_type: str = "") -> None:
        """Remember which rooms an event changed, so a room's long-poll wakes only for its own news."""
        rooms = set()
        if isinstance(p.get("room_id"), str):
            rooms.add(p["room_id"])
        rooms.update(r for r in (p.get("room_ids") or ()) if isinstance(r, str))
        if isinstance(p.get("agent_id"), str):
            rooms |= self._rooms_by_agent.get(p["agent_id"], set())
        if event_type == "workspace_terminal_authorized":
            return                                  # an audit record only: wakes nobody
        # Link changes are shown to people only (never guests or agents), so they wake only people's polls.
        target = self._room_link_seq if event_type in self._LINK_EVENTS else self._room_seq
        for r in rooms:
            target[r] = seq

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
            for k, v in self._usage_reserved.items():
                self._usage[k] = max(self._usage[k], v)      # restart: the durable ceiling, never lower than used
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
        if self._apply_workbench(ev) or self._apply_context(ev):
            return
        if t == "external_usage_reserved":
            k = (p["scope"], p["key"])
            self._usage_reserved[k] = max(self._usage_reserved[k], int(p["reserved"]))
        elif t == "project_created":
            self._projects[p["project_id"]] = {"name": p["name"], "members": {p["actor_user_id"]: "manager"}}
        elif t == "project_member_added":
            self._projects[p["project_id"]]["members"][p["subject_user_id"]] = p["role"]
        elif t == "room_created":
            self._rooms[p["room_id"]] = {
                "id": p["room_id"], "project_id": p["project_id"], "name": p["name"], "created_by": p["actor_user_id"],
                "members": {p["actor_user_id"]: {"role": "owner", "expires_at": None, "joined_seq": seq,
                                                 "joined_at": p.get("created_at")}},
                "grants": {}}
        elif t == "user_registered":
            self._users[p["user_id"]] = {"status": p["status"], "since": p["at"], "decided_by": p.get("decided_by"),
                                         "decided_at": p["at"] if p["status"] != "pending" else None, "reason": None}
        elif t == "user_decided":
            u = self._users.setdefault(p["user_id"], {"since": p["at"]})
            u.update(status=p["status"], decided_by=p["decided_by"], decided_at=p["at"], reason=p.get("reason"))
        elif t == "member_added":
            self._rooms[p["room_id"]]["members"][p["subject_user_id"]] = {
                "role": p["role"], "expires_at": p.get("expires_at"), "joined_seq": seq,
                "joined_at": p.get("created_at")}
        elif t == "member_removed":
            room = self._rooms[p["room_id"]]
            room["members"].pop(p["subject_user_id"], None)
            # Agents of a removed member lose their grants for good: re-adding the person
            # must not silently revive access they never re-approved.
            for aid in [a for a in room["grants"] if self._agents.get(a, {}).get("owner") == p["subject_user_id"]]:
                room["grants"].pop(aid, None)
                self._rooms_by_agent[aid].discard(p["room_id"])
            gone = p["subject_user_id"]                # re-adding them later must not restore a terminal right
            self._prune_terminal(p["room_id"], lambda k: k == f"user:{gone}" or (
                k.startswith("agent:") and self._agents.get(k[6:], {}).get("owner") == gone))
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
                "cred_digest": p["secret_digest"], "expires_at": p["expires_at_epoch"],
                "external": ({**p["external"], "enrolled_at": p["created_at"], "revoked_at": None}
                             if isinstance(p.get("external"), dict) else None)}
            self._by_cred[p["secret_digest"]] = p["agent_id"]
            self._agents_by_owner[p["owner_user_id"]].add(p["agent_id"])
            if isinstance(p.get("external"), dict):
                self._externals_by_room[p["external"]["room_id"]].append(p["agent_id"])
            if p.get("join_digest") in self._joins:
                self._joins[p["join_digest"]]["uses"] += 1
        elif t == "join_token_created":
            self._joins[p["join_digest"]] = {
                "id": p["join_id"], "owner": p["actor_user_id"], "family": p["harness_type"],
                "prefix": p["name_prefix"], "max_uses": p["max_uses"], "uses": 0,
                "expires_at": p["expires_at_epoch"], "room_id": p.get("room_id"),
                "actions": list(p.get("actions") or []), "revoked": False, "created_at": p["created_at"],
                "parent": p.get("parent_agent_id"),
                "external": dict(p["external"]) if isinstance(p.get("external"), dict) else None}
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
            if self._agents[p["agent_id"]].get("external"):
                self._agents[p["agent_id"]]["external"]["revoked_at"] = p["created_at"]
                self._agents[p["agent_id"]]["external"]["revoked_by"] = (
                    "agent:" + p["actor_agent_id"] if p.get("actor_agent_id") else "user:" + str(p.get("actor_user_id")))
            self._prune_terminal(None, lambda k: k == "agent:" + p["agent_id"])
            for rid in self._requests_by_agent.get(p["agent_id"], ()):
                self._requests[rid]["open"] = False
        elif t in ("agent_grant_revoked", "agent_grant_expired"):
            self._rooms[p["room_id"]]["grants"].pop(p["agent_id"], None)
            self._rooms_by_agent[p["agent_id"]].discard(p["room_id"])
            self._prune_terminal(p["room_id"], lambda k: k == "agent:" + p["agent_id"])
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
            if p.get("bounds"):          # T11: a bounded delegation is this same task plus its bounds
                self._tasks[p["task_id"]].update(bounds=p["bounds"], version=1, steps=0, tokens=0, proposals=0)
                if p.get("idempotency_key"):
                    self._idem[(p["actor_agent_id"] or "user:" + p["actor_user_id"], p["room_id"],
                                "deleg:" + p["idempotency_key"])] = (p["fingerprint"], p["task_id"])
            self._tasks_by_target[p["target_agent_id"]].append(p["task_id"])
        elif t == "task_updated":
            tk = self._tasks.get(p["task_id"])
            if tk:
                tk.update(status=p["status"], result=p.get("result"), updated_at=p["created_at"])
                if "bounds" in tk:
                    tk["version"] += 1
                    if p.get("idempotency_key"):
                        self._idem[(p["actor_agent_id"], p["room_id"], "resp:" + p["idempotency_key"])] = (
                            p["fingerprint"], tk["version"])
        elif t == "task_progress":
            tk = self._tasks.get(p["task_id"])
            if tk and "bounds" in tk:
                tk.update(steps=tk["steps"] + 1, tokens=tk["tokens"] + p["tokens"], note=p.get("note"),
                          updated_at=p["created_at"], version=tk["version"] + 1)
                if p.get("idempotency_key"):
                    self._idem[(p["actor_agent_id"], p["room_id"], "resp:" + p["idempotency_key"])] = (
                        p["fingerprint"], tk["version"])
        elif t == "proposal_submitted":
            pid = p["proposal_id"]
            self._proposals[pid] = {
                "id": pid, "task_id": p["task_id"], "room_id": p["room_id"], "project_id": p["project_id"],
                "agent_id": p["actor_agent_id"], "sponsor": p["sponsor"], "kind": p["kind"], "title": p["title"],
                "content": p["content"], "url": p["url"], "refs": p["refs"], "size": p["size"],
                "digest": p["digest"], "flags": list(p["flags"]), "state": "quarantined",
                "supersedes": p.get("supersedes"), "created_epoch": p["created_epoch"], "created_at": p["created_at"],
                "decided_by": None, "decided_at": None, "reason": None, "promoted": False, "seq": seq}
            self._proposals_by_task[p["task_id"]].append(pid)
            self._proposals_by_room[p["room_id"]].append(pid)
            self._proposals_by_agent[p["actor_agent_id"]].append(pid)
            prior = self._proposals.get(p.get("supersedes") or "")
            if prior and prior["state"] == "quarantined":
                prior.update(state="superseded", decided_at=p["created_at"], reason="superseded by " + pid)
            tk = self._tasks.get(p["task_id"])
            if tk and "bounds" in tk:
                tk.update(proposals=tk["proposals"] + 1, steps=tk["steps"] + 1, version=tk["version"] + 1)
            if p.get("idempotency_key"):
                self._idem[(p["actor_agent_id"], p["room_id"], "prop:" + p["idempotency_key"])] = (p["digest"], pid)
        elif t == "proposal_decided":
            pr_ = self._proposals.get(p["proposal_id"])
            if pr_ and pr_["state"] == "quarantined":
                pr_.update(state=p["decision"], decided_by=p["actor_agent_id"] or p["actor_user_id"],
                           decided_at=p["created_at"], reason=p.get("reason"))
        elif t == "proposal_promoted":
            pr_ = self._proposals.get(p["proposal_id"])
            if pr_:
                pr_["promoted"] = True
        elif t == "agent_granted":
            self._rooms[p["room_id"]]["grants"][p["agent_id"]] = {
                "actions": list(p["actions"]), "expires_at": p.get("expires_at"), "granted_seq": seq}
            self._agent_projects[p["agent_id"]].add(p["project_id"])
            self._rooms_by_agent[p["agent_id"]].add(p["room_id"])
        elif t == "workspace_linked":
            self._links[p["link_id"]] = {
                "link_id": p["link_id"], "room_id": p["room_id"], "workspace_id": p["workspace_id"],
                "host": p["host"], "linked_by": p["actor_user_id"], "created_at": p["created_at"],
                "is_primary": bool(p["is_primary"]), "grants": list(p["grants"]),
                "terminal_principals": list(p.get("terminal_principals") or []),
                "host_proof": p.get("host_proof")}
            self._links_by_room[p["room_id"]].append(p["link_id"])
            self._demote(p.get("demoted_link_id"))
            if p["is_primary"]:
                self._primary[(p["host"], p["workspace_id"])] = p["link_id"]
        elif t == "workspace_unlinked":
            lk = self._links.pop(p["link_id"], None)
            if lk:
                if p["link_id"] in self._links_by_room.get(lk["room_id"], []):
                    self._links_by_room[lk["room_id"]].remove(p["link_id"])
                if self._primary.get((lk["host"], lk["workspace_id"])) == p["link_id"]:
                    self._primary.pop((lk["host"], lk["workspace_id"]), None)
        elif t == "workspace_link_updated":
            lk = self._links.get(p["link_id"])
            if lk:
                lk.update(grants=list(p["grants"]), terminal_principals=list(p.get("terminal_principals") or []))
                if "host_proof" in p:
                    lk["host_proof"] = p["host_proof"]
        elif t == "workspace_primary_set":
            lk = self._links.get(p["link_id"])
            if lk:
                self._demote(p.get("demoted_link_id"))
                lk["is_primary"] = True
                lk["host_proof"] = p.get("host_proof") or lk.get("host_proof")
                self._primary[(lk["host"], lk["workspace_id"])] = p["link_id"]
        elif t == "workspace_primary_cleared":
            lk = self._links.get(p["link_id"])
            if lk:
                lk["is_primary"] = False
                if self._primary.get((lk["host"], lk["workspace_id"])) == p["link_id"]:
                    self._primary.pop((lk["host"], lk["workspace_id"]), None)
        elif t in ("host_registered", "host_unregistered"):
            if t == "host_registered":
                self._hosts[p["host"]] = {"owners": list(p["owners"]), "gen": seq}
            else:
                self._hosts.pop(p["host"], None)
            for lk in self._links.values():          # a primary whose host proof no longer holds is gone
                if lk["host"] == p["host"] and lk["is_primary"] and not self._proof_ok(lk):
                    self._demote(lk["link_id"])
        elif t == "mission_linked":
            self._missions[(p["room_id"], p["mission_id"])] = {
                "mission_id": p["mission_id"], "room_id": p["room_id"], "members": list(p["members"]),
                "linked_by": p["actor_user_id"], "created_at": p["created_at"]}
        elif t == "mission_unlinked":
            self._missions.pop((p["room_id"], p["mission_id"]), None)
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

    def _demote(self, link_id: str | None) -> None:
        lk = self._links.get(link_id) if link_id else None
        if lk:
            lk["is_primary"] = False
            if self._primary.get((lk["host"], lk["workspace_id"])) == link_id:
                self._primary.pop((lk["host"], lk["workspace_id"]), None)

    def _prune_terminal(self, room_id: str | None, gone) -> None:
        """Drop named terminal principals that no longer qualify; no principals left = no terminal grant."""
        for lk in self._links.values():
            if room_id is not None and lk["room_id"] != room_id:
                continue
            keep = [k for k in lk["terminal_principals"] if not gone(k)]
            if len(keep) != len(lk["terminal_principals"]):
                lk["terminal_principals"] = keep
                if not keep:
                    lk["grants"] = [g for g in lk["grants"] if g != "terminal"]

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
                pr = Principal("agent", aid, agent["owner"], digest)
                pr.external = bool(agent.get("external"))
                return pr
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
        bound = agent.get("external")
        if bound:
            # An external agent is bound to the room it was invited into: a later grant anywhere else
            # (by anyone) is inert, and it never holds more than EXTERNAL_ACTIONS.
            if bound["room_id"] != room["id"] or self._now() >= bound["hard_expiry"]:
                return None
            allowed &= ext.EXTERNAL_ACTIONS
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
        return {"actor_user_id": pr.owner, "actor_agent_id": pr.id if pr.is_agent else None,
                "actor_kind": "external" if pr.external else pr.kind, **extra}

    def _require_user(self, pr: Principal) -> None:
        if pr.is_agent:
            self.stats["policy_denied"] += 1
            raise CollabError(403, "forbidden: agents cannot perform this operation")

    # ------------------------------------------------------------ quotas
    def _no_external(self, pr: Principal) -> None:
        """An external agent reads the room's chat and nothing else: not the roster (owner ids and
        emails), the flow view, the task list or the room bundle."""
        if pr.external:
            self.stats["policy_denied"] += 1
            raise CollabError(403, "forbidden")

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
        self._xlink_sweep(room_id)          # T14: links this person made no longer stand
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
            rows = [{"at": e["payload"].get("at"), "user_id": e["payload"]["user_id"],
                     "email": self._user_email.get(e["payload"]["user_id"]), "action": e["payload"].get("action"),
                     "status": e["payload"]["status"], "by": e["payload"].get("decided_by"),
                     "reason": e["payload"].get("reason")}
                    for e in reversed(self._log) if e["event_type"] in ("user_registered", "user_decided")]
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
                 "room_id": r["room_id"], "actions": r["actions"], "external": bool(r.get("external")),
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

    @staticmethod
    def _client_label(client: object) -> str:
        """The address the HTTP layer resolved (its existing proxy convention), or "unknown". Only a
        parseable IP is kept, so the counter and audit rows cannot be filled with free text."""
        try:
            return str(ipaddress.ip_address(str(client).strip())) if client else "unknown"
        except ValueError:
            return "unknown"

    def _redeem_failed(self, client: object) -> None:
        """Count a failed invite redemption per client address. Aggregate only (never one log row per
        attempt, so unauthenticated traffic cannot grow the log); bounded at 256 addresses."""
        self.stats["auth_failures"] += 1
        who = self._client_label(client)
        if who not in self.redeem_failures and len(self.redeem_failures) >= 256:
            who = "other"
        self.redeem_failures[who] = self.redeem_failures.get(who, 0) + 1

    def redeem_join_token(self, body: dict, client: str | None = None) -> dict:
        """Public, token-authenticated: one use = one new agent for the token's owner."""
        token = body.get("token")
        if not isinstance(token, str) or not token.startswith("hcj_") or len(token) > 80:
            with self._lock:
                self._redeem_failed(client)
            raise CollabError(401, "unauthorized")
        digest = _digest(token)
        instance = body.get("instance_id")
        with self._lock:
            rec = self._joins.get(digest)
            if (rec is None or rec["revoked"] or self._now() >= rec["expires_at"]
                    or rec["uses"] >= rec["max_uses"] or not self._ok(rec["owner"])):
                self._redeem_failed(client)
                raise CollabError(401, "unauthorized")
            self._throttle("join:" + digest, 120, 60.0, "join")
            if rec.get("external"):
                return self._redeem_external(rec, digest, body, client)
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
                    or agent.get("join") != digest or agent["status"] != "active"
                    or rec.get("external") or agent.get("external")):     # an external agent has no pane
                self.stats["auth_failures"] += 1
                raise CollabError(401, "unauthorized")
            self._throttle("joinlink:" + digest, 120, 60.0, "join link")
            self._commit("agent_linked", {"actor_user_id": rec["owner"], "actor_agent_id": None, "agent_id": aid,
                                          "instance_id": inst, "action": "agent.link", "outcome": "allowed",
                                          "project_ids": [], "resource_id": aid})
        return {"agent_id": aid, "instance_id": inst}

    # ------------------------------------------------- external coworker / remote agents (T10)
    # An external agent is an HCN agent record with an `external` block: bound to ONE room at invite
    # time, sponsored (and accountable) by the room owner who invited it, limited to EXTERNAL_ACTIONS,
    # with a hard credential expiry rotation cannot extend. It never gets a terminal, a task right or a
    # pane link; its room_id is never taken from a request.
    def _require_external(self, pr: Principal) -> dict:
        agent = self._agents.get(pr.id) if pr.is_agent else None
        if not agent or not agent.get("external"):
            self.stats["policy_denied"] += 1
            raise CollabError(403, "forbidden")
        return agent

    def _external_load(self, room_id: str) -> int:
        """Live external agents plus open invites in one room (what the per-room cap counts)."""
        now = self._now()
        live = sum(1 for aid in self._externals_by_room.get(room_id, ())
                   if (a := self._agents[aid])["status"] == "active" and now < a["external"]["hard_expiry"]
                   and aid in self._rooms[room_id]["grants"])
        invites = sum(1 for r in self._joins.values()
                      if r.get("external") and r["external"]["room_id"] == room_id and not r["revoked"]
                      and now < r["expires_at"] and r["uses"] < r["max_uses"])
        return live + invites

    def create_external_invite(self, pr: Principal, room_id: str, body: dict) -> dict:
        """The room owner invites a coworker's agent. The reply carries the one-time invite code (shown
        once, stored only as a digest); the coworker's bridge redeems it at ``/agents/join``."""
        self._require_user(pr)
        unknown = set(body) - {"label", "actions", "lineage", "proposals", "ttl_seconds", "credential_ttl_seconds"}
        if unknown:
            raise CollabError(400, "unknown field")
        label = ext.clean_text(body.get("label"), ext.MAX_LABEL)
        if not label:
            raise CollabError(400, f"label is required (1-{ext.MAX_LABEL} printable characters)")
        actions = body.get("actions", list(ext.DEFAULT_EXTERNAL_ACTIONS))
        if (not isinstance(actions, list) or not actions
                or not all(isinstance(a, str) and a in ext.EXTERNAL_ACTIONS for a in actions)):
            raise CollabError(400, "actions must be a non-empty subset of read, post")
        lineage = body.get("lineage", False)
        if not isinstance(lineage, bool):
            raise CollabError(400, "lineage must be true or false")
        proposals = body.get("proposals", False)
        if not isinstance(proposals, bool):
            raise CollabError(400, "proposals must be true or false")

        def span(name, default, lo, hi):
            v = body.get(name, default)
            if not isinstance(v, (int, float)) or isinstance(v, bool) or not lo <= v <= hi:
                raise CollabError(400, f"{name} must be {int(lo)}-{int(hi)}")
            return float(v)
        ttl = span("ttl_seconds", ext.INVITE_TTL_DEFAULT, ext.INVITE_TTL_MIN, ext.INVITE_TTL_MAX)
        cred_ttl = span("credential_ttl_seconds", ext.CRED_TTL_DEFAULT, ext.CRED_TTL_MIN, ext.CRED_TTL_MAX)
        with self._lock:
            room, _ = self._authorize(pr, room_id, "invite", "external.invite")
            self._throttle("extinvite:" + pr.id, ext.INVITES_PER_HOUR, 3600.0, "external invite")
            if self._external_load(room_id) >= ext.MAX_EXTERNALS_PER_ROOM:
                raise CollabError(429, "this room has as many external agents and open invites as it allows")
            self._agent_slots(pr.id)
            token, jid = "hcj_" + secrets.token_urlsafe(24), ids.new_id("collab")
            expires = self._now() + ttl
            block = {"room_id": room_id, "project_id": room["project_id"], "sponsor": pr.id, "label": label,
                     "actions": sorted(set(actions)), "lineage": lineage, "proposals": proposals,
                     "cred_ttl": cred_ttl}
            self._commit("join_token_created", self._actor(
                pr, join_id=jid, join_digest=_digest(token), harness_type="external", name_prefix="ext",
                max_uses=1, expires_at_epoch=expires, room_id=room_id, actions=block["actions"],
                parent_agent_id=None, external=block, project_id=room["project_id"],
                action="external.invite", outcome="allowed", project_ids=[room["project_id"]], resource_id=jid))
        return {"invite_id": jid, "code": token, "expires_at": expires, "room_id": room_id, "label": label,
                "actions": block["actions"], "lineage": lineage, "proposals": proposals,
                "credential_ttl_seconds": cred_ttl, "redeem": "POST /api/v1/collab/agents/join"}

    def revoke_external_invite(self, pr: Principal, room_id: str, join_id: str) -> dict:
        self._require_user(pr)
        with self._lock:
            room, _ = self._authorize(pr, room_id, "invite", "external.invite.revoke")
            digest = self._joins_by_id.get(join_id)
            rec = self._joins.get(digest) if digest else None
            if not rec or not rec.get("external") or rec["external"]["room_id"] != room_id:
                raise CollabError(404, "not found")
            if not rec["revoked"]:
                self._commit("join_token_revoked", self._actor(
                    pr, join_id=join_id, room_id=room_id, project_id=room["project_id"],
                    action="external.invite.revoke", outcome="allowed",
                    project_ids=[room["project_id"]], resource_id=join_id))
        return {"ok": True}

    def _redeem_external(self, rec: dict, digest: str, body: dict, client: str | None = None) -> dict:
        """Called with the lock held, after the token passed every check. One use = one external agent,
        bound to the invite's room, granted exactly the invite's actions and no more."""
        unknown = set(body) - {"token", "harness_type", "instance_id"}
        if unknown:
            raise CollabError(400, "unknown field")
        x, owner = rec["external"], rec["owner"]
        room = self._rooms.get(x["room_id"])
        m = self._member(room, owner) if room else None
        if not m or "invite" not in ROLE_ACTIONS[m["role"]]:
            self._redeem_failed(client)
            raise CollabError(401, "unauthorized")          # the sponsor no longer owns the room
        if self._external_load(x["room_id"]) - 1 >= ext.MAX_EXTERNALS_PER_ROOM:   # this invite is already counted
            raise CollabError(429, "this room has as many external agents as it allows")
        self._agent_slots(owner)
        aid, cred = ids.new_id("agent"), "hca_" + secrets.token_urlsafe(32)
        device = "remote-" + secrets.token_hex(8)
        now = self._now()
        hard = now + x["cred_ttl"]
        name = f"ext-{ext.slug(x['label'])}-{aid[-4:]}"
        declared = ext.clean_text(body.get("harness_type"), 40) or "external"
        block = {**x, "hard_expiry": hard}
        block.pop("cred_ttl", None)
        self._commit("agent_enrolled", {
            "actor_user_id": owner, "actor_agent_id": None, "agent_id": aid, "owner_user_id": owner,
            "code_digest": None, "join_digest": digest, "device_id": device, "harness_type": declared,
            "name": name, "parent_agent_id": None, "instance_id": None, "secret_digest": _digest(cred),
            "expires_at_epoch": hard, "external": block, "room_id": x["room_id"],
            "actor_kind": "external", "client_ip": self._client_label(client),
            "action": "external.enroll", "outcome": "allowed",
            "project_ids": [x["project_id"]], "resource_id": aid})
        self._commit("agent_granted", {
            "actor_user_id": owner, "actor_agent_id": None, "project_id": x["project_id"],
            "room_id": x["room_id"], "agent_id": aid, "actions": list(x["actions"]), "expires_at": hard,
            "actor_kind": "user", "action": "external.grant", "outcome": "allowed",
            "project_ids": [x["project_id"]], "resource_id": aid})
        return {"agent_id": aid, "kind": "external", "name": name, "credential": cred, "device_id": device,
                "expires_at": hard, "room_id": x["room_id"], "actions": list(x["actions"]),
                "lineage": x["lineage"], "proposals": bool(x.get("proposals")), "label": x["label"],
                "status": "active", "terminal": False}

    def _external_state(self, a: dict, room: dict, aid: str) -> str:
        x = a["external"]
        if a["status"] != "active":
            return "revoked"
        if self._now() >= x["hard_expiry"] or self._now() >= a["expires_at"]:
            return "expired"
        if aid not in room["grants"]:
            return "removed"
        return "active"

    def _presence_view(self, aid: str, state: str) -> dict:
        p = self._presence.get(aid)
        if p is None:
            return {"state": "none", "status": None, "note": "no heartbeat since this service started"}
        age = self._now() - p["at"]
        base = {"last_seen_at": self._iso(p["at"]), "last_activity": p["activity"], "age_seconds": int(age)}
        if state == "active" and age < ext.PRESENCE_STALE:
            return {"state": "online", "status": p["status"], **base}
        return {"state": "stale" if state == "active" else "offline", "status": None,
                "last_status": p["status"], **base}

    def list_room_externals(self, pr: Principal, room_id: str) -> dict:
        """The virtual-tab roster: every external agent ever invited into this room (revoked ones stay
        listed, marked), with server-derived provenance and presence. People only, never guests/agents."""
        self._require_user(pr)
        with self._lock:
            room, _ = self._authorize(pr, room_id, "read", "external.list")
            self._no_guest(pr, room)
            owner = "invite" in (self._effective(pr, room) or (frozenset(), 0))[0]
            rows = []
            for aid in self._externals_by_room.get(room_id, ()):
                a = self._agents[aid]
                x = a["external"]
                state = self._external_state(a, room, aid)
                rows.append({
                    "agent_id": aid, "node_id": "agent:" + aid, "kind": "external", "label": x["label"],
                    "name": a["name"], "state": state, "invited_by": x["sponsor"],
                    "invited_by_email": self._user_email.get(x["sponsor"]),
                    "enrolled_at": x["enrolled_at"], "expires_at": min(a["expires_at"], x["hard_expiry"]),
                    "revoked_at": x.get("revoked_at"), "revoked_by": x.get("revoked_by"),
                    "actions": sorted(set(room["grants"].get(aid, {"actions": x["actions"]})["actions"])
                                      & ext.EXTERNAL_ACTIONS),
                    "lineage": bool(x["lineage"]), "proposals": bool(x.get("proposals")),
                    "declared_harness": a["harness_type"], "presence": self._presence_view(aid, state), "terminal": False})
            invites = []
            if owner:
                now = self._now()
                for r in self._joins.values():
                    x = r.get("external")
                    if (x and x["room_id"] == room_id and not r["revoked"] and now < r["expires_at"]
                            and r["uses"] < r["max_uses"]):
                        invites.append({"invite_id": r["id"], "label": x["label"], "actions": x["actions"],
                                        "lineage": x["lineage"], "proposals": bool(x.get("proposals")),
                                        "invited_by": x["sponsor"], "expires_at": r["expires_at"]})
        return {"room_id": room_id, "externals": rows, "invites": invites if owner else None,
                "stale_after_seconds": ext.PRESENCE_STALE, "untrusted_text": True}

    def revoke_external(self, pr: Principal, room_id: str, agent_id: str) -> dict:
        """Any owner of the room (not only the sponsor) cuts an external agent off. The next request with
        its credential is a 401; what it already posted or emitted stays visible."""
        self._require_user(pr)
        with self._lock:
            room, _ = self._authorize(pr, room_id, "invite", "external.revoke")
            a = self._agents.get(agent_id)
            if not a or not a.get("external") or a["external"]["room_id"] != room_id:
                raise CollabError(404, "not found")
            if a["status"] == "active":
                self._commit("agent_revoked", self._actor(
                    pr, agent_id=agent_id, room_id=room_id, room_ids=[room_id], project_id=room["project_id"],
                    action="external.revoke", outcome="allowed",
                    project_ids=[room["project_id"]], resource_id=agent_id))
        return {"ok": True, "state": "revoked"}

    def external_me(self, pr: Principal) -> dict:
        with self._lock:
            a = self._require_external(pr)
            x = a["external"]
            return {"agent_id": pr.id, "kind": "external", "room_id": x["room_id"], "label": x["label"],
                    "actions": list(x["actions"]), "lineage": bool(x["lineage"]),
                    "proposals": bool(x.get("proposals")) and self.delegation_enabled,
                    "expires_at": min(a["expires_at"], x["hard_expiry"]), "terminal": False,
                    "heartbeat": {"statuses": list(ext.EXTERNAL_STATUSES), "stale_after_seconds": ext.PRESENCE_STALE}}

    def external_leave(self, pr: Principal) -> dict:
        """The external agent withdraws itself; the same revocation a room owner would apply."""
        with self._lock:
            a = self._require_external(pr)
            x = a["external"]
            self._commit("agent_revoked", self._actor(
                pr, agent_id=pr.id, room_id=x["room_id"], room_ids=[x["room_id"]], project_id=x["project_id"],
                action="external.leave", outcome="allowed", project_ids=[x["project_id"]], resource_id=pr.id))
        return {"ok": True}

    def external_heartbeat(self, pr: Principal, body: dict) -> dict:
        """Presence and a status from a fixed list. Kept in memory (a heartbeat is not a log event); only
        a CHANGE of status is written down, at most STATUS_CHANGES_PER_10_MIN times per agent."""
        unknown = set(body) - {"status", "activity"}
        if unknown:
            raise CollabError(400, "unknown field")
        status = body.get("status")
        if not isinstance(status, str) or status not in ext.EXTERNAL_STATUSES:
            raise CollabError(400, "status must be one of " + ", ".join(ext.EXTERNAL_STATUSES))
        activity = body.get("activity")
        if activity is not None and not isinstance(activity, str):
            raise CollabError(400, "activity must be a string")
        text = ext.clean_text(activity, ext.MAX_ACTIVITY) if activity else ""
        with self._lock:
            a = self._require_external(pr)
            x = a["external"]
            room = self._rooms.get(x["room_id"])
            if room is None or self._effective(pr, room) is None:
                self._audit_denied(pr, "external.heartbeat", "not_a_member", room_id=x["room_id"],
                                   project_ids=[x["project_id"]], agent_id=pr.id)
                raise CollabError(404, "not found")
            self._throttle("hb:" + pr.id, ext.HEARTBEATS_PER_MINUTE, 60.0, "heartbeat")
            prev = self._presence.get(pr.id)
            self._presence[pr.id] = {"status": status, "activity": text, "at": self._now()}
            if prev is None or prev["status"] != status:
                now = self._now()
                hits = [t for t in self._hits["extstatus:" + pr.id] if now - t < 600.0]
                if len(hits) < ext.STATUS_CHANGES_PER_10_MIN:
                    hits.append(now)
                    self._hits["extstatus:" + pr.id] = hits
                    self._commit("external_status_changed", self._actor(
                        pr, agent_id=pr.id, room_id=x["room_id"], project_id=x["project_id"],
                        status=status, action="external.status", outcome="allowed",
                        project_ids=[x["project_id"]], resource_id=pr.id))
        return {"ok": True, "stale_after_seconds": ext.PRESENCE_STALE}

    def is_external_node(self, node_id: str) -> bool:
        """Is this lineage node id inside an external agent's namespace? The ingest path reserves those
        ids for the agent itself (an operator key excepted), so nobody can pre-empt or forge its nodes."""
        aid = ext.namespace_of(node_id)
        a = self._agents.get(aid) if aid else None
        return bool(a and a.get("external"))

    def _usage_take(self, items: list[tuple]) -> None:
        """Count one lineage write against each ``(scope, key, cap)`` or refuse the lot. Called with the
        lock held. A counter is made durable ahead of its value, so what a restart restores is never lower
        than what was used. All the log rows are written before any counter moves."""
        for scope, key, cap in items:
            if self._usage[(scope, key)] >= cap:
                self.stats["rate_limited"] += 1
                raise CollabError(429, f"external lineage quota for this {scope} is used up",
                                  code="external_lineage_quota:" + scope)
        for scope, key, _ in items:
            k = (scope, key)
            if self._usage[k] + 1 > self._usage_reserved[k]:
                self._commit("external_usage_reserved", {
                    "actor_user_id": None, "actor_agent_id": None, "scope": scope, "key": key,
                    "reserved": self._usage[k] + ext.USAGE_RESERVE_STEP})
        for scope, key, _ in items:
            self._usage[(scope, key)] += 1

    def _usage_give_back(self, items: list[tuple]) -> None:
        with self._lock:
            for scope, key, _ in items:
                self._usage[(scope, key)] = max(0, self._usage[(scope, key)] - 1)

    def _credential_live(self, aid: str) -> bool:
        """Is this external agent's credential still good right now? Read without the service lock: it is
        evaluated inside the ingest ledger lock, and a lock-free read of a status that only ever moves from
        active to revoked cannot deadlock against it."""
        a = self._agents.get(aid)
        return bool(a and a["status"] == "active" and self._now() < a["external"]["hard_expiry"])

    def _refuse_lineage(self, pr: Principal, room_id: str) -> None:
        with self._lock:
            x = (self._agents.get(pr.id) or {}).get("external") or {}
            self._audit_denied(pr, "external.lineage", "forbidden_node", room_id=room_id,
                               project_ids=[x["project_id"]] if x else [], agent_id=pr.id)

    def external_lineage(self, pr: Principal, body: dict) -> dict:
        """Lineage an external agent reports about ITSELF. The server builds the envelope: room scope
        and room id come from the enrollment, the actor from the credential, the source from the agent
        id; the agent supplies only the event id, sequence and a payload that is re-validated here."""
        from .graph import validate_lineage
        unknown = set(body) - ext.LINEAGE_BODY_FIELDS
        with self._lock:
            a = self._require_external(pr)
            x = a["external"]
            room = self._rooms.get(x["room_id"])
            if unknown:
                self._audit_denied(pr, "external.lineage", "forged_identity", room_id=x["room_id"],
                                   project_ids=[x["project_id"]], agent_id=pr.id)
                self.stats["forged_identity"] += 1
                raise CollabError(400, "scope and identity are derived from the enrollment, not the request")
            if self.lineage_sink is None:
                raise CollabError(404, "lineage is not accepted by this service")
            if not x["lineage"]:
                self._audit_denied(pr, "external.lineage", "forbidden", room_id=x["room_id"],
                                   project_ids=[x["project_id"]], agent_id=pr.id)
                raise CollabError(403, "this invite does not allow lineage")
            if room is None or self._effective(pr, room) is None:
                raise CollabError(404, "not found")
            self._throttle("lin:" + pr.id, ext.LINEAGE_PER_MINUTE, 60.0, "lineage")
            room_id, label, sponsor = x["room_id"], x["label"], x["sponsor"]
        event_id, seq, etype = body.get("event_id"), body.get("source_sequence"), body.get("event_type")
        if (not isinstance(event_id, str) or not _KEY_RE.match(event_id)
                or isinstance(seq, bool) or not isinstance(seq, int) or not 0 <= seq < 2**53):
            raise CollabError(400, "event_id (1-128 of A-Za-z0-9._:-) and an integer source_sequence are required")
        payload = body.get("payload")
        try:
            op = validate_lineage(etype, payload)
        except ValueError as exc:
            raise CollabError(400, str(exc)) from None
        aid = pr.id
        attrs_in = payload.get("attrs") if isinstance(payload.get("attrs"), dict) else {}
        if op["op"] in ("node.upsert", "node.state"):
            if op["node_type"] not in ext.LINEAGE_NODE_TYPES or not ext.owns_node(aid, op["node_id"]):
                self._refuse_lineage(pr, room_id)
                raise CollabError(403, "an external agent may only write its own agent/attempt/session nodes")
            entity = op["node_id"]
        else:
            if (op["edge_type"] not in ext.LINEAGE_EDGE_TYPES or op["from_type"] not in ext.LINEAGE_NODE_TYPES
                    or op["to_type"] not in ext.LINEAGE_NODE_TYPES
                    or not (ext.owns_node(aid, op["from_id"]) and ext.owns_node(aid, op["to_id"]))):
                self._refuse_lineage(pr, room_id)
                raise CollabError(403, "an external agent may only link its own nodes with spawn edges")
            entity = op["from_id"]
        clean_attrs = {}
        for k, v in attrs_in.items():
            if len(clean_attrs) >= 16:
                raise CollabError(400, "too many attrs (16 at most)")
            if not isinstance(k, str) or not ext.ATTR_KEY.fullmatch(k):
                raise CollabError(400, "attr names are 1-64 of A-Za-z0-9._-")
            if k in ext.RESERVED_ATTRS or k.startswith("_"):
                raise CollabError(400, f"attrs.{k} is not allowed")
            if isinstance(v, str):
                v = ext.clean_text(v, 200)
            elif not (v is None or isinstance(v, (bool, int, float))):
                raise CollabError(400, f"attrs.{k} must be a short scalar")
            clean_attrs[k] = v
        if op["op"] == "edge.add" or op["op"] == "edge.remove":
            if clean_attrs:
                raise CollabError(400, "edges from external agents carry no attrs")
            out_payload = {k: payload[k] for k in ("edge_type", "from_id", "to_id") if k in payload}
            if op["op"] == "edge.remove":
                out_payload["reason"] = ext.clean_text(payload.get("reason"), 200) or "removed"
        elif op["op"] == "node.state":
            out_payload = {"node_id": op["node_id"], "state": op["state"]}
            if payload.get("reason") is not None:
                out_payload["reason"] = ext.clean_text(payload.get("reason"), 200)
        else:
            out_payload = {"node_type": op["node_type"], "node_id": op["node_id"],
                           "attrs": {**clean_attrs, "external": True, "reported_by": label, "invited_by": sponsor}}
            if payload.get("label") is not None:
                out_payload["label"] = ext.clean_text(payload.get("label"), 120)
            if op["state"] is not None:
                out_payload["state"] = op["state"]
        envelope = {"schema_version": 2, "event_id": event_id, "source": "ext-" + aid[-12:].lower(),
                    "source_sequence": seq, "entity_type": "lineage", "entity_id": entity, "event_type": etype,
                    "payload": out_payload, "visibility_scope": "room", "room_id": room_id}
        if body.get("occurred_at") is not None:
            envelope["occurred_at"] = body["occurred_at"]
        principal = {"kind": "external", "id": "external:" + aid, "authenticated_by": "collab"}
        items = [("agent", aid, ext.LINEAGE_MAX_EVENTS), ("sponsor", sponsor, ext.LINEAGE_MAX_PER_SPONSOR),
                 ("room", room_id, ext.LINEAGE_MAX_PER_ROOM)]
        with self._lock:
            self._usage_take(items)           # refused here, before the graph sees anything
        try:
            res = self.lineage_sink(envelope, principal, lambda: self._credential_live(aid))
        except PermissionError:
            self._usage_give_back(items)
            if not self._credential_live(aid):            # revoked or expired while the request was in flight
                with self._lock:
                    self._audit_denied(pr, "external.lineage", "revoked_in_flight", room_id=room_id,
                                       project_ids=[x["project_id"]], agent_id=aid)
                raise CollabError(401, "unauthorized") from None
            self._refuse_lineage(pr, room_id)
            raise CollabError(403, "not permitted to modify this lineage object") from None
        except ValueError as exc:
            self._usage_give_back(items)
            raise CollabError(400, str(exc)) from None
        except Exception as exc:
            self._usage_give_back(items)
            if type(exc).__name__ == "EnvelopeConflict":
                raise CollabError(409, str(exc)) from None
            self.stats["lineage_failures"] += 1
            raise CollabError(503, "lineage is unavailable") from None
        dup = bool(((res or {}).get("envelope") or {}).get("duplicate"))
        if dup:
            self._usage_give_back(items)      # a replay writes nothing, so it costs nothing
        return {"accepted": True, "duplicate": dup}

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
                            "instance_id": a.get("instance_id"), "external": bool(a.get("external")),
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
            if (self._agents.get(pr.id) or {}).get("status") != "active":
                raise CollabError(401, "unauthorized")
            bound = (self._agents.get(pr.id) or {}).get("external")
            if bound:                                   # rotation replaces the secret; the life stays the invite's
                expires = bound["hard_expiry"]
            self._commit("credential_rotated", self._actor(
                pr, agent_id=pr.id, secret_digest=_digest(cred), expires_at_epoch=expires,
                action="agent.rotate", outcome="allowed",
                project_ids=[bound["project_id"]] if bound else [], resource_id=pr.id))
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
        return {"ok": True}

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
            if agent.get("external"):
                raise CollabError(403, "an external agent holds only the grant of its invite")
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
                if agent.get("external"):
                    raise CollabError(403, "an external agent holds only the grant of its invite")
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
            self._no_external(pr)
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
                    m_now = None if pr.is_agent else self._member(self._rooms[room_id], pr.id)
                    if m_now and m_now["role"] != "guest":
                        cur = max(cur, self._room_link_seq.get(room_id, 0))
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
            with self._lock:                      # quiet reads: people only, never guests or agents
                visible = not pr.is_agent and not guest
                room_now = self._rooms[room_id]
                out["links"] = self._links_visible(pr, room_now) if visible else None
                out["missions"] = self._missions_visible(pr, room_now) if visible else None
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
            self._no_external(pr)
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
                        "external": bool(a.get("external")),
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
            if agent.get("external"):
                raise CollabError(403, "an external agent is not tied to a pane or session")
            self._throttle("link:" + pr.id, 120, 600.0, "agent link")
            self._commit("agent_linked", self._actor(
                pr, agent_id=agent_id, instance_id=inst, action="agent.link", outcome="allowed",
                project_ids=[], resource_id=agent_id))
        return {"agent_id": agent_id, "instance_id": inst}

    def list_room_agents(self, pr: Principal, room_id: str) -> dict:
        """Capability summaries only: no device ids, hostnames, paths or instance ids."""
        with self._lock:
            room, _ = self._authorize(pr, room_id, "read", "agent.list")
            self._no_external(pr)
            self._housekeeping()
            # A roster (who owns which agent): never for guests, like the member list.
            if not pr.is_agent and (self._member(room, pr.id) or {}).get("role") == "guest":
                raise CollabError(403, "forbidden")
            out = []
            for aid, g in room["grants"].items():
                a = self._agents.get(aid)
                if a and a["status"] == "active":
                    out.append({"agent_id": aid, "name": a["name"], "harness_type": a["harness_type"],
                                "owner_user_id": a["owner"], "actions": g["actions"],
                                "external": bool(a.get("external"))})
        return {"agents": out}

    # -- room <-> workspace and mission links
    # Room access NEVER implies workspace access. A link only says which metadata scopes room members
    # may see; the terminal needs its own explicit grant on the link, and the effective permission is
    # the intersection of user/agent role, room grant, link grant, the named terminal principals and a
    # proof that the linker controls the host. HOST REGISTRY: the operator maps host -> owning users
    # (register_host). A link may carry `terminal` and/or `is_primary` only with such a proof (the
    # linker is a registered owner of the host, or is the operator and the host is registered); with no
    # registry entry both are refused. The proof (registry generation) is recorded in the link event
    # and rechecked on every use: unregistering or re-registering the host, or removing the user from
    # its owners, ends it. Metadata-only links need no proof and are reported `host_verified: false`.
    def register_host(self, pr: Principal, host: str, body: dict) -> dict:
        """Operator only. owners = users who control this host's workspaces; an empty list unregisters it."""
        self._require_user(pr)
        if not pr.admin:
            self.stats["policy_denied"] += 1
            raise CollabError(403, "forbidden")
        host = self._require_host(host)
        owners = body.get("owners")
        if (not isinstance(owners, list) or len(owners) > 20 or not all(isinstance(u, str) for u in owners)):
            raise CollabError(400, "owners must be a list of at most 20 user ids")
        owners = sorted(set(owners))
        with self._lock:
            self._throttle("hostreg:" + pr.id, 60, 3600.0, "host registry")
            if not owners:
                if host not in self._hosts:
                    raise CollabError(404, "not found")
                self._commit("host_unregistered", self._actor(
                    pr, host=host, action="host.unregister", outcome="allowed", project_ids=[], resource_id=host))
                return {"host": host, "owners": []}
            if not all(self._ok(u) for u in owners):
                raise CollabError(404, "unknown user")
            if len(self._hosts) >= 200 and host not in self._hosts:
                raise CollabError(429, "too many hosts")
            self._commit("host_registered", self._actor(
                pr, host=host, owners=owners, action="host.register", outcome="allowed",
                project_ids=[], resource_id=host))
        return {"host": host, "owners": owners}

    def list_host_registry(self, pr: Principal) -> dict:
        self._require_user(pr)
        if not pr.admin:
            raise CollabError(403, "forbidden")
        with self._lock:
            return {"hosts": [{"host": h, "owners": list(e["owners"])} for h, e in sorted(self._hosts.items())]}

    def _proof_for(self, pr: Principal, host: str) -> dict | None:
        entry = self._hosts.get(host)
        if entry is None or pr.is_agent:
            return None
        if pr.admin:
            return {"user": pr.id, "via": "operator", "gen": entry["gen"]}
        if pr.id in entry["owners"]:
            return {"user": pr.id, "via": "owner", "gen": entry["gen"]}
        return None

    def _proof_ok(self, lk: dict) -> bool:
        proof, entry = lk.get("host_proof"), self._hosts.get(lk["host"])
        return bool(proof and entry and proof["gen"] == entry["gen"]
                    and (proof["via"] == "operator" or proof["user"] in entry["owners"]))

    def _is_link_manager(self, pr: Principal, room: dict) -> bool:
        m = self._member(room, pr.id) if not pr.is_agent else None
        return bool(m and m["role"] == "owner")

    def _terminal_ok(self, pr: Principal, room: dict, link: dict) -> bool:
        """Pure check (no audit): may this principal open the linked workspace's terminal?"""
        if "terminal" not in link["grants"] or not self._proof_ok(link):
            return False
        prover = self._member(room, link["host_proof"]["user"])
        if not prover or prover["role"] != "owner" or not self._ok(link["host_proof"]["user"]):
            return False                      # whoever vouched for the host must still own the room
        eff = self._effective(pr, room)
        if eff is None or "read" not in eff[0]:
            return False
        named = set(link["terminal_principals"])
        if pr.key not in named:
            return False
        if pr.is_agent:
            agent = self._agents.get(pr.id)
            m = self._member(room, agent["owner"]) if agent else None
            return bool(m and m["role"] in TERMINAL_ROLES and "execute" in eff[0]
                        and f"user:{agent['owner']}" in named)
        m = self._member(room, pr.id)
        return bool(m and m["role"] in TERMINAL_ROLES)

    TERMINAL_AUDIT_EVERY = 600.0

    def authorize_workspace(self, pr: Principal, room_id: str, link_id: str, action: str) -> dict:
        """The one place that answers 'may this principal do `action` on the workspace linked to this room?'
        action is status, tabs, panes or terminal. Fails closed: anything unknown is a 404, anything not
        explicitly granted a 403, and both are audited. Allowed terminal use is audited too, at most once
        per principal and link per TERMINAL_AUDIT_EVERY seconds, and at once after any denial or change."""
        with self._lock:
            room, _ = self._authorize(pr, room_id, "read", "workspace.access")
            link = self._links.get(link_id)
            if link is None or link["room_id"] != room_id:
                raise CollabError(404, "not found")
            if action not in LINK_GRANTS:
                raise CollabError(400, "unknown workspace action")
            gate = (pr.key, link_id)
            if action == "terminal":
                ok = self._terminal_ok(pr, room, link)
            elif pr.is_agent or (self._member(room, pr.id) or {}).get("role") == "guest":
                ok = False                                  # same audience as the links in room_state: people, not guests
            else:
                ok = action in link["grants"]
            if not ok:
                if action == "terminal":
                    self._term_gate.pop(gate, None)         # only a terminal denial re-arms the allow audit
                self._audit_denied(pr, f"workspace.{action}", "no_workspace_grant", room_id=room_id,
                                   resource_id=link_id, project_ids=[room["project_id"]])
                self.stats["policy_denied"] += 1
                raise CollabError(403, "forbidden")
            if action == "terminal":
                eff = self._effective(pr, room)
                fp = (tuple(sorted(eff[0])), tuple(link["grants"]), tuple(link["terminal_principals"]),
                      link["host_proof"]["gen"])
                last = self._term_gate.get(gate)
                now = self._now()
                if last is None or last[1] != fp or now - last[0] >= self.TERMINAL_AUDIT_EVERY:
                    self._commit("workspace_terminal_authorized", self._actor(
                        pr, project_id=room["project_id"], room_id=room_id, link_id=link_id,
                        action="workspace.terminal", outcome="allowed", project_ids=[room["project_id"]],
                        resource_id=link_id))                  # a log failure raises 503: no audit, no terminal
                    if len(self._term_gate) > 10_000:
                        self._term_gate.clear()
                    self._term_gate[gate] = (now, fp)
            return {"allowed": True, "link_id": link_id, "workspace_id": link["workspace_id"],
                    "host": link["host"], "action": action}

    def _link_view(self, pr: Principal, room: dict, lk: dict, manager: bool) -> dict:
        out = {"link_id": lk["link_id"], "room_id": lk["room_id"], "workspace_id": lk["workspace_id"],
               "host": lk["host"], "is_primary": lk["is_primary"], "grants": list(lk["grants"]),
               "linked_by": lk["linked_by"], "created_at": lk["created_at"],
               "host_verified": self._proof_ok(lk), "can_terminal": self._terminal_ok(pr, room, lk)}
        if manager:
            out["terminal_principals"] = list(lk["terminal_principals"])
        return out

    def _links_visible(self, pr: Principal, room: dict) -> list[dict]:
        """Links this person may see: owners see all; others only links that grant them something."""
        manager = self._is_link_manager(pr, room)
        return [self._link_view(pr, room, lk, manager)
                for lid in self._links_by_room.get(room["id"], ())
                if (lk := self._links[lid]) and (manager or lk["grants"])]

    def _missions_visible(self, pr: Principal, room: dict) -> list[dict]:
        manager = self._is_link_manager(pr, room)
        out = []
        for (rid, mid), ms in self._missions.items():
            if rid != room["id"]:
                continue
            if manager or pr.id in ms["members"]:
                out.append({"mission_id": mid, "room_id": rid, "linked_by": ms["linked_by"],
                            "created_at": ms["created_at"],
                            **({"members": list(ms["members"])} if manager else {})})
        return out

    def _no_guest(self, pr: Principal, room: dict) -> None:
        if (self._member(room, pr.id) or {}).get("role") == "guest":
            raise CollabError(403, "forbidden")

    def _link_budget(self, pr: Principal, room_id: str) -> None:
        """Every mutating link/mission call spends from a per-person and a per-room budget, so one owner
        cannot fill the shared log."""
        self._throttle("wslink:" + pr.id, 120, 600.0, "workspace link")
        self._throttle("wsroom:" + room_id, 300, 3600.0, "workspace link changes for this room")

    def list_workspace_links(self, pr: Principal, room_id: str) -> dict:
        self._require_user(pr)
        with self._lock:
            room, _ = self._authorize(pr, room_id, "read", "workspace.list")
            self._no_guest(pr, room)
            return {"links": self._links_visible(pr, room)}

    def _grants_body(self, pr: Principal, room: dict, body: dict) -> tuple[list, list]:
        grants = body.get("grants", list(DEFAULT_LINK_GRANTS))
        if (not isinstance(grants, list) or len(grants) > len(LINK_GRANTS)
                or not all(isinstance(g, str) and g in LINK_GRANTS for g in grants)):
            raise CollabError(400, "grants must be a subset of status, tabs, panes, terminal")
        grants = sorted(set(grants))
        tp = body.get("terminal_principals", [])
        if (not isinstance(tp, list) or len(tp) > MAX_TERMINAL_PRINCIPALS
                or not all(isinstance(x, str) and _KEY_RE.fullmatch(x) for x in tp)):
            raise CollabError(400, "invalid terminal_principals")
        tp = sorted(set(tp))
        if ("terminal" in grants) != bool(tp):
            raise CollabError(400, "terminal needs named terminal_principals, and they need the terminal grant")
        for key in tp:
            kind, _, ident = key.partition(":")
            if kind == "user":
                m = self._member(room, ident)
                if not m or m["role"] not in TERMINAL_ROLES:
                    raise CollabError(400, "terminal principals must be owners or contributors of the room")
            elif kind == "agent":
                if ident not in room["grants"] or self._agents.get(ident, {}).get("external"):
                    raise CollabError(400, "terminal agents must hold a grant in the room, and an external agent never does")
            else:
                raise CollabError(400, "invalid terminal_principals")
        return grants, tp

    PRIMARY_UNAVAILABLE = "primary is not available for this workspace"

    def _claim_primary(self, pr: Principal, host: str, ws: str, own_link_id: str, demote_other: bool):
        """Decide (no commit) whether `pr` may make (host, ws) primary for this link. Returns the proof and
        the link to demote in the SAME event, or raises one generic 409 that is identical whether the slot is
        taken, the host unregistered or the caller not its owner."""
        proof = self._proof_for(pr, host)
        if proof is None:
            raise CollabError(409, self.PRIMARY_UNAVAILABLE)
        cur_id = self._primary.get((host, ws))
        if cur_id is None or cur_id == own_link_id:
            return proof, None
        orm = self._rooms[self._links[cur_id]["room_id"]]
        if not demote_other or not (pr.admin or self._is_link_manager(pr, orm)):
            raise CollabError(409, self.PRIMARY_UNAVAILABLE)
        return proof, (cur_id, orm)

    def link_workspace(self, pr: Principal, room_id: str, body: dict) -> dict:
        self._require_user(pr)
        with self._lock:
            room, _ = self._authorize(pr, room_id, "invite", "workspace.link")
            host, ws = body.get("host"), body.get("workspace_id")
            if not isinstance(host, str) or not _FLEET_HOST.fullmatch(host):
                raise CollabError(400, "invalid host")
            if not isinstance(ws, str) or not _KEY_RE.fullmatch(ws):
                raise CollabError(400, "invalid workspace_id")
            primary = body.get("is_primary", False)
            if not isinstance(primary, bool) or not isinstance(body.get("demote_other", False), bool):
                raise CollabError(400, "is_primary and demote_other must be booleans")
            grants, tp = self._grants_body(pr, room, body)
            ids_here = self._links_by_room.get(room_id, ())
            if any(self._links[i]["host"] == host and self._links[i]["workspace_id"] == ws for i in ids_here):
                raise CollabError(409, "this workspace is already linked to the room")
            if len(ids_here) >= MAX_LINKS_PER_ROOM:
                raise CollabError(429, "workspace link limit reached for this room")
            self._link_budget(pr, room_id)
            proof, demote = self._proof_for(pr, host), None
            if "terminal" in grants and proof is None:
                raise CollabError(403, "a terminal grant needs a host registered to you")
            if primary:
                proof, demote = self._claim_primary(pr, host, ws, "", body.get("demote_other", False))
            lid = ids.new_id("grant")
            extra = self._demote_fields(demote)
            self._commit("workspace_linked", self._actor(
                pr, project_id=room["project_id"], room_id=room_id, link_id=lid, workspace_id=ws, host=host,
                is_primary=primary, grants=grants, terminal_principals=tp, host_proof=proof,
                action="workspace.link", outcome="allowed", resource_id=lid,
                **extra, project_ids=[room["project_id"], *([extra["demoted_project_id"]] if extra else [])]))
            return self._link_view(pr, room, self._links[lid], True)

    @staticmethod
    def _demote_fields(demote) -> dict:
        if not demote:
            return {}
        cur_id, orm = demote
        return {"demoted_link_id": cur_id, "room_ids": [orm["id"]], "demoted_project_id": orm["project_id"]}

    def _own_link(self, pr: Principal, room_id: str, link_id: str, audit_action: str) -> tuple[dict, dict]:
        room, _ = self._authorize(pr, room_id, "invite", audit_action)
        lk = self._links.get(link_id)
        if lk is None or lk["room_id"] != room_id:
            raise CollabError(404, "not found")
        return room, lk

    def unlink_workspace(self, pr: Principal, room_id: str, link_id: str) -> dict:
        self._require_user(pr)
        with self._lock:
            room, lk = self._own_link(pr, room_id, link_id, "workspace.unlink")
            self._link_budget(pr, room_id)
            self._commit("workspace_unlinked", self._actor(
                pr, project_id=room["project_id"], room_id=room_id, link_id=link_id, action="workspace.unlink",
                outcome="allowed", project_ids=[room["project_id"]], resource_id=link_id))
        self._xlink_sweep(room_id)          # T14: a cross-workspace link that leaned on this workspace lapses now
        return {"ok": True}

    def set_workspace_primary(self, pr: Principal, room_id: str, link_id: str, body: dict) -> dict:
        self._require_user(pr)
        primary, demote_other = body.get("primary", True), body.get("demote_other", False)
        if not isinstance(primary, bool) or not isinstance(demote_other, bool):
            raise CollabError(400, "primary and demote_other must be booleans")
        with self._lock:
            room, lk = self._own_link(pr, room_id, link_id, "workspace.primary")
            if primary and not lk["is_primary"]:
                self._link_budget(pr, room_id)
                proof, demote = self._claim_primary(pr, lk["host"], lk["workspace_id"], link_id, demote_other)
                extra = self._demote_fields(demote)
                self._commit("workspace_primary_set", self._actor(
                    pr, project_id=room["project_id"], room_id=room_id, link_id=link_id, host_proof=proof,
                    action="workspace.primary", outcome="allowed", resource_id=link_id,
                    **extra, project_ids=[room["project_id"], *([extra["demoted_project_id"]] if extra else [])]))
            elif not primary and lk["is_primary"]:
                self._link_budget(pr, room_id)
                self._commit("workspace_primary_cleared", self._actor(
                    pr, project_id=room["project_id"], room_id=room_id, link_id=link_id,
                    action="workspace.demote", outcome="allowed", reason="demoted_by_owner",
                    project_ids=[room["project_id"]], resource_id=link_id))
            return self._link_view(pr, room, self._links[link_id], True)

    def set_workspace_grants(self, pr: Principal, room_id: str, link_id: str, body: dict) -> dict:
        self._require_user(pr)
        with self._lock:
            room, lk = self._own_link(pr, room_id, link_id, "workspace.grants")
            grants, tp = self._grants_body(pr, room, body)
            self._link_budget(pr, room_id)
            extra = {}
            if "terminal" in grants:
                proof = self._proof_for(pr, lk["host"])
                if proof is None:
                    raise CollabError(403, "a terminal grant needs a host registered to you")
                extra["host_proof"] = proof
            self._commit("workspace_link_updated", self._actor(
                pr, project_id=room["project_id"], room_id=room_id, link_id=link_id, grants=grants,
                terminal_principals=tp, old_grants=list(lk["grants"]), action="workspace.grants",
                outcome="allowed", project_ids=[room["project_id"]], resource_id=link_id, **extra))
            out = self._link_view(pr, room, self._links[link_id], True)
        self._xlink_sweep(room_id)
        return out

    def graph_access(self, pr: Principal) -> dict | None:
        """Snapshot of what ``pr`` may read in the lineage graph, taken NOW under the service lock
        (so a removal, mission unlink or link-grant change is effective on the next request).
        A person who is a non-guest member of a room sees `room`-scoped lineage that names it; a
        mission member sees `mission`-scoped lineage that names the mission; an event that also
        names a workspace additionally needs a link granting status/tabs/panes from that room.
        Guests, agents and people with no non-guest membership get ``None`` (no graph at all):
        room access never implies more than the metadata the room's links grant."""
        if pr.kind != "user":
            return None
        with self._lock:
            if not self._ok(pr.id):
                return None
            rooms, room_ws = set(), set()
            for rid, room in self._rooms.items():
                m = self._member(room, pr.id)
                if not m or m["role"] == "guest":
                    continue
                rooms.add(rid)
                for lid in self._links_by_room.get(rid, ()):
                    lk = self._links[lid]
                    if set(lk["grants"]) & LINK_VIEW_GRANTS:
                        room_ws.add((rid, lk["workspace_id"]))
            if not rooms:
                return None
            # a mission is readable only through the room that linked it, by the people that link lists
            room_missions = {(rid, mid) for (rid, mid), ms in self._missions.items()
                             if rid in rooms and pr.id in ms["members"]}
            return {"rooms": frozenset(rooms), "room_ws": frozenset(room_ws),
                    "room_missions": frozenset(room_missions)}

    def graph_read(self, pr: Principal, parts: list[str], query: dict) -> dict:
        """Lineage graph read for a signed-in person. Hidden, missing and 'no graph for you' all
        answer one 404; nothing here lets a read widen what the graph's own checks allow."""
        from .graph import read_graph
        graph = self.graph_source() if self.graph_source is not None else None
        access = self.graph_access(pr) if graph is not None else None
        if access is None:
            raise CollabError(404, "not found")
        principal = {"kind": "member", "id": f"user:{pr.id}", "authenticated_by": "collab", "access": access}
        contexts = None
        room_id = query.get("room_id")
        if parts[:1] == ["lineage"]:
            return self._lineage_read(pr, graph, access, parts[1:], query)
        if parts == ["nodes"]:
            # a listing is always ONE room's view (its own scope plus the missions it linked that this person
            # belongs to), so its cost never depends on other rooms' data
            if room_id is None:
                raise CollabError(400, "room_id is required")
            if room_id not in access["rooms"]:
                raise CollabError(404, "not found")
            contexts = frozenset({("room", room_id, None), *(("mission", m, r) for r, m in access["room_missions"]
                                                             if r == room_id)})
        try:
            return read_graph(graph, principal, parts, lambda name: query.get(name), contexts=contexts)
        except KeyError:
            raise CollabError(404, "not found") from None
        except ValueError as exc:
            raise CollabError(400, str(exc)) from None

    def _lineage_read(self, pr: Principal, graph, access: dict, parts: list[str], query: dict) -> dict:
        """T09 read-only lineage views of ONE room: ``lineage`` (graph, optionally as of a replay position),
        ``lineage/timeline`` (Gantt data) and ``lineage/events`` (the reader's visible ledger). They sit behind
        the same access snapshot, the same fail-closed 404 and the same per-room context as the node listing;
        a replay is filtered by the reader's membership AT THIS REQUEST, never the membership of the past."""
        from .graph import EDGE_TYPES, NODE_TYPES
        room_id = query.get("room_id")
        if room_id is None:
            raise CollabError(400, "room_id is required")
        if room_id not in access["rooms"]:
            raise CollabError(404, "not found")
        contexts = frozenset({("room", room_id, None), *(("mission", m, r) for r, m in access["room_missions"]
                                                         if r == room_id)})
        principal = {"kind": "member", "id": f"user:{pr.id}", "authenticated_by": "collab", "access": access}
        since = None
        if self.replay_history == "since_join":
            with self._lock:
                joined = ((self._rooms.get(room_id) or {}).get("members", {}).get(pr.id) or {}).get("joined_at")
            try:
                since = datetime.fromisoformat(joined).timestamp()
            except (TypeError, ValueError):
                since = self._now()        # no recorded join time: fail closed, history starts now

        def num(name, default):
            raw = query.get(name)
            if raw in (None, ""):
                return default
            if not re.fullmatch(r"[0-9]{1,9}", str(raw)):          # ASCII digits only, bounded
                raise CollabError(400, f"{name} must be a non-negative integer")
            return int(raw)

        def token(name):
            raw = query.get(name)
            if raw in (None, ""):
                return None
            if not re.fullmatch(r"[0-9a-f]{32}", str(raw)):
                raise CollabError(400, f"{name} is not a valid page token")
            return str(raw)

        def kinds(name, allowed):
            raw = query.get(name)
            if not raw:
                return None
            got = {x for x in str(raw).split(",") if x}
            if not got or not got <= allowed:
                raise CollabError(400, f"unknown {name}")
            return got
        def wanted_edges():
            got = kinds("edge_types", EDGE_TYPES)
            if not self._ctx_on(self.ctx_cfg(), "cross_links"):      # T14: switched off = no link in any lineage view
                got = (set(EDGE_TYPES) if got is None else got) - {"cross_link"}
            return got
        step = num("step", None)
        at = query.get("at") or None
        with self._lock:                # a replay rebuilds the past: bound how often one person can ask
            self._throttle(f"lineage:{pr.id}", 240, 60.0, "lineage")
        try:
            if parts == []:
                return graph.room_lineage(principal, contexts, step=step, at=at, since=since,
                                          node_types=kinds("node_types", NODE_TYPES),
                                          edge_types=wanted_edges(),
                                          limit=num("limit", 200), offset=num("offset", 0))
            if parts == ["timeline"]:
                return graph.room_timeline(principal, contexts, step=step, at=at, since=since)
            if parts == ["events"]:
                return graph.room_ledger(principal, contexts, cursor=token("cursor"), limit=num("limit", 200),
                                         since=since)
        except ValueError as exc:
            raise CollabError(400, str(exc)) from None
        raise CollabError(404, "not found")

    def list_room_missions(self, pr: Principal, room_id: str) -> dict:
        self._require_user(pr)
        with self._lock:
            room, _ = self._authorize(pr, room_id, "read", "mission.list")
            self._no_guest(pr, room)
            return {"missions": self._missions_visible(pr, room)}

    def link_mission(self, pr: Principal, room_id: str, body: dict) -> dict:
        """Tie a room to a mission id (an opaque string from Pi). Only the listed room members see the link."""
        self._require_user(pr)
        with self._lock:
            room, _ = self._authorize(pr, room_id, "invite", "mission.link")
            mid, members = body.get("mission_id"), body.get("members", [])
            if not isinstance(mid, str) or not _KEY_RE.fullmatch(mid):
                raise CollabError(400, "invalid mission_id")
            if (not isinstance(members, list) or len(members) > self.max_members_per_room
                    or not all(isinstance(u, str) for u in members)):
                raise CollabError(400, "members must be a list of user ids")
            for u in members:
                m = self._member(room, u)
                if not m or m["role"] == "guest":
                    raise CollabError(400, "mission members must be non-guest members of the room")
            if (room_id, mid) in self._missions:
                raise CollabError(409, "this mission is already linked to the room")
            if sum(1 for r, _ in self._missions if r == room_id) >= MAX_MISSIONS_PER_ROOM:
                raise CollabError(429, "mission link limit reached for this room")
            self._link_budget(pr, room_id)
            self._commit("mission_linked", self._actor(
                pr, project_id=room["project_id"], room_id=room_id, mission_id=mid, members=sorted(set(members)),
                action="mission.link", outcome="allowed", project_ids=[room["project_id"]], resource_id=mid))
            return {"mission_id": mid, "room_id": room_id, "members": sorted(set(members))}

    def unlink_mission(self, pr: Principal, room_id: str, mission_id: str) -> dict:
        self._require_user(pr)
        with self._lock:
            room, _ = self._authorize(pr, room_id, "invite", "mission.unlink")
            if (room_id, mission_id) not in self._missions:
                raise CollabError(404, "not found")
            self._link_budget(pr, room_id)
            self._commit("mission_unlinked", self._actor(
                pr, project_id=room["project_id"], room_id=room_id, mission_id=mission_id,
                action="mission.unlink", outcome="allowed", project_ids=[room["project_id"]],
                resource_id=mission_id))
        return {"ok": True}

    # -- tasks (work for agents)
    def _task_status(self, t: dict) -> str:
        if t["status"] in ("pending", "accepted") and (
                self._now() >= t["bounds"]["expires_at"] if "bounds" in t
                else self._now() - t["created_epoch"] >= TASK_TTL):
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
            room, _ = self._authorize(pr, room_id, "instruct" if pr.is_agent else "task", "task.create")
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
            if ikey is not None and (prior := self._idem.get((pr.key, room_id, "task:" + ikey))):
                if prior[0] != _digest(json.dumps([room_id, tid_, text])):
                    raise CollabError(409, "idempotency_key already used for a different task")
                return {"task_id": prior[1], "status": "pending", "replayed": True}
            depth, parent, root = 0, body.get("parent_task_id"), pr.owner
            if pr.is_agent:
                # Agents only delegate: every agent-made task traces to a task that someone gave
                # this agent and it accepted, so a chain always starts with a person.
                pt = self._tasks.get(parent) if isinstance(parent, str) else None
                if (not pt or pt["room_id"] != room_id or pt["target"] != pr.id
                        or self._task_status(pt) != "accepted"):
                    raise CollabError(400, "agents delegate: parent_task_id must be a task you accepted in this room")
                if pt["by_kind"] == "agent" and pt["by_id"] == tid_:
                    raise CollabError(400, "cannot hand a task straight back to the agent that gave it")
                depth, root = pt["depth"] + 1, pt["root"]
                if depth > MAX_TASK_DEPTH:
                    raise CollabError(400, "task chain is too deep")
            elif parent is not None:
                raise CollabError(400, "parent_task_id is for agents delegating a task")
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
                visibility_scope="room", action="task.create", outcome="allowed",
                project_ids=[room["project_id"]], resource_id=task_id))
            if ikey is not None:
                self._idem[(pr.key, room_id, "task:" + ikey)] = (_digest(json.dumps([room_id, tid_, text])), task_id)
        return {"task_id": task_id, "status": "pending", "event": self._view(ev)}

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
        for tid in [i for i, t in self._tasks.items()
                    if self._task_status(t) not in ("pending", "accepted")
                    and now - t["created_epoch"] > (14 if "bounds" in t else 7) * 86400]:
            t = self._tasks.pop(tid)
            idx = self._tasks_by_target.get(t["target"])
            if idx and tid in idx:
                idx.remove(tid)
        for rid in [i for i, r in self._requests.items() if not r["open"] and now - r["created_epoch"] > REQUEST_TTL]:
            r = self._requests.pop(rid)
            self._requests_by_room[r["room_id"]].discard(rid)
            self._requests_by_agent[r["agent_id"]].discard(rid)

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
                "created_at": t["created_at"], "updated_at": t["updated_at"],
                **({"bounds": t["bounds"], "version": t["version"]} if "bounds" in t else {})}

    def list_tasks(self, pr: Principal, room_id: str, limit: int = 100) -> dict:
        with self._lock:
            _, first = self._authorize(pr, room_id, "read", "task.list")
            self._no_external(pr)
            rows = [(t["seq"], tid, t) for tid, t in self._tasks.items()
                    if t["room_id"] == room_id and t["seq"] >= first]
            rows.sort(key=lambda r: r[0], reverse=True)
            out = [dict(self._task_json(tid, t), untrusted=True) for _, tid, t in rows[:limit]]
        return {"tasks": out}

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
        if not pr.is_agent or pr.external:
            raise CollabError(403, "forbidden")
        with self._lock:
            out = []
            for tid in list(self._tasks_by_target.get(pr.id, ())):
                t = self._tasks[tid]
                if self._task_status(t) not in ("pending", "accepted") or "bounds" in t:
                    continue                            # a bounded delegation is never an instruction to execute
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
        if not pr.is_agent or pr.external:
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

    # -- bounded delegation and artifact proposals (T11)
    # A delegation is a task in the SAME store with a ``bounds`` block, addressed to an external coworker agent
    # invited with ``proposals``. The delegate holds no ``execute`` right and no terminal: its only output is a
    # proposal that sits in quarantine until a room owner accepts it, and even an accepted proposal is only
    # recorded (graph node + ``produced`` edges), never applied. MCP, A2A and REST call these same methods.
    def _need_delegation(self) -> None:
        if not self.delegation_enabled:
            self.stats["policy_denied"] += 1
            raise CollabError(403, "bounded delegation is switched off on this service", code="delegation_disabled")

    @staticmethod
    def _refuse(exc: dlg.DelegationError) -> CollabError:
        return CollabError(exc.status, exc.message, code=exc.code)

    def is_hcn_node(self, node_id: str) -> bool:
        """Graph ids only this service writes (delegation tasks and accepted proposals): the ingest path refuses
        everybody else, so provenance cannot be pre-empted, edited or attached to by an ingest key."""
        return isinstance(node_id, str) and node_id.startswith(dlg.HCN_NODE_PREFIXES)

    def _delegate_task(self, pr: Principal, task_id: Any) -> tuple[dict, dict, dict]:
        """(task, room, agent) for a bounded task addressed to THIS external agent, in its bound room, whose
        requester still has the authority to ask; every miss is the same 404."""
        a = self._require_external(pr)
        t = self._tasks.get(task_id) if isinstance(task_id, str) else None
        room = self._rooms.get(t["room_id"]) if t else None
        if (not a["external"].get("proposals") or not t or "bounds" not in t or t["target"] != pr.id
                or not room or room["id"] != a["external"]["room_id"] or self._effective(pr, room) is None
                or not self._creator_valid(t, room)):
            raise CollabError(404, "not found")
        return t, room, a

    def _task_usage(self, t: dict) -> dict:
        return {"steps": t["steps"], "proposals": t["proposals"], "tokens_reported": t["tokens"]}

    def _proposal_state(self, p: dict) -> str:
        if p["state"] == "quarantined" and self._now() - p["created_epoch"] >= dlg.PROPOSAL_TTL:
            return "expired"
        return p["state"]

    def _proposal_brief(self, p: dict) -> dict:
        return {"proposal_id": p["id"], "kind": p["kind"], "state": self._proposal_state(p), "digest": p["digest"],
                "size": p["size"], "created_at": p["created_at"], "promoted": p["promoted"]}

    def _delegation_view(self, tid: str, t: dict) -> dict:
        out = self._task_json(tid, t)
        out.update(usage=self._task_usage(t), delegation=True,
                   proposals=[self._proposal_brief(self._proposals[i]) for i in self._proposals_by_task.get(tid, ())])
        return out

    def create_delegation(self, pr: Principal, room_id: str, body: dict) -> dict:
        """A room owner delegates ONE bounded task to a coworker agent that was invited with ``proposals``."""
        self._require_user(pr)
        self._need_delegation()
        with self._lock:
            room, _ = self._authorize(pr, room_id, "invite", "delegation.create")
            allowed = {"target_agent_id", "text", "idempotency_key", "scope", "budget", "expires_in_seconds", "output_kinds"}
            if set(body) - allowed:
                raise CollabError(400, "unknown field")
            if self.lineage_sink is None:
                # Accepting a proposal records it in the lineage graph; a service without one would only ever
                # strand proposals in quarantine, so it does not take delegations.
                raise CollabError(409, "this service has no lineage graph, so it cannot take delegations",
                                  code="graph_unavailable")
            aid = body.get("target_agent_id")
            target = self._agents.get(aid) if isinstance(aid, str) else None
            x = (target or {}).get("external")
            if (not x or x["room_id"] != room_id or not x.get("proposals")
                    or self._external_state(target, room, aid) != "active"):
                raise CollabError(404, "no such agent in this room")
            text = self._task_text(body.get("text"), "text")
            try:
                bounds = dlg.parse_bounds({k: body[k] for k in ("scope", "budget", "expires_in_seconds", "output_kinds") if k in body},
                                          self._now(), min(target["expires_at"], x["hard_expiry"]), TASK_TTL)
            except dlg.DelegationError as exc:
                raise self._refuse(exc) from None
            ikey = body.get("idempotency_key")
            if ikey is not None and (not isinstance(ikey, str) or not _KEY_RE.match(ikey)):
                raise CollabError(400, "invalid idempotency_key")
            fp = _digest(json.dumps([room_id, aid, text, {k: body.get(k) for k in sorted(allowed - {"text", "idempotency_key", "target_agent_id"})}],
                                    sort_keys=True, default=str))
            if ikey is not None and (prior := self._idem.get(("user:" + pr.id, room_id, "deleg:" + ikey))):
                if prior[0] != fp:
                    raise CollabError(409, "idempotency_key already used for a different delegation")
                return {"task_id": prior[1], "status": "pending", "replayed": True}
            self._prune()
            open_now = sum(1 for i in self._tasks_by_target.get(aid, ())
                           if "bounds" in self._tasks[i] and self._task_status(self._tasks[i]) in ("pending", "accepted"))
            if open_now >= dlg.MAX_OPEN_BOUNDED_PER_AGENT:
                raise CollabError(429, "that agent has too many open delegations")
            self._throttle("deleg:" + pr.key, dlg.DELEGATIONS_PER_10_MIN, 600.0, "delegation")
            task_id = ids.new_id("grant")
            ev = self._commit("task_created", self._actor(
                pr, project_id=room["project_id"], room_id=room_id, task_id=task_id, target_agent_id=aid, text=text,
                depth=0, parent_task_id=None, created_by_kind="user", created_by_id=pr.id, created_epoch=self._now(),
                root_user_id=pr.owner, bounds=bounds, idempotency_key=ikey, fingerprint=fp, visibility_scope="room",
                action="delegation.create", outcome="allowed", project_ids=[room["project_id"]], resource_id=task_id))
        return {"task_id": task_id, "status": "pending", "version": 1, "bounds": bounds, "event": self._view(ev)}

    def list_delegations(self, pr: Principal, room_id: str) -> dict:
        """Bounded tasks of a room with their proposals' state (never the proposals' content), to room owners."""
        self._require_user(pr)
        with self._lock:
            self._authorize(pr, room_id, "invite", "delegation.list")
            rows = [(t["seq"], tid, t) for tid, t in self._tasks.items() if t["room_id"] == room_id and "bounds" in t]
            rows.sort(key=lambda r: r[0], reverse=True)
            out = [dict(self._delegation_view(tid, t), untrusted=True) for _, tid, t in rows[:100]]
        return {"delegations": out, "enabled": self.delegation_enabled}

    def delegations_inbox(self, pr: Principal) -> dict:
        """The bounded tasks addressed to this external agent that it may still act on."""
        self._need_delegation()
        with self._lock:
            self._delegate_task_check(pr)
            self._throttle("deleg-inbox:" + pr.id, 120, 60.0, "inbox")
            out = []
            for tid in list(self._tasks_by_target.get(pr.id, ())):
                t = self._tasks[tid]
                if "bounds" not in t or self._task_status(t) not in ("pending", "accepted"):
                    continue
                try:
                    self._delegate_task(pr, tid)
                except CollabError:
                    continue
                out.append(self._delegation_view(tid, t))
        return {"delegations": out}

    def _delegate_task_check(self, pr: Principal) -> None:
        a = self._require_external(pr)
        if not a["external"].get("proposals"):
            raise CollabError(403, "forbidden")

    def delegation_status(self, pr: Principal, task_id: Any) -> dict:
        self._need_delegation()
        with self._lock:
            t, _, _ = self._delegate_task(pr, task_id)
            return self._delegation_view(task_id, t)

    _RESPONSES = {"accept": ("pending", "accepted"), "decline": ("pending", "declined"),
                  "complete": ("accepted", "done"), "fail": ("pending", "accepted", "failed"),
                  "cancel": ("pending", "accepted", "cancelled")}

    def delegation_respond(self, pr: Principal, task_id: Any, body: dict) -> dict:
        """accept | decline | progress | complete | fail | cancel, by the delegate. State-changing calls carry the
        ``expected_version`` they last saw: a task that changed underneath them (cancelled, expired, already
        answered) is refused as stale rather than silently overwritten."""
        self._need_delegation()
        with self._lock:
            t, room, a = self._delegate_task(pr, task_id)
            room_id = room["id"]
            x = a["external"]
            if set(body) - {"action", "expected_version", "idempotency_key", "note", "result", "tokens_used"}:
                self._audit_denied(pr, "delegation.respond", "forged_identity", room_id=room_id,
                                   project_ids=[x["project_id"]], agent_id=pr.id)
                raise CollabError(400, "unknown field")
            action = body.get("action")
            if not isinstance(action, str) or action not in (*self._RESPONSES, "progress"):
                raise CollabError(400, "action must be accept, decline, progress, complete, fail or cancel")
            ikey, ver = body.get("idempotency_key"), body.get("expected_version")
            if ikey is not None and (not isinstance(ikey, str) or not _KEY_RE.match(ikey)):
                raise CollabError(400, "invalid idempotency_key")
            if ver is not None and (isinstance(ver, bool) or not isinstance(ver, int)):
                raise CollabError(400, "expected_version must be an integer")
            fp = _digest(json.dumps([task_id, action, body.get("note"), body.get("result"), body.get("tokens_used")]))
            if ikey is not None and (prior := self._idem.get((pr.id, room_id, "resp:" + ikey))):
                if prior[0] != fp:
                    raise CollabError(409, "idempotency_key already used for a different answer")
                return {"task_id": task_id, "status": self._task_status(t), "version": t["version"], "replayed": True}
            if ver is None and action not in ("decline", "cancel"):
                raise CollabError(400, "expected_version is required", code="version_required")
            status = self._task_status(t)
            if ver is not None and ver != t["version"]:
                self._audit_denied(pr, "delegation.respond", "stale_task_state", room_id=room_id,
                                   project_ids=[x["project_id"]], resource_id=task_id, agent_id=pr.id)
                raise CollabError(409, f"stale task state: the task is at version {t['version']}", code="stale_task_state")
            allowed_from = {"progress": ("accepted",), **{k: v[:-1] for k, v in self._RESPONSES.items()}}[action]
            if status not in allowed_from:
                raise CollabError(409, f"the task is {status}", code="task_" + status)
            note = ext.clean_text(body.get("note"), dlg.MAX_NOTE)
            result = ext.clean_text(body.get("result"), 1000) if body.get("result") is not None else None
            tokens = body.get("tokens_used", 0)
            if isinstance(tokens, bool) or not isinstance(tokens, int) or not 0 <= tokens <= dlg.MAX_TOKENS:
                raise CollabError(400, "tokens_used must be a non-negative integer")
            self._throttle("deleg-resp:" + pr.key, dlg.RESPONSES_PER_MINUTE, 60.0, "task answer")
            base = dict(project_id=x["project_id"], room_id=room_id, task_id=task_id, target_agent_id=pr.id,
                        idempotency_key=ikey, fingerprint=fp, visibility_scope="room", outcome="allowed",
                        project_ids=[x["project_id"]], resource_id=task_id)
            b = t["bounds"]["budget"]
            if action == "progress":
                if t["steps"] + 1 > b["max_steps"] or t["tokens"] + tokens > b["max_tokens"]:
                    return self._exhaust(pr, t, task_id, base)
                self._commit("task_progress", self._actor(pr, note=note, tokens=tokens, action="delegation.progress", **base))
            else:
                new = self._RESPONSES[action][-1]
                self._commit("task_updated", self._actor(pr, status=new, result=result or note or None,
                                                         action="delegation." + action, **base))
            return {"task_id": task_id, "status": self._task_status(t), "version": t["version"]}

    def _exhaust(self, pr: Principal, t: dict, task_id: str, base: dict) -> dict:
        """The delegate went past its step or token budget: the task is failed, further answers are refused."""
        self._audit_denied(pr, "delegation.budget", "budget_exhausted", room_id=t["room_id"],
                           project_ids=base["project_ids"], resource_id=task_id, agent_id=pr.id)
        self._commit("task_updated", self._actor(pr, status="failed", result="budget exhausted",
                                                 action="delegation.budget", **{**base, "idempotency_key": None}))
        raise CollabError(409, "the task's budget is used up; it is closed", code="budget_exhausted")

    def submit_proposal(self, pr: Principal, task_id: Any, body: dict) -> dict:
        """The delegate's ONLY output: a proposal that goes to quarantine. Nothing here touches a workspace or
        the graph. Every limit (kind, scope, size, count, steps, expiry, rate) is enforced here, once."""
        self._need_delegation()
        with self._lock:
            t, room, a = self._delegate_task(pr, task_id)
            room_id, x = room["id"], a["external"]
            audit = dict(room_id=room_id, project_ids=[x["project_id"]], resource_id=task_id, agent_id=pr.id)
            if set(body) - {"kind", "title", "content", "url", "refs", "supersedes", "expected_version", "idempotency_key"}:
                self._audit_denied(pr, "delegation.proposal", "forged_identity", **audit)
                self.stats["forged_identity"] += 1
                raise CollabError(400, "identity and scope are derived from the credential, not the request")
            try:
                rec = dlg.build_proposal(task_id, t["bounds"], body)
            except dlg.DelegationError as exc:
                self._audit_denied(pr, "delegation.proposal", exc.code or "invalid", **audit)
                self.stats["policy_denied"] += 1
                raise self._refuse(exc) from None
            ikey, ver, sup = body.get("idempotency_key"), body.get("expected_version"), body.get("supersedes")
            if ikey is not None and (not isinstance(ikey, str) or not _KEY_RE.match(ikey)):
                raise CollabError(400, "invalid idempotency_key")
            pid = rec["proposal_id"]
            if ikey is not None and (prior := self._idem.get((pr.id, room_id, "prop:" + ikey))) and prior[0] != rec["digest"]:
                raise CollabError(409, "idempotency_key already used for a different proposal")
            same = self._proposals.get(pid)
            if same and same["task_id"] == task_id:             # the same content again: a replay, costs nothing
                return {"proposal_id": pid, "state": self._proposal_state(same), "digest": rec["digest"],
                        "version": t["version"], "replayed": True}
            if isinstance(ver, bool) or not isinstance(ver, int):
                raise CollabError(400, "expected_version is required", code="version_required")
            if ver != t["version"]:
                self._audit_denied(pr, "delegation.proposal", "stale_task_state", **audit)
                raise CollabError(409, f"stale task state: the task is at version {t['version']}", code="stale_task_state")
            status = self._task_status(t)
            if status != "accepted":
                self._audit_denied(pr, "delegation.proposal", "task_" + status, **audit)
                raise CollabError(409, f"the task is {status}: accept it first, and only while it is open", code="task_" + status)
            b = t["bounds"]["budget"]
            if t["proposals"] >= b["max_proposals"] or t["steps"] + 1 > b["max_steps"]:
                self._audit_denied(pr, "delegation.proposal", "budget_exhausted", **audit)
                raise CollabError(409, "the task's proposal or step budget is used up", code="budget_exhausted")
            mine = self._proposals_by_agent.get(pr.id, ())
            sponsor_total = sum(len(self._proposals_by_agent.get(i, ())) for i in self._externals_by_sponsor(x["sponsor"]))
            quarantined = sum(1 for i in self._proposals_by_room.get(room_id, ())
                              if self._proposal_state(self._proposals[i]) == "quarantined")
            if (len(mine) >= dlg.MAX_PROPOSALS_PER_AGENT or sponsor_total >= dlg.MAX_PROPOSALS_PER_SPONSOR
                    or quarantined >= dlg.MAX_QUARANTINED_PER_ROOM):
                self._audit_denied(pr, "delegation.proposal", "quota", **audit)
                self.stats["rate_limited"] += 1
                raise CollabError(429, "too many proposals; the room owner must decide the open ones first", code="quota")
            if sup is not None:
                prior_p = self._proposals.get(sup) if isinstance(sup, str) else None
                if (not prior_p or prior_p["task_id"] != task_id or prior_p["agent_id"] != pr.id
                        or self._proposal_state(prior_p) != "quarantined"):
                    raise CollabError(409, "supersedes must name your own undecided proposal for this task", code="cannot_supersede")
            self._throttle("prop:" + pr.id, dlg.PROPOSALS_PER_10_MIN, 600.0, "proposal")
            self._commit("proposal_submitted", self._actor(
                pr, project_id=x["project_id"], room_id=room_id, task_id=task_id, sponsor=x["sponsor"],
                supersedes=sup, idempotency_key=ikey, created_epoch=self._now(), visibility_scope="room",
                action="delegation.proposal", outcome="allowed", project_ids=[x["project_id"]], resource_id=pid,
                **{k: rec[k] for k in ("proposal_id", "digest", "kind", "title", "content", "url", "refs", "size", "flags")}))
            return {"proposal_id": pid, "state": "quarantined", "digest": rec["digest"], "version": t["version"]}

    def _externals_by_sponsor(self, sponsor: str) -> list[str]:
        return [aid for rid_agents in self._externals_by_room.values() for aid in rid_agents
                if self._agents[aid]["external"]["sponsor"] == sponsor]

    def withdraw_proposal(self, pr: Principal, proposal_id: Any, body: dict) -> dict:
        self._need_delegation()
        with self._lock:
            p = self._proposals.get(proposal_id) if isinstance(proposal_id, str) else None
            if set(body) - {"idempotency_key"}:
                raise CollabError(400, "unknown field")
            a = self._require_external(pr)
            if not p or p["agent_id"] != pr.id or not a["external"].get("proposals"):
                raise CollabError(404, "not found")
            state = self._proposal_state(p)
            if state == "withdrawn":
                return {"proposal_id": proposal_id, "state": "withdrawn", "replayed": True}
            if state != "quarantined":
                raise CollabError(409, f"the proposal is {state}", code="proposal_" + state)
            self._commit("proposal_decided", self._actor(
                pr, project_id=p["project_id"], room_id=p["room_id"], proposal_id=proposal_id, decision="withdrawn",
                reason=None, visibility_scope="room", action="delegation.withdraw", outcome="allowed",
                project_ids=[p["project_id"]], resource_id=proposal_id))
        return {"proposal_id": proposal_id, "state": "withdrawn"}

    # -- owner side: review, accept, reject
    def list_proposals(self, pr: Principal, room_id: str) -> dict:
        self._require_user(pr)
        with self._lock:
            self._authorize(pr, room_id, "invite", "proposal.list")
            out = [dict(self._proposal_brief(self._proposals[i]), title=self._proposals[i]["title"],
                        task_id=self._proposals[i]["task_id"], agent_id=self._proposals[i]["agent_id"],
                        flags=self._proposals[i]["flags"], untrusted=True)
                   for i in reversed(self._proposals_by_room.get(room_id, ()))][:200]
        return {"proposals": out, "enabled": self.delegation_enabled}

    def get_proposal(self, pr: Principal, room_id: str, proposal_id: str) -> dict:
        """The quarantined content, for a room owner to review. It is data: marked untrusted, not executable
        and not applied, and the reviewer's accept must echo the digest of exactly this content."""
        self._require_user(pr)
        with self._lock:
            self._authorize(pr, room_id, "invite", "proposal.read")
            p = self._proposals.get(proposal_id)
            if not p or p["room_id"] != room_id:
                raise CollabError(404, "not found")
            ag = self._agents.get(p["agent_id"], {})
            return dict(self._proposal_brief(p), task_id=p["task_id"], agent_id=p["agent_id"],
                        delegate_label=(ag.get("external") or {}).get("label"), title=p["title"], content=p["content"],
                        url=p["url"], refs=p["refs"], flags=p["flags"], supersedes=p["supersedes"],
                        decided_by=p["decided_by"], decided_at=p["decided_at"], reason=p["reason"],
                        trust="untrusted", executable=False, applied=False)

    def decide_proposal(self, pr: Principal, room_id: str, proposal_id: str, body: dict) -> dict:
        """A room owner accepts or rejects a quarantined proposal. Accepting needs the digest of the content
        that was reviewed, is terminal, is idempotent, and records the proposal in the lineage graph (it never
        applies anything). A person only: no agent credential reaches this, over any protocol."""
        self._require_user(pr)
        if set(body) - {"decision", "expected_digest", "reason", "idempotency_key"}:
            raise CollabError(400, "unknown field")
        decision = body.get("decision")
        if not isinstance(decision, str) or decision not in ("accept", "reject"):
            raise CollabError(400, "decision must be accept or reject")
        if decision == "accept":
            self._need_delegation()
        with self._lock:
            room, _ = self._authorize(pr, room_id, "invite", "proposal.decide")
            p = self._proposals.get(proposal_id) if isinstance(proposal_id, str) else None
            if not p or p["room_id"] != room_id:
                raise CollabError(404, "not found")
            audit = dict(room_id=room_id, project_ids=[p["project_id"]], resource_id=proposal_id)
            if body.get("expected_digest") != p["digest"]:
                self._audit_denied(pr, "proposal.decide", "digest_mismatch", **audit)
                raise CollabError(409, "expected_digest does not match the proposal you were shown", code="digest_mismatch")
            state = self._proposal_state(p)
            want = "accepted" if decision == "accept" else "rejected"
            if state == want:
                replay = True                                   # the same decision again: no new event
            elif state != "quarantined":
                raise CollabError(409, f"the proposal is {state}", code="proposal_" + state)
            else:
                replay = False
                reason = ext.clean_text(body.get("reason"), dlg.MAX_REASON) or None
                self._throttle("decide:" + pr.key, dlg.DECISIONS_PER_MINUTE, 60.0, "decision")
                if decision == "accept":
                    ag, t = self._agents.get(p["agent_id"]), self._tasks.get(p["task_id"])
                    # Revocation is distrust: a delegate that was revoked or removed from the room, or a task
                    # that was cancelled / declined / failed, leaves its quarantined proposals unacceptable.
                    # (A credential that merely lapsed, or a delegation whose time ran out after the proposal
                    # was made in budget, does not.)
                    if not ag or ag["status"] != "active" or p["agent_id"] not in room["grants"]:
                        self._audit_denied(pr, "proposal.decide", "delegate_revoked", **audit)
                        raise CollabError(409, "the delegate was revoked or removed; reject this proposal", code="delegate_revoked")
                    if t is None or t["status"] in ("cancelled", "declined", "failed", "pending"):
                        self._audit_denied(pr, "proposal.decide", "task_closed", **audit)
                        raise CollabError(409, "the delegation was cancelled, declined or failed", code="task_closed")
                    if self.lineage_sink is None:
                        raise CollabError(409, "this service has no lineage graph", code="graph_unavailable")
                self._commit("proposal_decided", self._actor(
                    pr, project_id=p["project_id"], room_id=room_id, proposal_id=proposal_id, decision=want,
                    reason=reason, digest=p["digest"], visibility_scope="room", action="proposal." + decision,
                    outcome="allowed", project_ids=[p["project_id"]], resource_id=proposal_id))
        promoted = self._promote(proposal_id) if decision == "accept" else False
        out = {"proposal_id": proposal_id, "state": want, "replayed": replay}
        if decision == "accept":
            out.update(promoted=promoted, applied=False,
                       retry="repeat this request to finish recording it in the graph" if not promoted else None)
        return out

    def _promote(self, proposal_id: str) -> bool:
        """Record an accepted proposal in the lineage graph: a delegation task node, an artifact node and a
        ``produced`` edge, written by the SERVICE (principal kind ``collab``), never by the delegate. Event
        ids and sequences are derived from the proposal id, so a repeat is a harmless duplicate. Returns True
        once everything is in the graph. Nothing here applies the proposal to anything."""
        with self._lock:
            p = self._proposals.get(proposal_id)
            if not p or p["state"] != "accepted":
                return False
            if p["promoted"]:
                return True
            sink = self.lineage_sink
            ag = self._agents.get(p["agent_id"], {})
            label = (ag.get("external") or {}).get("label", "")
            t = self._tasks.get(p["task_id"], {})
            tnode, anode = "task:hcn-delegation." + p["task_id"], "artifact:hcn-proposal." + proposal_id
            attrs = {"source": "hcn-proposal", "kind": p["kind"], "digest": p["digest"], "bytes": p["size"],
                     "delegate_agent": p["agent_id"], "delegate_label": label, "delegated_by": p["sponsor"],
                     "accepted_by": p["decided_by"], "accepted_at": p["decided_at"], "delegation_id": p["task_id"],
                     "trust": "accepted-untrusted-content", "applied": False, "executable": False}
            if p["kind"] == "link":
                attrs["link_host"] = (p["url"] or "").split("/")[2] if p["url"] else None
            room_id, source = p["room_id"], "hcn-" + proposal_id[4:28]
            steps = [("node.upsert", tnode, {"node_type": "task", "node_id": tnode, "label": (t.get("text") or "delegation")[:120],
                                             "state": "accepted", "attrs": {"source": "hcn-delegation", "delegate_agent": p["agent_id"],
                                                                           "delegated_by": p["sponsor"]}}),
                     ("node.upsert", anode, {"node_type": "artifact", "node_id": anode, "label": p["title"],
                                             "state": "accepted", "attrs": attrs}),
                     # The delegate's own agent node belongs to the delegate (T10 namespace): the service cannot be
                     # a writer of it, so the edge starts at the delegation task, which names the delegate in attrs.
                     ("edge.add", tnode, {"edge_type": "produced", "from_id": tnode, "to_id": anode})]
        principal = {"kind": "collab", "id": "collab:room:" + room_id, "authenticated_by": "collab"}
        try:
            for n, (etype, entity, payload) in enumerate(steps, 1):
                sink({"schema_version": 2, "event_id": f"hcn-{proposal_id}-{n}", "source": source, "source_sequence": n,
                      "entity_type": "lineage", "entity_id": entity, "event_type": etype, "payload": payload,
                      "visibility_scope": "room", "room_id": room_id}, principal, None)
        except Exception:                          # noqa: BLE001 - the decision stands; the owner repeats the request
            self.stats["lineage_failures"] += 1
            return False
        with self._lock:
            if not self._proposals[proposal_id]["promoted"]:
                self._commit("proposal_promoted", {
                    "actor_user_id": None, "actor_agent_id": None, "proposal_id": proposal_id, "room_id": room_id,
                    "project_id": p["project_id"], "project_ids": [p["project_id"]], "action": "proposal.promote",
                    "outcome": "allowed", "resource_id": proposal_id})
        return True

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
            if pr.external:               # untrusted: no invisible, direction-changing or tag characters
                if len(text) > MAX_TEXT:
                    raise CollabError(400, f"text must be 1-{MAX_TEXT} characters")
                clean = ext.clean_text(text, MAX_TEXT, multiline=True)
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
            ctx = None
            if "context" in body:
                # T14: a reference to a graph node/edge/proposal/comment. Validated, and visible to THIS poster now.
                ctx = self._wb_call(self._context_for_post, pr, room, room_id, body["context"])
            fp = _digest(json.dumps([room_id, clean, reply] + ([ctx] if ctx else [])))
            prior = self._idem.get((pr.key, room_id, key))
            if prior:
                if prior[0] != fp:
                    raise CollabError(409, "idempotency_key already used for a different message")
                return 200, {"event": self._view(self._log[prior[1] - 1], viewer=pr, replayed=True), "replayed": True}
            now = self._now()
            hits = [t for t in self._hits[pr.key] if now - t < 60]
            # an external agent posts at a third of the rate a person's own agent may; it still draws on
            # its sponsor's budget below, so inviting more of them never buys more throughput
            if len(hits) >= (self.message_limit // 3 if pr.external else self.message_limit):
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
                sender_kind=pr.kind, sender_id=pr.id, sender_external=pr.external, idempotency_key=key, fingerprint=fp,
                text=redact_text(clean), in_reply_to=reply, trust="untrusted",
                **({"context": ctx} if ctx else {}),
                visibility_scope="room", action="message.post", outcome="allowed",
                project_ids=[room["project_id"]], resource_id=room_id))
        self.stats["messages"] += 1
        with self._lock:
            return 201, {"event": self._view(ev, viewer=pr)}

    # -- reads
    def _view(self, ev: dict, viewer=None, _ctx_cache: dict | None = None, **extra) -> dict:
        """One feed event as ``viewer`` may see it. A message's context reference is resolved per viewer; with no
        viewer (an internal caller) it is always hidden."""
        p, t = ev["payload"], ev["event_type"]
        out = {"sequence": ev["sequence"], "event_id": ev["event_id"], "event_type": t,
               "schema_version": ev["schema_version"], "room_id": p.get("room_id"),
               "project_id": p.get("project_id"), "created_at": p["created_at"],
               "correlation_id": p.get("correlation_id"), "causation_id": p.get("causation_id"),
               "visibility_scope": p.get("visibility_scope", "room")}
        if t == "message":
            out.update(sender={"type": "external" if p.get("sender_external") else p["sender_kind"],
                               "id": p["sender_id"], "owner_user_id": p["actor_user_id"]},
                       text=p["text"], trust="untrusted", in_reply_to=p.get("in_reply_to"))
            if p.get("context"):
                out["context"] = self._message_context(p, viewer, {} if _ctx_cache is None else _ctx_cache)
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
        out.update(extra)
        return out

    def read_events(self, pr: Principal, room_id: str, cursor: int, limit: int) -> dict:
        with self._lock:
            _, first = self._authorize(pr, room_id, "read", "events.read")
            out, more, cache = [], False, {}
            feed = self._by_room.get(room_id, [])
            lo = bisect.bisect_right(feed, max(cursor, first - 1), key=lambda e: e["sequence"])
            for ev in feed[lo:]:
                if pr.external and ev["event_type"] != "message":
                    continue                    # chat only: no membership, grant or task events
                if len(out) >= limit:
                    more = True
                    break
                out.append({k: v for k, v in self._view(ev, viewer=pr, _ctx_cache=cache).items() if k != "project_id"})
            nxt = out[-1]["sequence"] if out else cursor
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
                if not (project_id in p.get("project_ids", ())
                        or (aid and project_id in self._agent_projects.get(aid, ()))):
                    continue
                if len(out) >= limit:
                    more = True
                    break
                out.append({"sequence": ev["sequence"], "event_type": ev["event_type"],
                            "action": p["action"], "outcome": p["outcome"], "reason": p.get("reason"),
                            "actor_user_id": p.get("actor_user_id"), "actor_agent_id": p.get("actor_agent_id"),
                            "actor_kind": p.get("actor_kind"), "client_ip": p.get("client_ip"),
                            "room_id": p.get("room_id"), "resource_id": p.get("resource_id"),
                            "created_at": p["created_at"], "correlation_id": p.get("correlation_id")})
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
                 authorization: str, device: str | None, operator: str | None = None,
                 client: str | None = None):
        """parts: path segments after /api/v1/collab. Returns (status, json) or a Stream."""
        try:
            if parts == ["agents", "enrollment", "complete"] and method == "POST":
                return 201, self.complete_enrollment(body)
            if parts == ["agents", "join"] and method == "POST":
                return 201, self.redeem_join_token(body, client)  # the token IS the authentication
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
                if parts == ["agents", "external", "me"]:
                    return 200, self.external_me(pr)
                if parts[:1] == ["fleet"]:
                    if operator is None:                  # dashboard owner only
                        raise CollabError(404, "not found")
                    if parts == ["fleet", "hosts"]:
                        return 200, self.list_fleet_hosts(pr)
                    if parts == ["fleet", "host-registry"]:
                        return 200, self.list_host_registry(pr)
                    if parts == ["fleet", "agent-links"]:
                        return 200, self.agent_links(pr)
                    if parts == ["fleet", "spawns"]:
                        st = str(query["status"]) if "status" in query else None
                        return 200, self.list_spawns(pr, st)
                    raise CollabError(404, "not found")
                if parts[:1] == ["graph"]:
                    return 200, self.graph_read(pr, parts[1:], query)
                if parts == ["tasks", "inbox"]:
                    return 200, self.task_inbox(pr)
                if parts == ["delegations", "inbox"]:
                    return 200, self.delegations_inbox(pr)
                if n == 2 and parts[0] == "delegations":
                    return 200, self.delegation_status(pr, parts[1])
                if parts == ["rooms"]:
                    return 200, self.list_rooms(pr)
                if parts == ["audit"]:
                    return 200, self.audit(pr, str(query.get("project_id", "")),
                                           _int(query, "cursor", 0), _int(query, "limit", 100, 1, 500))
                if n >= 2 and parts[0] == "rooms":
                    rid = parts[1]
                    if n == 2:
                        return 200, self.get_room(pr, rid)
                    if parts[2:] == ["agents"]:
                        return 200, self.list_room_agents(pr, rid)
                    if parts[2:] == ["invites"]:
                        return 200, self.list_invites(pr, rid)
                    if parts[2:] == ["externals"]:
                        return 200, self.list_room_externals(pr, rid)
                    if parts[2:] == ["agent-requests"]:
                        return 200, self.list_agent_requests(pr, rid)
                    if parts[2:] == ["tasks"]:
                        return 200, self.list_tasks(pr, rid)
                    if parts[2:] == ["delegations"]:
                        return 200, self.list_delegations(pr, rid)
                    if parts[2:] == ["proposals"]:
                        return 200, self.list_proposals(pr, rid)
                    if n == 4 and parts[2] == "proposals":
                        return 200, self.get_proposal(pr, rid, parts[3])
                    if parts[2:] == ["consultations"]:
                        return 200, self.list_consultations(pr, rid)
                    if parts[2:] == ["xlinks"]:
                        return 200, self.list_links(pr, rid)
                    if parts[2:] == ["comments"]:
                        return 200, self.list_comments(pr, rid, query)
                    if n == 5 and parts[2] == "comments" and parts[4] == "history":
                        return 200, self.comment_history(pr, rid, parts[3])
                    if parts[2:] == ["steering"]:
                        return 200, self.list_steering(pr, rid, query)
                    if parts[2:] == ["steering", "target"]:
                        return 200, self.steering_target(pr, rid, str(query.get("node_id", "")))
                    if parts[2:] == ["flow"]:
                        return 200, self.room_flow(pr, rid)
                    if parts[2:] == ["workspaces"]:
                        return 200, self.list_workspace_links(pr, rid)
                    if parts[2:] == ["missions"]:
                        return 200, self.list_room_missions(pr, rid)
                    if parts[2:] == ["state"]:
                        wait = _int(query, "wait", 0, 0, 10)
                        return 200, self.room_state(pr, rid, _int(query, "since", -1, -1),
                                                    _int(query, "cursor", 0), float(wait))
                    if parts[2:] == ["events"]:
                        return 200, self.read_events(pr, rid, _int(query, "cursor", 0),
                                                     _int(query, "limit", 100, 1, 200))
                    if parts[2:] == ["events", "stream"]:
                        return self.open_stream(pr, rid, _int(query, "cursor", 0))
            elif method == "POST":
                if parts == ["me"]:
                    return 200, self.link_identity(pr, body)
                if parts == ["agents", "connect"]:
                    return 201, self.connect_agent(pr, body)
                if parts == ["agents", "external", "heartbeat"]:
                    return 200, self.external_heartbeat(pr, body)
                if parts == ["agents", "external", "lineage"]:
                    return 200, self.external_lineage(pr, body)
                if parts == ["agents", "external", "leave"]:
                    return 200, self.external_leave(pr)
                if parts == ["agents", "join-tokens"]:
                    return 201, self.create_join_token(pr, body)
                if n == 4 and parts[:2] == ["agents", "join-tokens"] and parts[3] == "revoke":
                    return 200, self.revoke_join_token(pr, parts[2])
                if parts[:1] == ["fleet"]:
                    if operator is None:                  # dashboard owner only
                        raise CollabError(404, "not found")
                    if parts == ["fleet", "hosts"]:
                        return 200, self.report_fleet_hosts(pr, body)
                    if n == 3 and parts[1] == "hosts":
                        return 200, self.set_fleet_host(pr, parts[2], body)
                    if n == 3 and parts[1] == "host-registry":
                        return 200, self.register_host(pr, parts[2], body)
                    if parts == ["fleet", "spawns"]:
                        return 201, self.request_spawn(pr, body)
                    if parts == ["fleet", "task-activity"]:
                        return 200, self.task_activity(pr, body)
                    if n == 4 and parts[1] == "spawns" and parts[3] == "claim":
                        return 200, self.claim_spawn(pr, parts[2])
                    if n == 4 and parts[1] == "spawns" and parts[3] == "done":
                        return 200, self.finish_spawn(pr, parts[2], body)
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
                if n == 3 and parts[0] == "delegations" and parts[2] == "respond":
                    return 200, self.delegation_respond(pr, parts[1], body)
                if n == 3 and parts[0] == "delegations" and parts[2] == "proposals":
                    return 201, self.submit_proposal(pr, parts[1], body)
                if n == 3 and parts[0] == "proposals" and parts[2] == "withdraw":
                    return 200, self.withdraw_proposal(pr, parts[1], body)
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
                    if parts[2:] == ["externals", "invites"]:
                        return 201, self.create_external_invite(pr, rid, body)
                    if n == 6 and parts[2:4] == ["externals", "invites"] and parts[5] == "revoke":
                        return 200, self.revoke_external_invite(pr, rid, parts[4])
                    if n == 5 and parts[2] == "externals" and parts[4] == "revoke":
                        return 200, self.revoke_external(pr, rid, parts[3])
                    if n == 5 and parts[2] == "agents" and parts[4] == "remove":
                        return 200, self.remove_agent_from_room(pr, rid, parts[3])
                    if parts[2:] == ["agent-requests", "approve-all"]:
                        return 200, self.approve_all_requests(pr, rid, body)
                    if n == 5 and parts[2] == "agent-requests" and parts[4] == "decide":
                        return 200, self.decide_agent_request(pr, rid, parts[3], body)
                    if parts[2:] == ["workspaces"]:
                        return 201, self.link_workspace(pr, rid, body)
                    if n == 5 and parts[2] == "workspaces" and parts[4] == "unlink":
                        return 200, self.unlink_workspace(pr, rid, parts[3])
                    if n == 5 and parts[2] == "workspaces" and parts[4] == "primary":
                        return 200, self.set_workspace_primary(pr, rid, parts[3], body)
                    if n == 5 and parts[2] == "workspaces" and parts[4] == "grants":
                        return 200, self.set_workspace_grants(pr, rid, parts[3], body)
                    if n == 5 and parts[2] == "workspaces" and parts[4] == "authorize":
                        return 200, self.authorize_workspace(pr, rid, parts[3], str(body.get("action", "")))
                    if parts[2:] == ["missions"]:
                        return 201, self.link_mission(pr, rid, body)
                    if n == 5 and parts[2] == "missions" and parts[4] == "unlink":
                        return 200, self.unlink_mission(pr, rid, parts[3])
                    if parts[2:] == ["tasks"]:
                        return 201, self.create_task(pr, rid, body)
                    if parts[2:] == ["delegations"]:
                        return 201, self.create_delegation(pr, rid, body)
                    if n == 5 and parts[2] == "proposals" and parts[4] == "decide":
                        return 200, self.decide_proposal(pr, rid, parts[3], body)
                    if parts[2:] == ["consultations"]:
                        return self.ask_consultation(pr, rid, body)
                    if n == 5 and parts[2] == "consultations" and parts[4] == "answer":
                        return 200, self.answer_consultation(pr, rid, parts[3], body)
                    if n == 5 and parts[2] == "consultations" and parts[4] == "cancel":
                        return 200, self.cancel_consultation(pr, rid, parts[3])
                    if parts[2:] == ["xlinks"]:
                        return self.create_link(pr, rid, body)
                    if n == 5 and parts[2] == "xlinks" and parts[4] == "revoke":
                        return 200, self.revoke_link(pr, rid, parts[3])
                    if parts[2:] == ["comments"]:
                        return self.add_comment(pr, rid, body)
                    if n == 5 and parts[2] == "comments" and parts[4] in ("edit", "retract"):
                        return 200, self.revise_comment(pr, rid, parts[3], parts[4], body)
                    if parts[2:] == ["steering"]:
                        return self.propose_steering(pr, rid, body)
                    if n == 5 and parts[2] == "steering" and parts[4] == "confirm":
                        return 200, self.confirm_steering(pr, rid, parts[3], body)
                    if n == 5 and parts[2] == "steering" and parts[4] == "reject":
                        return 200, self.reject_steering(pr, rid, parts[3], body)
                    if n == 5 and parts[2] == "tasks" and parts[4] == "cancel":
                        return 200, self.cancel_task(pr, rid, parts[3])
                    if parts[2:] == ["messages"]:
                        return self.post_message(pr, rid, body)
            raise CollabError(404, "not found")
        except CollabError as exc:
            return exc.status, {"error": exc.message, **({"code": exc.code} if exc.code else {}), **getattr(exc, "extra", {})}


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
        raise CollabError(400, "actions must be a non-empty subset of read, post, instruct, execute")
    return sorted(set(value))

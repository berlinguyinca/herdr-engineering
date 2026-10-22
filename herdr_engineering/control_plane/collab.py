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

from . import ids
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
AGENT_ACTIONS = frozenset({"read", "post", "instruct", "execute"})  # an agent can never invite/grant/administer
PROJECT_ROLES = frozenset({"manager", "contributor"})

MAX_TEXT = 8000
MAX_BODY = 64 * 1024
_KEY_RE = re.compile(r"[A-Za-z0-9._:\-]{1,128}\Z")
_MESSAGE_FIELDS = frozenset({"text", "idempotency_key", "in_reply_to"})
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")   # C0, DEL and C1 (8-bit CSI)
_NAME_BAD = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")
_EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s]{2,}$")
INVITE_TTL = 7 * 24 * 3600.0
MAX_OPEN_INVITES = 50
_FEED_TYPES = frozenset({"message", "member_added", "member_removed",
                         "agent_granted", "agent_revoked", "agent_grant_revoked",
                         "task_created", "task_updated"})
MAX_TASK_TEXT = 4000
TASK_TTL = 24 * 3600.0           # a task nobody finished in a day is expired
MAX_TASK_DEPTH = 3               # human -> agent -> agent -> agent
TASK_RATE = 20                   # new tasks per creator per 10 minutes
MAX_OPEN_TASKS_PER_AGENT = 10
MAX_PENDING_REQUESTS = 120       # open access requests per room (one family of agents fits)
MAX_JOIN_AGENTS = 50             # agents one join token / bulk call may create
JOIN_TOKEN_MAX_TTL = 24 * 3600.0
REQUEST_TTL = 7 * 24 * 3600.0
_HANDLE_BAD = re.compile(r"[^a-z0-9\-]")


class CollabError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


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
    __slots__ = ("kind", "id", "owner", "digest", "exp")

    def __init__(self, kind: str, id: str, owner: str, digest: str | None = None,
                 exp: float | None = None):
        self.kind, self.id, self.owner, self.digest, self.exp = kind, id, owner, digest, exp

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
                 default_grant_ttl: float = 30 * 24 * 3600.0):
        self.auth = authenticator
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
        self._requests: dict[str, dict] = {}                 # open agent-access requests
        # Indexes kept only by _apply (so hydrate rebuilds them): they replace whole-state scans.
        self._agents_by_owner: dict[str, set] = defaultdict(set)
        self._pending_by_owner: dict[str, set] = defaultdict(set)
        self._requests_by_room: dict[str, set] = defaultdict(set)
        self._requests_by_agent: dict[str, set] = defaultdict(set)
        self._rooms_by_agent: dict[str, set] = defaultdict(set)
        self._housekept = 0.0
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
            self._cv.notify_all()
            return ev

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
            known = {ev["payload"].get(k) for ev in parsed
                     for k in ("actor_user_id", "subject_user_id", "user_id", "owner_user_id")}
            mark = getattr(self.auth, "mark_known", None)
            if mark:
                mark({u for u in known if isinstance(u, str)})
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
                "cred_digest": p["secret_digest"], "expires_at": p["expires_at_epoch"]}
            self._by_cred[p["secret_digest"]] = p["agent_id"]
            self._agents_by_owner[p["owner_user_id"]].add(p["agent_id"])
            if p.get("join_digest") in self._joins:
                self._joins[p["join_digest"]]["uses"] += 1
        elif t == "join_token_created":
            self._joins[p["join_digest"]] = {
                "id": p["join_id"], "owner": p["actor_user_id"], "family": p["harness_type"],
                "prefix": p["name_prefix"], "max_uses": p["max_uses"], "uses": 0,
                "expires_at": p["expires_at_epoch"], "room_id": p.get("room_id"),
                "actions": list(p.get("actions") or []), "revoked": False, "created_at": p["created_at"]}
            self._joins_by_id[p["join_id"]] = p["join_digest"]
        elif t == "join_token_revoked":
            if p["join_id"] in self._joins_by_id:
                self._joins[self._joins_by_id[p["join_id"]]]["revoked"] = True
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
        elif t == "task_updated":
            tk = self._tasks.get(p["task_id"])
            if tk:
                tk.update(status=p["status"], result=p.get("result"), updated_at=p["created_at"])
        elif t == "agent_granted":
            self._rooms[p["room_id"]]["grants"][p["agent_id"]] = {
                "actions": list(p["actions"]), "expires_at": p.get("expires_at"), "granted_seq": seq}
            self._agent_projects[p["agent_id"]].add(p["project_id"])
            self._rooms_by_agent[p["agent_id"]].add(p["room_id"])
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
            got = self.auth.verify(token)
            if got is None:
                self.stats["auth_failures"] += 1
                raise CollabError(401, "unauthorized")
            return Principal("user", got[0], got[0], exp=got[1])
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
                elif not self.auth.exists(agent["owner"]):
                    reason = "owner_disabled"
                if reason:
                    self.stats["auth_failures"] += 1
                    self._audit_denied(Principal("agent", aid, agent["owner"]), "auth.agent", reason,
                                       resource_id=aid, agent_id=aid)
                    raise CollabError(401, "unauthorized")
                return Principal("agent", aid, agent["owner"], digest)
        self.stats["auth_failures"] += 1
        raise CollabError(401, "unauthorized")

    def still_valid(self, pr: Principal) -> bool:
        """Re-check a long-lived stream's principal (revocation / expiry / owner loss)."""
        with self._lock:
            if pr.kind == "user":
                # An open stream ends when its token expires (the client re-opens
                # with a fresh one) or when the user loses access.
                return (pr.exp is None or self._now() < pr.exp) and self.auth.exists(pr.id)
            a = self._agents.get(pr.id)
            return bool(a and a["status"] == "active" and self._now() < a["expires_at"]
                        and self._by_cred.get(a["cred_digest"]) == pr.id
                        and a["cred_digest"] == pr.digest and self.auth.exists(a["owner"]))

    # ---------------------------------------------------------- authorization
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
        if not m or not self.auth.exists(agent["owner"]):
            return None  # owner lost membership: the agent loses it too
        allowed = frozenset(grant["actions"]) & ROLE_ACTIONS[m["role"]] & AGENT_ACTIONS
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
            if not isinstance(uid, str) or not self.auth.exists(uid):
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
            if not isinstance(uid, str) or not self.auth.exists(uid):
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
        return {"ok": True}

    # -- people: link email at sign-in, invite by email
    def me(self, pr: Principal) -> dict:
        self._require_user(pr)
        return {"user_id": pr.id, "email": self._user_email.get(pr.id)}

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
            self._deliver_pending(pr.id, email)
        return {"user_id": pr.id, "email": email}

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
            if inviter is None or "invite" not in ROLE_ACTIONS[inviter["role"]] or not self.auth.exists(inv["inviter"]):
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
            if user and self.auth.exists(user):
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
                    or rec["device_id"] != device or not self.auth.exists(rec["owner"])):
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
            self._commit("agent_enrolled", {
                "actor_user_id": pr.id, "actor_agent_id": None, "agent_id": aid,
                "owner_user_id": pr.id, "code_digest": None, "device_id": device,
                "harness_type": label, "instance_id": None, "name": handle,
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
            token, jid = "hcj_" + secrets.token_urlsafe(24), ids.new_id("collab")
            expires = self._now() + float(ttl)
            self._commit("join_token_created", self._actor(
                pr, join_id=jid, join_digest=_digest(token), harness_type=family, name_prefix=prefix,
                max_uses=count, expires_at_epoch=expires, room_id=room_id, actions=actions,
                action="join.token.create", outcome="allowed", project_ids=[], resource_id=jid))
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
                    or rec["uses"] >= rec["max_uses"] or not self.auth.exists(rec["owner"])):
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
                "instance_id": instance if isinstance(instance, str) and len(instance) <= 120
                and instance and not _NAME_BAD.search(instance) else None,
                "secret_digest": _digest(cred), "expires_at_epoch": expires,
                "action": "agent.join", "outcome": "allowed", "project_ids": [], "resource_id": aid})
            request_id = self._file_request(owner, rec["room_id"], aid, rec["actions"]) if rec["room_id"] else None
        return {"agent_id": aid, "name": name, "credential": cred, "device_id": device, "expires_at": expires,
                "room_id": rec["room_id"], "request_id": request_id,
                "status": "pending" if request_id else "no access yet"}

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
                    info = self.connect_agent(pr, {"harness_type": family, "name": name})
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
        """Handles are how people address agents (@name): two agents in one room never share one."""
        for other in set(room["grants"]) | {self._requests[i]["agent_id"]
                                            for i in self._requests_by_room.get(room["id"], ())
                                            if self._requests[i]["open"]}:
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
                    if self._task_status(t) not in ("pending", "accepted") and now - t["created_epoch"] > 7 * 86400]:
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
            return bool(m and "task" in ROLE_ACTIONS[m["role"]] and self.auth.exists(t["by_id"]))
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
        out.update(extra)
        return out

    def read_events(self, pr: Principal, room_id: str, cursor: int, limit: int) -> dict:
        with self._lock:
            _, first = self._authorize(pr, room_id, "read", "events.read")
            out, more = [], False
            feed = self._by_room.get(room_id, [])
            lo = bisect.bisect_right(feed, max(cursor, first - 1), key=lambda e: e["sequence"])
            for ev in feed[lo:]:
                if len(out) >= limit:
                    more = True
                    break
                out.append({k: v for k, v in self._view(ev).items() if k != "project_id"})
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
                 authorization: str, device: str | None, operator: str | None = None):
        """parts: path segments after /api/v1/collab. Returns (status, json) or a Stream."""
        try:
            if parts == ["agents", "enrollment", "complete"] and method == "POST":
                return 201, self.complete_enrollment(body)
            if parts == ["agents", "join"] and method == "POST":
                return 201, self.redeem_join_token(body)         # the token IS the authentication
            if parts == ["config"] and method == "GET":
                return 200, self.public_config  # public identifiers only; no auth needed
            if operator is not None:
                # Only the operator listener (tailnet-only, Host-checked) ever passes this: the
                # local owner uses the dashboard without a sign-in, as the same user id they
                # sign in as on the public site. They must still be a current group member.
                if not self.auth.exists(operator):
                    raise CollabError(403, "operator is not enabled for collaboration")
                pr = Principal("user", operator, operator)
            else:
                pr = self.authenticate(authorization, device)
            n = len(parts)
            if method == "GET":
                if parts == ["me"]:
                    return 200, self.me(pr)
                if parts == ["agents"]:
                    return 200, self.list_my_agents(pr)
                if parts == ["agents", "join-tokens"]:
                    return 200, self.list_join_tokens(pr)
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
                    if parts[2:] == ["agents"]:
                        return 200, self.list_room_agents(pr, rid)
                    if parts[2:] == ["invites"]:
                        return 200, self.list_invites(pr, rid)
                    if parts[2:] == ["agent-requests"]:
                        return 200, self.list_agent_requests(pr, rid)
                    if parts[2:] == ["tasks"]:
                        return 200, self.list_tasks(pr, rid)
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
                if parts == ["agents", "join-tokens"]:
                    return 201, self.create_join_token(pr, body)
                if n == 4 and parts[:2] == ["agents", "join-tokens"] and parts[3] == "revoke":
                    return 200, self.revoke_join_token(pr, parts[2])
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
                    if n == 5 and parts[2] == "tasks" and parts[4] == "cancel":
                        return 200, self.cancel_task(pr, rid, parts[3])
                    if parts[2:] == ["messages"]:
                        return self.post_message(pr, rid, body)
            raise CollabError(404, "not found")
        except CollabError as exc:
            return exc.status, {"error": exc.message}


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

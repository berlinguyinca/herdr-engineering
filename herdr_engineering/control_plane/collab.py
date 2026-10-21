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

import hashlib
import json
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

ROLE_ACTIONS = {
    "owner": frozenset({"read", "post", "invite", "grant_agent"}),
    "contributor": frozenset({"read", "post", "grant_agent"}),
    "viewer": frozenset({"read"}),
    "guest": frozenset({"read"}),
}
INVITABLE_ROLES = frozenset({"contributor", "viewer", "guest"})
AGENT_ACTIONS = frozenset({"read", "post"})  # an agent can never invite/grant/administer
PROJECT_ROLES = frozenset({"manager", "contributor"})

MAX_TEXT = 8000
MAX_BODY = 64 * 1024
_KEY_RE = re.compile(r"^[A-Za-z0-9._:\-]{1,128}$")
_MESSAGE_FIELDS = frozenset({"text", "idempotency_key", "in_reply_to"})
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s]{2,}$")
INVITE_TTL = 7 * 24 * 3600.0
MAX_OPEN_INVITES = 50
_FEED_TYPES = frozenset({"message", "member_added", "member_removed",
                         "agent_granted", "agent_revoked"})


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


class CollabService:
    def __init__(self, authenticator: UserAuthenticator, *, sink: Any = None,
                 now=None, enrollment_ttl: float = 600.0, credential_ttl: float = 12 * 3600.0,
                 stream_poll_seconds: float = 1.0, message_limit: int = 60,
                 max_streams_per_principal: int = 5, public_config: dict | None = None):
        self.auth = authenticator
        self._sink = sink
        self._now = now or time.time
        self.enrollment_ttl = enrollment_ttl
        self.credential_ttl = credential_ttl
        self.stream_poll_seconds = stream_poll_seconds
        self.message_limit = message_limit
        self.max_streams = max_streams_per_principal
        self._lock = threading.RLock()
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
            return ev

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
                "project_id": p["project_id"], "name": p["name"], "created_by": p["actor_user_id"],
                "members": {p["actor_user_id"]: {"role": "owner", "expires_at": None, "joined_seq": seq}},
                "grants": {}}
        elif t == "member_added":
            self._rooms[p["room_id"]]["members"][p["subject_user_id"]] = {
                "role": p["role"], "expires_at": p.get("expires_at"), "joined_seq": seq}
        elif t == "member_removed":
            self._rooms[p["room_id"]]["members"].pop(p["subject_user_id"], None)
        elif t == "enrollment_started":
            self._pending[p["code_digest"]] = {
                "owner": p["actor_user_id"], "device_id": p["device_id"],
                "harness_type": p["harness_type"], "expires_at": p["expires_at_epoch"], "used": False}
        elif t == "agent_enrolled":
            if p["code_digest"] in self._pending:
                self._pending[p["code_digest"]]["used"] = True
            self._agents[p["agent_id"]] = {
                "owner": p["owner_user_id"], "device_id": p["device_id"], "harness_type": p["harness_type"],
                "instance_id": p.get("instance_id"), "status": "active",
                "cred_digest": p["secret_digest"], "expires_at": p["expires_at_epoch"]}
            self._by_cred[p["secret_digest"]] = p["agent_id"]
        elif t == "credential_rotated":
            a = self._agents[p["agent_id"]]
            self._by_cred.pop(a["cred_digest"], None)
            a.update(cred_digest=p["secret_digest"], expires_at=p["expires_at_epoch"])
            self._by_cred[p["secret_digest"]] = p["agent_id"]
        elif t == "agent_revoked":
            self._agents[p["agent_id"]]["status"] = "revoked"
        elif t == "agent_granted":
            self._rooms[p["room_id"]]["grants"][p["agent_id"]] = {
                "actions": list(p["actions"]), "expires_at": p.get("expires_at"), "granted_seq": seq}
            self._agent_projects[p["agent_id"]].add(p["project_id"])
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

    # ------------------------------------------------------------ operations
    def create_project(self, pr: Principal, body: dict) -> dict:
        self._require_user(pr)
        name = _name(body.get("name"))
        with self._lock:
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
            if role not in PROJECT_ROLES:
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
            if role not in INVITABLE_ROLES:
                raise CollabError(400, "invalid role")
            exp = body.get("expires_at")
            if exp is not None and (not isinstance(exp, (int, float)) or isinstance(exp, bool) or exp <= self._now()):
                raise CollabError(400, "invalid expires_at")
            if not isinstance(uid, str) or not self.auth.exists(uid):
                raise CollabError(404, "unknown user")
            if self._member(room, uid):
                raise CollabError(409, "already a member")
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
            if not self._member(room, user):
                self._commit("member_added", {
                    "actor_user_id": inv["inviter"], "actor_agent_id": None,
                    "project_id": room["project_id"], "room_id": inv["room_id"],
                    "subject_user_id": user, "role": inv["role"], "expires_at": None,
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
            if role not in INVITABLE_ROLES:
                raise CollabError(400, "invalid role")
            reply = {"status": "invited", "email": email, "role": role}
            user = self._email_user.get(email)
            if user and self.auth.exists(user):
                if self._member(room, user):
                    raise CollabError(409, "already a member")
                self._commit("member_added", self._actor(
                    pr, project_id=room["project_id"], room_id=room_id, subject_user_id=user,
                    role=role, expires_at=None, action="member.add", outcome="allowed",
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
                "instance_id": instance if isinstance(instance, str) else None,
                "secret_digest": _digest(cred), "expires_at_epoch": expires,
                "action": "agent.enroll", "outcome": "allowed",
                "project_ids": [], "resource_id": aid})
        return {"agent_id": aid, "credential": cred, "expires_at": expires}

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

    def grant_agent(self, pr: Principal, room_id: str, body: dict) -> dict:
        self._require_user(pr)
        with self._lock:
            room, _ = self._authorize(pr, room_id, "grant_agent", "agent.grant")
            agent = self._agents.get(body.get("agent_id")) if isinstance(body.get("agent_id"), str) else None
            if not agent or agent["owner"] != pr.id or agent["status"] != "active":
                raise CollabError(404, "not found")
            actions = body.get("actions")
            if (not isinstance(actions, list) or not actions
                    or not all(isinstance(a, str) and a in AGENT_ACTIONS for a in actions)):
                raise CollabError(400, "actions must be a non-empty subset of read, post")
            exp = body.get("expires_at")
            if exp is not None and (not isinstance(exp, (int, float)) or isinstance(exp, bool) or exp <= self._now()):
                raise CollabError(400, "invalid expires_at")
            self._commit("agent_granted", self._actor(
                pr, project_id=room["project_id"], room_id=room_id, agent_id=body["agent_id"],
                actions=sorted(set(actions)), expires_at=exp, action="agent.grant", outcome="allowed",
                project_ids=[room["project_id"]], resource_id=body["agent_id"]))
        return {"room_id": room_id, "agent_id": body["agent_id"], "actions": sorted(set(actions))}

    def list_room_agents(self, pr: Principal, room_id: str) -> dict:
        """Capability summaries only: no device ids, hostnames, paths or instance ids."""
        with self._lock:
            room, _ = self._authorize(pr, room_id, "read", "agent.list")
            out = []
            for aid, g in room["grants"].items():
                a = self._agents.get(aid)
                if a and a["status"] == "active":
                    out.append({"agent_id": aid, "harness_type": a["harness_type"],
                                "owner_user_id": a["owner"], "actions": g["actions"]})
        return {"agents": out}

    # -- messages
    def post_message(self, pr: Principal, room_id: str, body: dict) -> tuple[int, dict]:
        with self._lock:
            room, _ = self._authorize(pr, room_id, "post", "message.post")
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
            if reply is not None and not any(
                    e["payload"].get("room_id") == room_id and e["event_id"] == reply and
                    e["event_type"] == "message" for e in self._log):
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
            hits.append(now)
            self._hits[pr.key] = hits
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
            for ev in self._log[max(cursor, first - 1):]:
                if ev["sequence"] <= cursor or ev["sequence"] < first:
                    continue
                p = ev["payload"]
                if ev["event_type"] not in _FEED_TYPES:
                    continue
                if p.get("room_id") != room_id and room_id not in p.get("room_ids", ()):
                    continue
                if len(out) >= limit:
                    more = True
                    break
                out.append(self._view(ev))
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
            if self._streams[pr.key] >= self.max_streams:
                raise CollabError(429, "too many streams")
        return Stream(self, pr, room_id, cursor)

    def pump(self, stream: Stream, max_seconds: float = 3600.0):
        """Yield frames; re-authorizes on every tick and closes on any loss of access."""
        started = time.monotonic()
        cursor = stream.cursor
        with self._lock:
            if self._streams[stream.principal.key] >= self.max_streams:
                yield {"type": "closed", "reason": "too_many_streams"}
                return
            self._streams[stream.principal.key] += 1
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

    # --------------------------------------------------------------- routing
    def dispatch(self, method: str, parts: list[str], query: dict, body: dict,
                 authorization: str, device: str | None):
        """parts: path segments after /api/v1/collab. Returns (status, json) or a Stream."""
        try:
            if parts == ["agents", "enrollment", "complete"] and method == "POST":
                return 201, self.complete_enrollment(body)
            if parts == ["config"] and method == "GET":
                return 200, self.public_config  # public identifiers only; no auth needed
            pr = self.authenticate(authorization, device)
            n = len(parts)
            if method == "GET":
                if parts == ["me"]:
                    return 200, self.me(pr)
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
                    if parts[2:] == ["events"]:
                        return 200, self.read_events(pr, rid, _int(query, "cursor", 0),
                                                     _int(query, "limit", 100, 1, 200))
                    if parts[2:] == ["events", "stream"]:
                        return self.open_stream(pr, rid, _int(query, "cursor", 0))
            elif method == "POST":
                if parts == ["me"]:
                    return 200, self.link_identity(pr, body)
                if parts == ["projects"]:
                    return 201, self.create_project(pr, body)
                if n == 3 and parts[0] == "projects" and parts[2] == "rooms":
                    return 201, self.create_room(pr, parts[1], body)
                if n == 3 and parts[0] == "projects" and parts[2] == "members":
                    return 201, self.add_project_member(pr, parts[1], body)
                if parts == ["agents", "enrollment", "start"]:
                    return 201, self.start_enrollment(pr, body)
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
                    if parts[2:] == ["messages"]:
                        return self.post_message(pr, rid, body)
            raise CollabError(404, "not found")
        except CollabError as exc:
            return exc.status, {"error": exc.message}


def _name(value: Any, field: str = "name") -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 120 or _CONTROL.search(value):
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

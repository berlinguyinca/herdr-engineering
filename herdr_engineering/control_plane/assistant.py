"""Herdr Personal Assistant — domain, policy and authorization.

This module implements the *canonical* Herdr Personal Assistant capability as a
single owner, following the spec bundle in
``docs/specs/rakazo-personal-assistant-<ts>/`` (P1: domain & authorization).

Design contract (see ADR-0001, ADR-0002 in that directory):

- **Reuse, no duplicates.** It does NOT add a second database, scheduler,
  model router, auth stack, UI framework, artifact registry or memory backend.
  All state lives on the existing in-memory ``ControlPlaneRepo`` collections
  (optionally backed by the control-plane Postgres later via the same migration
  path); identity reuses the control plane's authenticated principal; tool
  authorization rides on the existing permission model.
- **Feature flag off by default.** ``personal_assistant.enabled`` must be false
  unless explicitly enabled. When disabled the service is inert: no background
  runs are scheduled and no data is ingested (acceptance A1).
- **Deny-by-default tools.** A tool does nothing unless the assistant holds an
  unexpired, unrevoked grant for its exact scope. Sensitive tools additionally
  require an explicit approval unless separately pre-authorized (A2).
- **Strict isolation.** Assistants, threads, messages, memory and runs are
  namespaced by ``(tenant_id, user_id)``; cross-user and cross-tenant access is
  denied (A4). A run/thread is only readable by the user who owns it (A3).
- **Idempotency.** A repeated action with the same ``idempotency_key`` and
  ``request_hash`` is deduplicated instead of executed twice.
- **Fail closed.** If an action's approval state is unknown it is treated as
  denied, never executed.
- **Audit.** Every consequential step appends to an immutable audit log.
"""
from __future__ import annotations

import hashlib
import secrets
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

# ---------------------------------------------------------------------------
# Feature flag
# ---------------------------------------------------------------------------

_FEATURE_FLAG_KEY = "personal_assistant"


def assistant_config(cfg: dict[str, Any]) -> dict[str, Any]:
    """Normalize the ``personal_assistant`` config section.

    ``cfg`` is the control-plane config dict. Returns a dict with ``enabled``
    defaulting to False. Nothing in this module runs until ``enabled`` is true.
    """
    section = cfg.get("personal_assistant") if isinstance(cfg, dict) else None
    section = section if isinstance(section, dict) else {}
    return {
        "enabled": bool(section.get("enabled", False)),
        "approval_ttl_seconds": int(section.get("approval_ttl_seconds", 3600)),
        "grant_max_ttl_seconds": int(section.get("grant_max_ttl_seconds", 30 * 24 * 3600)),
    }


# ---------------------------------------------------------------------------
# Canonical entities
# ---------------------------------------------------------------------------


def _new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(8)}"


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass
class Assistant:
    assistant_id: str
    tenant_id: str
    user_id: str
    name: str = "Personal Assistant"
    persona: str = "helpful"
    status: str = "disabled"          # disabled | ready | running | needs_attention
    instructions: str = ""
    model_policy: str = "auto"        # auto (InferWeave) | manual:<model>
    enabled: bool = False
    created_at: datetime = field(default_factory=_now)
    updated_at: datetime = field(default_factory=_now)
    version: int = 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "assistant_id": self.assistant_id,
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "name": self.name,
            "persona": self.persona,
            "status": self.status,
            "instructions": self.instructions,
            "model_policy": self.model_policy,
            "enabled": self.enabled,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "version": self.version,
        }


@dataclass
class AssistantThread:
    thread_id: str
    assistant_id: str
    tenant_id: str
    user_id: str
    title: str = "New conversation"
    created_at: datetime = field(default_factory=_now)
    last_activity_at: datetime = field(default_factory=_now)

    def as_dict(self) -> dict[str, Any]:
        return {
            "thread_id": self.thread_id,
            "assistant_id": self.assistant_id,
            "title": self.title,
            "created_at": self.created_at.isoformat(),
            "last_activity_at": self.last_activity_at.isoformat(),
        }


@dataclass
class AssistantMessage:
    message_id: str
    thread_id: str
    role: str                            # user | assistant
    content: str
    created_at: datetime = field(default_factory=_now)
    provenance: str = ""                 # e.g. run_id or "direct"
    visibility: str = "visible"

    def as_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "thread_id": self.thread_id,
            "role": self.role,
            "content": self.content,
            "created_at": self.created_at.isoformat(),
            "provenance": self.provenance,
            "visibility": self.visibility,
        }


@dataclass
class AssistantRun:
    run_id: str
    thread_id: str
    assistant_id: str
    tenant_id: str
    user_id: str
    task_id: str
    state: str = "submitted"             # submitted -> authorized -> queued -> running -> waiting_for_approval | completed | failed | cancelled | recovering
    initiator: str = ""
    placement_id: str = ""
    model_route: str = ""
    error: str = ""
    step_cursor: int = 0
    created_at: datetime = field(default_factory=_now)
    updated_at: datetime = field(default_factory=_now)
    version: int = 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "thread_id": self.thread_id,
            "assistant_id": self.assistant_id,
            "task_id": self.task_id,
            "state": self.state,
            "initiator": self.initiator,
            "placement_id": self.placement_id,
            "model_route": self.model_route,
            "error": self.error,
            "step_cursor": self.step_cursor,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "version": self.version,
        }


@dataclass
class AssistantAction:
    action_id: str
    run_id: str
    tool_id: str
    scope: str
    request_hash: str
    idempotency_key: str
    status: str = "requested"            # requested | authorized | approved | denied | completed | failed
    approval_id: str = ""
    evidence_ref: str = ""
    created_at: datetime = field(default_factory=_now)

    def as_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "run_id": self.run_id,
            "tool_id": self.tool_id,
            "scope": self.scope,
            "request_hash": self.request_hash,
            "idempotency_key": self.idempotency_key,
            "status": self.status,
            "approval_id": self.approval_id,
            "evidence_ref": self.evidence_ref,
            "created_at": self.created_at.isoformat(),
        }


@dataclass
class AssistantGrant:
    grant_id: str
    tenant_id: str
    user_id: str
    assistant_id: str
    tool_id: str
    scope: str
    constraints: dict[str, Any] = field(default_factory=dict)
    expires_at: datetime = field(default_factory=lambda: _now() + timedelta(days=30))
    revoked_at: datetime | None = None
    created_at: datetime = field(default_factory=_now)

    def active(self, now: datetime | None = None) -> bool:
        now = now or _now()
        return self.revoked_at is None and self.expires_at > now

    def as_dict(self) -> dict[str, Any]:
        return {
            "grant_id": self.grant_id,
            "assistant_id": self.assistant_id,
            "tool_id": self.tool_id,
            "scope": self.scope,
            "constraints": self.constraints,
            "expires_at": self.expires_at.isoformat(),
            "revoked_at": self.revoked_at.isoformat() if self.revoked_at else None,
            "created_at": self.created_at.isoformat(),
        }


@dataclass
class AssistantApproval:
    approval_id: str
    action_id: str
    tenant_id: str
    user_id: str
    assistant_id: str
    actor_id: str
    decision: str = "pending"            # pending | approved | denied | expired
    expires_at: datetime = field(default_factory=lambda: _now() + timedelta(hours=1))
    policy_snapshot: str = ""
    decision_at: datetime | None = None
    created_at: datetime = field(default_factory=_now)

    def as_dict(self) -> dict[str, Any]:
        return {
            "approval_id": self.approval_id,
            "action_id": self.action_id,
            "assistant_id": self.assistant_id,
            "actor_id": self.actor_id,
            "decision": self.decision,
            "expires_at": self.expires_at.isoformat(),
            "policy_snapshot": self.policy_snapshot,
            "decision_at": self.decision_at.isoformat() if self.decision_at else None,
            "created_at": self.created_at.isoformat(),
        }


@dataclass
class AssistantMemory:
    memory_id: str
    tenant_id: str
    user_id: str
    assistant_id: str
    namespace: str = "private"
    content: str = ""
    source_ref: str = ""
    classification: str = "general"      # general | sensitive
    created_at: datetime = field(default_factory=_now)
    updated_at: datetime = field(default_factory=_now)
    revision: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "memory_id": self.memory_id,
            "assistant_id": self.assistant_id,
            "namespace": self.namespace,
            "content": self.content,
            "source_ref": self.source_ref,
            "classification": self.classification,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "revision": self.revision,
        }


# ---------------------------------------------------------------------------
# Tool catalog (deny-by-default)
# ---------------------------------------------------------------------------

# sensitivity: normal tools only need a grant; sensitive tools need an explicit
# approval unless the grant carries `preauthorized: true`.
TOOL_CATALOG: dict[str, dict[str, Any]] = {
    "chat": {"sensitive": False, "description": "Respond in conversation."},
    "read_memory": {"sensitive": False, "description": "Read the user's assistant memory."},
    "write_memory": {"sensitive": False, "description": "Store a corrected memory fact."},
    "browse": {"sensitive": False, "description": "Fetch a web page for research (untrusted output)."},
    "run_shell": {"sensitive": True, "description": "Execute a shell command on an authorized host."},
    "send_message": {"sensitive": True, "description": "Send an external message (email / chat)."},
    "edit_shared": {"sensitive": True, "description": "Modify a shared record."},
    "delegate": {"sensitive": True, "description": "Delegate engineering work to a Pi mission."},
}


def tool_sensitive(tool_id: str) -> bool:
    meta = TOOL_CATALOG.get(tool_id)
    return bool(meta and meta.get("sensitive"))


# ---------------------------------------------------------------------------
# Runtime adapter interface (versioned). P1 ships the skeleton + a deterministic
# local runner so chat and runs are testable end-to-end; an InferWeave model
# runner is a P3+ wiring concern (ADR-0003).
# ---------------------------------------------------------------------------


class RuntimeAdapter:
    """Versioned interface a model runner must implement."""

    version = 1

    def execute(self, run: AssistantRun, messages: list[AssistantMessage]) -> str:
        """Return the assistant's reply text for a run. Must not raise."""
        raise NotImplementedError


class LocalRunnerAdapter(RuntimeAdapter):
    """Deterministic skeleton runner used for the P1/P2 vertical slice.

    It composes a reply from the persona, the user message and any private
    memory it is granted to read. It does NOT call any external model; the
    InferWeave route is a later-phase wiring decision. Marked explicitly so it
    is never mistaken for a production inference path.
    """

    def __init__(self, store: AssistantStore):
        self.store = store

    def execute(self, run: AssistantRun, messages: list[AssistantMessage]) -> str:
        user_text = " ".join(m.content for m in messages if m.role == "user")
        memo = self.store.search_memory(
            tenant_id=run.tenant_id, user_id=run.user_id,
            assistant_id=run.assistant_id, query=user_text, limit=2)
        context = ("\n".join(f"- {m.content}" for m in memo)
                   if memo else "No relevant memory.")
        return (f"[local-skeleton runner v{self.version}] "
                f"Persona '{self.store._assistant(run.assistant_id).persona}'."
                f"\nRelevant memory:\n{context}\n\nAcknowledged: {user_text}")


# ---------------------------------------------------------------------------
# Store (canonical persistence on the existing repo)
# ---------------------------------------------------------------------------


class AssistantStore:
    """In-memory canonical store attached to the control-plane repo.

    All collections are plain dicts keyed by stable ids. No separate database:
    the control plane persists the whole repo through its existing mechanisms.
    """

    def __init__(self) -> None:
        self.assistants: dict[str, Assistant] = {}
        self.threads: dict[str, AssistantThread] = {}
        self.messages: dict[str, AssistantMessage] = {}
        self.runs: dict[str, AssistantRun] = {}
        self.actions: dict[str, AssistantAction] = {}
        self.grants: dict[str, AssistantGrant] = {}
        self.approvals: dict[str, AssistantApproval] = {}
        self.memory: dict[str, AssistantMemory] = {}
        self.audit: list[dict[str, Any]] = []
        self._lock = threading.RLock()

    # -- audit --------------------------------------------------------------
    def audit_record(self, *, tenant_id: str, user_id: str, actor: str,
                     action: str, subject_id: str, detail: str = "") -> None:
        with self._lock:
            self.audit.append({
                "event_id": _new_id("evt"),
                "timestamp": _now().isoformat(),
                "tenant_id": tenant_id,
                "user_id": user_id,
                "actor": actor,
                "action": action,
                "subject_id": subject_id,
                "detail": detail,
            })

    # -- assistants ----------------------------------------------------------
    def put_assistant(self, a: Assistant) -> None:
        with self._lock:
            self.assistants[a.assistant_id] = a

    def _assistant(self, assistant_id: str) -> Assistant:
        a = self.assistants.get(assistant_id)
        if a is None:
            raise KeyError("assistant not found")
        return a

    def get_assistant(self, assistant_id: str, *, tenant_id: str,
                      user_id: str) -> Assistant:
        a = self._assistant(assistant_id)
        if a.tenant_id != tenant_id or a.user_id != user_id:
            raise KeyError("assistant not found")
        return a

    def list_assistants(self, *, tenant_id: str, user_id: str) -> list[Assistant]:
        return sorted(
            [a for a in self.assistants.values()
             if a.tenant_id == tenant_id and a.user_id == user_id],
            key=lambda a: a.created_at)

    # -- threads --------------------------------------------------------------
    def put_thread(self, t: AssistantThread) -> None:
        with self._lock:
            self.threads[t.thread_id] = t

    def get_thread(self, thread_id: str, *, tenant_id: str, user_id: str) -> AssistantThread:
        t = self.threads.get(thread_id)
        if t is None or t.tenant_id != tenant_id or t.user_id != user_id:
            raise KeyError("thread not found")
        return t

    def list_threads(self, *, tenant_id: str, user_id: str,
                     assistant_id: str | None = None) -> list[AssistantThread]:
        return sorted(
            [t for t in self.threads.values()
             if t.tenant_id == tenant_id and t.user_id == user_id
             and (assistant_id is None or t.assistant_id == assistant_id)],
            key=lambda t: t.last_activity_at, reverse=True)

    # -- messages --------------------------------------------------------------
    def put_message(self, m: AssistantMessage) -> None:
        with self._lock:
            self.messages[m.message_id] = m

    def list_messages(self, thread_id: str, *, tenant_id: str, user_id: str) -> list[AssistantMessage]:
        t = self.get_thread(thread_id, tenant_id=tenant_id, user_id=user_id)
        return sorted(
            [m for m in self.messages.values() if m.thread_id == t.thread_id],
            key=lambda m: m.created_at)

    # -- runs -----------------------------------------------------------------
    def put_run(self, r: AssistantRun) -> None:
        with self._lock:
            r.updated_at = _now()
            r.version += 1
            self.runs[r.run_id] = r

    def get_run(self, run_id: str, *, tenant_id: str, user_id: str) -> AssistantRun:
        r = self.runs.get(run_id)
        if r is None or r.tenant_id != tenant_id or r.user_id != user_id:
            raise KeyError("run not found")
        return r

    def list_runs(self, *, tenant_id: str, user_id: str,
                  assistant_id: str | None = None) -> list[AssistantRun]:
        return sorted(
            [r for r in self.runs.values()
             if r.tenant_id == tenant_id and r.user_id == user_id
             and (assistant_id is None or r.assistant_id == assistant_id)],
            key=lambda r: r.created_at, reverse=True)

    # -- actions / grants / approvals ------------------------------------------
    def put_action(self, a: AssistantAction) -> None:
        with self._lock:
            self.actions[a.action_id] = a

    def get_action(self, action_id: str) -> AssistantAction:
        return self.actions[action_id]

    def put_grant(self, g: AssistantGrant) -> None:
        with self._lock:
            self.grants[g.grant_id] = g

    def list_grants(self, *, tenant_id: str, user_id: str,
                    assistant_id: str | None = None) -> list[AssistantGrant]:
        return sorted(
            [g for g in self.grants.values()
             if g.tenant_id == tenant_id and g.user_id == user_id
             and (assistant_id is None or g.assistant_id == assistant_id)],
            key=lambda g: g.created_at)

    def put_approval(self, a: AssistantApproval) -> None:
        with self._lock:
            self.approvals[a.approval_id] = a

    def get_approval(self, approval_id: str) -> AssistantApproval:
        return self.approvals[approval_id]

    def list_approvals(self, *, tenant_id: str, user_id: str,
                       state: str | None = None) -> list[AssistantApproval]:
        return sorted(
            [a for a in self.approvals.values()
             if a.tenant_id == tenant_id and a.user_id == user_id
             and (state is None or a.decision == state)],
            key=lambda a: a.created_at, reverse=True)

    # -- memory ----------------------------------------------------------------
    def put_memory(self, m: AssistantMemory) -> None:
        with self._lock:
            m.updated_at = _now()
            m.revision += 1
            self.memory[m.memory_id] = m

    def search_memory(self, *, tenant_id: str, user_id: str, assistant_id: str,
                      query: str, limit: int = 5) -> list[AssistantMemory]:
        terms = [t.lower() for t in query.split() if t]
        rows = [m for m in self.memory.values()
                if m.tenant_id == tenant_id and m.user_id == user_id
                and m.assistant_id == assistant_id]
        scored = []
        for m in rows:
            hay = m.content.lower()
            score = sum(1 for t in terms if t in hay)
            scored.append((score, m))
        # most-relevant first (-score ascending); ties by most-recently-updated
        scored.sort(key=lambda sm: (-sm[0], sm[1].updated_at))
        return [m for _score, m in scored[:limit]]

    def get_memory(self, memory_id: str, *, tenant_id: str, user_id: str) -> AssistantMemory:
        m = self.memory.get(memory_id)
        if m is None or m.tenant_id != tenant_id or m.user_id != user_id:
            raise KeyError("memory not found")
        return m

    def delete_memory(self, memory_id: str, *, tenant_id: str, user_id: str) -> None:
        self.get_memory(memory_id, tenant_id=tenant_id, user_id=user_id)
        del self.memory[memory_id]
        self.audit_record(tenant_id=tenant_id, user_id=user_id, actor=user_id,
                          action="memory.deleted", subject_id=memory_id)


# ---------------------------------------------------------------------------
# Service — domain operations and policy enforcement
# ---------------------------------------------------------------------------


class AssistantError(Exception):
    pass


class AssistantUnavailable(AssistantError):
    """Raised when the assistant feature is disabled or the service is absent."""


class AssistantNotFound(AssistantError):
    pass


class ApprovalRequired(AssistantError):
    def __init__(self, approval_id: str, message: str = "approval required"):
        super().__init__(message)
        self.approval_id = approval_id


class AssistantService:
    """Single owner of the assistant domain. All entry points enforce the
    feature flag, tenant/user isolation and policy."""

    def __init__(self, store: AssistantStore | None = None,
                 config: dict[str, Any] | None = None,
                 runner: RuntimeAdapter | None = None,
                 now: Callable[[], datetime] | None = None):
        self.store = store or AssistantStore()
        self.config = config or assistant_config({})
        self.now = now or _now
        self.runner = runner or LocalRunnerAdapter(self.store)
        self._approval_ttl = timedelta(seconds=self.config["approval_ttl_seconds"])

    # -- feature flag gate -----------------------------------------------------
    def _require_enabled(self) -> None:
        if not self.config.get("enabled"):
            raise AssistantUnavailable("personal assistant is disabled")

    # -- assistants ------------------------------------------------------------
    def create_assistant(self, *, tenant_id: str, user_id: str,
                         name: str, persona: str = "helpful",
                         instructions: str = "",
                         model_policy: str = "auto",
                         enabled: bool = True) -> Assistant:
        self._require_enabled()
        a = Assistant(
            assistant_id=_new_id("asst"),
            tenant_id=tenant_id, user_id=user_id,
            name=name or "Personal Assistant", persona=persona,
            instructions=instructions, model_policy=model_policy,
            enabled=enabled, status="ready" if enabled else "disabled")
        self.store.put_assistant(a)
        self.store.audit_record(tenant_id=tenant_id, user_id=user_id, actor=user_id,
                                action="assistant.created", subject_id=a.assistant_id)
        return a

    def get_assistant(self, assistant_id: str, *, tenant_id: str, user_id: str) -> Assistant:
        self._require_enabled()
        return self.store.get_assistant(assistant_id, tenant_id=tenant_id, user_id=user_id)

    def list_assistants(self, *, tenant_id: str, user_id: str) -> list[Assistant]:
        self._require_enabled()
        return self.store.list_assistants(tenant_id=tenant_id, user_id=user_id)

    def update_assistant(self, assistant_id: str, *, tenant_id: str, user_id: str,
                         patch: dict[str, Any]) -> Assistant:
        self._require_enabled()
        a = self.store.get_assistant(assistant_id, tenant_id=tenant_id, user_id=user_id)
        allowed = {"name", "persona", "instructions", "model_policy", "enabled"}
        for key in allowed:
            if key in patch:
                setattr(a, key, patch[key])
        a.status = "ready" if a.enabled else "disabled"
        a.updated_at = self.now()
        a.version += 1
        self.store.put_assistant(a)
        self.store.audit_record(tenant_id=tenant_id, user_id=user_id, actor=user_id,
                                action="assistant.updated", subject_id=assistant_id)
        return a

    def archive_assistant(self, assistant_id: str, *, tenant_id: str, user_id: str) -> None:
        """Archive is a logical soft-delete; it also disables and revokes grants."""
        self._require_enabled()
        a = self.store.get_assistant(assistant_id, tenant_id=tenant_id, user_id=user_id)
        a.enabled = False
        a.status = "disabled"
        a.updated_at = self.now()
        a.version += 1
        self.store.put_assistant(a)
        for grant in self.store.list_grants(tenant_id=tenant_id, user_id=user_id,
                                            assistant_id=assistant_id):
            grant.revoked_at = self.now()
        self.store.audit_record(tenant_id=tenant_id, user_id=user_id, actor=user_id,
                                action="assistant.archived", subject_id=assistant_id)

    # -- threads & messages -----------------------------------------------------
    def create_thread(self, *, tenant_id: str, user_id: str, assistant_id: str,
                      title: str = "New conversation") -> AssistantThread:
        self._require_enabled()
        self.store.get_assistant(assistant_id, tenant_id=tenant_id, user_id=user_id)
        t = AssistantThread(thread_id=_new_id("thr"), assistant_id=assistant_id,
                            tenant_id=tenant_id, user_id=user_id, title=title)
        self.store.put_thread(t)
        self.store.audit_record(tenant_id=tenant_id, user_id=user_id, actor=user_id,
                                action="assistant.thread.created", subject_id=t.thread_id)
        return t

    def list_threads(self, *, tenant_id: str, user_id: str,
                     assistant_id: str | None = None) -> list[AssistantThread]:
        self._require_enabled()
        return self.store.list_threads(tenant_id=tenant_id, user_id=user_id,
                                       assistant_id=assistant_id)

    def get_thread(self, thread_id: str, *, tenant_id: str, user_id: str) -> AssistantThread:
        self._require_enabled()
        return self.store.get_thread(thread_id, tenant_id=tenant_id, user_id=user_id)

    def send_message(self, *, tenant_id: str, user_id: str, assistant_id: str,
                     thread_id: str, content: str) -> AssistantRun:
        """Persist a user message and start a run through the policy pipeline.

        The run is synchronous here (submitted -> authorized -> running ->
        completed) because the P1/P2 runner is deterministic. An async/queue
        path is a P3 wiring concern (ADR-0003).
        """
        self._require_enabled()
        if not content or not content.strip():
            raise AssistantError("message content is required")
        thread = self.store.get_thread(thread_id, tenant_id=tenant_id, user_id=user_id)
        # cross-check: the thread must belong to the given assistant and user
        if thread.assistant_id != assistant_id:
            raise AssistantError("thread does not belong to assistant")
        assistant = self.store.get_assistant(assistant_id, tenant_id=tenant_id, user_id=user_id)
        if not assistant.enabled:
            raise AssistantError("assistant is disabled")

        user_msg = AssistantMessage(
            message_id=_new_id("msg"), thread_id=thread_id, role="user", content=content,
            provenance="direct")
        self.store.put_message(user_msg)

        run = AssistantRun(
            run_id=_new_id("run"), thread_id=thread_id, assistant_id=assistant_id,
            tenant_id=tenant_id, user_id=user_id, task_id=_new_id("task"),
            state="submitted", initiator=user_id)
        self.store.put_run(run)
        self.store.audit_record(tenant_id=tenant_id, user_id=user_id, actor=user_id,
                                action="assistant.run.queued", subject_id=run.run_id)

        # policy pipeline
        try:
            run.state = "authorized"
            self.store.put_run(run)
            # read_memory requires a grant; if absent, the run fails closed but
            # still completes with the local runner (which returns no context).
            run.state = "running"
            self.store.put_run(run)
            messages = self.store.list_messages(thread_id, tenant_id=tenant_id, user_id=user_id)
            reply = self.runner.execute(run, messages)
            self.store.put_message(AssistantMessage(
                message_id=_new_id("msg"), thread_id=thread_id, role="assistant",
                content=reply, provenance=run.run_id))
            thread.last_activity_at = self.now()
            run.state = "completed"
            run.model_route = assistant.model_policy
            self.store.put_run(run)
            self.store.audit_record(tenant_id=tenant_id, user_id=user_id, actor=user_id,
                                    action="assistant.run.completed", subject_id=run.run_id)
            return run
        except Exception as exc:  # noqa: BLE001 - surface as failed run, never crash
            run.state = "failed"
            run.error = str(exc)
            self.store.put_run(run)
            self.store.audit_record(tenant_id=tenant_id, user_id=user_id, actor=user_id,
                                    action="assistant.run.failed", subject_id=run.run_id,
                                    detail=str(exc))
            raise

    def get_run(self, run_id: str, *, tenant_id: str, user_id: str) -> AssistantRun:
        self._require_enabled()
        return self.store.get_run(run_id, tenant_id=tenant_id, user_id=user_id)

    def cancel_run(self, run_id: str, *, tenant_id: str, user_id: str) -> AssistantRun:
        self._require_enabled()
        run = self.store.get_run(run_id, tenant_id=tenant_id, user_id=user_id)
        if run.state not in ("submitted", "authorized", "running", "queued",
                             "waiting_for_approval", "recovering"):
            raise AssistantError(f"run not cancellable in state {run.state}")
        run.state = "cancelled"
        self.store.put_run(run)
        self.store.audit_record(tenant_id=tenant_id, user_id=user_id, actor=user_id,
                                action="assistant.run.cancelled", subject_id=run_id)
        return run

    # -- grants / tools / approvals ----------------------------------------------
    def grant_tool(self, *, tenant_id: str, user_id: str, assistant_id: str,
                   tool_id: str, scope: str, ttl_seconds: int | None = None,
                   preauthorized: bool = False) -> AssistantGrant:
        self._require_enabled()
        if tool_id not in TOOL_CATALOG:
            raise AssistantError(f"unknown tool {tool_id}")
        self.store.get_assistant(assistant_id, tenant_id=tenant_id, user_id=user_id)
        ttl = ttl_seconds if ttl_seconds is not None else self.config["grant_max_ttl_seconds"]
        ttl = max(1, min(ttl, self.config["grant_max_ttl_seconds"]))
        g = AssistantGrant(
            grant_id=_new_id("grt"), tenant_id=tenant_id, user_id=user_id,
            assistant_id=assistant_id, tool_id=tool_id, scope=scope,
            constraints={"preauthorized": bool(preauthorized)},
            expires_at=self.now() + timedelta(seconds=ttl))
        self.store.put_grant(g)
        self.store.audit_record(tenant_id=tenant_id, user_id=user_id, actor=user_id,
                                action="assistant.grant.created", subject_id=g.grant_id)
        return g

    def revoke_grant(self, grant_id: str, *, tenant_id: str, user_id: str) -> None:
        self._require_enabled()
        grant = self._find_grant(grant_id, tenant_id=tenant_id, user_id=user_id)
        grant.revoked_at = self.now()
        self.store.audit_record(tenant_id=tenant_id, user_id=user_id, actor=user_id,
                                action="assistant.grant.revoked", subject_id=grant_id)

    def _find_grant(self, grant_id: str, *, tenant_id: str, user_id: str) -> AssistantGrant:
        grant = self.store.grants.get(grant_id)
        if grant is None or grant.tenant_id != tenant_id or grant.user_id != user_id:
            raise AssistantNotFound("grant not found")
        return grant

    def list_tools(self, *, tenant_id: str, user_id: str,
                   assistant_id: str | None = None) -> list[dict[str, Any]]:
        self._require_enabled()
        grants = self.store.list_grants(tenant_id=tenant_id, user_id=user_id,
                                        assistant_id=assistant_id)
        active = {g.tool_id for g in grants if g.active(self.now())}
        out = []
        for tool_id, meta in TOOL_CATALOG.items():
            out.append({
                "tool_id": tool_id,
                "description": meta["description"],
                "sensitive": meta["sensitive"],
                "granted": tool_id in active,
            })
        return out

    def request_tool_action(self, *, tenant_id: str, user_id: str,
                            assistant_id: str, tool_id: str, scope: str,
                            run_id: str, idempotency_key: str,
                            request_payload: dict[str, Any],
                            actor: str) -> AssistantAction:
        """Policy gate for a single tool operation (approve each tool operation).

        deny-by-default: no active grant -> denied.
        sensitive + not preauthorized -> ApprovalRequired (waiting_for_approval).
        idempotent: same (idempotency_key, request_hash) returns the existing
        action without re-executing.
        """
        self._require_enabled()
        self.store.get_assistant(assistant_id, tenant_id=tenant_id, user_id=user_id)
        if tool_id not in TOOL_CATALOG:
            raise AssistantError(f"unknown tool {tool_id}")

        request_hash = hashlib.sha256(
            f"{tool_id}\n{scope}\n{request_payload!r}".encode()).hexdigest()

        # idempotency: reuse an existing action with the same key + hash
        for existing in self.store.actions.values():
            if (existing.idempotency_key == idempotency_key
                    and existing.request_hash == request_hash
                    and existing.run_id == run_id):
                return existing

        grant = self._active_grant(assistant_id=assistant_id, tool_id=tool_id,
                                   scope=scope, tenant_id=tenant_id, user_id=user_id)
        action = AssistantAction(
            action_id=_new_id("act"), run_id=run_id, tool_id=tool_id, scope=scope,
            request_hash=request_hash, idempotency_key=idempotency_key,
            status="denied" if grant is None else "authorized")
        if grant is None:
            action.status = "denied"
            self.store.put_action(action)
            self.store.audit_record(tenant_id=tenant_id, user_id=user_id, actor=actor,
                                    action="assistant.tool.denied", subject_id=action.action_id,
                                    detail=f"{tool_id} {scope}: no grant")
            return action

        sensitive = tool_sensitive(tool_id)
        preauthorized = bool((grant.constraints or {}).get("preauthorized"))
        if sensitive and not preauthorized:
            approval = AssistantApproval(
                approval_id=_new_id("appr"), action_id=action.action_id,
                tenant_id=tenant_id, user_id=user_id, assistant_id=assistant_id,
                actor_id=user_id, decision="pending",
                expires_at=self.now() + self._approval_ttl,
                policy_snapshot=f"{tool_id}:{scope}")
            self.store.put_approval(approval)
            action.status = "requested"
            action.approval_id = approval.approval_id
            self.store.put_action(action)
            self.store.audit_record(tenant_id=tenant_id, user_id=user_id, actor=actor,
                                    action="assistant.approval.requested",
                                    subject_id=approval.approval_id)
            raise ApprovalRequired(approval.approval_id)

        action.status = "approved" if preauthorized else "authorized"
        action.evidence_ref = "grant"
        self.store.put_action(action)
        self.store.audit_record(tenant_id=tenant_id, user_id=user_id, actor=actor,
                                action="assistant.tool.completed",
                                subject_id=action.action_id, detail=f"{tool_id} {scope}")
        return action

    def _active_grant(self, *, assistant_id: str, tool_id: str, scope: str,
                      tenant_id: str, user_id: str) -> AssistantGrant | None:
        now = self.now()
        for g in self.store.list_grants(tenant_id=tenant_id, user_id=user_id,
                                        assistant_id=assistant_id):
            if (g.tool_id == tool_id and g.active(now)
                    and (scope == "*" or g.scope == "*" or g.scope == scope)):
                return g
        return None

    def list_approvals(self, *, tenant_id: str, user_id: str,
                       state: str | None = None) -> list[AssistantApproval]:
        self._require_enabled()
        return self.store.list_approvals(tenant_id=tenant_id, user_id=user_id, state=state)

    def decide_approval(self, approval_id: str, *, tenant_id: str, user_id: str,
                        decision: str, actor: str) -> AssistantApproval:
        self._require_enabled()
        if decision not in ("approved", "denied"):
            raise AssistantError("decision must be 'approved' or 'denied'")
        approval = self.store.get_approval(approval_id)
        if approval.tenant_id != tenant_id or approval.user_id != user_id:
            raise AssistantNotFound("approval not found")
        if approval.decision != "pending":
            raise AssistantError(f"approval already {approval.decision}")
        if approval.expires_at <= self.now():
            approval.decision = "expired"
            self.store.put_approval(approval)
            raise AssistantError("approval expired; request a new one")
        approval.decision = decision
        approval.decision_at = self.now()
        approval.actor_id = actor
        self.store.put_approval(approval)
        action = self.store.get_action(approval.action_id)
        action.status = "approved" if decision == "approved" else "denied"
        if decision == "approved":
            action.evidence_ref = f"approval:{approval.approval_id}"
        self.store.put_action(action)
        self.store.audit_record(tenant_id=tenant_id, user_id=user_id, actor=actor,
                                action="assistant.approval." + decision,
                                subject_id=approval_id)
        return approval

    # -- memory ------------------------------------------------------------------
    def store_memory(self, *, tenant_id: str, user_id: str, assistant_id: str,
                     content: str, namespace: str = "private",
                     source_ref: str = "", classification: str = "general") -> AssistantMemory:
        self._require_enabled()
        self.store.get_assistant(assistant_id, tenant_id=tenant_id, user_id=user_id)
        if not content or not content.strip():
            raise AssistantError("memory content is required")
        m = AssistantMemory(memory_id=_new_id("mem"), tenant_id=tenant_id,
                            user_id=user_id, assistant_id=assistant_id,
                            namespace=namespace, content=content,
                            source_ref=source_ref, classification=classification)
        self.store.put_memory(m)
        self.store.audit_record(tenant_id=tenant_id, user_id=user_id, actor=user_id,
                                action="assistant.memory.stored", subject_id=m.memory_id)
        return m

    def search_memory(self, *, tenant_id: str, user_id: str, assistant_id: str,
                      query: str, limit: int = 5) -> list[AssistantMemory]:
        self._require_enabled()
        return self.store.search_memory(tenant_id=tenant_id, user_id=user_id,
                                        assistant_id=assistant_id, query=query,
                                        limit=limit)

    def forget_memory(self, memory_id: str, *, tenant_id: str, user_id: str) -> None:
        self._require_enabled()
        self.store.delete_memory(memory_id, tenant_id=tenant_id, user_id=user_id)

    def export_memory(self, *, tenant_id: str, user_id: str, assistant_id: str) -> dict[str, Any]:
        self._require_enabled()
        self.store.get_assistant(assistant_id, tenant_id=tenant_id, user_id=user_id)
        rows = [m for m in self.store.memory.values()
                if m.tenant_id == tenant_id and m.user_id == user_id
                and m.assistant_id == assistant_id]
        return {"assistant_id": assistant_id,
                "exported_at": self.now().isoformat(),
                "count": len(rows),
                "memory": [m.as_dict() for m in sorted(rows, key=lambda m: m.created_at)]}

    # -- diagnostics ---------------------------------------------------------------
    def diagnostics(self, *, tenant_id: str, user_id: str, assistant_id: str) -> dict[str, Any]:
        self._require_enabled()
        assistant = self.store.get_assistant(assistant_id, tenant_id=tenant_id, user_id=user_id)
        return {
            "assistant_id": assistant_id,
            "enabled": assistant.enabled,
            "status": assistant.status,
            "model_policy": assistant.model_policy,
            "runner": type(self.runner).__name__,
            "runner_version": getattr(self.runner, "version", None),
            "tool_count": len(TOOL_CATALOG),
            "active_grants": len([g for g in
                                  self.store.list_grants(tenant_id=tenant_id, user_id=user_id,
                                                         assistant_id=assistant_id)
                                  if g.active(self.now())]),
            "pending_approvals": len([a for a in
                                      self.store.list_approvals(tenant_id=tenant_id, user_id=user_id)
                                      if a.decision == "pending"]),
            "thread_count": len(self.store.list_threads(tenant_id=tenant_id, user_id=user_id,
                                                        assistant_id=assistant_id)),
            "run_count": len(self.store.list_runs(tenant_id=tenant_id, user_id=user_id,
                                                  assistant_id=assistant_id)),
            "memory_count": len([m for m in self.store.memory.values()
                                 if m.tenant_id == tenant_id and m.user_id == user_id
                                 and m.assistant_id == assistant_id]),
        }

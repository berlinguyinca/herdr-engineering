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
import re
import secrets
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any


def redact_secrets(text: str | None) -> str | None:
    """Scrub common secret patterns from a string (audit/log/event output).

    Guarantees a hardening invariant: authorization headers, api keys, tokens,
    passwords and secrets are never written to the audit log or event output.
    """
    if not text:
        return text
    pat = re.compile(
        r"(?i)(authorization\s*:\s*bearer\s+|api[_-]?key[^\w]|token[^\w]|password[^\w]|secret[^\w])"
        r"([A-Za-z0-9_\-.:/=]{4,})")
    return pat.sub(lambda m: m.group(1) + "***REDACTED***", text)


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
    computer = section.get("computer") if isinstance(section, dict) else None
    computer = computer if isinstance(computer, dict) else {}
    return {
        "enabled": bool(section.get("enabled", False)),
        "approval_ttl_seconds": int(section.get("approval_ttl_seconds", 3600)),
        "grant_max_ttl_seconds": int(section.get("grant_max_ttl_seconds", 30 * 24 * 3600)),
        "max_requests_per_minute": int(section.get("max_requests_per_minute", 120)),
        # Computer-tool sandbox policy. deny-by-default: shell/browser do nothing
        # unless the operator opts in AND the target host/command is allowlisted.
        "computer": {
            "allow_shell": bool(computer.get("allow_shell", False)),
            "allow_browser": bool(computer.get("allow_browser", False)),
            "allowed_hosts": [str(h) for h in computer.get("allowed_hosts", [])],
            "allowed_commands": [str(c) for c in computer.get("allowed_commands", [])],
            "max_timeout_seconds": int(computer.get("max_timeout_seconds", 30)),
            # connector allowlist: only these https origins may be reached by the
            # connector_request / browse tools (SSRF guard).
            "allowed_connector_origins": [str(o) for o in computer.get("allowed_connector_origins", [])],
        },
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
class AssistantRoutine:
    """A scheduled/event-driven routine bound to one assistant.

    Firing is idempotent: each scheduled bucket fires at most once. Firing is
    deferred across quiet hours and computed in the routine's own timezone
    (DST-aware via zoneinfo). No new daemon is introduced: the control plane
    polls ``due_routines`` and calls ``trigger`` (or a later phase wires the
    existing scheduler loop to do so).
    """
    routine_id: str
    assistant_id: str
    tenant_id: str
    user_id: str
    name: str
    trigger_type: str = "interval"     # interval | daily
    trigger_spec: dict[str, Any] = field(default_factory=dict)
    timezone: str = "UTC"
    quiet_hours: dict[str, str] | None = None  # {start: "HH:MM", end: "HH:MM"}
    enabled: bool = True
    last_result: str = ""
    last_run_at: datetime | None = None
    next_at: datetime | None = None
    created_at: datetime = field(default_factory=_now)
    updated_at: datetime = field(default_factory=_now)

    def as_dict(self) -> dict[str, Any]:
        return {
            "routine_id": self.routine_id,
            "assistant_id": self.assistant_id,
            "name": self.name,
            "trigger_type": self.trigger_type,
            "trigger_spec": self.trigger_spec,
            "timezone": self.timezone,
            "quiet_hours": self.quiet_hours,
            "enabled": self.enabled,
            "last_result": self.last_result,
            "last_run_at": self.last_run_at.isoformat() if self.last_run_at else None,
            "next_at": self.next_at.isoformat() if self.next_at else None,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


@dataclass
class Connector:
    """An external integration reachable by the assistant (MCP / OpenAPI / HTTP).

    The connector is the canonical owner of its endpoint + auth. Credentials are
    stored but NEVER surfaced through as_dict() (only ``auth_present``). The
    runtime adapter must never log them either.
    """
    connector_id: str
    tenant_id: str
    user_id: str
    name: str
    kind: str = "http"            # http | mcp | openapi
    base_url: str = ""
    auth: dict[str, Any] = field(default_factory=dict)  # never serialized/logged
    enabled: bool = True
    created_at: datetime = field(default_factory=_now)
    updated_at: datetime = field(default_factory=_now)

    def as_dict(self) -> dict[str, Any]:
        return {
            "connector_id": self.connector_id,
            "name": self.name,
            "kind": self.kind,
            "base_url": self.base_url,
            "auth_present": bool(self.auth),
            "enabled": self.enabled,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


@dataclass
class DelegationRequest:
    """A delegation of engineering work to a Pi mission.

    Records the request, its target and status, and any artifacts registered
    back. Status is tracked (queued/running/succeeded/failed/cancelled); the
    runtime adapter fulfils the actual dispatch. No completion is fabricated:
    an undelivered delegation stays ``queued``.
    """
    delegation_id: str
    tenant_id: str
    user_id: str
    assistant_id: str
    task: str
    target_system: str = "pi"
    status: str = "queued"        # queued|running|succeeded|failed|cancelled
    artifact_refs: list[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=_now)
    updated_at: datetime = field(default_factory=_now)

    def as_dict(self) -> dict[str, Any]:
        return {
            "delegation_id": self.delegation_id,
            "assistant_id": self.assistant_id,
            "task": self.task,
            "target_system": self.target_system,
            "status": self.status,
            "artifact_refs": self.artifact_refs,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
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
    "connector_request": {"sensitive": True,
                          "description": "Call an authorized external connector (MCP/OpenAPI/HTTP)."},
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


class ConnectorAdapter:
    """Versioned adapter a connector kind must implement.

    ``request`` must not raise for transport failures: it returns a dict with
    ``ok`` False and a safe ``error``. Implementations MUST NOT include
    credentials in any output or log.
    """
    version = 1
    kind = "http"

    def request(self, connector: Connector, method: str, path: str,
                body: dict[str, Any], timeout: int) -> dict[str, Any]:
        raise NotImplementedError


class HttpConnectorAdapter(ConnectorAdapter):
    """Deny-by-default HTTP connector adapter.

    Security properties:
    - only https is allowed;
    - the resolved origin must be an operator allowlisted origin (SSRF guard);
    - credentials are attached via header only, never echoed in output;
    - responses are size-truncated and body is returned as text.
    A transport callable is injectable for tests.
    """
    kind = "http"

    def __init__(self, urlopen: Callable | None = None):
        self._urlopen = urlopen  # tests inject a fake; None uses urllib

    def request(self, connector: Connector, method: str, path: str,
                body: dict[str, Any], timeout: int) -> dict[str, Any]:
        base = (connector.base_url or "").rstrip("/")
        if not base.startswith("https://"):
            return {"ok": False, "error": "connector requires https"}
        try:
            from urllib.parse import urljoin, urlparse
            target = urljoin(base + "/", (path or "").lstrip("/"))
            parsed = urlparse(target)
            if parsed.scheme != "https" or not parsed.hostname:
                return {"ok": False, "error": "refusing non-https target"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"invalid target: {exc}"}
        allowed = self._allowed_origins(connector)
        origin = f"https://{parsed.hostname}".lower()
        if parsed.hostname not in allowed and origin not in allowed:
            return {"ok": False, "error": "connector origin not allowlisted"}
        if not self._urlopen:
            return {"ok": False, "error": "no transport configured (runtime skeleton)"}
        headers = {"Accept": "application/json"}
        if connector.auth:
            headers.setdefault("Authorization",
                               "Bearer " + str(connector.auth.get("token", "")))
        try:
            resp = self._urlopen(target, method=method.upper(), headers=headers,
                                 body=body, timeout=timeout)
            return {"ok": True, "status": resp.get("status", 200),
                    "body": (resp.get("body") or "")[:20000]}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": redact_secrets(f"connector call failed: {exc}")}

    @staticmethod
    def _allowed_origins(connector: Connector) -> list[str]:
        # allowlist comes from the connector auth config, populated by the
        # operator when registering the connector (never derived from user input).
        allowed = connector.auth.get("_allowed_origins")
        if isinstance(allowed, list):
            return [str(o).lower().rstrip("/") for o in allowed]
        return []


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
        self.routines: dict[str, AssistantRoutine] = {}
        self._routine_fired: set[str] = set()  # dedupe: routine_id|bucket_iso
        self.connectors: dict[str, Connector] = {}
        self.delegations: dict[str, DelegationRequest] = {}
        self._rate: dict[str, list[int]] = {}  # key -> [minute_window, count]
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
                "action": redact_secrets(action),
                "subject_id": subject_id,
                "detail": redact_secrets(detail),
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

    # -- routines ---------------------------------------------------------------
    def put_routine(self, r: AssistantRoutine) -> None:
        with self._lock:
            r.updated_at = _now()
            self.routines[r.routine_id] = r

    def get_routine(self, routine_id: str, *, tenant_id: str, user_id: str) -> AssistantRoutine:
        r = self.routines.get(routine_id)
        if r is None or r.tenant_id != tenant_id or r.user_id != user_id:
            raise KeyError("routine not found")
        return r

    def list_routines(self, *, tenant_id: str, user_id: str,
                      assistant_id: str | None = None) -> list[AssistantRoutine]:
        return sorted(
            [r for r in self.routines.values()
             if r.tenant_id == tenant_id and r.user_id == user_id
             and (assistant_id is None or r.assistant_id == assistant_id)],
            key=lambda r: r.created_at)

    # -- connectors ---------------------------------------------------------------
    def put_connector(self, c: Connector) -> None:
        with self._lock:
            c.updated_at = _now()
            self.connectors[c.connector_id] = c

    def get_connector(self, connector_id: str, *, tenant_id: str, user_id: str) -> Connector:
        c = self.connectors.get(connector_id)
        if c is None or c.tenant_id != tenant_id or c.user_id != user_id:
            raise KeyError("connector not found")
        return c

    def list_connectors(self, *, tenant_id: str, user_id: str) -> list[Connector]:
        return sorted(
            [c for c in self.connectors.values()
             if c.tenant_id == tenant_id and c.user_id == user_id],
            key=lambda c: c.created_at)

    # -- delegations ---------------------------------------------------------------
    def put_delegation(self, d: DelegationRequest) -> None:
        with self._lock:
            d.updated_at = _now()
            self.delegations[d.delegation_id] = d

    def get_delegation(self, delegation_id: str, *, tenant_id: str, user_id: str) -> DelegationRequest:
        d = self.delegations.get(delegation_id)
        if d is None or d.tenant_id != tenant_id or d.user_id != user_id:
            raise KeyError("delegation not found")
        return d

    def list_delegations(self, *, tenant_id: str, user_id: str) -> list[DelegationRequest]:
        return sorted(
            [d for d in self.delegations.values()
             if d.tenant_id == tenant_id and d.user_id == user_id],
            key=lambda d: d.created_at, reverse=True)

    def routine_fired(self, routine_id: str, bucket_iso: str) -> bool:
        return f"{routine_id}|{bucket_iso}" in self._routine_fired

    def mark_routine_fired(self, routine_id: str, bucket_iso: str) -> None:
        with self._lock:
            self._routine_fired.add(f"{routine_id}|{bucket_iso}")

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


class RateLimited(AssistantError):
    """Raised when a caller exceeds the assistant API rate limit."""
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

    # -- routines ------------------------------------------------------------------
    def _tz(self, routine: AssistantRoutine):
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo(routine.timezone)
        except Exception:  # noqa: BLE001 - unknown zone falls back to UTC
            from zoneinfo import ZoneInfo
            return ZoneInfo("UTC")

    @staticmethod
    def _in_quiet_hours(dt: datetime, quiet: dict[str, str] | None) -> bool:
        """True when ``dt`` (wall-clock in its own tz) falls inside quiet hours.

        Quiet hours are a [start, end) interval; an end <= start means the window
        wraps past midnight.
        """
        if not quiet:
            return False
        start = quiet.get("start")
        end = quiet.get("end")
        if not start or not end:
            return False
        try:
            hh, mm = map(int, start.split(":")); start_min = hh * 60 + mm
            hh, mm = map(int, end.split(":")); end_min = hh * 60 + mm
        except (ValueError, AttributeError):
            return False
        cur_min = dt.hour * 60 + dt.minute
        if start_min < end_min:
            return start_min <= cur_min < end_min
        # wraps midnight: quiet from start->24:00 and 00:00->end
        return cur_min >= start_min or cur_min < end_min

    def _compute_next(self, after: datetime, routine: AssistantRoutine) -> datetime | None:
        """Next fire time strictly after ``after``, honoring timezone + quiet hours.

        Returns None when the routine can never fire again (e.g. disabled or a
        daily routine with no matching day).
        """
        if not routine.enabled:
            return None
        tz = self._tz(routine)
        local_after = after.astimezone(tz)
        spec = routine.trigger_spec or {}
        # scan a bounded horizon so a daily routine with no matching weekday is
        # detected rather than looping forever
        horizon = 366 * 24 * 3600
        candidate = self._first_candidate(local_after, routine.trigger_type, spec, tz)
        while candidate is not None:
            if (candidate - local_after).total_seconds() > horizon:
                return None
            if not self._in_quiet_hours(candidate, routine.quiet_hours):
                return candidate
            # inside quiet hours: advance one step and retest
            candidate = self._first_candidate(candidate, routine.trigger_type, spec, tz)
        return None

    def _first_candidate(self, after: datetime, trigger_type: str,
                         spec: dict[str, Any], tz) -> datetime | None:
        """First fire time strictly after ``after`` ignoring quiet hours."""
        if trigger_type == "interval":
            every = str(spec.get("every", "minutes"))
            amount = int(spec.get("amount", 1) or 1)
            step = {"minutes": 60, "hours": 3600, "days": 86400}.get(every, 60)
            return after + timedelta(seconds=step * amount)
        if trigger_type == "daily":
            t = str(spec.get("time", "09:00"))
            days = spec.get("days") or ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
            hh, mm = map(int, t.split(":"))
            for offset in range(1, 8):  # at most a week ahead
                day = after + timedelta(days=offset)
                if day.strftime("%a").lower()[:3] not in days:
                    continue
                cand = day.replace(hour=hh, minute=mm, second=0, microsecond=0)
                if cand > after:
                    return cand
            return None
        return None

    def create_routine(self, *, tenant_id: str, user_id: str, assistant_id: str,
                       name: str, trigger_type: str = "interval",
                       trigger_spec: dict[str, Any] | None = None,
                       timezone: str = "UTC", quiet_hours: dict[str, str] | None = None,
                       enabled: bool = True) -> AssistantRoutine:
        self._require_enabled()
        self.store.get_assistant(assistant_id, tenant_id=tenant_id, user_id=user_id)
        if trigger_type not in ("interval", "daily"):
            raise AssistantError(f"unsupported trigger_type {trigger_type}")
        if trigger_type == "interval":
            every = str((trigger_spec or {}).get("every", "minutes"))
            if every not in ("minutes", "hours", "days"):
                raise AssistantError(f"unsupported interval unit {every}")
            amount = int((trigger_spec or {}).get("amount", 1) or 1)
            if not (1 <= amount <= 366 * 24):
                raise AssistantError("interval amount out of range")
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo(timezone)
        except Exception as exc:  # noqa: BLE001
            raise AssistantError(f"invalid timezone {timezone!r}") from exc
        spec = trigger_spec or {}
        r = AssistantRoutine(
            routine_id=_new_id("rout"), assistant_id=assistant_id,
            tenant_id=tenant_id, user_id=user_id, name=name,
            trigger_type=trigger_type, trigger_spec=spec, timezone=timezone,
            quiet_hours=quiet_hours, enabled=enabled)
        r.next_at = self._compute_next(self.now(), r)
        self.store.put_routine(r)
        self.store.audit_record(tenant_id=tenant_id, user_id=user_id, actor=user_id,
                                action="assistant.routine.created", subject_id=r.routine_id)
        return r

    def list_routines(self, *, tenant_id: str, user_id: str,
                      assistant_id: str | None = None) -> list[AssistantRoutine]:
        self._require_enabled()
        return self.store.list_routines(tenant_id=tenant_id, user_id=user_id,
                                        assistant_id=assistant_id)

    def get_routine(self, routine_id: str, *, tenant_id: str, user_id: str) -> AssistantRoutine:
        self._require_enabled()
        return self.store.get_routine(routine_id, tenant_id=tenant_id, user_id=user_id)

    def update_routine(self, routine_id: str, *, tenant_id: str, user_id: str,
                       patch: dict[str, Any]) -> AssistantRoutine:
        self._require_enabled()
        r = self.store.get_routine(routine_id, tenant_id=tenant_id, user_id=user_id)
        for key in ("name", "trigger_spec", "timezone", "quiet_hours", "enabled", "trigger_type"):
            if key in patch:
                setattr(r, key, patch[key])
        r.next_at = self._compute_next(self.now(), r)
        self.store.put_routine(r)
        self.store.audit_record(tenant_id=tenant_id, user_id=user_id, actor=user_id,
                                action="assistant.routine.updated", subject_id=r.routine_id)
        return r

    def archive_routine(self, routine_id: str, *, tenant_id: str, user_id: str) -> None:
        self._require_enabled()
        r = self.store.get_routine(routine_id, tenant_id=tenant_id, user_id=user_id)
        r.enabled = False
        r.next_at = None
        self.store.put_routine(r)
        self.store.audit_record(tenant_id=tenant_id, user_id=user_id, actor=user_id,
                                action="assistant.routine.archived", subject_id=r.routine_id)

    def due_routines(self, *, tenant_id: str, user_id: str, now: datetime | None = None) -> list[AssistantRoutine]:
        """Enabled routines whose next_at has arrived (for an existing scheduler
        loop or the dashboard to drive). Not a background daemon."""
        self._require_enabled()
        now = now or self.now()
        return [r for r in self.store.list_routines(tenant_id=tenant_id, user_id=user_id)
                if r.enabled and r.next_at is not None and r.next_at <= now]

    def trigger(self, routine_id: str, *, tenant_id: str, user_id: str,
                now: datetime | None = None) -> AssistantRoutine:
        """Fire a due routine idempotently: each scheduled bucket fires once.

        Returns the routine with an updated ``next_at`` / ``last_result``. If the
        bucket was already fired, it is a no-op (acceptance A5 dedupe).
        """
        self._require_enabled()
        r = self.store.get_routine(routine_id, tenant_id=tenant_id, user_id=user_id)
        now = now or self.now()
        if not r.enabled or r.next_at is None or r.next_at > now:
            return r
        bucket_iso = r.next_at.astimezone(self._tz(r)).strftime("%Y-%m-%dT%H:%M")
        if self.store.routine_fired(r.routine_id, bucket_iso):
            r.last_result = "skipped: already fired for this schedule bucket"
            self.store.put_routine(r)
            return r
        r.last_run_at = now
        r.last_result = "triggered"
        r.next_at = self._compute_next(now, r)
        self.store.put_routine(r)
        self.store.mark_routine_fired(r.routine_id, bucket_iso)
        self.store.audit_record(tenant_id=tenant_id, user_id=user_id, actor=user_id,
                                action="assistant.routine.triggered", subject_id=r.routine_id,
                                detail=bucket_iso)
        return r

    def correct_memory(self, memory_id: str, *, tenant_id: str, user_id: str,
                       new_content: str, source_ref: str = "correction") -> AssistantMemory:
        """Correct a stored fact: bump revision, record provenance; future
        retrieval uses the new revision (acceptance A6 memory correction)."""
        self._require_enabled()
        m = self.store.get_memory(memory_id, tenant_id=tenant_id, user_id=user_id)
        if not new_content or not new_content.strip():
            raise AssistantError("memory content is required")
        m.content = new_content
        m.source_ref = source_ref
        # put_memory bumps revision on every write
        self.store.put_memory(m)
        self.store.audit_record(tenant_id=tenant_id, user_id=user_id, actor=user_id,
                                action="assistant.memory.updated", subject_id=memory_id,
                                detail=f"revision={m.revision}")
        return m

    # -- connectors / computer tools / delegation (P4) ----------------------------------
    def _computer(self) -> dict[str, Any]:
        return self.config.get("computer", {})

    def register_connector(self, *, tenant_id: str, user_id: str, name: str,
                           kind: str = "http", base_url: str = "",
                           auth: dict[str, Any] | None = None) -> Connector:
        """Register an external connector. ``auth`` is stored but never exposed.

        ``auth["_allowed_origins"]`` is the operator-declared SSRF allowlist for
        this connector; it must be a list of hostnames/origins.
        """
        self._require_enabled()
        if kind not in ("http", "mcp", "openapi"):
            raise AssistantError(f"unsupported connector kind {kind}")
        if not base_url:
            raise AssistantError("connector base_url is required")
        if not base_url.startswith("https://"):
            raise AssistantError("connector base_url must be https")
        allowed = (auth or {}).get("_allowed_origins")
        if not (isinstance(allowed, list) and allowed):
            raise AssistantError("connector requires an allowlisted origin (_allowed_origins)")
        for origin in allowed:
            if not isinstance(origin, str) or origin.lower().startswith("http://"):
                raise AssistantError("connector allowlist origins must be https hostnames/origins")
        c = Connector(connector_id=_new_id("conn"), tenant_id=tenant_id,
                      user_id=user_id, name=name, kind=kind, base_url=base_url,
                      auth=dict(auth or {}))
        self.store.put_connector(c)
        self.store.audit_record(tenant_id=tenant_id, user_id=user_id, actor=user_id,
                                action="assistant.connector.created", subject_id=c.connector_id)
        return c

    def list_connectors(self, *, tenant_id: str, user_id: str) -> list[Connector]:
        self._require_enabled()
        return self.store.list_connectors(tenant_id=tenant_id, user_id=user_id)

    def get_connector(self, connector_id: str, *, tenant_id: str, user_id: str) -> Connector:
        self._require_enabled()
        return self.store.get_connector(connector_id, tenant_id=tenant_id, user_id=user_id)

    def archive_connector(self, connector_id: str, *, tenant_id: str, user_id: str) -> None:
        self._require_enabled()
        c = self.store.get_connector(connector_id, tenant_id=tenant_id, user_id=user_id)
        c.enabled = False
        self.store.put_connector(c)
        self.store.audit_record(tenant_id=tenant_id, user_id=user_id, actor=user_id,
                                action="assistant.connector.archived", subject_id=connector_id)

    def connector_request(self, *, tenant_id: str, user_id: str, connector_id: str,
                          method: str = "GET", path: str = "/",
                          body: dict[str, Any] | None = None,
                          actor: str = "") -> dict[str, Any]:
        """Call a connector through its adapter. deny-by-default on the origin."""
        self._require_enabled()
        c = self.store.get_connector(connector_id, tenant_id=tenant_id, user_id=user_id)
        if not c.enabled:
            raise AssistantError("connector is disabled")
        adapter = self._adapter_for(c.kind)
        result = adapter.request(c, method or "GET", path or "/", body or {},
                                 self._computer().get("max_timeout_seconds", 30))
        # defense-in-depth: never surface a credential-bearing error regardless of
        # the adapter implementation
        if not result.get("ok") and result.get("error"):
            result = {**result, "error": redact_secrets(result["error"])}
        self.store.audit_record(tenant_id=tenant_id, user_id=user_id, actor=actor or user_id,
                                action="assistant.connector.requested", subject_id=connector_id,
                                detail=("ok" if result.get("ok") else result.get("error", "denied")))
        return result

    def _adapter_for(self, kind: str) -> ConnectorAdapter:
        # MCP / OpenAPI adapters are a later-phase wiring decision; the skeleton
        # routes all kinds through the deny-by-default HTTP adapter.
        return HttpConnectorAdapter()

    def evaluate_computer_policy(self, *, tool: str, target: str) -> dict[str, Any]:
        """deny-by-default computer-tool sandbox policy check."""
        pol = self._computer()
        if tool == "browse":
            if not pol.get("allow_browser"):
                return {"allowed": False, "reason": "browser tool disabled by policy"}
            host = (target or "").split("://")[-1].split("/")[0].lower()
            hosts = [h.lower() for h in pol.get("allowed_hosts", [])]
            if hosts and host not in hosts:
                return {"allowed": False, "reason": f"host {host} not allowlisted"}
            return {"allowed": True}
        if tool == "run_shell":
            if not pol.get("allow_shell"):
                return {"allowed": False, "reason": "shell tool disabled by policy"}
            cmd = (target or "").split(" ", 1)[0].strip()
            cmds = pol.get("allowed_commands", [])
            if cmds and cmd not in cmds:
                return {"allowed": False, "reason": f"command '{cmd}' not allowlisted"}
            return {"allowed": True}
        return {"allowed": False, "reason": "unknown computer tool"}

    def browse_tool(self, *, tenant_id: str, user_id: str, url: str) -> dict[str, Any]:
        """Fetch a web page under the sandbox policy (deny-by-default)."""
        self._require_enabled()
        verdict = self.evaluate_computer_policy(tool="browse", target=url)
        if not verdict["allowed"]:
            return {"ok": False, "denied": True, "reason": verdict["reason"]}
        host = url.split("://")[-1].split("/")[0]
        c = Connector(connector_id="__browse__", tenant_id=tenant_id, user_id=user_id,
                      name="browse", kind="http", base_url=url,
                      auth={"_allowed_origins": [host]})
        adapter = HttpConnectorAdapter()
        result = adapter.request(c, "GET", "/", {}, 30)
        if not result.get("ok") and "no transport" in result.get("error", ""):
            return {"ok": False, "denied": True,
                    "reason": "browse allowed by policy but no real executor configured (skeleton)"}
        return result

    def run_shell_tool(self, *, tenant_id: str, user_id: str, command: str) -> dict[str, Any]:
        """Shell tool under the sandbox policy. deny-by-default; no execution in
        the skeleton (a real executor is a later-phase wiring decision)."""
        self._require_enabled()
        verdict = self.evaluate_computer_policy(tool="run_shell", target=command)
        if not verdict["allowed"]:
            return {"ok": False, "denied": True, "reason": verdict["reason"]}
        return {"ok": False, "denied": True,
                "reason": "shell allowed by policy but no real executor configured (skeleton)"}

    def delegate(self, *, tenant_id: str, user_id: str, assistant_id: str,
                 task: str, target_system: str = "pi",
                 actor: str = "") -> DelegationRequest:
        """Record a delegation request. Status stays ``queued`` until the runtime
        adapter fulfils it; no completion is fabricated."""
        self._require_enabled()
        self.store.get_assistant(assistant_id, tenant_id=tenant_id, user_id=user_id)
        if not task or not task.strip():
            raise AssistantError("delegation task is required")
        d = DelegationRequest(delegation_id=_new_id("dlg"), tenant_id=tenant_id,
                              user_id=user_id, assistant_id=assistant_id,
                              task=task, target_system=target_system)
        self.store.put_delegation(d)
        self.store.audit_record(tenant_id=tenant_id, user_id=user_id, actor=actor or user_id,
                                action="assistant.delegation.created", subject_id=d.delegation_id)
        return d

    def list_delegations(self, *, tenant_id: str, user_id: str) -> list[DelegationRequest]:
        self._require_enabled()
        return self.store.list_delegations(tenant_id=tenant_id, user_id=user_id)

    def get_delegation(self, delegation_id: str, *, tenant_id: str, user_id: str) -> DelegationRequest:
        self._require_enabled()
        return self.store.get_delegation(delegation_id, tenant_id=tenant_id, user_id=user_id)

    def cancel_delegation(self, delegation_id: str, *, tenant_id: str, user_id: str) -> DelegationRequest:
        self._require_enabled()
        d = self.store.get_delegation(delegation_id, tenant_id=tenant_id, user_id=user_id)
        if d.status not in ("queued", "running"):
            raise AssistantError(f"cannot cancel delegation in state {d.status}")
        d.status = "cancelled"
        self.store.put_delegation(d)
        self.store.audit_record(tenant_id=tenant_id, user_id=user_id, actor=user_id,
                                action="assistant.delegation.cancelled", subject_id=delegation_id)
        return d

    def register_delegation_artifact(self, delegation_id: str, *, tenant_id: str,
                                     user_id: str, artifact_ref: str) -> DelegationRequest:
        """Attach an artifact (e.g. a Pi mission/artifact URI) back to a delegation."""
        self._require_enabled()
        d = self.store.get_delegation(delegation_id, tenant_id=tenant_id, user_id=user_id)
        if artifact_ref not in d.artifact_refs:
            d.artifact_refs.append(artifact_ref)
        if d.status == "queued":
            d.status = "running"
        self.store.put_delegation(d)
        self.store.audit_record(tenant_id=tenant_id, user_id=user_id, actor=user_id,
                                action="assistant.delegation.artifact", subject_id=delegation_id,
                                detail=artifact_ref)
        return d

    def execute_tool(self, *, tenant_id: str, user_id: str, assistant_id: str,
                     tool_id: str, payload: dict[str, Any],
                     actor: str = "") -> dict[str, Any]:
        """Dispatch a granted tool to its implementation (P4 executor).

        Policy gating happens in ``request_tool_action``; this executes only a
        tool that has already passed the gate.
        """
        self._require_enabled()
        self.store.get_assistant(assistant_id, tenant_id=tenant_id, user_id=user_id)
        if tool_id == "browse":
            return self.browse_tool(tenant_id=tenant_id, user_id=user_id,
                                    url=payload.get("url", ""))
        if tool_id == "run_shell":
            return self.run_shell_tool(tenant_id=tenant_id, user_id=user_id,
                                       command=payload.get("command", ""))
        if tool_id == "connector_request":
            return self.connector_request(tenant_id=tenant_id, user_id=user_id,
                                          connector_id=payload.get("connector_id", ""),
                                          method=payload.get("method", "GET"),
                                          path=payload.get("path", "/"),
                                          body=payload.get("body") or {},
                                          actor=actor or user_id)
        if tool_id == "delegate":
            d = self.delegate(tenant_id=tenant_id, user_id=user_id,
                              assistant_id=assistant_id,
                              task=payload.get("task", ""),
                              target_system=payload.get("target_system", "pi"),
                              actor=actor or user_id)
            return {"ok": True, "delegation": d.as_dict()}
        raise AssistantError(f"tool {tool_id} has no executor")

    # -- hardening: rate limiting ----------------------------------------------------
    def check_rate_limit(self, *, key: str) -> None:
        """Fixed-window per-key limiter (deny-by-default over the cap).

        Returns nothing on allow; raises AssistantError(429) when the caller has
        exceeded ``max_requests_per_minute`` in the current minute window.
        """
        cap = self.config.get("max_requests_per_minute", 120)
        window = int(self.now().timestamp() // 60)
        with self.store._lock:
            hits = self.store._rate.get(key)
            if hits is None or hits[0] != window:
                hits = [window, 0]
                self.store._rate[key] = hits
            if hits[1] >= cap:
                raise RateLimited("rate limit exceeded; slow down")
            hits[1] += 1

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

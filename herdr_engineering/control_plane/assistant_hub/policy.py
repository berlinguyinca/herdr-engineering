"""Decision engine, owner decisions and activation gates for the assistant hub (spec 6.2, 6.3, 8, 13).

``decide`` answers whether a worker's action is authorized, denied or left to the owner. Nothing here is autonomous
by accident: vetoes win over everything, only an ACTIVE grant with the exact key id (same tenant, user and worker)
authorizes, the global shadow period blocks all authorization, and activation needs every gate to hold. Request
rows and audit entries carry the canonical key and key id only, never the raw command text.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Literal

from ..assistant import AssistantStore
from .actiongrants import ActionEvidence, ActionGrant, ActionRequestRow, GrantState, as_utc, revoke
from .canonical import Derived, RawAction, derive
from .events import scrub_payload
from .evidence import _shadow_elapsed, propose_if_ready
from .verbs import ActionKey, Verb
from .vetoes import Veto, VetoRule, check_vetoes
from .workers import Worker

POLICY_ACTOR = "hub:policy"
Outcome = Literal["authorized", "denied", "pending"]
Source = Literal["veto", "grant", "owner", "shadow", "unknown"]


@dataclass(frozen=True)
class Decision:
    outcome: Outcome
    reason: str
    request_id: str | None
    grant_id: str | None
    veto_code: str | None
    source: Source


class ActivationRefused(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


_MAX_DISPLAY = 300
_SECRET_WORDS = r"(?:token|secret|passw(?:or)?d|passwd|api[_-]?key|credential|auth\w*|cookie)"
_USERINFO_RE = re.compile(r"(://)[^/\s@]*@")
_OPTION_RE = re.compile(rf"(?i)(--?[\w-]*{_SECRET_WORDS}[\w-]*)(=|\s+)([^\s\"']+)")
_ASSIGN_RE = re.compile(rf"(?i)\b([\w]*(?:{_SECRET_WORDS}|pwd|pass|key)|[\w]*{_SECRET_WORDS}[\w]*)=([^\s\"']+)")
_BEARER_RE = re.compile(r"(?i)\b(bearer|basic)(\s+)[^\s\"']+")
_USER_OPT_RE = re.compile(r"(?i)(\s(?:-u|--user|--proxy-user)(?:=|\s+))[^\s\"']+")
_HEADER_RE = re.compile(r"(?i)\b(x-api-key|api-key|cookie|set-cookie|proxy-authorization|authorization)(\s*:\s*)[^\"'\n]*")
_SSHPASS_RE = re.compile(r"(?i)\b(sshpass\s+-p\s*)[^\s\"']+")
_LOGIN_RE = re.compile(r"(?i)\b((?:docker|podman)\s+login\b[^\n]*?\s(?:-p|--password)(?:=|\s+))[^\s\"']+")
_DB_ATTACHED_RE = re.compile(r"(?i)\b((?:mysql|mysqldump|mysqladmin|psql|mariadb)\b[^\n]*?\s-p)[^\s\"']+")
_MASK = "***"


def display_text(raw: RawAction) -> str:
    if raw.command:
        return raw.command
    return " ".join(p for p in (raw.tool, raw.operation or "", ",".join(raw.recipients), raw.repo or "",
                                raw.environment or "") if p)


def scrub_display(text: str) -> str:
    """A masked, truncated rendering for the OWNER-facing request row only (never audit, grants or events).

    Pattern based: URL userinfo, secret-named options and NAME=value pairs, Bearer headers, plus the hub's token-shape
    scrubber. A secret that has no recognizable shape (a bare word like ``hunter2``) cannot be detected, which is why
    this text lives only on the request row and key_canonical holds a digest instead of command text.
    """
    out = _USERINFO_RE.sub(r"\1" + _MASK + "@", text)
    out = _HEADER_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{_MASK}", out)
    out = _SSHPASS_RE.sub(lambda m: m.group(1) + _MASK, out)
    out = _LOGIN_RE.sub(lambda m: m.group(1) + _MASK, out)
    out = _DB_ATTACHED_RE.sub(lambda m: m.group(1) + _MASK, out)
    out = _USER_OPT_RE.sub(lambda m: m.group(1) + _MASK, out)
    out = _OPTION_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{_MASK}", out)
    out = _ASSIGN_RE.sub(lambda m: f"{m.group(1)}={_MASK}", out)
    out = _BEARER_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{_MASK}", out)
    return str(scrub_payload(out))[:_MAX_DISPLAY]


def in_shadow(store: AssistantStore, cfg: dict, now: datetime) -> bool:
    return not _shadow_elapsed(store, cfg, now)


def _extra_rules(cfg: dict) -> list[VetoRule]:
    return [VetoRule(code=e["code"], reason=e["reason"], verb=Verb(e["verb"]) if "verb" in e else None,
                     argv_contains=e.get("argv_contains")) for e in cfg.get("extra_vetoes", [])]


def _new_row(worker: Worker, raw: RawAction, derived: Derived, session_id: str, task_id: str, status: str, reason: str,
             now: datetime) -> ActionRequestRow:
    key = derived.key
    return ActionRequestRow(
        request_id="ar_" + secrets.token_urlsafe(12), tenant_id=worker.tenant_id, user_id=worker.user_id,
        worker_id=worker.worker_id, session_id=session_id, task_id=task_id,
        key_canonical=key.canonical() if key else "", key_id=key.key_id() if key else "",
        verb=key.verb.value if key else Verb.UNKNOWN.value, risk_class=key.risk_class.value if key else "outward",
        status=status, reason=reason, created_at=as_utc(now), display=scrub_display(display_text(raw)))


def _audit(store: AssistantStore, worker: Worker, actor: str, action: str, subject: str, detail: str = "") -> None:
    store.audit_record(tenant_id=worker.tenant_id, user_id=worker.user_id, actor=actor, action=action,
                       subject_id=subject, detail=detail)


def _deny(store: AssistantStore, worker: Worker, raw: RawAction, derived: Derived, veto: Veto, session_id: str, task_id: str,
          now: datetime) -> Decision:
    row = _new_row(worker, raw, derived, session_id, task_id, "vetoed", f"vetoed: {veto.reason}", now)
    store.put_action_request(row)
    _audit(store, worker, POLICY_ACTOR, "assistant.action.vetoed", row.request_id,
           f"veto={veto.code} key_id={row.key_id}")
    return Decision("denied", f"Denied by the {veto.code} rule: {veto.reason}.", row.request_id, None, veto.code, "veto")


def _live_grant(store: AssistantStore, cfg: dict, worker: Worker, key: ActionKey, now: datetime) -> ActionGrant | None:
    key_id, policy = key.key_id(), int(cfg["policy_version"])
    for g in store.list_action_grants(worker.tenant_id, worker.user_id):
        if (g.worker_id == worker.worker_id and g.key_id == key_id and g.tenant_id == worker.tenant_id
                and g.user_id == worker.user_id and g.effective_state(now, policy) is GrantState.ACTIVE):
            return g
    return None


def _pending(store: AssistantStore, worker: Worker, raw: RawAction, derived: Derived, session_id: str, task_id: str, now: datetime,
             source: Source, reason: str) -> Decision:
    with store._lock:                                   # find-or-create must be atomic
        key_id = derived.key.key_id() if derived.key else ""
        for r in store.list_action_requests(worker.tenant_id, worker.user_id, "pending"):
            if (r.worker_id, r.session_id, r.task_id, r.key_id) == (worker.worker_id, session_id, task_id, key_id):
                return Decision("pending", reason, r.request_id, None, None, source)
        row = _new_row(worker, raw, derived, session_id, task_id, "pending", reason, now)
        store.put_action_request(row)
    _audit(store, worker, POLICY_ACTOR, "assistant.action.requested", row.request_id,
           f"source={source} key_id={row.key_id}")
    return Decision("pending", reason, row.request_id, None, None, source)


def decide(store: AssistantStore, cfg: dict, worker: Worker, raw: RawAction, session_id: str, task_id: str,
           now: datetime) -> Decision:
    derived = derive(raw)
    veto = check_vetoes(derived, _extra_rules(cfg))
    if veto is not None:
        return _deny(store, worker, raw, derived, veto, session_id, task_id, now)
    if derived.owner_decides or derived.key is None:
        return _pending(store, worker, raw, derived, session_id, task_id, now, "unknown",
                        "The action could not be classified with certainty, so the owner decides.")
    grant = _live_grant(store, cfg, worker, derived.key, now)
    if grant is not None and in_shadow(store, cfg, now):
        return _pending(store, worker, raw, derived, session_id, task_id, now, "shadow",
                        "The shadow period is still running, so the owner decides.")
    if grant is not None:
        _audit(store, worker, POLICY_ACTOR, "assistant.action.authorized", grant.grant_id,
               f"grant={grant.grant_id} key_id={grant.key_id} worker={worker.worker_id}")
        return Decision("authorized", "Authorized by the owner's active grant for this exact action.", None,
                        grant.grant_id, None, "grant")
    return _pending(store, worker, raw, derived, session_id, task_id, now, "owner",
                    "No active grant covers this exact action, so the owner decides.")


def record_owner_decision(store: AssistantStore, cfg: dict, now: datetime, tenant_id: str, user_id: str,
                          request_id: str, approve: bool, decided_by: str) -> ActionRequestRow:
    if not decided_by.strip():
        raise ValueError("decided_by must not be empty")
    with store._lock:
        row = store.get_action_request(request_id, tenant_id=tenant_id, user_id=user_id)
        if row is None:
            raise KeyError(request_id)
        if row.status == "vetoed":
            raise ValueError("a vetoed request cannot be decided")
        if row.status != "pending":
            raise ValueError("request already decided")
        row = replace(row, status="approved" if approve else "denied", decided_at=as_utc(now), decided_by=decided_by)
        store.put_action_request(row)
        store.add_action_evidence(ActionEvidence(
            evidence_id="ev_" + secrets.token_urlsafe(12), tenant_id=tenant_id, user_id=user_id,
            worker_id=row.worker_id, session_id=row.session_id, task_id=row.task_id, key_id=row.key_id,
            outcome=row.status, provenance="owner_api", created_at=as_utc(now)))
        if approve and row.verb != Verb.UNKNOWN.value and row.key_canonical:
            key = ActionKey.from_canonical(row.key_canonical)
            if key.key_id() == row.key_id:
                propose_if_ready(store, cfg, now, tenant_id, user_id, row.worker_id, key, vetoed=False)
    store.audit_record(tenant_id=tenant_id, user_id=user_id, actor=decided_by, action="assistant.action.decided",
                       subject_id=request_id, detail=f"{'approve' if approve else 'deny'} request={request_id}")
    return row


def _refuse(code: str, message: str) -> ActivationRefused:
    return ActivationRefused(code, message)


def activate_grant(store: AssistantStore, cfg: dict, now: datetime, tenant_id: str, user_id: str, grant_id: str,
                   confirmed_by: str, judge_gate_satisfied: bool) -> ActionGrant:
    with store._lock:
        grant = store.get_action_grant(grant_id, tenant_id=tenant_id, user_id=user_id)
        if grant is None:
            raise _refuse("not_found", "grant not found")
        if grant.effective_state(now, int(cfg["policy_version"])) is not GrantState.PROPOSED:
            raise _refuse("not_proposed", "only a proposed grant can be activated")
        if in_shadow(store, cfg, now):
            raise _refuse("still_in_shadow", "the shadow period has not elapsed")
        if not confirmed_by.strip():
            raise _refuse("confirmation_missing", "the owner's confirmation is required")
        if judge_gate_satisfied is not True:
            raise _refuse("judge_gate_not_satisfied", "judge gate not satisfied")
        active = replace(grant, state=GrantState.ACTIVE, activated_at=as_utc(now), confirmed_by=confirmed_by,
                         expires_at=as_utc(now) + timedelta(days=int(cfg["grant_ttl_days"])))
        store.put_action_grant(active)
    store.audit_record(tenant_id=tenant_id, user_id=user_id, actor=confirmed_by, action="assistant.grant.activated",
                       subject_id=grant_id, detail=f"key_id={active.key_id} worker={active.worker_id}")
    return active


def revoke_action_grant(store: AssistantStore, now: datetime, tenant_id: str, user_id: str, grant_id: str,
                        revoked_by: str) -> ActionGrant:
    with store._lock:
        grant = store.get_action_grant(grant_id, tenant_id=tenant_id, user_id=user_id)
        if grant is None:
            raise KeyError(grant_id)
        revoked = revoke(grant, as_utc(now))
        store.put_action_grant(revoked)
    store.audit_record(tenant_id=tenant_id, user_id=user_id, actor=revoked_by, action="assistant.grant.revoked",
                       subject_id=grant_id, detail=f"key_id={revoked.key_id}")
    return revoked

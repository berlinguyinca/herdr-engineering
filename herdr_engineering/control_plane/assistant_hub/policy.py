"""Decision engine, owner decisions and activation gates for the assistant hub (spec 6.2, 6.3, 8, 13).

``decide`` answers whether a worker's action is authorized, denied or left to the owner. Nothing here is autonomous
by accident: vetoes win over everything, only an ACTIVE grant with the exact key id (same tenant, user and worker)
authorizes, the global shadow period blocks all authorization, and activation needs every gate to hold. Request
rows and audit entries carry the canonical key and key id only, never the raw command text.
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Literal

from ..assistant import AssistantStore
from .actiongrants import ActionEvidence, ActionGrant, ActionRequestRow, GrantState, as_utc, revoke
from .calibration import build_report, gate_satisfied
from .canonical import Derived, RawAction, derive
from .deploy import DeployMatch, scope_key
from .events import scrub_payload
from .evidence import OWNER_PROVENANCE, _shadow_elapsed, propose_if_ready
from .jev import JudgeUnavailable
from .judge import SEVERITY_LEVELS, JudgeRuntime, Verdict, consult
from .judge_version import judge_version as _judge_version
from .shadow import ShadowJob, label_decision, mark_regret
from .verbs import ActionKey, Verb
from .vetoes import Veto, VetoRule, check_vetoes
from .workers import Worker

POLICY_ACTOR = "hub:policy"
Outcome = Literal["authorized", "denied", "pending"]
Notify = Callable[..., None]      # (row, display, reason, *, project="") -> None; must only enqueue
Source = Literal["veto", "grant", "owner", "shadow", "unknown", "judge", "judge_unavailable", "deploy_preconditions"]


@dataclass(frozen=True)
class Decision:
    outcome: Outcome
    reason: str
    request_id: str | None
    grant_id: str | None
    veto_code: str | None
    source: Source
    verb: str = ""          # derived key facts for the caller's event; empty when no key could be derived
    key_id: str = ""


class ActivationRefused(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


_MAX_DISPLAY = 300
_MAX_SCRUB_INPUT = 1000          # regexes only ever see this much text, so their cost is bounded
_SECRET_WORDS = r"(?:token|secret|passw(?:or)?d|passwd|api[_-]?key|credential|auth\w{0,20}|cookie)"
_USERINFO_RE = re.compile(r"(://)[^/\s@]*@")
_OPTION_RE = re.compile(rf"(?i)(--?[\w-]{{0,40}}{_SECRET_WORDS}[\w-]{{0,40}})(=|\s+)([^\s\"']+)")
_ASSIGN_RE = re.compile(rf"(?i)\b(\w{{0,40}}{_SECRET_WORDS}\w{{0,40}}|\w{{0,40}}(?:pwd|pass|key))=([^\s\"']+)")
_BEARER_RE = re.compile(r"(?i)\b(bearer|basic)(\s+)[^\s\"']+")
_USER_OPT_RE = re.compile(r"(?i)(\s(?:-u|--user|--proxy-user)(?:=|\s+))[^\s\"']+")
_HEADER_RE = re.compile(r"(?i)\b(x-api-key|api-key|cookie|set-cookie|proxy-authorization|authorization)(\s*:\s*)[^\"'\n]*")
_SSHPASS_RE = re.compile(r"(?i)\b(sshpass\s+-p\s*)[^\s\"']+")
_LOGIN_RE = re.compile(r"(?i)\b((?:docker|podman)\s+login\b[^\n]*?\s(?:-p|--password)(?:=|\s+))[^\s\"']+")
_DB_ATTACHED_RE = re.compile(r"(?i)\b((?:mysql|mysqldump|mysqladmin|psql|mariadb)\b[^\n]*?\s-p)[^\s\"']+")
_MASK = "***"
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]+")


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
    out = _CONTROL_RE.sub(" ", text[:_MAX_SCRUB_INPUT])                    # no ESC/C0/C1 (or newline/tab) in anything a terminal may print
    out = _USERINFO_RE.sub(r"\1" + _MASK + "@", out)
    out = _HEADER_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{_MASK}", out)
    out = _SSHPASS_RE.sub(lambda m: m.group(1) + _MASK, out)
    out = _LOGIN_RE.sub(lambda m: m.group(1) + _MASK, out)
    out = _DB_ATTACHED_RE.sub(lambda m: m.group(1) + _MASK, out)
    out = _USER_OPT_RE.sub(lambda m: m.group(1) + _MASK, out)
    out = _OPTION_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{_MASK}", out)
    out = _ASSIGN_RE.sub(lambda m: f"{m.group(1)}={_MASK}", out)
    out = _BEARER_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{_MASK}", out)
    out = str(scrub_payload(out))
    cut = max(0, len(out) - _MAX_DISPLAY) + max(0, len(text) - _MAX_SCRUB_INPUT)
    return out[:_MAX_DISPLAY] + (f" \u2026(+{cut} chars)" if cut else "")


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


def _existing(store: AssistantStore, worker: Worker, status: str, session_id: str, task_id: str,
              key_id: str) -> ActionRequestRow | None:
    for r in store.list_action_requests(worker.tenant_id, worker.user_id, status):
        if (r.worker_id, r.session_id, r.task_id, r.key_id) == (worker.worker_id, session_id, task_id, key_id):
            return r
    return None


def _find_or_put(store: AssistantStore, worker: Worker, row: ActionRequestRow) -> tuple[ActionRequestRow, bool]:
    """The existing identical row, or ``row`` once stored. The row (scrubbing included) is built outside the lock."""
    with store._lock:
        found = _existing(store, worker, row.status, row.session_id, row.task_id, row.key_id)
        if found is not None:
            return found, False
        store.put_action_request(row)
    return row, True


def _deny(store: AssistantStore, worker: Worker, raw: RawAction, derived: Derived, veto: Veto, session_id: str,
          task_id: str, now: datetime) -> Decision:
    row = _new_row(worker, raw, derived, session_id, task_id, "vetoed", f"vetoed: {veto.reason}", now)
    row, created = _find_or_put(store, worker, row)
    if created:
        _audit(store, worker, POLICY_ACTOR, "assistant.action.vetoed", row.request_id,
               f"veto={veto.code} key_id={row.key_id}")
    return Decision("denied", f"Denied by the {veto.code} rule: {veto.reason}.", row.request_id, None, veto.code, "veto")


STANDING = "owner_standing"      # ActionGrant.source of a deploy scope the owner confirmed in Slack


def _live_grant(store: AssistantStore, cfg: dict, worker: Worker, key: ActionKey, now: datetime,
                judge_version: str = "", deploy: DeployMatch | None = None) -> ActionGrant | None:
    """The ACTIVE grant that covers ``key`` for this worker.

    A configured deploy (``deploy`` set) is covered ONLY by a standing grant for its scope (tenant, user, worker, verb,
    repo, scope; never the sha, which each request binds on its own key and Task 8 checks).  Every other action is
    matched on the exact key id, and a standing grant never covers it."""
    policy = int(cfg["policy_version"])
    if deploy is not None:
        key_id, standing = scope_key(deploy).key_id(), True
    else:
        key_id, standing = key.key_id(), False
    for g in store.list_action_grants(worker.tenant_id, worker.user_id):
        if (g.worker_id == worker.worker_id and g.key_id == key_id and g.tenant_id == worker.tenant_id
                and g.user_id == worker.user_id and (g.source == STANDING) is standing
                and (not standing or (g.verb == Verb.DEPLOY_PROD.value
                                      and g.resource_scope.casefold() == deploy.repo.casefold()))
                and g.effective_state(now, policy, judge_version) is GrantState.ACTIVE):
            return g
    return None


def _fresh_now(judge: JudgeRuntime | None, fallback: datetime) -> datetime:
    """The clock after a slow consult (never earlier than the caller's ``now``)."""
    try:
        return max(as_utc(judge.now()), as_utc(fallback)) if judge is not None else fallback
    except Exception:
        return fallback


def current_judge_version(judge: JudgeRuntime | None) -> str:
    """The version stamp of the live judge config, or ``""`` (no version check) when there is no usable judge."""
    if judge is None:
        return ""
    try:
        return _judge_version(judge.cfg_getter())
    except Exception:
        return ""


def _submit_to_recorder(judge: JudgeRuntime | None, worker: Worker, derived: Derived, request_id: str,
                        session_id: str, task_id: str) -> None:
    """A NEW pending row is recorded once.  Unclassifiable actions have no key facts to judge, so nothing is sent."""
    key = derived.key
    if judge is None or key is None or derived.owner_decides or key.verb is Verb.UNKNOWN:
        return
    try:
        judge.recorder.submit(ShadowJob(worker.tenant_id, worker.user_id, worker.worker_id, session_id, task_id,
                                        request_id, key))
    except Exception:       # recording is best effort and must never change or break a decision
        pass


def _notify_owner(notify: Notify | None, row: ActionRequestRow, raw: RawAction) -> None:
    """A NEW pending row is offered to the owner's Slack once.  The notifier only enqueues; a failure changes nothing."""
    if notify is None:
        return
    try:
        notify(row, row.display, row.reason, project=raw.repo or "")
    except Exception:
        pass


def _pending(store: AssistantStore, worker: Worker, raw: RawAction, derived: Derived, session_id: str,
             task_id: str, now: datetime, source: Source, reason: str, judge: JudgeRuntime | None = None,
             extra: str = "", notify: Notify | None = None) -> Decision:
    row = _new_row(worker, raw, derived, session_id, task_id, "pending", reason, now)
    row, created = _find_or_put(store, worker, row)
    if created:
        _audit(store, worker, POLICY_ACTOR, "assistant.action.requested", row.request_id,
               f"source={source} key_id={row.key_id}{extra}")
        _submit_to_recorder(judge, worker, derived, row.request_id, session_id, task_id)
        _notify_owner(notify, row, raw)
    return Decision("pending", reason, row.request_id, None, None, source)


_JUDGE_REASON = "The judge flagged this action, so the owner decides."
_JUDGE_UNAVAILABLE_REASON = "The judge is unavailable, so the owner decides."


def _apply_verdict(verdict: Verdict) -> tuple[Outcome, Source]:
    """Pure: the judge can only ADD friction.  An escalation sends an otherwise-authorized request to the owner."""
    if verdict.escalate:
        return "pending", "judge"
    return "authorized", "grant"


def _pct(value: float) -> str:
    return f"{value * 100:.2f}%"


def _verdict_detail(v: Verdict) -> str:
    """Version and the four answers as two-decimal percentages (severity as a share of the top level); no key, endpoint or text."""
    top = float(len(SEVERITY_LEVELS) - 1)
    return (f" judge_version={v.judge_version} destructive={_pct(v.destructive)} irreversible={_pct(v.irreversible)}"
            f" severity={_pct(v.severity / top)} covers={_pct(v.covers)}")


def _consult(judge: JudgeRuntime | None, key: ActionKey) -> Verdict | str:
    """The verdict, or a short reason code when the judge cannot answer (fail closed)."""
    if judge is None:
        return "no_judge"
    try:
        cfg = judge.cfg_getter()
        if cfg.get("enabled") is not True:
            return "disabled"
        if cfg.get("backend_confirmed") is not True:
            return "judge_unconfirmed"       # the owner withdrew (or never gave) the attestation: no autonomy
        client = judge.client_factory()
        if client is None:
            return "no_client"
        return consult(client, key, cfg, timeout=float(cfg["timeout_seconds"]))
    except JudgeUnavailable as exc:
        return exc.code
    except Exception:
        return "internal"


_PRECONDITIONS_REASON = "The deploy preconditions could not be verified, so the owner decides."


@dataclass(frozen=True)
class PreconditionResult:
    """Outcome of the hub's own deploy checks.  Only ``passed`` lets a standing-grant deploy through."""
    status: Literal["passed", "failed", "not_checked"]
    detail: str = ""


def _deploy_preconditions(store: AssistantStore, match: DeployMatch, grant: ActionGrant, worker: Worker,
                          now: datetime) -> PreconditionResult:
    """The hub-verified checks (on main, CI green, descends from the deployed commit) belong to Task 8.

    Until they exist this FAILS CLOSED: nothing is checked, so a standing-grant deploy is never authorized."""
    return PreconditionResult("not_checked")


def decide(store: AssistantStore, cfg: dict, worker: Worker, raw: RawAction, session_id: str, task_id: str,
           now: datetime, *, judge: JudgeRuntime | None = None, notify: Notify | None = None,
           deploy_cfg: dict | None = None) -> Decision:
    derived = derive(raw, deploy_cfg)
    key = derived.key
    return replace(_decide(store, cfg, worker, raw, derived, session_id, task_id, now, judge, notify),
                   verb=key.verb.value if key else Verb.UNKNOWN.value, key_id=key.key_id() if key else "")


def _consume_approval(store: AssistantStore, cfg: dict, worker: Worker, key_id: str,
                      now: datetime) -> ActionRequestRow | None:
    """Atomically spend one fresh owner approval for exactly this tenant, user, worker and key (oldest first)."""
    ttl = timedelta(minutes=int(cfg.get("approval_ttl_minutes", 15)))
    with store._lock:
        for r in store.list_action_requests(worker.tenant_id, worker.user_id, "approved"):
            if (r.worker_id, r.key_id) != (worker.worker_id, key_id) or r.consumed_at is not None:
                continue
            if r.decided_at is None or as_utc(now) - as_utc(r.decided_at) > ttl or as_utc(now) < as_utc(r.decided_at):
                continue
            spent = replace(r, consumed_at=as_utc(now))
            store.put_action_request(spent)
            _audit(store, worker, POLICY_ACTOR, "assistant.action.authorized", spent.request_id,
                   f"source=owner request={spent.request_id} key_id={key_id} worker={worker.worker_id}")
            return spent
    return None


def _decide(store: AssistantStore, cfg: dict, worker: Worker, raw: RawAction, derived: Derived, session_id: str,
            task_id: str, now: datetime, judge: JudgeRuntime | None, notify: Notify | None = None) -> Decision:
    veto = check_vetoes(derived, _extra_rules(cfg))
    if veto is not None:
        return _deny(store, worker, raw, derived, veto, session_id, task_id, now)
    if derived.key is not None and derived.one_shot_safe:     # fail closed: only keys that digest the whole action
        consumed = _consume_approval(store, cfg, worker, derived.key.key_id(), now)
        if consumed is not None:
            return Decision("authorized", "Authorized once by the owner's approval of this exact action.",
                            consumed.request_id, None, None, "owner")
    if derived.owner_decides or derived.key is None:
        return _pending(store, worker, raw, derived, session_id, task_id, now, "unknown",
                        "The action could not be classified with certainty, so the owner decides.", judge, notify=notify)
    grant = _live_grant(store, cfg, worker, derived.key, now, current_judge_version(judge), derived.deploy)
    if grant is not None and in_shadow(store, cfg, now):
        return _pending(store, worker, raw, derived, session_id, task_id, now, "shadow",
                        "The shadow period is still running, so the owner decides.", judge, notify=notify)
    if grant is not None:
        # The only path that would authorize: the judge is consulted here and nowhere else (no lock is held).
        result = _consult(judge, derived.key)
        if isinstance(result, str):
            return _pending(store, worker, raw, derived, session_id, task_id, now, "judge_unavailable",
                            _JUDGE_UNAVAILABLE_REASON, judge, extra=f" code={result}", notify=notify)
        outcome, source = _apply_verdict(result)
        if outcome == "authorized":
            # The consult can be slow: re-check that the grant is still live (revoked/expired/judge changed meanwhile).
            grant = _live_grant(store, cfg, worker, derived.key, _fresh_now(judge, now), current_judge_version(judge),
                                derived.deploy)
            if grant is None:
                return _pending(store, worker, raw, derived, session_id, task_id, now, "owner",
                                "No active grant covers this exact action, so the owner decides.", judge, notify=notify)
            if derived.deploy is not None:
                checked = _deploy_preconditions(store, derived.deploy, grant, worker, _fresh_now(judge, now))
                if checked.status != "passed":
                    return _pending(store, worker, raw, derived, session_id, task_id, now, "deploy_preconditions",
                                    _PRECONDITIONS_REASON, judge, extra=f" preconditions={checked.status}",
                                    notify=notify)
        if outcome == "pending":
            return _pending(store, worker, raw, derived, session_id, task_id, now, source, _JUDGE_REASON, judge,
                            extra=_verdict_detail(result), notify=notify)
        _audit(store, worker, POLICY_ACTOR, "assistant.action.authorized", grant.grant_id,
               f"grant={grant.grant_id} key_id={grant.key_id} worker={worker.worker_id}{_verdict_detail(result)}")
        return Decision("authorized", "Authorized by the owner's active grant for this exact action.", None,
                        grant.grant_id, None, source)
    return _pending(store, worker, raw, derived, session_id, task_id, now, "owner",
                    "No active grant covers this exact action, so the owner decides.", judge, notify=notify)


def record_owner_decision(store: AssistantStore, cfg: dict, now: datetime, tenant_id: str, user_id: str,
                          request_id: str, approve: bool, decided_by: str, *, judge_version: str = "",
                          provenance: str = "owner_api") -> ActionRequestRow:
    if provenance not in OWNER_PROVENANCE:
        raise ValueError(f"unknown provenance {provenance!r}")
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
            outcome=row.status, provenance=provenance, created_at=as_utc(now)))
        if approve and row.verb != Verb.UNKNOWN.value and row.key_canonical:
            key = ActionKey.from_canonical(row.key_canonical)
            if key.key_id() == row.key_id:
                propose_if_ready(store, cfg, now, tenant_id, user_id, row.worker_id, key, vetoed=False,
                                 judge_version=judge_version)
    label_decision(store, tenant_id, user_id, request_id, approve, as_utc(now))   # first label wins
    store.audit_record(tenant_id=tenant_id, user_id=user_id, actor=decided_by, action="assistant.action.decided",
                       subject_id=request_id, detail=f"{'approve' if approve else 'deny'} request={request_id}")
    return row


def activate_grant(store: AssistantStore, cfg: dict, now: datetime, tenant_id: str, user_id: str, grant_id: str,
                   confirmed_by: str, judge: JudgeRuntime | None) -> ActionGrant:
    try:
        return _activate_grant(store, cfg, now, tenant_id, user_id, grant_id, confirmed_by, judge)
    except ActivationRefused as exc:
        if exc.code != "not_found":                     # an unknown id has no grant to attach the entry to
            store.audit_record(tenant_id=tenant_id, user_id=user_id, actor=confirmed_by.strip() or "unknown",
                               action="assistant.grant.activate_refused", subject_id=grant_id,
                               detail=f"{exc.code} grant={grant_id}")
        raise


_GATE_MESSAGES = {
    "judge_disabled": "the judge is not enabled",
    "backend_unconfirmed": "the owner has not confirmed a real model is serving the judge",
    "stale_report": "the calibration report does not match the current judge version",
    "insufficient_labels": "not enough labeled decisions for this risk class",
    "insufficient_diversity": "labels come from too few different actions",
    "no_discrimination": "too few denied or regretted decisions to show the judge can tell them apart",
    "fn_bound_exceeded": "the judge's false-negative bound is too high",
}


def _check_judge_gate(store: AssistantStore, now: datetime, tenant_id: str, user_id: str, grant: ActionGrant,
                      judge: JudgeRuntime | None) -> str:
    """Builds the calibration report from the store and returns the judge version, or refuses with the gate's code."""
    if judge is None:
        raise ActivationRefused("judge_disabled", _GATE_MESSAGES["judge_disabled"])
    try:
        risk = ActionKey.from_canonical(grant.key_canonical).risk_class.value
    except Exception:
        raise ActivationRefused("insufficient_labels", "the grant has no readable action key") from None
    try:
        jcfg = judge.cfg_getter()
        report = build_report(store.list_judge_records(tenant_id, user_id), jcfg, now, tenant_id, user_id)
        ok, code = gate_satisfied(report, jcfg, risk)
        version = _judge_version(jcfg)
    except Exception:
        raise ActivationRefused("judge_error", "the judge gate could not be evaluated") from None
    if not ok:
        raise ActivationRefused(code, _GATE_MESSAGES.get(code, "judge gate not satisfied"))
    return version


def _activate_grant(store: AssistantStore, cfg: dict, now: datetime, tenant_id: str, user_id: str, grant_id: str,
                    confirmed_by: str, judge: JudgeRuntime | None) -> ActionGrant:
    with store._lock:
        grant = store.get_action_grant(grant_id, tenant_id=tenant_id, user_id=user_id)
        if grant is None:
            raise ActivationRefused("not_found", "grant not found")
        if grant.effective_state(now, int(cfg["policy_version"])) is not GrantState.PROPOSED:
            raise ActivationRefused("not_proposed", "only a proposed grant can be activated")
        if in_shadow(store, cfg, now):
            raise ActivationRefused("still_in_shadow", "the shadow period has not elapsed")
        if not confirmed_by.strip():
            raise ActivationRefused("confirmation_missing", "the owner's confirmation is required")
        version = _check_judge_gate(store, now, tenant_id, user_id, grant, judge)
        active = replace(grant, state=GrantState.ACTIVE, activated_at=as_utc(now), confirmed_by=confirmed_by,
                         judge_version=version,
                         expires_at=as_utc(now) + timedelta(days=int(cfg["grant_ttl_days"])))
        store.put_action_grant(active)
    store.audit_record(tenant_id=tenant_id, user_id=user_id, actor=confirmed_by, action="assistant.grant.activated",
                       subject_id=grant_id, detail=f"key_id={active.key_id} worker={active.worker_id}")
    return active


def create_standing_grant(store: AssistantStore, cfg: dict, now: datetime, tenant_id: str, user_id: str,
                          worker_id: str, key: ActionKey, confirmed_by: str, judge_version: str,
                          judge: JudgeRuntime | None) -> ActionGrant:
    """An ACTIVE standing grant for one deploy scope, created at the owner's verified Slack confirmation.

    It skips ONLY the approval-count and session/task diversity rules.  The shadow period and the judge gate (every
    code of ``_check_judge_gate``) must hold now, exactly as for ``activate_grant``; at use the grant still obeys
    ``effective_state``, the shadow period, the judge consult and the vetoes.  ``judge_version`` is the version the caller
    saw; when given and no longer current the creation is refused as ``stale_report``.  An existing live standing grant
    for the same worker and scope is returned unchanged."""
    if (key.tool != "deploy" or key.verb is not Verb.DEPLOY_PROD or not key.resource_scope
            or not key.target.startswith("scope:")):
        raise ValueError("a standing grant needs a deploy scope key")
    try:
        return _create_standing_grant(store, cfg, now, tenant_id, user_id, worker_id, key, confirmed_by, judge_version,
                                      judge)
    except ActivationRefused as exc:
        store.audit_record(tenant_id=tenant_id, user_id=user_id, actor=confirmed_by.strip() or "unknown",
                           action="assistant.grant.activate_refused", subject_id=key.key_id(),
                           detail=f"{exc.code} key_id={key.key_id()} worker={worker_id}")
        raise


def _create_standing_grant(store: AssistantStore, cfg: dict, now: datetime, tenant_id: str, user_id: str,
                           worker_id: str, key: ActionKey, confirmed_by: str, judge_version: str,
                           judge: JudgeRuntime | None) -> ActionGrant:
    if not confirmed_by.strip():
        raise ActivationRefused("confirmation_missing", "the owner's confirmation is required")
    now, key_id, policy = as_utc(now), key.key_id(), int(cfg["policy_version"])
    grant = ActionGrant(
        grant_id="ag_" + secrets.token_urlsafe(16), tenant_id=tenant_id, user_id=user_id, worker_id=worker_id,
        resource_scope=key.resource_scope, key_id=key_id, verb=key.verb.value, key_canonical=key.canonical(),
        state=GrantState.PROPOSED, created_at=now, proposed_at=now, policy_version=policy, source=STANDING)
    with store._lock:
        current = current_judge_version(judge)
        for g in store.list_action_grants(tenant_id, user_id):
            if (g.worker_id, g.key_id, g.source) == (worker_id, key_id, STANDING) \
                    and g.effective_state(now, policy, current) is GrantState.ACTIVE:
                return g                                  # a second confirmation creates nothing more
        if in_shadow(store, cfg, now):
            raise ActivationRefused("still_in_shadow", "the shadow period has not elapsed")
        version = _check_judge_gate(store, now, tenant_id, user_id, grant, judge)
        if judge_version and judge_version != version:
            raise ActivationRefused("stale_report", _GATE_MESSAGES["stale_report"])
        active = replace(grant, state=GrantState.ACTIVE, activated_at=now, confirmed_by=confirmed_by,
                         judge_version=version, expires_at=now + timedelta(days=int(cfg["grant_ttl_days"])))
        try:
            store.put_action_grant(active)
        except ValueError:                                # per-tenant grant cap
            raise ActivationRefused("grant_cap", "the tenant already holds the maximum number of grants") from None
    store.audit_record(tenant_id=tenant_id, user_id=user_id, actor=confirmed_by, action="assistant.grant.activated",
                       subject_id=active.grant_id, detail=f"key_id={key_id} worker={worker_id} source={STANDING}")
    return active


def revoke_action_grant(store: AssistantStore, now: datetime, tenant_id: str, user_id: str, grant_id: str,
                        revoked_by: str, *, policy_version: int | None = None, judge_version: str = "") -> ActionGrant:
    with store._lock:
        grant = store.get_action_grant(grant_id, tenant_id=tenant_id, user_id=user_id)
        if grant is None:
            raise KeyError(grant_id)
        was_active = grant.effective_state(
            now, grant.policy_version if policy_version is None else policy_version, judge_version) is GrantState.ACTIVE
        revoked = revoke(grant, as_utc(now))
        store.put_action_grant(revoked)
    if was_active:      # only approvals that actually rode a live grant are regretted; a proposal never authorized anything
        mark_regret(store, tenant_id, user_id, revoked.key_id, revoked.worker_id)
    store.audit_record(tenant_id=tenant_id, user_id=user_id, actor=revoked_by, action="assistant.grant.revoked",
                       subject_id=grant_id, detail=f"key_id={revoked.key_id}")
    return revoked

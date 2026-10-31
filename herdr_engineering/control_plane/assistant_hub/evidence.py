"""Evidence counting and grant proposals (spec 6.2, 13).

Only the owner's own decisions count. Worker-reported evidence is ignored entirely: it can neither earn trust nor
reset a streak, so a worker cannot farm approvals or erase denials. ``propose_if_ready`` can only create a PROPOSED
grant; activation needs the owner's confirmation elsewhere.
"""

from __future__ import annotations

import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from ..assistant import AssistantStore
from .actiongrants import ActionEvidence, ActionGrant, GrantState, as_utc
from .verbs import ActionKey, RiskClass, Verb


@dataclass(frozen=True)
class Counts:
    approvals: int = 0
    sessions: int = 0
    tasks: int = 0
    denials: int = 0


def count_qualifying(evidence: Sequence[ActionEvidence], key_id: str, worker_id: str, resource_scope: str) -> Counts:
    """Count the owner's approvals for one key and worker since the latest owner denial.

    ``key_id`` already embeds the resource scope (ActionKey.canonical), so matching it is the scope match;
    ``resource_scope`` is accepted for the caller's clarity and is not compared separately.
    """
    owned = sorted((e for e in evidence if e.provenance == "owner_api" and e.key_id == key_id
                    and e.worker_id == worker_id and e.outcome in ("approved", "denied")),
                   key=lambda e: (as_utc(e.created_at), e.evidence_id))
    denials = sum(1 for e in owned if e.outcome == "denied")
    streak: list[ActionEvidence] = []
    for e in owned:
        if e.outcome == "denied":
            streak = []
        else:
            streak.append(e)
    return Counts(approvals=len(streak),
                  sessions=len({e.session_id for e in streak if e.session_id}),
                  tasks=len({e.task_id for e in streak if e.task_id}),
                  denials=denials)


def required_approvals(risk: RiskClass, cfg: Mapping, scope_is_new_for_confirmed_verb: bool) -> int:
    n = int(cfg["required_approvals"][risk.value])
    return max(3, n // 3) if scope_is_new_for_confirmed_verb else n


def _scope_is_new_for_confirmed_verb(grants: Sequence[ActionGrant], worker_id: str, key: ActionKey) -> bool:
    scope = key.resource_scope.casefold()
    return any(g.worker_id == worker_id and g.verb == key.verb.value and g.confirmed_by
               and g.state in (GrantState.ACTIVE, GrantState.EXPIRED) and g.activated_at is not None
               and g.resource_scope.casefold() != scope
               for g in grants)


def _shadow_elapsed(store: AssistantStore, cfg: Mapping, now: datetime) -> bool:
    configured = cfg.get("shadow_started_at")
    started = store.ensure_shadow_started(now, datetime.fromisoformat(configured) if configured else None)
    return as_utc(started) + timedelta(days=int(cfg["shadow_days"])) <= as_utc(now)


def propose_if_ready(store: AssistantStore, cfg: Mapping, now: datetime, tenant_id: str, user_id: str,
                     worker_id: str, key: ActionKey, *, vetoed: bool) -> ActionGrant | None:
    """The existing live grant for (worker, key), or a new PROPOSED one when the evidence suffices, else None."""
    if vetoed or key.verb is Verb.UNKNOWN or not _shadow_elapsed(store, cfg, now):
        return None
    now = as_utc(now)
    key_id = key.key_id()
    with store._lock:                                       # check-and-put must be atomic
        grants = store.list_action_grants(tenant_id, user_id)
        mine = [g for g in grants if g.worker_id == worker_id and g.key_id == key_id]
        after: datetime | None = None
        if mine:
            latest = mine[-1]
            effective = latest.effective_state(now, int(cfg["policy_version"]))
            if latest.state in (GrantState.PROPOSED, GrantState.ACTIVE) and effective is not GrantState.EXPIRED:
                return latest
            terminal = latest.revoked_at if latest.state is GrantState.REVOKED else latest.expires_at
            after = as_utc(terminal) if terminal else None
        evidence = store.list_action_evidence(tenant_id, user_id, key_id)
        if after is not None:
            evidence = [e for e in evidence if as_utc(e.created_at) > after]
        counts = count_qualifying(evidence, key_id, worker_id, key.resource_scope)
        need = required_approvals(key.risk_class, cfg, _scope_is_new_for_confirmed_verb(grants, worker_id, key))
        if counts.approvals < need or counts.sessions < int(cfg["min_sessions"]) or counts.tasks < int(cfg["min_tasks"]):
            return None
        grant = ActionGrant(
            grant_id="ag_" + secrets.token_urlsafe(16), tenant_id=tenant_id, user_id=user_id, worker_id=worker_id,
            resource_scope=key.resource_scope, key_id=key_id, verb=key.verb.value, key_canonical=key.canonical(),
            state=GrantState.PROPOSED, created_at=now, proposed_at=now,
            expires_at=now + timedelta(days=int(cfg["grant_ttl_days"])),
            policy_version=int(cfg["policy_version"]), confirmed_by="")
        store.put_action_grant(grant)
        return grant

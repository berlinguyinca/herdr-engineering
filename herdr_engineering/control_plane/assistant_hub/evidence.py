"""Evidence counting and grant proposals (spec 6.2, 13).

Only the owner's own decisions count. Worker-reported evidence is ignored entirely: it can neither earn trust nor
reset a streak, so a worker cannot farm approvals or erase denials. ``propose_if_ready`` can only create a PROPOSED
grant; activation needs the owner's confirmation elsewhere.
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from ..assistant import AssistantStore
from .actiongrants import ActionEvidence, ActionGrant, GrantState, as_utc
from .verbs import ActionKey, RiskClass, Verb

log = logging.getLogger(__name__)


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
    # REVOKED confirmed grants intentionally stop lowering the requirement: revocation withdraws that confirmation.
    scope = key.resource_scope.casefold()
    return any(g.worker_id == worker_id and g.verb == key.verb.value and g.confirmed_by
               and g.state in (GrantState.ACTIVE, GrantState.EXPIRED) and g.activated_at is not None
               and g.resource_scope.casefold() != scope
               for g in grants)


_NO_FLOOR = datetime.min.replace(tzinfo=UTC)               # sentinel: stale but no usable timestamp


def _stale_floor(g: ActionGrant, now: datetime, policy: int) -> datetime | None:
    """None when ``g`` is live; otherwise the time before which evidence no longer counts (or ``_NO_FLOOR``)."""
    effective = g.effective_state(now, policy)
    if g.state in (GrantState.PROPOSED, GrantState.ACTIVE) and effective in (GrantState.PROPOSED, GrantState.ACTIVE):
        if g.state is GrantState.ACTIVE or g.expires_at is None or as_utc(g.expires_at) > now:
            return None
    if g.state is GrantState.REVOKED:
        stamp = g.revoked_at
    elif effective is GrantState.SHADOW:
        stamp = max((as_utc(t) for t in (g.proposed_at, g.activated_at) if t is not None), default=None)
    else:                                                   # expired, or PROPOSED past its expiry
        stamp = g.expires_at
    return as_utc(stamp) if stamp is not None else _NO_FLOOR


def _shadow_elapsed(store: AssistantStore, cfg: Mapping, now: datetime) -> bool:
    configured = cfg.get("shadow_started_at")
    started = store.ensure_shadow_started(now, datetime.fromisoformat(configured) if configured else None)
    return as_utc(started) + timedelta(days=int(cfg["shadow_days"])) <= as_utc(now)


def propose_if_ready(store: AssistantStore, cfg: Mapping, now: datetime, tenant_id: str, user_id: str,
                     worker_id: str, key: ActionKey, *, vetoed: bool) -> ActionGrant | None:
    """The existing live grant for (worker, key), or a new PROPOSED one when the evidence suffices, else None.

    Known limit: a worker can enumerate session/task ids, but each counted approval is an owner decision, so
    diversity only protects against one agent repeating itself.
    """
    if vetoed or key.verb is Verb.UNKNOWN or not _shadow_elapsed(store, cfg, now):
        return None
    now = as_utc(now)
    key_id = key.key_id()
    with store._lock:                                       # check-and-put must be atomic
        grants = store.list_action_grants(tenant_id, user_id)
        mine = [g for g in grants if g.worker_id == worker_id and g.key_id == key_id]
        policy = int(cfg["policy_version"])
        floors: list[datetime] = []
        for g in mine:
            floor = _stale_floor(g, now, policy)
            if floor is None:
                return g                                    # a live grant wins, whatever the store order
            floors.append(floor)
        after = max(floors) if floors else None
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
        try:
            store.put_action_grant(grant)
        except ValueError:                                  # per-tenant grant cap
            log.warning("action grant cap reached for tenant %r; not proposing", tenant_id)
            return None
        return grant

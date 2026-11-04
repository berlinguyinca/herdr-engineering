"""Evidence counting and grant proposals."""
from datetime import UTC, datetime, timedelta

from herdr_engineering.control_plane.assistant import AssistantStore, _grants_config
from herdr_engineering.control_plane.assistant_hub.actiongrants import (
    ActionEvidence,
    ActionGrant,
    GrantState,
)
from herdr_engineering.control_plane.assistant_hub.evidence import (
    Counts,
    count_qualifying,
    propose_if_ready,
    required_approvals,
)
from herdr_engineering.control_plane.assistant_hub.verbs import (
    VERB_RISK,
    ActionKey,
    FlagClass,
    RiskClass,
    Verb,
)

T0 = datetime(2026, 10, 1, tzinfo=UTC)
LATE = T0 + timedelta(days=60)
T, U, W = "t", "u", "w1"


def _key(verb: Verb = Verb.GIT_PUSH, scope: str = "repo-a", target: str = "main") -> ActionKey:
    return ActionKey("git", verb, scope, target, FlagClass.NONE, VERB_RISK[verb])


def _cfg(**over):
    cfg = _grants_config({"shadow_started_at": T0.isoformat(), **over})
    return cfg


def _ev(i: int, key: ActionKey, session: str = "", task: str = "", outcome: str = "approved",
        provenance: str = "owner_api", worker: str = W, minutes: int | None = None) -> ActionEvidence:
    return ActionEvidence(evidence_id=f"e{i:03d}", tenant_id=T, user_id=U, worker_id=worker, session_id=session,
                          task_id=task, key_id=key.key_id(), outcome=outcome, provenance=provenance,
                          created_at=T0 + timedelta(minutes=i if minutes is None else minutes))


def _count(evs, key: ActionKey, worker: str = W) -> Counts:
    return count_qualifying(evs, key.key_id(), worker, key.resource_scope)


def test_one_session_repeating_does_not_count_past_one_session():
    k = _key()
    c = _count([_ev(i, k, "s1", f"t{i}") for i in range(10)], k)
    assert c.approvals == 10 and c.sessions == 1 and c.tasks == 10


def test_requires_distinct_sessions_and_tasks():
    k = _key()
    evs = [_ev(0, k, "s1", "a"), _ev(1, k, "s2", "a"), _ev(2, k, "s3", "b"), _ev(3, k, "", "")]
    c = _count(evs, k)
    assert (c.approvals, c.sessions, c.tasks) == (4, 3, 2)


def test_worker_reported_evidence_counts_nothing():
    k = _key()
    evs = [_ev(0, k, "s1", "a"), _ev(1, k, "s2", "b", provenance="worker_reported"),
           _ev(2, k, "s3", "c", outcome="denied", provenance="worker_reported")]
    c = _count(evs, k)
    assert (c.approvals, c.sessions, c.tasks, c.denials) == (1, 1, 1, 0)


def test_denial_restarts_the_streak():
    k = _key()
    evs = [_ev(0, k, "s1", "a"), _ev(1, k, "s2", "b"), _ev(2, k, "s3", "c", outcome="denied"), _ev(3, k, "s4", "d")]
    c = _count(evs, k)
    assert (c.approvals, c.sessions, c.tasks, c.denials) == (1, 1, 1, 1)


def test_evidence_for_a_different_key_scope_or_worker_is_ignored():
    k = _key()
    other_scope = _key(scope="repo-b")
    other_verb = _key(Verb.PR_COMMENT)
    evs = [_ev(0, k, "s1", "a"), _ev(1, other_scope, "s2", "b"), _ev(2, other_verb, "s3", "c"),
           _ev(3, k, "s4", "d", worker="w2"), _ev(4, other_scope, "s5", "e", outcome="denied")]
    c = _count(evs, k)
    assert (c.approvals, c.sessions, c.tasks, c.denials) == (1, 1, 1, 0)


def test_threshold_by_risk_class():
    cfg = _cfg()
    assert required_approvals(RiskClass.LOCAL_REVERSIBLE, cfg, False) == 5
    assert required_approvals(RiskClass.REMOTE_REVERSIBLE, cfg, False) == 10
    assert required_approvals(RiskClass.OUTWARD, cfg, False) == 30
    cfg = _cfg(required_approvals={"outward": 12})
    assert required_approvals(RiskClass.OUTWARD, cfg, False) == 12


def test_new_scope_for_confirmed_verb_uses_third_rule():
    cfg = _cfg()
    assert required_approvals(RiskClass.OUTWARD, cfg, True) == 10
    assert required_approvals(RiskClass.REMOTE_REVERSIBLE, cfg, True) == 3
    assert required_approvals(RiskClass.LOCAL_REVERSIBLE, cfg, True) == 3


def _fill(store: AssistantStore, k: ActionKey, n: int, start: int = 0) -> None:
    for i in range(start, start + n):
        store.add_action_evidence(_ev(i, k, f"s{i % 3}", f"t{i % 2}"))


def _propose(store, cfg, k, now=LATE, vetoed=False):
    return propose_if_ready(store, cfg, now, T, U, W, k, vetoed=vetoed)


def test_no_proposal_during_global_shadow():
    store, cfg, k = AssistantStore(), _cfg(), _key()
    _fill(store, k, 10)
    assert _propose(store, cfg, k, now=T0 + timedelta(days=13)) is None
    assert _propose(store, cfg, k, now=T0 + timedelta(days=14)) is not None


def test_shadow_start_is_stamped_when_unconfigured():
    store, k = AssistantStore(), _key()
    cfg = _grants_config({})
    _fill(store, k, 10)
    assert _propose(store, cfg, k, now=T0) is None              # stamps shadow start at T0
    assert _propose(store, cfg, k, now=T0 + timedelta(days=15)) is not None


def test_not_ready_below_thresholds_unknown_or_vetoed():
    cfg = _cfg()
    store, k = AssistantStore(), _key()
    _fill(store, k, 9)
    assert _propose(store, cfg, k) is None
    _fill(store, k, 1, start=9)
    assert _propose(store, cfg, k, vetoed=True) is None
    # one session only
    s2, k2 = AssistantStore(), _key(scope="r2")
    for i in range(10):
        s2.add_action_evidence(_ev(i, k2, "s1", f"t{i % 2}"))
    assert _propose(s2, cfg, k2) is None
    uk = ActionKey("x", Verb.UNKNOWN, "s", "t", FlagClass.NONE, RiskClass.OUTWARD)
    s3 = AssistantStore()
    _fill(s3, uk, 40)
    assert _propose(s3, cfg, uk) is None
    assert store.list_action_grants(T, U) == []


def test_propose_is_idempotent_and_never_active():
    store, cfg, k = AssistantStore(), _cfg(), _key()
    _fill(store, k, 10)
    g1 = _propose(store, cfg, k)
    g2 = _propose(store, cfg, k, now=LATE + timedelta(days=1))
    assert g1 is not None and g2 is not None and g1.grant_id == g2.grant_id
    assert g1.grant_id.startswith("ag_") and g1.state is GrantState.PROPOSED
    assert g1.proposed_at == LATE and g1.expires_at == LATE + timedelta(days=30)
    assert (g1.tenant_id, g1.user_id, g1.worker_id, g1.confirmed_by) == (T, U, W, "")
    assert (g1.key_id, g1.verb, g1.resource_scope, g1.key_canonical) == (
        k.key_id(), "git.push", k.resource_scope, k.canonical())
    assert g1.policy_version == cfg["policy_version"]
    assert len(store.list_action_grants(T, U)) == 1


def test_active_grant_is_returned_unchanged():
    store, cfg, k = AssistantStore(), _cfg(), _key()
    _fill(store, k, 10)
    g = _propose(store, cfg, k)
    g.state, g.activated_at, g.confirmed_by = GrantState.ACTIVE, LATE, "owner"
    store.put_action_grant(g)
    again = _propose(store, cfg, k)
    assert again.state is GrantState.ACTIVE and len(store.list_action_grants(T, U)) == 1


def test_reproposal_after_revoke_needs_fresh_evidence():
    store, cfg, k = AssistantStore(), _cfg(), _key()
    _fill(store, k, 10)
    g = _propose(store, cfg, k)
    g.state, g.revoked_at = GrantState.REVOKED, T0 + timedelta(days=40)
    store.put_action_grant(g)
    assert _propose(store, cfg, k, now=LATE) is None
    for i in range(10):
        store.add_action_evidence(_ev(100 + i, k, f"s{i % 3}", f"t{i % 2}", minutes=60 * 24 * 41 + i))
    g2 = _propose(store, cfg, k, now=LATE)
    assert g2 is not None and g2.grant_id != g.grant_id and g2.state is GrantState.PROPOSED


def test_reproposal_after_expiry_uses_expires_at():
    store, cfg, k = AssistantStore(), _cfg(), _key()
    _fill(store, k, 10)
    g = _propose(store, cfg, k)
    g.state, g.activated_at, g.expires_at, g.confirmed_by = GrantState.EXPIRED, T0, T0 + timedelta(days=30), "o"
    store.put_action_grant(g)
    assert _propose(store, cfg, k, now=LATE) is None


def test_new_scope_for_confirmed_verb_lowers_requirement():
    store, cfg = AssistantStore(), _cfg()
    old, new = _key(scope="repo-a"), _key(scope="repo-b")
    store.put_action_grant(ActionGrant(
        grant_id="g0", tenant_id=T, user_id=U, worker_id=W, resource_scope=old.resource_scope, key_id=old.key_id(),
        verb="git.push", state=GrantState.ACTIVE, activated_at=T0, confirmed_by="owner", created_at=T0))
    _fill(store, new, 3)
    g = _propose(store, cfg, new)
    assert g is not None and g.key_id == new.key_id()
    # unconfirmed or other-worker grants do not lower it
    s2 = AssistantStore()
    s2.put_action_grant(ActionGrant(
        grant_id="g0", tenant_id=T, user_id=U, worker_id=W, key_id=old.key_id(), verb="git.push",
        state=GrantState.ACTIVE, activated_at=T0, confirmed_by="", created_at=T0))
    _fill(s2, new, 3)
    assert _propose(s2, cfg, new) is None


def _stale_setup(**grant_over):
    store, cfg, k = AssistantStore(), _cfg(), _key()
    _fill(store, k, 10)                                   # evidence at T0 .. T0+9min
    g = _propose(store, cfg, k)
    for name, value in grant_over.items():
        setattr(g, name, value)
    store.put_action_grant(g)
    return store, cfg, k, g


def _fresh(store, k, base_minutes: int) -> None:
    for i in range(10):
        store.add_action_evidence(_ev(200 + i, k, f"s{i % 3}", f"t{i % 2}", minutes=base_minutes + i))


def test_proposed_past_expiry_needs_evidence_newer_than_expiry():
    store, cfg, k, g = _stale_setup()
    later = g.expires_at + timedelta(days=1)
    assert _propose(store, cfg, k, now=later) is None
    _fresh(store, k, int((g.expires_at - T0).total_seconds() // 60) + 5)
    g2 = _propose(store, cfg, k, now=later)
    assert g2 is not None and g2.grant_id != g.grant_id and g2.state is GrantState.PROPOSED
    assert store.get_action_grant(g.grant_id, tenant_id=T, user_id=U).state is GrantState.PROPOSED   # untouched


def test_active_grant_after_expiry_needs_newer_evidence():
    store, cfg, k, g = _stale_setup(state=GrantState.ACTIVE, activated_at=LATE, confirmed_by="o")
    later = g.expires_at + timedelta(days=1)
    assert _propose(store, cfg, k, now=later) is None


def test_policy_bump_proposed_needs_evidence_newer_than_proposed_at():
    store, _, k, g = _stale_setup()
    bumped = _cfg(policy_version=2)
    assert _propose(store, bumped, k) is None             # no newer evidence
    _fresh(store, k, 60 * 24 * 61)                        # after proposed_at (LATE = +60d)
    g2 = _propose(store, bumped, k, now=LATE + timedelta(days=2))
    assert g2 is not None and g2.grant_id != g.grant_id and g2.policy_version == 2


def test_policy_bump_active_uses_activated_at_floor():
    act = LATE + timedelta(days=3)
    store, _, k, g = _stale_setup(state=GrantState.ACTIVE, activated_at=act, confirmed_by="o")
    bumped = _cfg(policy_version=2)
    now = act + timedelta(days=2)
    _fresh(store, k, 60 * 24 * 61)                        # newer than proposed_at, older than activated_at
    assert _propose(store, bumped, k, now=now) is None
    _fresh(store, k, 60 * 24 * 64)
    assert _propose(store, bumped, k, now=now) is not None


def test_live_grant_wins_over_revoked_regardless_of_store_order():
    for live_first in (True, False):
        store, cfg, k = AssistantStore(), _cfg(), _key()
        _fill(store, k, 10)
        live = _propose(store, cfg, k)
        dead = ActionGrant(grant_id="ag_dead", tenant_id=T, user_id=U, worker_id=W, key_id=k.key_id(),
                           verb="git.push", state=GrantState.REVOKED, revoked_at=T0 + timedelta(days=1),
                           created_at=LATE + timedelta(days=1 if live_first else -1))
        store.put_action_grant(dead)
        assert _propose(store, cfg, k).grant_id == live.grant_id


def test_floor_is_max_over_all_stale_grants():
    store, cfg, k = AssistantStore(), _cfg(), _key()
    _fill(store, k, 10)
    for gid, days in (("ag_a", 5), ("ag_b", 40)):
        store.put_action_grant(ActionGrant(
            grant_id=gid, tenant_id=T, user_id=U, worker_id=W, key_id=k.key_id(), verb="git.push",
            state=GrantState.REVOKED, revoked_at=T0 + timedelta(days=days), created_at=T0 + timedelta(days=days)))
    store.add_action_evidence(_ev(300, k, "s9", "t9", minutes=60 * 24 * 10))
    for i in range(9):
        store.add_action_evidence(_ev(310 + i, k, f"s{i % 3}", f"t{i % 2}", minutes=60 * 24 * 10 + i))
    assert _propose(store, cfg, k) is None                # all evidence predates the later revocation (day 40)


def test_grant_cap_returns_none():
    store, cfg, k = AssistantStore(), _cfg(), _key()
    store.max_action_grants_per_tenant = 1
    store.put_action_grant(ActionGrant(grant_id="other", tenant_id=T, user_id=U, worker_id="wx", key_id="zz",
                                       state=GrantState.REVOKED, created_at=T0))
    _fill(store, k, 10)
    assert _propose(store, cfg, k) is None
    assert len(store.list_action_grants(T, U)) == 1

"""P05 (library): team templates, task admission, reviewer separation, lease/fencing.

Each refusal is paired with a positive control, so a fail-closed implementation that refuses everything cannot pass."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from herdr_engineering.control_plane.team_tasks import (
    LeaseError,
    LeaseStore,
    TaskSpec,
    TeamError,
    admit,
    template_from_dict,
)
from herdr_engineering.control_plane.workspace_contracts import Broker, StaticGrantSource, taint

T0 = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
ROOM = "room-1"
TEMPLATE = {
    "template_id": "feature-team",
    "budget": {"max_tasks": 5, "max_runtime_s": 3600},
    "roles": [
        {"name": "plan", "kind": "planner", "actions": ["read", "post"]},
        {"name": "build", "kind": "implementer", "actions": ["read", "post", "execute"], "max_count": 3},
        {"name": "review", "kind": "reviewer", "actions": ["read", "post"]},
        {"name": "coord", "kind": "coordinator", "optional": True, "min_count": 0},
    ],
}


class Clock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        return self.now


def broker(role="owner"):
    return Broker(StaticGrantSource(roles={("alice", ROOM): role} if role else {}), clock=lambda: T0)


def task(task_id="t1", role="build", **kw):
    base = dict(task_id=task_id, template_id="feature-team", role=role, submitted_by="alice", room_id=ROOM,
                allowed_actions=frozenset({"read", "post"}), max_runtime_s=600, deadline=T0 + timedelta(hours=1),
                executor="agent-impl", idempotency_key="k-" + task_id)
    base.update(kw)
    return TaskSpec(**base)


def reasons(fn):
    with pytest.raises(TeamError) as e:
        fn()
    return e.value.reasons


# ------------------------------------------------------------------ templates
def test_a_valid_template_names_roles_and_no_host():
    t = template_from_dict(TEMPLATE)
    assert [r.kind for r in t.roles] == ["planner", "implementer", "reviewer", "coordinator"]
    assert t.role("coord").optional and t.role("build").max_count == 3


@pytest.mark.parametrize("mutate,expected", [
    (lambda d: d["roles"][1].update(host="bender"), "host_pinning_not_allowed:host"),
    (lambda d: d["roles"][1].update(pinned_host="x"), "host_pinning_not_allowed:pinned_host"),
    (lambda d: d["roles"][1].update(actions=["read", "invite"]), "role_exceeds_agent_authority:build"),
    (lambda d: d["roles"][2].update(actions=["read", "execute"]), "reviewer_must_be_read_only"),
    (lambda d: d["roles"].pop(2), "needs_exactly_one_independent_reviewer"),
    (lambda d: d["roles"].pop(1), "needs_an_implementer"),
    (lambda d: d["roles"][0].update(kind="boss"), "unknown_role_kind:boss"),
    (lambda d: d["budget"].update(max_tasks=0), "budget_must_be_positive"),
])
def test_a_bad_template_is_refused_for_the_stated_reason(mutate, expected):
    import copy
    doc = copy.deepcopy(TEMPLATE)
    mutate(doc)
    with pytest.raises(TeamError) as e:
        template_from_dict(doc)
    assert expected in e.value.reasons


# ------------------------------------------------------------------ admission
def test_an_authorized_actor_may_submit_and_the_same_task_without_authority_may_not():
    tpl = template_from_dict(TEMPLATE)
    admit(task(), tpl, broker("owner"), {}, T0)                                   # positive control
    assert "broker_deny" in reasons(lambda: admit(task(), tpl, broker(None), {}, T0))
    assert "broker_deny" in reasons(lambda: admit(task(), tpl, broker("viewer"), {}, T0))


def test_a_message_or_tainted_text_never_becomes_a_task():
    tpl = template_from_dict(TEMPLATE)
    assert "task_must_come_from_an_authorized_actor_not_a_message" in reasons(
        lambda: admit(task(origin="message"), tpl, broker(), {}, T0))
    assert "task_derived_from_untrusted_content" in reasons(
        lambda: admit(task(taint=frozenset({"message:m1"})), tpl, broker(), {}, T0))
    assert taint("rm -rf /", "message:m1")                                         # content stays data
    admit(task(), tpl, broker(), {}, T0)


def test_bounds_deadline_budget_and_actions_are_enforced():
    tpl = template_from_dict(TEMPLATE)
    assert "allowed_actions_exceed_the_role" in reasons(
        lambda: admit(task(allowed_actions=frozenset({"read", "execute", "invite"})), tpl, broker(), {}, T0))
    assert "runtime_outside_the_template_budget" in reasons(
        lambda: admit(task(max_runtime_s=7200), tpl, broker(), {}, T0))
    assert "deadline_in_the_past" in reasons(lambda: admit(task(deadline=T0 - timedelta(minutes=1)), tpl, broker(), {}, T0))
    full = {f"x{i}": task(f"x{i}") for i in range(5)}
    assert "task_budget_exhausted" in reasons(lambda: admit(task("t9"), tpl, broker(), full, T0))
    admit(task("x1"), tpl, broker(), full, T0)                                    # an existing task is not a new one


def test_dependencies_must_exist_and_cannot_cycle():
    tpl = template_from_dict(TEMPLATE)
    a = task("a")
    assert "unknown_dependency:ghost" in reasons(lambda: admit(task("b", depends_on=("ghost",)), tpl, broker(), {"a": a}, T0))
    admit(task("b", depends_on=("a",)), tpl, broker(), {"a": a}, T0)
    chain = {"a": task("a", depends_on=("c",)), "c": task("c", depends_on=("b",))}
    assert "dependency_cycle" in reasons(lambda: admit(task("b", depends_on=("a",)), tpl, broker(), chain, T0))


def test_the_reviewer_must_be_independent_of_the_implementer_and_the_submitter():
    tpl = template_from_dict(TEMPLATE)
    work = task("work", executor="agent-impl")
    known = {"work": work}
    ok = task("rev", role="review", reviews="work", executor="agent-reviewer")
    admit(ok, tpl, broker(), known, T0)                                           # positive control
    for bad_exec in ("agent-impl", "alice", ""):
        assert "reviewer_is_not_independent_of_the_implementer" in reasons(
            lambda e=bad_exec: admit(task("rev", role="review", reviews="work", executor=e), tpl, broker(), known, T0))
    assert "a_review_must_name_the_task_it_reviews" in reasons(
        lambda: admit(task("rev", role="review", executor="agent-reviewer"), tpl, broker(), known, T0))


# ------------------------------------------------------------------ lease / fencing
def store():
    clk = Clock()
    return LeaseStore(clock=clk), clk


def test_a_task_runs_once_across_a_retry_of_the_same_completion():
    s, _ = store()
    lease = s.acquire(task(), "w1")
    assert s.complete("t1", lease.token, {"pr": 7}) == {"pr": 7}
    assert s.complete("t1", lease.token, {"pr": 8}) == {"pr": 7}                   # a retry gets the recorded result
    with pytest.raises(LeaseError, match="already_completed"):
        s.acquire(task(), "w2")


def test_after_a_crash_the_successor_wins_and_the_stale_worker_is_fenced_off():
    s, clk = store()
    old = s.acquire(task(), "w1", ttl_s=30)
    with pytest.raises(LeaseError, match="held_by_another_worker"):
        s.acquire(task(), "w2")                                                    # still live: no double execution
    clk.now = T0 + timedelta(seconds=31)                                           # w1 crashed; its lease lapsed
    new = s.acquire(task(), "w2", ttl_s=30)
    assert new.token == old.token + 1
    assert not s.authorizes("t1", old.token, "post") and s.authorizes("t1", new.token, "post")
    with pytest.raises(LeaseError, match="stale_fencing_token"):
        s.complete("t1", old.token, "late result from the crashed worker")
    assert s.complete("t1", new.token, "ok") == "ok"
    with pytest.raises(LeaseError, match="already_completed_by_another_token"):
        s.complete("t1", old.token, "again")


def test_an_aborted_execution_keeps_no_privileges():
    s, _ = store()
    lease = s.acquire(task(allowed_actions=frozenset({"read", "post"})), "w1")
    assert s.authorizes("t1", lease.token, "post")                                 # positive control
    assert not s.authorizes("t1", lease.token, "execute")                          # never beyond the task's actions
    s.abort("t1", lease.token)
    assert not s.authorizes("t1", lease.token, "post") and not s.authorizes("t1", lease.token, "read")
    with pytest.raises(LeaseError, match="lease_expired_or_revoked"):
        s.complete("t1", lease.token, "result after abort")
    again = s.acquire(task(), "w2")                                                # the task can be retried safely
    assert again.token == lease.token + 1


def test_a_completed_lease_authorizes_nothing_and_an_expired_one_cannot_heartbeat():
    s, clk = store()
    lease = s.acquire(task(), "w1", ttl_s=10)
    s.heartbeat("t1", lease.token, ttl_s=10)
    s.complete("t1", lease.token, "done")
    assert not s.authorizes("t1", lease.token, "post")
    s2, clk2 = store()
    l2 = s2.acquire(task("t2"), "w1", ttl_s=10)
    clk2.now = T0 + timedelta(seconds=11)
    with pytest.raises(LeaseError, match="lease_expired_or_revoked"):
        s2.heartbeat("t2", l2.token)

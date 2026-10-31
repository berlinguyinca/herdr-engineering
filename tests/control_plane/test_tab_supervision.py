"""Policy failures here must prevent any durable mutation or runtime side effect."""
from copy import deepcopy

import pytest

from herdr_engineering.control_plane.tab_supervision import Actor, Binding, Policy, Supervision


class Sink:
    def __init__(self):
        self.events = []
        self.fail = False

    def record_required(self, event):
        if self.fail:
            raise OSError("unavailable")
        self.events.append(deepcopy(event))


def human(owner="u"):
    return Actor(owner, None, frozenset({"repo", "tests"}), frozenset({"pi/a", "codex/b"}))


def agent(aid="root", owner="u", scopes=("repo", "tests")):
    return Actor(owner, aid, frozenset(scopes), frozenset({"pi/a", "codex/b"}))


def policy(tokens=100, scopes=("repo", "tests"), deadline=1000):
    return Policy(frozenset(scopes), frozenset({"pi/a"}), tokens, deadline)


@pytest.fixture
def tree():
    sink = Sink()
    s = Supervision(sink, now=lambda: 10)
    s.create_job(human(), "job", "server/tab", "root", "task-root", policy())
    return s, sink


def child(s, aid="child", actor=None, **kwargs):
    s.delegate(actor or agent(), aid, "task-" + aid, policy(**kwargs))


def binding(instance="i", pane="wD:p7", session="s"):
    return Binding("server", session, instance, "pi", "server/tab", pane)


def test_one_job_per_tab_and_one_root(tree):
    s, sink = tree
    with pytest.raises(ValueError):
        s.create_job(human(), "job2", "server/tab", "other", "task", policy())
    with pytest.raises(ValueError):
        s.create_job(agent(), "j", "tab2", "other", "task", policy())
    assert len(sink.events) == 1


def test_recursive_parent_is_caller_and_never_changes_with_binding(tree):
    s, sink = tree
    child(s)
    child(s, "grandchild", agent("child"))
    s.bind(agent(), "child", binding())
    s.bind(agent(), "child", binding("new", "wD:p8", "new-session"))
    restored = Supervision(sink, now=lambda: 10)
    restored.hydrate(sink.events)
    assert restored.view("child")["supervisor_id"] == "root"
    assert restored.view("grandchild")["supervisor_id"] == "child"
    assert restored.view("child")["binding"]["session_id"] == "new-session"
    assert restored.view("child")["job_id"] == "job"


@pytest.mark.parametrize("who,p", [
    (agent(owner="other"), policy()),
    (agent(scopes=("repo",)), policy()),
    (agent(), policy(tokens=101)),
    (agent(), policy(deadline=1001)),
    (agent(), policy(scopes=("outside",))),
])
def test_delegation_cannot_expand_current_or_persistent_authority(tree, who, p):
    s, sink = tree
    with pytest.raises(ValueError):
        s.delegate(who, "bad", "task", p)
    assert len(sink.events) == 1


def test_siblings_cannot_multiply_parent_budget_and_receipts_are_idempotent(tree):
    s, sink = tree
    child(s, "a")
    child(s, "b")
    s.charge(agent("a"), 70, "usage-a")
    s.charge(agent("a"), 70, "usage-a")
    s.charge(agent("b"), 30, "usage-b")
    with pytest.raises(ValueError):
        s.charge(agent("b"), 1, "over")
    with pytest.raises(ValueError):
        s.charge(agent("b"), 70, "usage-a")
    assert s.view("root")["used_tokens"] == 100
    restored = Supervision(sink, now=lambda: 10)
    restored.hydrate(sink.events)
    restored.charge(agent("a"), 70, "usage-a")
    assert restored.view("root")["used_tokens"] == 100


def test_sink_failure_does_not_mutate_and_returned_views_are_detached(tree):
    s, sink = tree
    sink.fail = True
    with pytest.raises(OSError):
        child(s)
    assert s.agents() == [s.view("root")]
    row = s.view("root")
    row["policy"]["scopes"].append("outside")
    assert "outside" not in s.view("root")["policy"]["scopes"]


def test_internal_subagent_has_real_instance_without_invented_pane(tree):
    s, _ = tree
    child(s)
    s.bind(agent(), "child", binding(pane=None))
    assert s.view("child")["binding"]["pane_id"] is None
    assert s.health("child", [], now=20) == "orphaned"


def test_pane_binding_collision_and_cross_job_authority_are_refused(tree):
    s, _ = tree
    child(s)
    s.bind(human(), "root", binding())
    with pytest.raises(ValueError):
        s.bind(agent(), "child", binding("other"))
    s.create_job(human(), "other-job", "other-tab", "other", "task", policy())
    with pytest.raises(ValueError):
        s.bind(agent("other"), "child", binding("x", "p2"))


def test_heartbeat_does_not_hide_drift_or_stall_and_loss_is_not_completion(tree):
    s, _ = tree
    s.bind(human(), "root", binding())
    s._now = lambda: 20
    s.checkpoint(agent(), 1, "evidence")
    s._now = lambda: 100
    s.checkpoint(agent(), 1, "heartbeat")
    assert s.health("root", [binding()], now=105, stale_after=50) == "drifting"
    assert s.health("root", [binding()], now=160, stale_after=50) == "stalled"
    assert s.health("root", [], now=105) == "orphaned"
    assert s.view("root")["status"] == "running"
    with pytest.raises(ValueError):
        s.checkpoint(agent(), 0, "old")


def test_cancel_is_recursive_durable_and_requires_cleanup_evidence(tree):
    s, sink = tree
    child(s)
    child(s, "grandchild", agent("child"))
    s.cancel(agent(), "child", "human-request")
    assert s.view("root")["status"] == "queued"
    assert s.view("grandchild")["status"] == "stop_requested"
    with pytest.raises(ValueError):
        child(s, "late", agent("child"))
    restored = Supervision(sink, now=lambda: 10)
    restored.hydrate(sink.events)
    with pytest.raises(ValueError):
        restored.conclude(human(), "child", "cancelled", "receipt")
    restored.conclude(human(), "grandchild", "cancelled", "cleanup-grandchild")
    restored.conclude(agent(), "child", "cancelled", "cleanup-child")
    assert restored.view("child")["status"] == "cancelled"


def test_completion_requires_supervisor_evidence_and_integrated_children(tree):
    s, _ = tree
    child(s)
    with pytest.raises(ValueError):
        s.conclude(agent("child"), "child", "verified", "self-says-done")
    with pytest.raises(ValueError):
        s.conclude(human(), "root", "verified", "result")
    with pytest.raises(ValueError):
        s.conclude(agent(), "child", "verified", "")
    s.bind(human(), "root", binding("root", "p-root"))
    s.bind(agent(), "child", binding("child", "p-child"))
    s.conclude(agent(), "child", "verified", "tests-and-integration")
    s.conclude(human(), "root", "verified", "reviewed-result")
    with pytest.raises(ValueError):
        child(s)


@pytest.mark.parametrize("mutation", ["gap", "version", "parent"])
def test_replay_rejects_corrupt_history_atomically(tree, mutation):
    s, sink = tree
    child(s)
    rows = deepcopy(sink.events)
    if mutation == "gap":
        rows[1]["sequence"] += 1
    elif mutation == "version":
        rows[1]["schema_version"] = 999
    else:
        rows[1]["payload"]["agent"]["supervisor_id"] = "child"
    restored = Supervision(sink, now=lambda: 10)
    with pytest.raises(ValueError):
        restored.hydrate(rows)
    assert restored.agents() == []


def test_narrowed_child_can_work_with_its_own_current_grants(tree):
    s, sink = tree
    child(s, scopes=("repo",))
    s.checkpoint(agent("child", scopes=("repo",)), 1, "checked")
    s.delegate(agent("child", scopes=("repo",)), "grandchild", "task-g", policy(scopes=("repo",)))
    restored = Supervision(sink, now=lambda: 10)
    restored.hydrate(sink.events)
    assert restored.view("grandchild")["supervisor_id"] == "child"


def test_audit_records_authenticated_actor_and_replay_rejects_self_verification(tree):
    s, sink = tree
    child(s)
    s.bind(agent(), "child", binding())
    s.conclude(agent(), "child", "verified", "review")
    assert sink.events[-1]["payload"]["actor"]["agent_id"] == "root"
    rows = deepcopy(sink.events)
    rows[-1]["payload"]["actor"]["agent_id"] = "child"
    restored = Supervision(sink, now=lambda: 10)
    with pytest.raises(ValueError):
        restored.hydrate(rows)
    assert restored.agents() == []


def test_binding_into_another_tab_requires_explicit_job_migration(tree):
    s, sink = tree
    foreign = Binding("server", "s", "i", "pi", "other-tab", "wD:p7")
    with pytest.raises(ValueError):
        s.bind(human(), "root", foreign)
    assert len(sink.events) == 1


def test_health_reports_exhausted_budget_and_queued_deadline(tree):
    s, _ = tree
    s.charge(agent(), 100, "usage")
    assert s.health("root", [], now=20) == "budget_exhausted"
    assert s.health("root", [], now=1001) == "deadline_exceeded"


def test_replay_rejects_duplicate_event_identity(tree):
    s, sink = tree
    child(s)
    rows = deepcopy(sink.events)
    rows[1]["event_id"] = rows[0]["event_id"]
    restored = Supervision(sink, now=lambda: 10)
    with pytest.raises(ValueError):
        restored.hydrate(rows)
    assert restored.agents() == []


def test_concurrent_siblings_share_atomic_ancestor_ceiling(tree):
    from concurrent.futures import ThreadPoolExecutor

    s, _ = tree
    child(s, "a")
    child(s, "b")

    def spend(aid):
        try:
            s.charge(agent(aid), 60, "usage-" + aid)
            return True
        except ValueError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        accepted = list(pool.map(spend, ("a", "b")))
    assert sorted(accepted) == [False, True]
    assert s.view("root")["used_tokens"] == 60


def test_real_clock_history_replays_exactly():
    sink = Sink()
    s = Supervision(sink)
    s.create_job(human(), "job", "server/tab", "root", "task", policy(deadline=9e9))
    restored = Supervision(sink)
    restored.hydrate(sink.events)
    assert restored.agents() == s.agents()


def test_terminal_binding_is_not_released_without_attested_cleanup(tree):
    s, _ = tree
    s.bind(human(), "root", binding())
    s.conclude(human(), "root", "verified", "review")
    s.create_job(human(), "j2", "tab2", "r2", "t2", policy())
    with pytest.raises(ValueError):
        s.bind(human(), "r2", Binding("server", "s", "i", "pi", "tab2", "wD:p7"))


def test_binding_resets_activity_clock_after_queue_delay(tree):
    s, sink = tree
    s._now = lambda: 100
    s.bind(human(), "root", binding())
    assert s.health("root", [binding()], now=105, stale_after=50) == "healthy"
    restored = Supervision(sink, now=lambda: 105)
    restored.hydrate(sink.events)
    assert restored.health("root", [binding()], stale_after=50) == "healthy"


def test_unbound_queued_agent_cannot_be_verified(tree):
    s, _ = tree
    with pytest.raises(ValueError):
        s.conclude(human(), "root", "verified", "self-reported")


def test_repeated_binding_receipt_does_not_hide_stall(tree):
    s, sink = tree
    s.bind(human(), "root", binding())
    before = len(sink.events)
    s._now = lambda: 100
    s.bind(human(), "root", binding())
    assert len(sink.events) == before
    assert s.health("root", [binding()], now=105, stale_after=50) == "stalled"


def test_rebinding_does_not_release_historical_workload_identity(tree):
    s, sink = tree
    s.bind(human(), "root", binding())
    s.bind(human(), "root", binding("new", "p-new", "s-new"))
    s.create_job(human(), "j2", "tab2", "r2", "t2", policy())
    restored = Supervision(sink, now=lambda: 10)
    restored.hydrate(sink.events)
    with pytest.raises(ValueError):
        restored.bind(human(), "r2", Binding("server", "s", "i", "pi", "tab2", "wD:p7"))

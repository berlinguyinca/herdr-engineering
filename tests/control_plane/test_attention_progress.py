# ruff: noqa: F811, E701  (pytest fixtures are imported by name)
"""T15: attention inbox, evidence-based progress and optimization analytics
(docs/contracts/lineage-events.md, "Attention inbox, progress and analytics (T15)").

Cast as in test_workbench: alice owner, bob contributor, carol viewer, dave guest, mallory in no room. Lineage is written
by the ingest key WRITER (an agent's own emitter); evidence the operator trusts is written by OPERATOR (`key:op`, listed in
``insights.evidence_writers``). The graph shares the service's CLOCK, so every duration here is exact. Credentials are
built at run time; nothing here is a secret."""
from __future__ import annotations

import hashlib
import json

import pytest

from herdr_engineering.control_plane import collab_insights as ins
from herdr_engineering.control_plane.collab import CollabError
from herdr_engineering.control_plane.collab_attention import (
    ATTN_EVENTS,
    normalize_insights,
)
from herdr_engineering.control_plane.collab_workbench import normalize_steering
from herdr_engineering.control_plane.ingest import IngestService
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from herdr_engineering.control_plane.review_quorum import QuorumConfig, receipt_binding
from tests.control_plane.test_collab_bounded import PATCH, make
from tests.control_plane.test_collab_external import enroll  # noqa: F401
from tests.control_plane.test_collab_links import link, w  # noqa: F401
from tests.control_plane.test_collab_tasks import USERS, code, svc, user  # noqa: F401
from tests.control_plane.test_graph import OPERATOR, WRITER, Feed
from tests.control_plane.test_lineage_views import edge, node

H1, H2 = "a" * 40, "b" * 40
PR1, PR2 = "pr:github.com/o/r#1", "pr:github.com/o/r#2"
# reviewers publish with their OWN keys; the room-scoped PR is addressable only by operator-role keys or its writers
R1 = {"kind": "operator", "id": "key:r1", "authenticated_by": "api_key"}
R2 = {"kind": "operator", "id": "key:r2", "authenticated_by": "api_key"}
_n = [0]


def k():
    _n[0] += 1
    return f"k{_n[0]}"


def err(fn, *a, **kw):
    with pytest.raises(CollabError) as exc:
        fn(*a, **kw)
    return exc.value.status, getattr(exc.value, "extra", {}).get("code"), exc.value.message


@pytest.fixture
def fx(w):
    """The HCN cast over a graph that shares the service's clock, wired as the combined server wires it."""
    repo = ControlPlaneRepo(now=w.s.clock)
    scope = {"rooms": ("*",), "missions": ("*",)}
    ing = IngestService(repo, key_scopes={n: dict(scope) for n in ("key:writer", "key:op", "key:r1", "key:r2", "key:other")})
    ing.reserved_node = w.s.is_external_node
    ing.service_node = w.s.is_hcn_node

    def sink(envelope, principal, valid=None):
        ing.key_scopes[principal["id"]] = {"rooms": frozenset({envelope["room_id"]}), "missions": frozenset()}
        return ing.ingest(envelope, principal, valid=valid)
    w.s.lineage_sink = sink
    w.s.graph_source = lambda: repo.graph
    w.s.replay_history = "full"
    w.s.delegation_enabled = True
    w.s.insights = normalize_insights({"enabled": True, "evidence_writers": ["key:op"]})
    w.repo, w.ing, w.feed = repo, ing, Feed(ing)
    return w


def cfg(f, **kw):
    f.s.insights = normalize_insights({"enabled": True, "evidence_writers": ["key:op"], **kw})


def tick(f, seconds=1.0):
    f.s.clock.t += seconds


def prog(f, who=None):
    return f.s.room_progress(who or f.alice, f.room, {})


def inbox(f, who=None, **q):
    return f.s.attention_inbox(who or f.alice, f.room, q)


def stats(f, who=None):
    return f.s.room_analytics(who or f.alice, f.room, {})


def keys(out):
    return {i["key"] for i in out["items"]}


def task_row(out, tid):
    return next(t for t in out["tasks"] if t["task_id"] == tid)


def rev(f, who, name, commit, verdict="lgtm", pr=PR1):
    """A reviewer's published verdict, written with the reviewer's OWN key."""
    d = hashlib.sha256(f"{name}{commit}".encode()).hexdigest()
    node(f.feed, f"review:{name}", room_id=f.room, principal=who)
    edge(f.feed, "reviewed", f"review:{name}", pr,
         {"commit": commit, "verdict": verdict, "receipt_digest": d, "receipt_binding": receipt_binding(pr, commit, who["id"], d)},
         room_id=f.room, principal=who)


def merged_task(f, n, state="done", trusted=True, link_by=OPERATOR):
    """A task whose PR is marked merged by `OPERATOR` and linked by `link_by`."""
    node(f.feed, f"task:t{n}", state=state, room_id=f.room)
    pr = f"pr:github.com/o/r#{n}"
    node(f.feed, pr, state="merged", room_id=f.room, principal=OPERATOR if trusted else WRITER)
    edge(f.feed, "produced", f"task:t{n}", pr, room_id=f.room, principal=link_by)
    return pr


# =============================================================================================== config and switches
def test_defaults_are_off_and_the_kill_switch_wins(fx):
    d = normalize_insights(None)
    assert d["enabled"] is False and d["kill_switch"] is False and d["evidence_writers"] == []
    fx.s.insights = normalize_insights(None)
    for fn in (prog, inbox, stats):
        assert err(fn, fx)[:2] == (403, "insights_disabled")
    cfg(fx)
    assert prog(fx)["room"]["tasks"] == 0
    cfg(fx, kill_switch=True)
    assert err(prog, fx)[:2] == (403, "insights_disabled")
    cfg(fx, progress=False)                                      # one feature off, the others stay on
    assert err(prog, fx)[:2] == (403, "insights_disabled") and inbox(fx)["items"] == []


@pytest.mark.parametrize("bad", [{"enabled": "yes"}, {"nope": 1}, {"evidence_writers": "key:x"}, {"evidence_writers": [1]},
                                 {"evidence_writers": ["k"] * 40}, {"verified_credit": 2}, {"verified_credit": True},
                                 {"min_group_size": 1}, {"max_items": 5}, {"max_pages": 0}, {"reads_per_minute": 999},
                                 {"snooze_min_seconds": 99999, "snooze_max_seconds": 3600}, "x"])
def test_config_validation(bad):
    with pytest.raises(ValueError):
        normalize_insights(bad)


def test_hot_config_failure_closes_everything(fx):
    fx.s.insights_source = lambda: (_ for _ in ()).throw(OSError("unreadable"))
    assert err(inbox, fx)[:2] == (403, "insights_disabled")
    fx.s.insights_source = lambda: {"enabled": True, "evidence_writers": "oops"}
    assert err(prog, fx)[:2] == (403, "insights_disabled")
    fx.s.insights_source = lambda: {"enabled": True}
    assert inbox(fx)["items"] == []


def test_fleet_config_block_is_validated_with_its_own_validator(tmp_path):
    from herdr_engineering.cli import _apply_collab_config

    class A:
        collab_insights = None
    ok = A()
    _apply_collab_config(ok, {"collab": {"insights": {"enabled": True}}})
    assert ok.collab_insights == {"enabled": True}
    with pytest.raises(SystemExit, match="insights"):
        _apply_collab_config(A(), {"collab": {"insights": {"bogus": 1}}})


# =============================================================================================== access
def test_only_non_guest_members_read_and_every_miss_is_the_same_404(fx):
    cfg(fx)
    for fn in (prog, inbox, stats):
        mallory = err(fn, fx, fx.mallory)
        dave = err(fn, fx, fx.dave)
        nowhere = err(lambda f, who: f.s.room_progress(who, "room_nope", {}), fx, fx.alice)
        assert mallory == dave == nowhere == (404, None, "not found")
        for ok in (fx.alice, fx.bob, fx.carol):
            fn(fx, ok)
    agent, _ = fx.agent("alice")
    for call in (fx.s.room_progress, fx.s.attention_inbox, fx.s.room_analytics):
        assert err(call, agent, fx.room, {})[0] == 403
    # nobody learns whether the feature is on by probing as a non-member
    cfg(fx, enabled=False)
    assert err(prog, fx, fx.mallory)[0] == 404 and err(prog, fx, fx.carol)[:2] == (403, "insights_disabled")


def test_collab_only_service_without_a_graph_serves_nothing(fx):
    fx.s.graph_source = None
    assert err(inbox, fx)[0] == 404


# =============================================================================================== evidence: progress
def test_only_evidence_from_a_trusted_writer_moves_progress(fx):
    f = fx
    merged_task(f, 1, state="running")                           # not self-reported done, but merged per a trusted writer
    node(f.feed, "task:t2", state="done", room_id=f.room)        # the agent says done: no evidence
    out = prog(f)
    assert out["room"]["complete"] == 1 and out["room"]["reported_done_without_evidence"] == 1
    assert out["room"]["completion"] == 0.5
    t1, t2 = task_row(out, "task:t1"), task_row(out, "task:t2")
    assert t1["stage"] == "complete" and t1["reported_state"] == "running"       # self-report and evidence differ; evidence wins
    assert t2["stage"] == "none" and t2["reported_done"] is True and t2["evidence"] == []
    ev = t1["evidence"][0]
    assert ev["kind"] == "merged_pr" and ev["counted"] and ev["url"] == "https://github.com/o/r/pull/1"


def test_forged_evidence_is_shown_but_never_counted(fx):
    f = fx
    merged_task(f, 1, trusted=False)                             # (a) the agent marks its own PR merged
    node(f.feed, "task:t2", state="done", room_id=f.room)        # (b) it says "done" and links nothing (the graph refuses it a link to a PR it does not own)
    merged_task(f, 9)
    node(f.feed, "task:t3", state="done", room_id=f.room)        # (c) it validates its own work
    node(f.feed, "ci:mine", state="passed", room_id=f.room)
    edge(f.feed, "validated", "ci:mine", "task:t3", {"commit": H1}, room_id=f.room)
    node(f.feed, "task:t4", state="done", room_id=f.room)        # (d) it forges the reserved name of an accepted proposal
    with pytest.raises(PermissionError):
        node(f.feed, "artifact:hcn-proposal.fake", state="accepted", room_id=f.room)
    out = prog(f)
    assert out["room"]["complete"] == 1 and out["room"]["tasks"] == 5                   # only the genuinely attested t9
    assert task_row(out, "task:t9")["stage"] == "complete"
    t1, t2, t3, t4 = (task_row(out, f"task:t{i}") for i in (1, 2, 3, 4))
    assert [t1["stage"], t2["stage"], t3["stage"], t4["stage"]] == ["none"] * 4
    assert [e["counted"] for e in t1["evidence"]] == [False]
    assert t2["evidence"] == []
    assert [(e["kind"], e["counted"], e["why_not"]) for e in t3["evidence"]] == [("validated_task", False, ["writer_not_trusted"])]
    assert t4["evidence"] == [] and all(t["reported_done"] for t in (t1, t2, t3, t4))     # all four say "done"; none is
    assert out["room"]["reported_done_without_evidence"] == 4


def test_no_evidence_writers_configured_is_said_plainly_not_shown_as_zero(fx):
    cfg(fx, evidence_writers=[])
    merged_task(fx, 1)
    out = prog(fx)
    assert out["room"]["status"] == "no_trusted_evidence_configured" and out["room"]["progress"] is None
    assert out["room"]["estimate"]["status"] == "insufficient_evidence"
    assert out["trusted_evidence_configured"] is False


def test_empty_room_is_insufficient_evidence(fx):
    out = prog(fx)
    assert out["room"]["status"] == "insufficient_evidence" and out["room"]["progress"] is None


def test_validated_at_head_is_verified_not_complete_and_a_stale_or_wrong_head_does_not_count(fx):
    f = fx
    node(f.feed, "task:t1", state="running", room_id=f.room)
    node(f.feed, PR1, attrs={"head_sha": H1}, state="open", room_id=f.room, principal=OPERATOR)
    edge(f.feed, "produced", "task:t1", PR1, room_id=f.room, principal=OPERATOR)
    edge(f.feed, "validated", "ci:a", PR1, {"commit": H2}, room_id=f.room, principal=OPERATOR)       # another head
    t = task_row(prog(f), "task:t1")
    assert t["stage"] == "none"
    assert [e["why_not"] for e in t["evidence"] if e["kind"] == "validated_at_head"] == [["bound_to_another_head"]]
    edge(f.feed, "validated", "ci:b", PR1, {"commit": H1}, room_id=f.room, principal=OPERATOR)
    out = prog(f)
    assert task_row(out, "task:t1")["stage"] == "verified"
    assert out["room"]["complete"] == 0 and out["room"]["verified_not_merged"] == 1
    assert out["room"]["progress"] == 0.5 and out["room"]["completion"] == 0.0            # verified_credit, never "done"
    node(f.feed, PR1, attrs={"head_sha": H2}, room_id=f.room, principal=OPERATOR)         # the head moves: both are now old
    assert task_row(prog(f), "task:t1")["stage"] == "none"
    node(f.feed, PR1, state="merged", room_id=f.room, principal=OPERATOR)
    assert task_row(prog(f), "task:t1")["stage"] == "complete"


def test_a_validated_edge_completes_a_task_only_when_no_attested_pr_decides(fx):
    f = fx
    node(f.feed, "task:t1", state="running", room_id=f.room)
    edge(f.feed, "validated", "ci:a", "task:t1", {"commit": H1}, room_id=f.room, principal=OPERATOR)
    assert task_row(prog(f), "task:t1")["stage"] == "complete"
    node(f.feed, "task:t2", state="running", room_id=f.room)
    edge(f.feed, "validated", "ci:b", "task:t2", {"commit": H1}, room_id=f.room)                # the agent's own: ignored
    assert task_row(prog(f), "task:t2")["stage"] == "none"
    node(f.feed, PR2, state="open", room_id=f.room, principal=OPERATOR)
    edge(f.feed, "produced", "task:t1", PR2, room_id=f.room, principal=OPERATOR)               # now a PR decides, and it is open
    t1 = task_row(prog(f), "task:t1")
    assert t1["stage"] == "none" and "an_attested_pull_request_decides" in t1["evidence"][-1]["why_not"] + t1["evidence"][0]["why_not"]


def test_two_hop_link_through_an_issue_needs_both_edges_attested(fx):
    f = fx
    node(f.feed, "task:t1", state="done", room_id=f.room)
    node(f.feed, "issue:github.com/o/r#9", label="Fix the parser", room_id=f.room, principal=WRITER)     # a commons node
    node(f.feed, "issue:github.com/o/r#9", label="Fix the parser", room_id=f.room, principal=OPERATOR)
    node(f.feed, PR1, state="merged", room_id=f.room, principal=OPERATOR)
    edge(f.feed, "issue_link", "pr:github.com/o/r#1", "issue:github.com/o/r#9", room_id=f.room, principal=OPERATOR)
    edge(f.feed, "issue_link", "task:t1", "issue:github.com/o/r#9", room_id=f.room, principal=WRITER)
    assert task_row(prog(f), "task:t1")["stage"] == "none"                                      # the agent's half is not attested
    edge(f.feed, "issue_link", "task:t1", "issue:github.com/o/r#9", room_id=f.room, principal=OPERATOR)
    assert task_row(prog(f), "task:t1")["stage"] == "complete"


def test_cancelling_a_task_yourself_does_not_shrink_the_denominator_but_a_trusted_writer_can(fx):
    f = fx
    merged_task(f, 1)
    node(f.feed, "task:t2", state="cancelled", room_id=f.room)                  # self-reported: still counted, not complete
    node(f.feed, "task:t3", state="running", room_id=f.room)
    out = prog(f)
    assert out["room"]["tasks"] == 3 and out["room"]["excluded_by_trusted_writer"] == 0
    node(f.feed, "task:t3", state="superseded", room_id=f.room, principal=WRITER)           # still the agent's own word
    assert prog(f)["room"]["tasks"] == 3


def test_accepted_proposal_completes_the_delegation_task_and_a_pending_one_does_not(fx):
    f = fx
    d, pr, tid = make(f)
    v = d.accept(tid)
    pid = d.propose(tid, kind="patch", title="Fix y", content=PATCH, expected_version=v)[2]["proposal_id"]
    assert prog(f)["room"]["tasks"] == 0                                         # quarantined: nothing in the graph, no progress
    dig = f.s.get_proposal(f.alice, f.room, pid)["digest"]
    f.s.decide_proposal(f.alice, f.room, pid, {"decision": "accept", "expected_digest": dig})
    out = prog(f)
    row = task_row(out, "task:hcn-delegation." + tid)
    assert row["stage"] == "complete" and row["evidence"][0]["kind"] == "accepted_proposal" and row["evidence"][0]["counted"]
    assert out["room"]["completion"] == 1.0


def test_review_quorum_counts_as_verified_only_when_configured_and_satisfied(fx):
    f = fx
    f.s.quorum_config = QuorumConfig(2, {"key:r1": {"login": None}, "key:r2": {"login": None}}, frozenset({"key:op"}), ())
    node(f.feed, "task:t1", state="running", room_id=f.room)
    node(f.feed, PR1, attrs={"head_sha": H1, "author": "somebody"}, room_id=f.room, principal=OPERATOR)
    edge(f.feed, "produced", "task:t1", PR1, room_id=f.room, principal=OPERATOR)

    rev(f, R1, "a", H1)
    assert task_row(prog(f), "task:t1")["stage"] == "none"
    rev(f, R2, "b", H1)
    t = task_row(prog(f), "task:t1")
    assert t["stage"] == "verified" and any(e["kind"] == "reviewed_at_head" and e["counted"] for e in t["evidence"])
    f.s.quorum_config = None                                                      # not configured: review is not evidence
    assert task_row(prog(f), "task:t1")["stage"] == "none"
    assert "not configured" in prog(f)["review_quorum"]


def test_milestones_group_the_tasks_of_a_linked_mission(fx):
    f = fx
    f.s.link_mission(f.alice, f.room, {"mission_id": "m-1", "members": ["bob"]})
    node(f.feed, "mission:m-1", label="Parser rewrite", scope="mission", room_id=f.room, mission_id="m-1")
    node(f.feed, "task:m1", state="done", scope="mission", room_id=f.room, mission_id="m-1")
    node(f.feed, "task:m2", state="running", scope="mission", room_id=f.room, mission_id="m-1")
    merged_task(f, 7)
    bob = prog(f, f.bob)
    assert bob["room"]["tasks"] == 3
    ms = {m["milestone"]: m for m in bob["milestones"]}
    assert ms["mission:m-1"]["tasks"] == 2 and ms["mission:m-1"]["label"] == "Parser rewrite"
    assert ms["mission:m-1"]["reported_done_without_evidence"] == 1 and ms["mission:m-1"]["complete"] == 0
    assert ms["room"]["tasks"] == 1 and ms["room"]["complete"] == 1
    for who in (f.alice, f.carol):                       # not on the mission's list: the milestone does not exist for them
        out = prog(f, who)
        assert out["room"]["tasks"] == 1 and out["milestones"] == []
        assert "m-1" not in json.dumps(out) and "Parser rewrite" not in json.dumps(out)


# =============================================================================================== estimates
def pool(n, gap=100.0, t0=1_000_000.0):
    return [t0 + i * gap for i in range(n)]


def est(p, remaining=4, now=None, **kw):
    p = list(p)
    snap = {"reason": kw.pop("reason", None), "timeline_reason": None}
    return ins.estimate(p, remaining, kw.pop("min_samples", 5), now if now is not None else (p[-1] + 10 if p else 0.0),
                        {}, snap, summary=kw.pop("summary", {"status": "ok"}))


def test_estimate_is_labelled_deterministic_and_ordered():
    a, b = est(pool(8)), est(pool(8))
    assert a == b and a["label"] == "estimate" and a["status"] == "estimate" and a["method"] == ins.METHOD
    f = a["finish"]
    assert f["earliest"] <= f["likely"] <= f["latest"]
    assert a["interval_seconds"] == {"p25": 100, "p50": 100, "p75": 100} and a["n_completed"] == 8 and a["n_remaining"] == 4
    assert a["assumptions"] and a["confidence"] == "medium" and "only 8 completions" not in a["confidence_reasons"]
    assert est(pool(6))["confidence"] == "low" and "only 6 completions" in est(pool(6))["confidence_reasons"]


def test_estimate_says_insufficient_evidence_instead_of_guessing():
    out = est(pool(3))
    assert out["status"] == "insufficient_evidence" and out["have"] == 3 and out["needed"] == 5 and "finish" not in out
    assert est([5.0] * 6)["status"] == "insufficient_evidence"                  # no spread: no pace
    assert est(pool(8), remaining=0)["status"] == "not_needed"
    assert est(pool(8), summary={"status": "no_trusted_evidence_configured", "note": "n"})["status"] == "insufficient_evidence"


def test_estimate_never_predicts_the_past_and_flags_a_stalled_pace_and_cut_reads():
    p = pool(10)
    late = est(p, now=p[-1] + 100_000)
    assert late["finish"]["earliest"] >= ins.iso(p[-1] + 100_000)
    assert late["confidence"] == "low" and any("no completion for a long time" in r for r in late["confidence_reasons"])
    assert "read budget" in " ".join(est(pool(10), reason="page_limit")["confidence_reasons"])
    wild = [0, 1, 2, 3, 4, 5, 6, 7, 5000, 5001]
    assert "vary widely" in " ".join(est([float(x) for x in wild], now=5002.0)["confidence_reasons"])


def test_quantile_is_nearest_rank_and_total():
    assert [ins.quantile([1, 2, 3, 4], p) for p in (0.0, 0.25, 0.5, 0.75, 1.0)] == [1, 1, 2, 3, 4]
    assert ins.quantile([7], 0.9) == 7


def test_progress_estimate_uses_server_receipt_times_end_to_end(fx):
    f = fx
    for n in range(1, 7):                     # six tasks finish 10 minutes apart, four more remain
        merged_task(f, n, state="running")
        tick(f, 600)
    for n in range(7, 11):
        node(f.feed, f"task:t{n}", state="running", room_id=f.room)
    out = prog(f)
    e = out["room"]["estimate"]
    assert out["room"]["complete"] == 6 and e["status"] == "estimate" and e["n_completed"] == 6 and e["n_remaining"] == 4
    assert e["interval_seconds"]["p50"] == 600 and e["window"]["from"] < e["window"]["to"]
    assert task_row(out, "task:t1")["evidence_at"] is not None
    # remaining work is estimated from this room's own pace, never from a self-reported "percent done"
    assert "percent" not in json.dumps(out)


# =============================================================================================== attention inbox: graph items
def test_blocked_failed_ci_stuck_and_expired_items_with_priority(fx):
    f = fx
    node(f.feed, "task:b1", state="blocked", room_id=f.room)
    node(f.feed, "ci:c1", state="failed", room_id=f.room)
    node(f.feed, "attempt:a1", state="running", room_id=f.room)
    node(f.feed, "attempt:a2", state="expired", room_id=f.room)
    node(f.feed, "task:ok", state="running", room_id=f.room)
    tick(f, 600)
    out = inbox(f)
    by = {i["key"]: i for i in out["items"]}
    assert set(by) == {"blocked|task:b1", "ci_failed|ci:c1", "expired_or_stuck|attempt:a2"}      # a1 is quiet, not yet stuck
    assert [i["category"] for i in out["items"]][0] == "ci_failed" and by["blocked|task:b1"]["severity"] == "notice"
    tick(f, 4000)                                                                                   # past stuck and escalation
    out = inbox(f)
    by = {i["key"]: i for i in out["items"]}
    assert by["blocked|task:b1"]["severity"] == "warning" and "expired_or_stuck|attempt:a1" in by
    assert by["expired_or_stuck|attempt:a1"]["detail"]["silent_for_seconds"] == 4600
    assert out["items"][0]["key"] == "ci_failed|ci:c1"                                              # error first
    sev = [i["severity"] for i in out["items"]]
    assert sev == sorted(sev, key=lambda s: -ins.SEVERITY_RANK[s])
    assert out["counts"]["by_severity"] == {"error": 1, "warning": 3}
    node(f.feed, "attempt:a1", state="running", room_id=f.room)                                    # fresh activity: no longer stuck
    assert "expired_or_stuck|attempt:a1" not in keys(inbox(f))
    node(f.feed, "task:b1", state="running", room_id=f.room)
    assert "blocked|task:b1" not in keys(inbox(f))
    tick(f, 8 * 86400)
    assert "ci_failed|ci:c1" not in keys(inbox(f))                                                  # an old failure ages out of the inbox


def test_inbox_is_derived_so_it_cannot_grow_with_noise_and_is_capped_and_deduplicated(fx):
    f = fx
    cfg(fx, max_items=10, max_items_per_category=4)
    for i in range(30):
        node(f.feed, f"task:b{i}", state="blocked", room_id=f.room)
        node(f.feed, f"ci:c{i}", state="failed", room_id=f.room)
        node(f.feed, f"task:b{i}", state="blocked", room_id=f.room)                    # the same condition again: one item
    out = inbox(f)
    assert len(out["items"]) == 8 and out["counts"]["by_category"] == {"ci_failed": 4, "blocked": 4}
    assert out["counts"]["not_shown_because_of_caps"] == {"ci_failed": 26, "blocked": 26}
    assert len({i["key"] for i in out["items"]}) == 8
    before = len(f.s._log)
    for _ in range(5):
        inbox(f)
    assert len(f.s._log) == before                                                     # reading stores nothing


def test_review_items_follow_the_t06_quorum_and_a_head_change_asks_for_a_new_review(fx):
    f = fx
    f.s.quorum_config = QuorumConfig(2, {"key:r1": {"login": None}, "key:r2": {"login": None}}, frozenset({"key:op"}), ())
    assert inbox(f)["review_quorum"].startswith("configured")
    node(f.feed, PR1, state="open", attrs={"head_sha": H1, "author": "somebody"}, room_id=f.room, principal=OPERATOR)

    assert keys(inbox(f)) == {"review_requested|" + PR1}
    rev(f, R1, "a", H1)
    rev(f, R2, "b", H1)
    assert keys(inbox(f)) == set()                                                  # satisfied: nothing to do
    node(f.feed, PR1, attrs={"head_sha": H2}, room_id=f.room, principal=OPERATOR)
    out = inbox(f)
    assert keys(out) == {"rereview_required|" + PR1} and out["items"][0]["detail"]["stale_reviewers"] == 2
    rev(f, R1, "c", H2, verdict="blocked")
    out = inbox(f)
    assert keys(out) == {"review_blocked|" + PR1} and out["items"][0]["severity"] == "error"     # blocked outranks "needs re-review"
    f.s.quorum_config = None
    assert keys(inbox(f)) == set() and "not configured" in inbox(f)["review_quorum"]


def test_pr_evaluation_is_budgeted_and_says_so(fx):
    f = fx
    f.s.quorum_config = QuorumConfig(1, {"key:r1": {"login": None}}, frozenset({"key:op"}), ())
    cfg(fx, max_prs=3)
    for i in range(6):
        node(f.feed, f"pr:github.com/o/r#{i + 10}", state="open", attrs={"head_sha": H1, "author": "x"}, room_id=f.room,
             principal=OPERATOR)
    out = inbox(f)
    assert len(out["items"]) == 3 and out["prs_not_evaluated"] == 3


def test_steering_and_promotion_confirmations_show_only_to_a_reader_who_can_confirm_and_see_the_target(fx):
    f = fx
    f.s.steering = normalize_steering({"enabled": True, "actions": ["pause", "resume", "redirect"]})
    node(f.feed, "attempt:a1", state="running", label="the parser attempt", room_id=f.room)
    node(f.feed, "attempt:hidden", state="running", scope="private")
    t = f.s.steering_target(f.bob, f.room, "attempt:a1")
    st = f.s.propose_steering(f.bob, f.room, {"action": "pause", "node_id": "attempt:a1", "expected_fence": t["fence"],
                                              "idempotency_key": k()})[1]["steering"]
    assert keys(inbox(f, f.alice)) == {"steering_confirm|" + st["steering_id"]}
    it = inbox(f, f.alice)["items"][0]
    assert it["for_you"] and it["subject"]["node_id"] == "attempt:a1" and it["detail"]["proposed_by"] == "bob"
    assert keys(inbox(f, f.bob)) == set()                       # the proposer is a contributor, not a confirmer
    assert keys(inbox(f, f.carol)) == set()                     # a viewer cannot confirm
    f.s.confirm_steering(f.alice, f.room, st["steering_id"], {"digest": st["digest"], "expected_fence": t["fence"]})
    assert keys(inbox(f, f.alice)) == set()                     # no longer waiting
    f.s.steering = normalize_steering({"enabled": False})
    assert keys(inbox(f, f.alice)) == set()


def test_a_steering_request_for_a_target_the_reader_cannot_see_is_not_listed(fx):
    f = fx
    f.s.steering = normalize_steering({"enabled": True})
    node(f.feed, "attempt:a1", state="running", room_id=f.room)
    t = f.s.steering_target(f.bob, f.room, "attempt:a1")
    f.s.propose_steering(f.bob, f.room, {"action": "pause", "node_id": "attempt:a1", "expected_fence": t["fence"],
                                         "idempotency_key": k()})
    assert len(inbox(f, f.alice)["items"]) == 1
    # the node becomes private to its emitter: still a stored request, no longer something alice may learn about
    node(f.feed, "attempt:a1", state="running", scope="private")
    assert keys(inbox(f, f.alice)) == set()


def test_proposals_waiting_for_acceptance_show_to_owners_only_without_content(fx):
    f = fx
    d, pr, tid = make(f)
    pid = d.propose(tid, kind="patch", title="Fix y", content=PATCH, expected_version=d.accept(tid))[2]["proposal_id"]
    out = inbox(f, f.alice)
    assert keys(out) == {"proposal_accept|" + pid} and out["items"][0]["for_you"]
    blob = json.dumps(out)
    assert "Fix y" not in blob and "diff --git" not in blob and out["items"][0]["detail"]["kind"] == "patch"
    for who in (f.bob, f.carol):
        assert keys(inbox(f, who)) == set()
    dig = f.s.get_proposal(f.alice, f.room, pid)["digest"]
    f.s.decide_proposal(f.alice, f.room, pid, {"decision": "reject", "expected_digest": dig})
    assert keys(inbox(f, f.alice)) == set()


def test_consultation_requests_show_to_their_target_and_a_hidden_subject_stays_hidden(fx):
    f = fx
    f.s.contextual = __import__("herdr_engineering.control_plane.collab_context", fromlist=["x"]).normalize_contextual({"enabled": True})
    node(f.feed, "task:a", label="build the parser", room_id=f.room)
    code_, out = f.s.ask_consultation(f.alice, f.room, {"target": {"kind": "user", "id": "bob"},
                                                         "subject": {"kind": "node", "node_id": "task:a"},
                                                         "question": "do you know why it crashes?", "idempotency_key": k()})
    assert code_ in (200, 201), out
    got = inbox(f, f.bob)
    assert [i["category"] for i in got["items"]] == ["consult_request"] and got["items"][0]["subject"]["node_id"] == "task:a"
    assert "crashes" not in json.dumps(got) and got["items"][0]["for_you"]
    assert keys(inbox(f, f.alice)) == set() and keys(inbox(f, f.carol)) == set()
    node(f.feed, "task:a", state="running", scope="private")                    # the subject is withdrawn from the room
    assert inbox(f, f.bob)["items"][0]["subject"] == {"hidden": True}


def test_expired_and_silent_external_agents_appear(fx):
    f = fx
    pr, _, _ = enroll(f, label="Dana's Claude")
    f.s.external_heartbeat(pr, {"status": "working", "activity": "tests"})
    assert keys(inbox(f)) == set()
    tick(f, 4000)
    got = inbox(f)
    assert keys(got) == {"expired_or_stuck|ext:" + pr.id}
    assert got["items"][0]["subject"]["label"] == "Dana's Claude" and got["items"][0]["detail"]["why"] == "silent"
    short, _, _ = enroll(f, label="Short-lived", credential_ttl_seconds=600)
    tick(f, 700)
    why = {i["subject"]["label"]: i["detail"]["why"] for i in inbox(f)["items"]}
    assert why["Short-lived"] == "expired"
    f.s.revoke_external(f.alice, f.room, short.id)                                       # revoked is a decision, not an alert
    assert "Short-lived" not in {i["subject"]["label"] for i in inbox(f)["items"]}
    tick(f, 10 * 86400)
    assert not [i for i in inbox(f)["items"] if i["detail"]["why"] == "expired"]       # long-expired agents stop alerting


# =============================================================================================== ack / snooze
def test_ack_hides_until_the_condition_changes_and_other_readers_are_unaffected(fx):
    f = fx
    node(f.feed, "ci:c1", state="failed", room_id=f.room)
    it = inbox(f, f.alice)["items"][0]
    assert f.s.attention_ack(f.alice, f.room, {"key": it["key"], "fp": it["fp"]}) == {"ok": True, "state": "acknowledged"}
    out = inbox(f, f.alice)
    assert out["items"] == [] and out["counts"]["acknowledged"] == 1 and out["quiet_items"] == []
    assert inbox(f, f.alice, include="acked")["quiet_items"][0]["state"] == "acknowledged"
    assert len(inbox(f, f.bob)["items"]) == 1                                           # bob never acknowledged it
    assert f.s.attention_ack(f.alice, f.room, {"key": it["key"], "fp": it["fp"]})["replayed"] is True
    tick(f)
    node(f.feed, "ci:c1", state="running", room_id=f.room)                              # fixed...
    assert inbox(f, f.alice)["items"] == []
    tick(f)
    node(f.feed, "ci:c1", state="failed", room_id=f.room)                               # ...and failing AGAIN: a new condition
    back = inbox(f, f.alice)["items"]
    assert [i["key"] for i in back] == [it["key"]] and back[0]["resurfaced"] is True and back[0]["fp"] != it["fp"]


def test_an_ack_does_not_survive_a_head_change(fx):
    f = fx
    f.s.quorum_config = QuorumConfig(1, {"key:r1": {"login": None}}, frozenset({"key:op"}), ())
    node(f.feed, PR1, state="open", attrs={"head_sha": H1, "author": "x"}, room_id=f.room, principal=OPERATOR)
    it = inbox(f)["items"][0]
    f.s.attention_ack(f.alice, f.room, {"key": it["key"], "fp": it["fp"]})
    assert inbox(f)["items"] == []
    node(f.feed, PR1, attrs={"head_sha": H2}, room_id=f.room, principal=OPERATOR)
    out = inbox(f)
    assert keys(out) == {it["key"]} and out["items"][0]["resurfaced"] is True and out["items"][0]["fp"] != it["fp"]


def test_snooze_expires_and_restore_takes_a_mark_back(fx):
    f = fx
    node(f.feed, "ci:c1", state="failed", room_id=f.room)
    it = inbox(f)["items"][0]
    out = f.s.attention_snooze(f.alice, f.room, {"key": it["key"], "fp": it["fp"], "seconds": 3600})
    assert out["state"] == "snoozed" and out["snoozed_until"]
    assert inbox(f)["items"] == [] and inbox(f)["counts"]["snoozed"] == 1
    tick(f, 3601)
    assert keys(inbox(f)) == {it["key"]}                                                # snooze lapsed: back, still open
    f.s.attention_ack(f.alice, f.room, {"key": it["key"], "fp": it["fp"]})
    assert inbox(f)["items"] == []
    assert f.s.attention_restore(f.alice, f.room, {"key": it["key"]})["state"] == "open"
    assert keys(inbox(f)) == {it["key"]} and f.s.attention_restore(f.alice, f.room, {"key": it["key"]})["replayed"] is True


def test_old_acknowledgements_lapse(fx):
    f = fx
    cfg(fx, ack_retention_days=1)
    node(f.feed, "ci:c1", state="failed", room_id=f.room, )
    it = inbox(f)["items"][0]
    f.s.attention_ack(f.alice, f.room, {"key": it["key"], "fp": it["fp"]})
    tick(f, 2 * 86400)
    node(f.feed, "ci:c2", state="failed", room_id=f.room)       # fresh activity so the CI window still holds ci:c1 (7 days)
    assert it["key"] in keys(inbox(f))


@pytest.mark.parametrize("body", [
    {}, {"key": "x"}, {"key": "nope|task:t1", "fp": "a" * 24}, {"key": "blocked|task t1", "fp": "a" * 24},
    {"key": "blocked|" + "x" * 500, "fp": "a" * 24}, {"key": "blocked|task:t1", "fp": "zz"}, {"key": 5, "fp": "a" * 24},
    {"key": "blocked|task:t1", "fp": "a" * 24, "extra": 1}, {"key": "blocked|task:t1", "fp": "a" * 24, "seconds": 5}])
def test_ack_input_is_validated(fx, body):
    assert code(fx.s.attention_ack, fx.alice, fx.room, body) == 400


def test_snooze_bounds(fx):
    k_ = {"key": "blocked|task:t1", "fp": "a" * 24}
    for secs in (1, 299, 14 * 86400 + 1, True, "3600", None):
        assert code(fx.s.attention_snooze, fx.alice, fx.room, {**k_, "seconds": secs}) == 400
    assert fx.s.attention_snooze(fx.alice, fx.room, {**k_, "seconds": 300})["state"] == "snoozed"


def test_marks_are_bounded_rate_limited_and_say_nothing_about_what_exists(fx):
    f = fx
    cfg(fx, max_acks_per_reader=10, acks_per_10min=15)
    for i in range(12):
        f.s.attention_ack(f.alice, f.room, {"key": f"blocked|task:n{i}", "fp": "a" * 24})
    assert len(f.s._attn[("alice", f.room)]) == 10                                    # oldest dropped, never unbounded
    assert "blocked|task:n0" not in f.s._attn[("alice", f.room)] and "blocked|task:n11" in f.s._attn[("alice", f.room)]
    for i in range(3):
        f.s.attention_ack(f.alice, f.room, {"key": f"blocked|task:m{i}", "fp": "a" * 24})
    assert code(f.s.attention_ack, f.alice, f.room, {"key": "blocked|task:z", "fp": "a" * 24}) == 429
    f.s.attention_ack(f.bob, f.room, {"key": "blocked|task:z", "fp": "a" * 24})         # bob's budget is his own
    # an existing and a non-existing key answer identically
    node(f.feed, "task:real", state="blocked", room_id=f.room)
    a = f.s.attention_ack(f.bob, f.room, {"key": "blocked|task:real", "fp": "b" * 24})
    b = f.s.attention_ack(f.bob, f.room, {"key": "blocked|task:fake", "fp": "b" * 24})
    assert a == b


def test_marks_need_the_same_access_as_reads(fx):
    body = {"key": "blocked|task:t1", "fp": "a" * 24}
    assert code(fx.s.attention_ack, fx.mallory, fx.room, body) == 404
    assert code(fx.s.attention_ack, fx.dave, fx.room, body) == 404
    assert code(fx.s.attention_snooze, fx.mallory, fx.room, {**body, "seconds": 600}) == 404
    assert code(fx.s.attention_restore, fx.dave, fx.room, {"key": body["key"]}) == 404
    cfg(fx, enabled=False)
    assert code(fx.s.attention_ack, fx.alice, fx.room, body) == 403


def test_marks_survive_a_restart_identically_and_never_reach_the_feed_audit_or_long_poll(fx):
    f = fx
    node(f.feed, "ci:c1", state="failed", room_id=f.room)
    it = inbox(f)["items"][0]
    seen = f.s.seq
    room_seq = dict(f.s._room_seq)
    f.s.attention_ack(f.alice, f.room, {"key": it["key"], "fp": it["fp"]})
    f.s.attention_snooze(f.bob, f.room, {"key": it["key"], "fp": it["fp"], "seconds": 600})
    assert f.s.seq == seen + 2 and f.s._room_seq == room_seq                           # nobody's long-poll wakes
    feed = f.s.read_events(f.carol, f.room, 0, 200)["events"]
    assert not any(e["event_type"] in ATTN_EVENTS for e in feed)
    assert not any(e["event_type"] in ATTN_EVENTS for e in f.s.audit(f.alice, f.s._rooms[f.room]["project_id"], 0, 500)["events"])
    assert it["key"] not in json.dumps(f.s.audit(f.alice, f.s._rooms[f.room]["project_id"], 0, 500))
    rebuilt = type(f.s)(f.s.auth, sink=None, now=f.s.clock)
    rebuilt.hydrate([e for e in f.s._log])
    assert repr(sorted(rebuilt._attn.items())) == repr(sorted(f.s._attn.items())) and len(rebuilt._attn) == 2


# =============================================================================================== leaks: hidden nodes, differencing
def hidden_noise(f):
    """Things bob and carol must not be able to see, in every shape the graph knows."""
    node(f.feed, "task:h-private", state="blocked", scope="private")
    node(f.feed, "ci:h-private", state="failed", scope="private")
    node(f.feed, "task:h-room2", state="blocked", room_id=f.room2)
    node(f.feed, "ci:h-room2", state="failed", room_id=f.room2)
    node(f.feed, "attempt:h-mission", state="expired", scope="mission", room_id=f.room, mission_id="m-secret")
    node(f.feed, "task:h-mission", state="done", scope="mission", room_id=f.room, mission_id="m-secret")
    node(f.feed, "approach:h", state="selected", scope="private")
    node(f.feed, PR2, state="merged", room_id=f.room2, principal=OPERATOR)
    edge(f.feed, "produced", "task:h-room2", PR2, room_id=f.room2, principal=OPERATOR)
    for i in range(8):
        node(f.feed, f"task:h-more{i}", state="done", scope="mission", room_id=f.room, mission_id="m-secret")


def test_hidden_nodes_change_no_number_no_item_and_no_count(fx):
    f = fx
    node(f.feed, "task:t1", state="blocked", room_id=f.room)
    node(f.feed, "ci:c1", state="failed", room_id=f.room)
    merged_task(f, 5)
    before = {who: (json.dumps(prog(f, w_), sort_keys=True), json.dumps(inbox(f, w_), sort_keys=True),
                    json.dumps(stats(f, w_), sort_keys=True)) for who, w_ in (("bob", f.bob), ("carol", f.carol))}
    hidden_noise(f)
    after = {who: (json.dumps(prog(f, w_), sort_keys=True), json.dumps(inbox(f, w_), sort_keys=True),
                   json.dumps(stats(f, w_), sort_keys=True)) for who, w_ in (("bob", f.bob), ("carol", f.carol))}
    strip = lambda s: json.loads(s)                                                   # noqa: E731
    for who in before:
        for a, b in zip(before[who], after[who], strict=True):
            a, b = strip(a), strip(b)
            for d in (a, b):
                d.pop("generated_at", None)
                d.pop("as_of", None)
            assert a == b, who                                                        # byte-for-byte the same view
    blob = json.dumps(after)
    assert "h-private" not in blob and "h-room2" not in blob and "h-mission" not in blob and "m-secret" not in blob


def test_a_mission_member_sees_the_mission_and_a_removed_member_stops_seeing_it_on_the_next_read(fx):
    f = fx
    f.s.link_mission(f.alice, f.room, {"mission_id": "m-secret", "members": ["bob"]})
    node(f.feed, "task:h-mission", state="blocked", scope="mission", room_id=f.room, mission_id="m-secret")
    assert "blocked|task:h-mission" in keys(inbox(f, f.bob)) and "blocked|task:h-mission" not in keys(inbox(f, f.carol))
    assert prog(f, f.bob)["room"]["tasks"] == 1 and prog(f, f.carol)["room"]["tasks"] == 0 and prog(f, f.alice)["room"]["tasks"] == 0
    f.s.unlink_mission(f.alice, f.room, "m-secret")
    assert keys(inbox(f, f.bob)) == set() and prog(f, f.bob)["room"]["tasks"] == 0


def test_a_cached_snapshot_never_outlives_a_membership_change(fx):
    f = fx
    node(f.feed, "task:t1", state="blocked", room_id=f.room)
    assert len(inbox(f, f.carol)["items"]) == 1
    f.s.remove_member(f.alice, f.room, "carol")
    assert err(inbox, f, f.carol)[0] == 404
    f.s.add_member(f.alice, f.room, {"user_id": "carol", "role": "viewer"})
    assert len(inbox(f, f.carol)["items"]) == 1


def test_a_quorum_for_a_hidden_pr_is_not_evaluated_for_the_reader(fx):
    f = fx
    f.s.quorum_config = QuorumConfig(1, {"key:r1": {"login": None}}, frozenset({"key:op"}), ())
    node(f.feed, PR1, state="open", attrs={"head_sha": H1, "author": "x"}, room_id=f.room2, principal=OPERATOR)
    assert keys(inbox(f)) == set() and PR1 not in json.dumps(inbox(f)) and PR1 not in json.dumps(prog(f))


# =============================================================================================== analytics
def many(f, n, prefix="t", state="running"):
    for i in range(n):
        node(f.feed, f"task:{prefix}{i}", state=state, room_id=f.room)


def test_small_groups_are_suppressed_and_large_ones_are_not(fx):
    f = fx
    k_ = f.s.insights["min_group_size"]
    for n in range(1, k_):
        merged_task(f, n)
        tick(f, 10)
    out = stats(f)
    for name in ("cycle_time", "rework", "rereview", "fork_win_rate", "blocked_time", "idle_time"):
        assert out[name]["suppressed"] is True and "n" not in out[name] and "rate" not in out[name], name
    assert out["state_mix"] == {"suppressed": True}
    merged_task(f, k_)
    tick(f, 10)
    out = stats(f)
    assert out["cycle_time"]["n"] == k_ and out["cycle_time"]["median_seconds"] >= 0 and "suppressed" not in out["cycle_time"]
    assert out["completion"] == {"complete": k_, "of": k_, "status": "ok"}


def test_state_mix_merges_small_cells_so_a_single_item_cannot_be_recovered_by_subtraction():
    m = ins._merge_small
    assert m({"a": 9, "b": 6, "c": 3, "d": 2}, 5) == {"a": 9, "b": 6, "other": 5}
    assert m({"a": 9, "b": 6, "c": 1}, 5) == {"a": 9, "other": 7}              # 'other' too small: the smallest listed cell joins it
    assert m({"a": 9, "b": 7, "c": 2, "d": 1}, 5) == {"a": 9, "other": 10}
    assert m({"a": 9, "b": 1, "c": 1}, 5) == {"other": 11}
    assert m({"a": 2, "b": 2}, 5) == {"suppressed": True}
    assert m({"a": 7, "b": 5}, 5) == {"a": 7, "b": 5} and m({}, 5) == {}
    for groups in ({"a": 4, "b": 4, "c": 4}, {"a": 11, "b": 1}, {"a": 5, "b": 4, "c": 1}):
        out = m(groups, 5)
        assert all(v >= 5 for v in out.values() if isinstance(v, int)), (groups, out)
        assert sum(v for v in out.values() if isinstance(v, int)) in (sum(groups.values()), 0)


def test_analytics_has_no_per_person_or_per_agent_breakdown(fx):
    f = fx
    for n in range(1, 8):
        merged_task(f, n)
        tick(f, 5)
    out = stats(f)
    blob = json.dumps(out)
    assert out["scope"]["per_person_metrics"] is False
    for banned in ("alice", "bob", "carol", "key:writer", "key:op", "actor", "writer", "user:", "owner", "by_agent", "per_agent"):
        assert banned not in blob, banned


def test_blocked_idle_rework_rereview_and_fork_win_rates(fx):
    f = fx
    for i in range(6):                                          # six tasks: each blocked 60s of 100s active time
        node(f.feed, f"task:w{i}", state="running", room_id=f.room)
    tick(f, 40)
    for i in range(6):
        node(f.feed, f"task:w{i}", state="blocked", room_id=f.room)
    tick(f, 60)
    for i in range(6):
        node(f.feed, f"task:w{i}", state="done", room_id=f.room)
    for i in range(3):                                          # three of them are reopened (rework)
        node(f.feed, f"task:w{i}", state="running", room_id=f.room)
    for i in range(6):                                          # approaches: 3 selected, 3 superseded
        node(f.feed, f"approach:f{i}", state="selected" if i < 3 else "superseded", room_id=f.room)
    out = stats(f)
    assert out["rework"] == {"basis": out["rework"]["basis"], "rate": 0.5, "of": 6}
    assert out["fork_win_rate"]["rate"] == 0.5 and out["fork_win_rate"]["of"] == 6
    bt = out["blocked_time"]
    assert bt["lanes"] == 6 and 0.55 <= bt["share"] <= 0.65
    assert out["idle_time"]["share"] == 0.0


def test_rereview_rate_counts_prs_whose_review_was_bound_to_a_newer_head(fx):
    f = fx
    for i in range(6):
        pr = f"pr:github.com/o/r#{20 + i}"
        node(f.feed, pr, state="open", room_id=f.room, principal=OPERATOR)
        rev(f, R1, f"x{i}", H1, pr=pr)
        if i < 2:
            rev(f, R1, f"y{i}", H2, pr=pr)
    out = stats(f)
    assert out["rereview"]["of"] == 6 and out["rereview"]["rate"] == round(2 / 6, 4)


def test_cost_is_self_reported_totalled_and_suppressed_when_few_report(fx):
    f = fx
    for i in range(3):
        node(f.feed, f"attempt:c{i}", state="running", attrs={"cost_usd": 1.5, "tokens": 100}, room_id=f.room)
    assert stats(f)["cost"] == {"suppressed": True, "reason": stats(f)["cost"]["reason"], "self_reported": True}
    for i in range(3, 6):
        node(f.feed, f"attempt:c{i}", state="running", attrs={"cost_usd": 1.5, "tokens": 100}, room_id=f.room)
    node(f.feed, "attempt:bad", state="running", attrs={"cost_usd": -5, "tokens": "many"}, room_id=f.room)
    cost = stats(f)["cost"]
    assert cost["self_reported"] is True and cost["usd"] == 9.0 and cost["tokens"] == 600 and cost["reporting_nodes"] == 6
    assert cost["of_nodes"] == 7
    assert stats(f)["completion"]["complete"] == 0                              # cost never moves progress


def test_milestone_rows_below_the_minimum_group_size_are_not_listed(fx):
    f = fx
    f.s.link_mission(f.alice, f.room, {"mission_id": "m-1", "members": ["bob"]})
    for i in range(2):
        node(f.feed, f"task:s{i}", state="running", scope="mission", room_id=f.room, mission_id="m-1")
    out = stats(f, f.bob)
    assert out["milestones"] == [] and out["milestones_small_groups_hidden"] is True
    for i in range(2, 6):
        node(f.feed, f"task:s{i}", state="running", scope="mission", room_id=f.room, mission_id="m-1")
    out = stats(f, f.bob)
    assert [m["milestone"] for m in out["milestones"]] == ["mission:m-1"] and out["milestones"][0]["tasks"] == 6


# =============================================================================================== budgets, locks, rate limits
def test_a_cut_read_is_flagged_not_hidden(fx, monkeypatch):
    f = fx
    many(f, 7)
    monkeypatch.setattr(ins, "MAX_NODES", 2)
    cfg(fx, max_pages=2)
    out = prog(f)
    assert out["truncated"] == "page_limit" and out["room"]["tasks"] == 4
    assert out["room"]["estimate"]["status"] != "estimate"
    assert inbox(f)["truncated"] == "page_limit" and stats(f)["truncated"] == "page_limit"


def test_the_service_lock_is_never_held_while_the_graph_is_scanned(fx):
    f = fx
    many(f, 3)
    f.s.quorum_config = QuorumConfig(1, {"key:r1": {"login": None}}, frozenset({"key:op"}), ())
    node(f.feed, PR1, state="open", attrs={"head_sha": H1, "author": "x"}, room_id=f.room, principal=OPERATOR)
    held = []
    graph = f.repo.graph
    for name in ("room_graph", "room_timeline", "list_nodes", "review_evidence", "node_context"):
        real = getattr(graph, name)

        def spy(*a, _real=real, _name=name, **kw):
            held.append((_name, f.s._lock._is_owned()))
            return _real(*a, **kw)
        setattr(graph, name, spy)
    inbox(f)
    prog(f)
    stats(f)
    assert held and not [h for h in held if h[1]], held


def test_repeated_reads_reuse_one_scan_and_a_new_event_forces_a_fresh_one(fx):
    f = fx
    many(f, 3)
    calls = []
    graph = f.repo.graph
    real = graph.room_graph
    graph.room_graph = lambda *a, **kw: (calls.append(1), real(*a, **kw))[1]
    for _ in range(5):
        prog(f)
        inbox(f)
        stats(f)
    assert len(calls) == 1
    node(f.feed, "task:new", state="running", room_id=f.room)
    prog(f)
    assert len(calls) == 2
    prog(f, f.bob)                                                                 # a different reader never shares a scan
    assert len(calls) == 3


def test_reads_are_rate_limited_per_person_and_per_room(fx):
    f = fx
    cfg(fx, reads_per_minute=5)
    for _ in range(5):
        inbox(f)
    assert err(inbox, f)[0] == 429
    inbox(f, f.bob)                                                                # bob has his own allowance
    tick(f, 61)
    inbox(f)
    f.s._hits["insights-room:" + f.room] = [f.s.clock.t] * 50                      # the room-wide allowance is 10x one person's
    assert err(inbox, f, f.carol)[0] == 429
    other = f.room2
    assert other != f.room and len(f.s._hits.get("insights-room:" + other, ())) == 0


def test_since_join_hides_history_from_a_new_member_but_not_the_current_state(fx):
    f = fx
    f.s.replay_history = "since_join"
    node(f.feed, "task:old", state="blocked", room_id=f.room)
    tick(f, 100)
    f.s.add_member(f.alice, f.room, {"user_id": "mallory", "role": "viewer"})
    tick(f, 100)
    node(f.feed, "ci:new", state="failed", room_id=f.room)
    out = inbox(f, f.mallory)
    assert "ci_failed|ci:new" in keys(out)
    assert prog(f, f.mallory)["room"]["tasks"] == 1                               # current state is visible (as in the lineage view)
    assert stats(f, f.mallory)["window"]["start"] >= f.s._iso(f.s.clock.t - 150)  # but no history before joining


# =============================================================================================== text safety
def test_text_from_the_graph_is_cleaned_and_bounded_before_it_is_returned(fx):
    f = fx
    evil = "<img src=x onerror=alert(1)>‮​\x07" + "A" * 400
    node(f.feed, "task:xss", state="blocked", label=evil, room_id=f.room)
    item = inbox(f)["items"][0]
    label = item["subject"]["label"]
    assert len(label) <= ins.MAX_LABEL and "‮" not in label and "​" not in label and "\x07" not in label
    assert label.startswith("<img")                       # markup is NOT interpreted by the server; the client escapes it
    assert ins.clip(None) is None and ins.clip("") is None and ins.clip(5) is None


def test_only_well_formed_github_pr_ids_get_a_link(fx):
    assert ins._pr_url("pr:github.com/o/r#1") == "https://github.com/o/r/pull/1"
    for bad in ("pr:github.com/o/r#1/../x", "pr:evil.example/o/r#1", "pr:github.com/o r/r#1", "pr:github.com/o/r#x",
                "pr:github.com/<script>/r#1", "javascript:alert(1)"):
        assert ins._pr_url(bad) is None


def test_stage_ordering_documents_the_priority_rule():
    items = [{"key": "b", "severity": "notice", "for_you": False, "since": "2026-01-01"},
             {"key": "a", "severity": "notice", "for_you": True, "since": "2026-02-01"},
             {"key": "c", "severity": "error", "for_you": False, "since": None},
             {"key": "d", "severity": "notice", "for_you": False, "since": "2025-01-01"}]
    assert [i["key"] for i in ins.order_items(items)] == ["c", "a", "d", "b"]


# =============================================================================================== review round: gaps the first pass left
def test_an_ack_about_a_hidden_node_leaks_neither_its_key_nor_its_id_to_anyone_else(fx):
    f = fx
    f.s.link_mission(f.alice, f.room, {"mission_id": "m-secret", "members": ["bob"]})
    node(f.feed, "ci:h-secret-ci", state="failed", scope="mission", room_id=f.room, mission_id="m-secret")
    it = inbox(f, f.bob)["items"][0]
    assert it["key"] == "ci_failed|ci:h-secret-ci" and inbox(f, f.carol)["items"] == []
    seen, room_seq = f.s.seq, dict(f.s._room_seq)
    f.s.attention_ack(f.bob, f.room, {"key": it["key"], "fp": it["fp"]})
    assert f.s.seq == seen + 1 and f.s._room_seq == room_seq                           # no long-poll wakes
    project = f.s._rooms[f.room]["project_id"]
    surfaces = {
        "feed": f.s.read_events(f.carol, f.room, 0, 500),
        "audit": f.s.audit(f.alice, project, 0, 500),
        "state": f.s.room_state(f.carol, f.room, -1, 0, 0),
        "carol_inbox": inbox(f, f.carol), "carol_progress": prog(f, f.carol), "carol_stats": stats(f, f.carol),
    }
    for name, out in surfaces.items():
        blob = json.dumps(out, default=str)
        assert "h-secret-ci" not in blob and "attn_" not in blob and it["key"] not in blob, name
    assert f.s.attention_ack(f.carol, f.room, {"key": it["key"], "fp": it["fp"]}) == f.s.attention_ack(
        f.carol, f.room, {"key": "ci_failed|ci:nothing-like-it", "fp": it["fp"]})      # carol cannot even probe whether the id exists


def test_a_hidden_approval_moves_no_number_and_a_hidden_block_is_flagged_but_never_named(fx):
    f = fx
    f.s.link_mission(f.alice, f.room, {"mission_id": "m-secret", "members": ["bob"]})
    f.s.quorum_config = QuorumConfig(2, {"key:r1": {"login": None}, "key:r2": {"login": None}}, frozenset({"key:op"}), ())
    node(f.feed, "task:t1", state="running", room_id=f.room)
    node(f.feed, PR1, attrs={"head_sha": H1, "author": "somebody"}, room_id=f.room, principal=OPERATOR)
    edge(f.feed, "produced", "task:t1", PR1, room_id=f.room, principal=OPERATOR)
    rev(f, R1, "a", H1)
    d = hashlib.sha256(f"b{H1}".encode()).hexdigest()                                    # the second approval is mission-scoped
    node(f.feed, "review:b", scope="mission", room_id=f.room, mission_id="m-secret", principal=R2)
    edge(f.feed, "reviewed", "review:b", PR1,
         {"commit": H1, "verdict": "lgtm", "receipt_digest": d, "receipt_binding": receipt_binding(PR1, H1, R2["id"], d)},
         scope="mission", room_id=f.room, mission_id="m-secret", principal=R2)
    seen_by_carol = task_row(prog(f, f.alice), "task:t1")
    assert seen_by_carol["stage"] == "none" and not any(e["counted"] for e in seen_by_carol["evidence"])
    alone = json.dumps(prog(f, f.alice), sort_keys=True)
    assert "review:b" not in alone and "m-secret" not in alone
    # the inbox follows T06's own status (decided over ALL reviews, so the merge gate is true): that adds nothing T06 does not already tell alice
    assert inbox(f, f.alice)["items"] == [] and inbox(f, f.bob)["items"] == []
    assert task_row(prog(f, f.bob), "task:t1")["stage"] == "verified"                    # whoever CAN see both approvals sees a verified task
    # a hidden block: the reader is told one exists, never whose
    node(f.feed, "review:c", scope="mission", room_id=f.room, mission_id="m-secret", principal=R2)
    c = hashlib.sha256(f"c{H1}".encode()).hexdigest()
    edge(f.feed, "reviewed", "review:c", PR1,
         {"commit": H1, "verdict": "blocked", "receipt_digest": c, "receipt_binding": receipt_binding(PR1, H1, R2["id"], c)},
         scope="mission", room_id=f.room, mission_id="m-secret", principal=R2)
    carol = json.dumps(inbox(f, f.alice), sort_keys=True)
    assert "review:c" not in carol and "key:r2" not in carol and "m-secret" not in carol
    blocked = [i for i in inbox(f, f.alice)["items"] if i["category"] == "review_blocked"]
    assert blocked and all(i["detail"].get("blocked_by_hidden") for i in blocked)


def test_more_linked_missions_than_the_cap_are_flagged_not_silently_dropped(fx, monkeypatch):
    f = fx
    monkeypatch.setattr(ins, "MAX_MISSION_GROUPS", 3)
    for i in range(5):
        f.s.link_mission(f.alice, f.room, {"mission_id": f"m-{i}", "members": ["bob"]})
        node(f.feed, f"task:mm{i}", state="running", scope="mission", room_id=f.room, mission_id=f"m-{i}")
    out = prog(f, f.bob)
    assert out["truncated"] == "mission_limit"
    assert len(out["milestones"]) <= 3 + 1 and out["room"]["tasks"] <= 5
    assert stats(f, f.bob)["truncated"] == "mission_limit"
    assert inbox(f, f.bob)["truncated"] == "mission_limit"


def test_a_pr_an_untrusted_writer_created_first_stays_unattested_even_after_a_trusted_writer_merges_it(fx):
    f = fx
    node(f.feed, "task:t1", state="done", room_id=f.room)
    node(f.feed, "pr:github.com/o/r#1", state="open", room_id=f.room, principal=WRITER)       # the agent's own PR node
    edge(f.feed, "produced", "task:t1", "pr:github.com/o/r#1", room_id=f.room, principal=WRITER)
    node(f.feed, "pr:github.com/o/r#1", state="merged", room_id=f.room, principal=OPERATOR)  # a trusted writer marks it merged
    out = prog(f)
    t = task_row(out, "task:t1")
    assert t["stage"] == "none" and out["room"]["complete"] == 0
    assert [e["counted"] for e in t["evidence"]] == [False] and t["evidence"][0]["why_not"]
    with pytest.raises(PermissionError):                                                      # and it cannot attach itself to a PR it does not own
        node(f.feed, "task:t2", state="done", room_id=f.room)
        merged_task(f, 9)
        edge(f.feed, "produced", "task:t2", "pr:github.com/o/r#9", room_id=f.room)

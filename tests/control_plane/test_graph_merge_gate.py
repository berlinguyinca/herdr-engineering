"""The graph's ``validated`` edges as input to the REAL merge gate (``GateService`` -> ``decide_merge``).

Loop under test (spec 10.4): CI fails on the head -> a repair push moves the head -> everything bound to the old head is
stale -> only fresh, exact-SHA evidence from a configured evidence writer lets the gate merge. The service, its pipeline
store, its status posting and ``decide_merge`` are the production ones; only GitHub (``FakeApp``) is faked.
"""
import pytest

from herdr_engineering.control_plane.graph_merge_gate import (
    GraphGateConfig,
    config_from_section,
    decide_from_graph,
    graph_checks,
    make_graph_gate,
)
from herdr_engineering.control_plane.ingest import IngestService
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from herdr_engineering.review_gate.config import AppConfig, RepoEnrollment, ReviewGateConfig
from herdr_engineering.review_gate.queue import ReviewQueue
from herdr_engineering.review_gate.registry import AuthorRegistry
from herdr_engineering.review_gate.service import GateService
from herdr_engineering.review_gate.store import PipelineStore
from tests.control_plane.test_graph import Feed
from tests.control_plane.test_review_quorum import HUMAN, PI, PR, R1, R2, actor, cfg, sha
from tests.control_plane.test_review_quorum import World as QuorumWorld
from tests.review_gate.test_service import CI, KEY, REPO, A, B, Clock, FakeApp, approve, ci_row, pr

EVIL = actor("evil")
SECTION = {"enabled": True, "evidence_writers": ["key:pi"], "required_checks": ["ci"]}
GPR = f"pr:{REPO}#7"


class Rig:
    """One GateService wired to a graph, exactly as ``ControlPlaneServer._build_graph_gate`` wires it."""

    def __init__(self, section=SECTION):
        self.cp = ControlPlaneRepo()
        self.feed = Feed(IngestService(self.cp))
        self.app, self.ci = FakeApp(), CI()
        self.store = PipelineStore(self.cp)
        self.section = section
        self.svc = GateService(
            config=ReviewGateConfig(enabled=True, app=AppConfig(1, 2, "/nonexistent/key.pem"),
                                    repos=(RepoEnrollment(repo=REPO),), poll_seconds=5),
            app=self.app, store=self.store, queue=ReviewQueue(self.cp), registry=AuthorRegistry(self.cp),
            ci_snapshot=self.ci, now=Clock(),
            graph_gate=make_graph_gate(graph_fn=lambda: self.cp.graph, section_fn=lambda: self.section))
        self.feed.node(GPR, label="PR 7", principal=PI)

    def validated(self, run, commit, conclusion, who=PI, **attrs):
        self.feed.node(run, principal=who, attrs={"commit": commit})
        self.feed.edge("validated", run, GPR, {"commit": commit, "conclusion": conclusion, **attrs}, principal=who,
                       scope="org")

    def push(self, head):
        self.app.prs[REPO] = [pr(sha=head)]
        self.ci.set(ci_row(head, "success"))                # GitHub's own CI is green throughout: the graph decides
        self.svc.tick()

    def last_status(self):
        return [c for c in self.app.calls if c[0] == "set_status"][-1]

    @property
    def merged(self):
        return [c for c in self.app.calls if c[0] == "merge_pr"]


def test_ci_failure_repair_push_stale_review_rereview_then_merge_through_the_real_gate():
    r = Rig()
    r.validated("ci:run-1", A, "fail")                       # CI failed on head A
    r.push(A)
    approve(r.store, KEY, A)                                 # the review gate approved A
    r.svc.tick()
    assert not r.merged and r.last_status()[3] == "failure" and "check ci failed" in r.last_status()[4]
    # repair push: the head moves to B. The approval and the red CI are both bound to A.
    r.push(B)
    assert r.store.snapshot(KEY).head_sha == B and r.store.snapshot(KEY).approved_sha != B   # stale review: re-review needed
    r.svc.tick()
    assert not r.merged
    approve(r.store, KEY, B)                                 # fresh independent re-review at B
    r.svc.tick()
    assert not r.merged and r.last_status()[3] == "pending"
    assert "required check ci missing" in r.last_status()[4] and "stale evidence ignored" in r.last_status()[4]
    # a PASS from a writer that is not a configured evidence writer changes nothing
    r.validated("ci:run-evil", B, "pass", who=EVIL)
    r.svc.tick()
    assert not r.merged
    # a PASS bound to the OLD head cannot unblock the new one, even from the trusted writer
    r.validated("ci:run-late", A, "pass")
    r.svc.tick()
    assert not r.merged
    # fresh, exact-SHA evidence from the trusted writer: now, and only now, the gate merges
    r.validated("ci:run-2", B, "pass")
    r.svc.tick()
    assert len(r.merged) == 1 and r.merged[0][3] == B


def test_a_red_on_the_same_head_after_a_green_blocks_and_a_rerun_green_unblocks():
    r = Rig()
    r.push(A)
    approve(r.store, KEY, A)
    r.validated("ci:run-1", A, "pass")
    r.validated("ci:run-2", A, "fail")                       # the later run on the same SHA is red
    r.svc.tick()
    assert not r.merged and r.last_status()[3] == "failure"
    r.validated("ci:run-3", A, "pass")                       # re-run is green: latest admission wins
    r.svc.tick()
    assert len(r.merged) == 1


def test_unknown_conclusions_are_never_a_pass():
    r = Rig()
    r.push(A)
    approve(r.store, KEY, A)
    r.validated("ci:run-1", A, "neutral-ish")
    r.svc.tick()
    assert not r.merged and r.last_status()[3] == "pending"


def test_without_the_section_every_merge_is_exactly_as_before():
    r = Rig(section=None)
    r.push(A)
    approve(r.store, KEY, A)
    r.svc.tick()
    assert len(r.merged) == 1                                # no graph evidence at all, and the gate is inert
    off = Rig(section={**SECTION, "enabled": False})
    off.push(A)
    approve(off.store, KEY, A)
    off.svc.tick()
    assert len(off.merged) == 1


def test_a_broken_section_or_graph_fails_closed_to_wait():
    r = Rig(section={"enabled": True, "evidence_writers": []})
    r.push(A)
    approve(r.store, KEY, A)
    r.svc.tick()
    assert not r.merged and "graph evidence" in r.last_status()[4]
    r2 = Rig()
    r2.svc._graph_gate = make_graph_gate(graph_fn=lambda: (_ for _ in ()).throw(RuntimeError("graph down")),
                                         section_fn=lambda: SECTION)
    r2.push(A)
    approve(r2.store, KEY, A)
    r2.svc.tick()
    assert not r2.merged and r2.last_status()[3] == "pending"
    r3 = Rig()                                               # the PR node is not in the graph at all
    r3.svc._graph_gate = make_graph_gate(graph_fn=lambda: ControlPlaneRepo().graph, section_fn=lambda: SECTION)
    r3.push(A)
    approve(r3.store, KEY, A)
    r3.svc.tick()
    assert not r3.merged


def test_the_answer_survives_a_rebuild_from_stored_events():
    r = Rig()
    r.validated("ci:run-1", A, "pass")
    before = graph_checks(r.cp.graph, __import__("herdr_engineering.control_plane.graph_merge_gate", fromlist=["READER"]).READER,
                          GPR, A, frozenset({"key:pi"}))
    r.cp._rebuild_graph()
    after = graph_checks(r.cp.graph, __import__("herdr_engineering.control_plane.graph_merge_gate", fromlist=["READER"]).READER,
                         GPR, A, frozenset({"key:pi"}))
    assert before == after and [c.status for c in after[0]] == ["pass"]


@pytest.mark.parametrize("raw", [{"enabled": True, "evidence_writers": ["key:pi"], "extra": 1},
                                 {"enabled": True, "evidence_writers": "key:pi"},
                                 {"enabled": True, "evidence_writers": ["key:pi"], "required_checks": []},
                                 {"enabled": True, "evidence_writers": ["key:pi"], "require_review_quorum": "yes"}])
def test_config_is_strict(raw):
    with pytest.raises(ValueError):
        config_from_section(raw)


def test_config_defaults_and_off_switch():
    assert config_from_section(None) is None and config_from_section({"enabled": False, "evidence_writers": ["x"]}) is None
    c = config_from_section({"enabled": True, "evidence_writers": ["key:pi", "key:pi"]})
    assert c == GraphGateConfig(frozenset({"key:pi"}), ("ci",), False)


def test_the_review_half_can_be_the_head_bound_quorum_from_the_same_graph():
    """decide_from_graph with the review quorum on: stale reviews and the author's own review never count."""
    w = QuorumWorld()
    w.feed.node(PR, label="PR 7", principal=PI, attrs={"author": "Author-Login"})
    w.feed.edge("produced", "attempt:impl-1", PR, {"commit": sha(1)}, principal=PI)
    gate_cfg = GraphGateConfig(frozenset({"key:pi"}), ("ci",), True)
    quorum = cfg()
    w.head(sha(1))
    w.feed.node("ci:r1", principal=PI, attrs={"commit": sha(1)})
    w.feed.edge("validated", "ci:r1", PR, {"commit": sha(1), "conclusion": "pass"}, principal=PI, scope="org")
    d, _ = decide_from_graph(w.repo.graph, PR, sha(1), gate_cfg, quorum_cfg=quorum)
    assert d.decision == "wait" and "no review" in d.reasons                 # CI green, no independent review yet
    w.review(R1, "review:a", sha(1))
    w.review(HUMAN, "review:author", sha(1))                                 # the author's login never counts
    assert decide_from_graph(w.repo.graph, PR, sha(1), gate_cfg, quorum_cfg=quorum)[0].decision == "wait"
    w.review(R2, "review:b", sha(1))
    assert decide_from_graph(w.repo.graph, PR, sha(1), gate_cfg, quorum_cfg=quorum)[0].decision == "merge"
    w.head(sha(2))                                                            # a new push: reviews and CI are stale
    d, notes = decide_from_graph(w.repo.graph, PR, sha(2), gate_cfg, quorum_cfg=quorum)
    assert d.decision == "wait" and any("stale evidence ignored" in x for x in d.reasons) and notes["stale"]
    assert decide_from_graph(w.repo.graph, PR, sha(2), gate_cfg, quorum_cfg=None)[0].reasons[0].startswith("review quorum")


def test_the_servers_own_builder_reads_the_yaml_section_hot_and_fails_closed_once_enabled():
    """ControlPlaneServer._build_graph_gate: the production wiring of the hook (YAML section, hot switch)."""
    from herdr_engineering.control_plane.api import ControlPlaneServer
    from herdr_engineering.fleet_config import FleetConfigError
    state = {"section": {"graph_merge_gate": SECTION}, "broken": False}

    class Section:
        def section(self, name):
            if state["broken"]:
                raise FleetConfigError("unreadable")
            return state["section"]

    class Fleet:
        def current(self):
            return Section()

    repo = ControlPlaneRepo()
    server = ControlPlaneServer(repo)
    gate = server._build_graph_gate(Fleet())
    Feed(IngestService(repo)).node(GPR, label="PR 7", principal=PI)
    enrollment, head = RepoEnrollment(repo=REPO), pr(sha=A)
    assert gate(enrollment, head, [])[0] == "wait"                     # enabled, no evidence at the head
    state["section"] = {}                                              # kill switch: the section is gone
    assert gate(enrollment, head, []) == ("allow", ())
    state["section"] = {"graph_merge_gate": SECTION}
    assert gate(enrollment, head, [])[0] == "wait"
    state["broken"] = True                                             # enabled once, config now unreadable: never "absent"
    with pytest.raises(FleetConfigError):
        gate(enrollment, head, [])
    server.shutdown()

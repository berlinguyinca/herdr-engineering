"""Delivery metrics from graph events (spec 5, 10.9): accepted changes, gate failures, handoff success and recovery
latency, rework, review findings, issue lead time; tokens and cost only when events report them.

Fixture: 12 handoffs across 61 sessions (two failed, one still pending), a forked branch with a decision, a PR whose head
moved after review, a CI failure on the new head, 6 findings, 5 closed issues. The clock is injected, so every duration
is exact. Evidence states come from the trusted writer `key:op`; the agent emitter `key:pi` reports about itself."""
import json
from datetime import UTC, datetime

import pytest

from herdr_engineering.control_plane import collab_insights as ins
from herdr_engineering.control_plane.ingest import IngestService
from herdr_engineering.control_plane.lineage_metrics import lineage_metrics
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from tests.control_plane.test_graph import OPERATOR, _AnyKey

ROOM = "room-1"
REVIEWER = {"kind": "operator", "id": "key:r1", "authenticated_by": "api_key"}
EMITTER = {"kind": "agent", "id": "key:pi", "authenticated_by": "api_key"}
CONTEXTS = frozenset({("room", ROOM, None)})
CFG = {"min_group_size": 5, "evidence_writers": ["key:op"]}
PR = "pr:github.com/o/r#7"
HA, HB = "a" * 40, "b" * 40


class Clock:
    def __init__(self):
        self.t = datetime(2026, 10, 29, 9, 0, tzinfo=UTC).timestamp()

    def __call__(self):
        return self.t

    def tick(self, seconds=1):
        self.t += seconds


class Fixture:
    def __init__(self, report_usage=False):
        self.clock = Clock()
        self.repo = ControlPlaneRepo(now=self.clock)
        self.svc = IngestService(self.repo, key_scopes=_AnyKey())
        self.n = 0
        self.report_usage = report_usage

    def send(self, who, event_type, payload, entity_id):
        self.n += 1
        self.clock.tick(1)
        return self.svc.ingest({"schema_version": 2, "event_id": f"e{self.n}", "source": "src-" + who["id"].split(":")[1],
                                "source_sequence": self.n, "entity_type": "lineage", "entity_id": entity_id,
                                "event_type": event_type, "payload": payload, "visibility_scope": "room",
                                "room_id": ROOM}, who)

    def node(self, node_id, who=EMITTER, **kw):
        return self.send(who, "node.upsert", {"node_type": node_id.split(":")[0], "node_id": node_id, **kw}, node_id)

    def edge(self, etype, a, b, attrs=None, who=EMITTER):
        payload = {"edge_type": etype, "from_id": a, "to_id": b}
        if attrs is not None:
            payload["attrs"] = attrs
        return self.send(who, "edge.add", payload, a)

    def snap(self):
        return ins.take_snapshot(self.repo.graph, OPERATOR, CONTEXTS, None, max_pages=3, mission_ctxs=[])


def build(report_usage=False) -> Fixture:
    f = Fixture(report_usage)
    # 61 sessions; s0..s12 form a chain of 12 handoffs
    for i in range(61):
        usage = {"tokens": 1000 + i, "cost_usd": 0.5} if report_usage and i < 6 else {}
        f.node(f"session:s{i}", state="running", attrs={"agent_id": "a1", **usage})
    for i in range(12):
        f.edge("handoff", f"session:s{i}", f"session:s{i + 1}", {"reason": "context"})
        f.clock.tick(2 + i)                                     # recovery latency of handoff i is 3 + i seconds
        if i == 3 or i == 7:
            f.node(f"session:s{i + 1}", state="failed")         # successor never recovered
        elif i == 11:
            pass                                                # successor stays at its first state "running"
        else:
            f.node(f"session:s{i + 1}", state="running")
    # a branch: two approaches from one origin, one selected
    f.node("attempt:t1", state="running")
    f.node("approach:main", state="selected")
    f.node("approach:alt", state="superseded")
    f.edge("fork", "attempt:t1", "approach:main")
    f.edge("fork", "attempt:t1", "approach:alt")
    f.edge("supersession", "approach:main", "approach:alt")
    # a PR: reviewed at head A, head moves to B (repair), CI fails on B, merged after a fresh review
    f.node(PR, who=OPERATOR, state="open", attrs={"head_sha": HA})
    f.node("review:r1", who=REVIEWER)
    f.edge("reviewed", "review:r1", PR, {"commit": HA, "verdict": "lgtm"},
           who=REVIEWER)
    f.node(PR, who=OPERATOR, attrs={"head_sha": HB})
    f.node("ci:run-9", who=OPERATOR, state="failed")
    f.edge("validated", "ci:run-9", PR, {"commit": HB, "conclusion": "fail"}, who=OPERATOR)
    f.edge("reviewed", "review:r1", PR, {"commit": HB, "verdict": "lgtm"},
           who=REVIEWER)
    f.node("ci:run-10", who=OPERATOR, state="passed")
    f.node(PR, who=OPERATOR, state="merged")
    # a second PR merged
    f.node("pr:github.com/o/r#8", who=OPERATOR, state="merged")
    f.node("gate:g1", who=OPERATOR, state="blocked")
    # findings: 3 fixed, 1 confirmed, 1 dismissed, 1 open
    for i, (state, sev) in enumerate([("fixed", "high"), ("fixed", "high"), ("fixed", "low"), ("confirmed", "low"),
                                      ("dismissed", "low"), ("open", "medium")]):
        f.node(f"finding:f{i}", state=state, attrs={"severity": sev})
    # issues: 5 closed after 60, 120, ... seconds
    for i in range(5):
        iid = f"issue:github.com/o/r#{100 + i}"
        f.node(iid, who=OPERATOR, state="open")
        f.clock.tick(60 * (i + 1))
        f.node(iid, who=OPERATOR, state="closed")
    f.node("issue:github.com/o/r#200", who=OPERATOR, state="open")      # still open: no lead time
    return f


@pytest.fixture(scope="module")
def metrics():
    f = build(report_usage=True)
    return lineage_metrics(f.snap(), CFG)


def test_fixture_has_the_required_scale(metrics):
    assert metrics["handoffs"]["total"] == 12 and metrics["sessions"]["total"] == 61
    assert metrics["truncated"] is None


def test_accepted_changes_and_gate_failures_count_only_trusted_writer_state(metrics):
    assert metrics["accepted_changes"]["count"] == 2
    gates = metrics["gate_failures"]
    assert (gates["count"], gates["ci_failed"], gates["gate_blocked"], gates["validated_edges_failed"]) == (2, 1, 1, 1)


def test_handoff_success_and_recovery_latency(metrics):
    h = metrics["handoffs"]
    assert h["outcomes"] == {"success": 10, "failed": 2, "pending": 0, "unknown_target": 0}
    assert h["success_rate"] == {"basis": h["success_rate"]["basis"], "rate": round(10 / 12, 4), "of": 12}
    lat = h["recovery_latency"]
    # handoff i waits 2+i s and its successor reports 1 s later: 3+i s. i in {0,1,2,4,5,6,8,9,10} succeeded with a
    # running span after the edge (9 samples); i=11's successor was already running BEFORE the edge, so it yields no
    # sample rather than an invented one
    assert lat["n"] == 9
    assert (lat["median_seconds"], lat["p75_seconds"], lat["p90_seconds"]) == (8, 11, 13)
    assert h["self_reported"] is True


def test_rework_counts_stale_reviews_supersession_and_head_changes(metrics):
    rework = metrics["rework"]
    assert rework["stale_reviews"] == 1 and rework["superseded"] == 1 and rework["head_changes_after_review"] == 1


def test_review_findings_are_counted_with_precision(metrics):
    f = metrics["review_findings"]
    assert (f["total"], f["open"]) == (6, 1)
    assert f["precision"]["rate"] == round(4 / 5, 4) and f["precision"]["of"] == 5
    # severity cells (high 2, low 3, medium 1) are each below min_group_size=5, so they merge instead of singling items out
    assert f["by_severity"] == {"other": 6}


def test_issue_lead_time_uses_receipt_times(metrics):
    lead = metrics["issue_lead_time"]
    # issue i is closed 60*(i+1) s after it was opened, plus 1 s of event spacing: 61..301 s; the open one has no sample
    assert lead["n"] == 5 and lead["median_seconds"] == 181 and lead["p90_seconds"] == 301


def test_tokens_and_cost_are_reported_only_when_events_reported_them(metrics):
    usage = metrics["usage"]
    assert usage["tokens"]["status"] == "reported" and usage["tokens"]["value"] == sum(1000 + i for i in range(6))
    assert usage["tokens"]["reporting_nodes"] == 6 and usage["cost_usd"]["value"] == 3.0


def test_unreported_usage_is_unknown_never_estimated():
    f = build(report_usage=False)
    usage = lineage_metrics(f.snap(), CFG)["usage"]
    assert usage["tokens"]["status"] == "unknown" and usage["cost_usd"]["status"] == "unknown"
    assert "value" not in usage["tokens"] and "value" not in usage["cost_usd"]


def test_fewer_than_k_reporters_is_suppressed_not_shown():
    f = Fixture()
    for i in range(3):
        f.node(f"session:s{i}", state="running", attrs={"tokens": 10, "cost_usd": 1})
    usage = lineage_metrics(f.snap(), CFG)["usage"]
    assert usage["tokens"].get("suppressed") and "value" not in usage["tokens"]


def test_without_evidence_writers_nothing_is_claimed_from_self_reported_state():
    f = build()
    m = lineage_metrics(f.snap(), {"min_group_size": 5, "evidence_writers": []})
    assert m["accepted_changes"]["count"] == "unknown" and m["gate_failures"]["count"] == "unknown"


def test_an_agent_cannot_inflate_accepted_changes_by_reporting_merged_itself():
    f = Fixture()
    f.node("pr:github.com/o/r#1", state="merged")                       # written by the agent emitter, not key:op
    assert lineage_metrics(f.snap(), CFG)["accepted_changes"]["count"] == 0


def test_the_computation_is_read_only_and_deterministic():
    f = build()
    before = json.dumps(f.repo.graph.snapshot(), sort_keys=True, default=str)
    snap = f.snap()
    a, b = lineage_metrics(snap, CFG), lineage_metrics(snap, CFG)
    assert a == b and json.dumps(f.repo.graph.snapshot(), sort_keys=True, default=str) == before


def test_no_per_person_or_per_agent_breakdown(metrics):
    text = json.dumps(metrics)
    assert "key:pi" not in text and "key:op" not in text and "agent_id" not in text
    assert metrics["scope"]["per_person_metrics"] is False


# -- the existing read route: room analytics now carries `delivery` ------------------------------------------------
from tests.control_plane.test_attention_progress import fx, merged_task, stats, tick  # noqa: E402,F401,F811
from tests.control_plane.test_collab_external import enroll  # noqa: E402,F401
from tests.control_plane.test_collab_links import w  # noqa: E402,F401
from tests.control_plane.test_collab_tasks import svc  # noqa: E402,F401


def test_room_analytics_route_includes_delivery_metrics_for_the_reader(fx):  # noqa: F811
    for n in range(1, 4):
        merged_task(fx, n)
        tick(fx, 5)
    out = stats(fx)
    delivery = out["delivery"]
    assert delivery["scope"]["per_person_metrics"] is False
    assert delivery["handoffs"]["total"] == 0 and delivery["usage"]["tokens"]["status"] == "unknown"
    assert delivery["accepted_changes"]["count"] >= 1          # key:op is the configured evidence writer in this fixture
    blob = json.dumps(delivery)
    for banned in ("alice", "bob", "carol", "key:writer", "key:op"):
        assert banned not in blob

"""The ``validated`` evidence writer, end to end: Woodpecker rows -> real IngestService -> graph -> the real merge gate.

The service, store, status posting, ``decide_merge`` and ingest are the production ones; only GitHub (``FakeApp``) is faked.
"""
import json
import threading
import urllib.parse
import urllib.request

import pytest

from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.graph_evidence_writer import (
    GraphEvidenceWriter,
    evidence_config_from_gate_section,
    validate_evidence_source,
)
from herdr_engineering.control_plane.graph_merge_gate import (
    READER,
    decide_from_graph,
    graph_checks,
    make_graph_gate,
    validate_section,
)
from herdr_engineering.control_plane.ingest import IngestService
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from herdr_engineering.review_gate.config import AppConfig, RepoEnrollment, ReviewGateConfig
from herdr_engineering.review_gate.queue import ReviewQueue
from herdr_engineering.review_gate.registry import AuthorRegistry
from herdr_engineering.review_gate.service import GateService
from herdr_engineering.review_gate.store import PipelineStore
from tests.review_gate.test_service import CI, KEY, REPO, A, B, Clock, FakeApp, approve, ci_row, pr

WRITER = "ci-evidence:woodpecker"
SOURCE = {"enabled": True, "principal": WRITER, "repos": [{"repo": REPO}]}
GATE = {"enabled": True, "evidence_writers": [WRITER], "required_checks": ["ci"], "evidence_source": SOURCE}
PR_NODE = f"pr:{REPO}#7"


class Rig:
    def __init__(self, section):
        self.section = section
        self.cp = ControlPlaneRepo()
        self.ingest = IngestService(self.cp)
        self.writer = GraphEvidenceWriter(ingest=self.ingest, section_fn=lambda: self.section)
        self.app, self.ci = FakeApp(), CI()
        self.store = PipelineStore(self.cp)
        self.svc = GateService(
            config=ReviewGateConfig(enabled=True, app=AppConfig(1, 2, "/nonexistent/key.pem"),
                                    repos=(RepoEnrollment(repo=REPO),), poll_seconds=5),
            app=self.app, store=self.store, queue=ReviewQueue(self.cp), registry=AuthorRegistry(self.cp),
            ci_snapshot=self.ci, now=Clock(),
            graph_gate=make_graph_gate(graph_fn=lambda: self.cp.graph, section_fn=lambda: self.section),
            evidence_hook=self.writer.observe)

    def push(self, head, state="success", n=1):
        self.app.prs[REPO] = [pr(sha=head)]
        self.ci.set(ci_row(head, state, n=n))
        self.svc.tick()

    def checks(self, head, writers=frozenset({WRITER})):
        return graph_checks(self.cp.graph, READER, PR_NODE, head, writers)

    @property
    def merged(self):
        return [c for c in self.app.calls if c[0] == "merge_pr"]

    def last_status(self):
        return [c for c in self.app.calls if c[0] == "set_status"][-1]


def test_pass_on_head_a_is_fresh_evidence_and_the_gate_merges():
    r = Rig(GATE)
    r.push(A)
    checks, notes = r.checks(A)
    assert [(c.name, c.status, c.sha) for c in checks] == [("ci", "pass", A)] and not notes["stale"]
    approve(r.store, KEY, A)
    r.svc.tick()
    assert len(r.merged) == 1 and r.merged[0][3] == A


def test_a_push_makes_the_old_heads_evidence_stale_and_the_new_head_waits_until_it_is_observed_green():
    r = Rig(GATE)
    r.push(A)
    off = {**GATE, "evidence_source": {**SOURCE, "enabled": False}}      # the writer is paused while head B arrives
    r.section = off
    r.push(B)                                         # B is green at GitHub, but nothing has been written for B
    approve(r.store, KEY, B)
    r.svc.tick()
    checks_b, notes = r.checks(B)
    assert checks_b == [] and any(A[:8] in s for s in notes["stale"])    # A's pass is stale evidence, never B's check
    assert not r.merged and "stale evidence ignored" in r.last_status()[4]
    r.section = GATE                                  # the writer observes B's finished CI: fresh evidence for exactly B
    r.svc.tick()
    assert [(c.status, c.sha) for c in r.checks(B)[0]] == [("pass", B)]
    assert len(r.merged) == 1 and r.merged[0][3] == B


def test_a_ci_failure_is_written_as_failure_and_blocks():
    r = Rig(GATE)
    r.push(A, state="failure")
    assert [(c.status, c.sha) for c in r.checks(A)[0]] == [("fail", A)]
    decision, _ = decide_from_graph(r.cp.graph, PR_NODE, A, validate_section(GATE))
    assert decision.decision == "reject" and "ci" in " ".join(decision.reasons)
    assert not r.merged and r.last_status()[3] == "failure"


def test_a_rerun_green_after_a_red_on_the_same_head_replaces_it():
    r = Rig(GATE)
    r.push(A, state="failure", n=1)
    r.push(A, state="success", n=2)
    assert [c.status for c in r.checks(A)[0]] == ["pass"]


def test_replay_is_idempotent_across_ticks_and_a_restart():
    r = Rig(GATE)
    r.push(A)
    r.push(A)
    again = GraphEvidenceWriter(ingest=r.ingest, section_fn=lambda: r.section)      # a restarted writer: empty memory
    n_before = len(r.cp.graph.neighborhood(READER, PR_NODE, depth=1)["edges"])
    assert again.observe(RepoEnrollment(repo=REPO), pr(sha=A), [ci_row(A, "success")], False) == "written"
    assert len(r.cp.graph.neighborhood(READER, PR_NODE, depth=1)["edges"]) == n_before == 1


def test_a_writer_the_gate_does_not_list_is_ignored_by_the_gate():
    r = Rig({**GATE, "evidence_writers": ["key:0123456789ab"]})      # step 1 done, step 3 not: gate lists someone else
    r.push(A)
    assert len(r.cp.graph.neighborhood(READER, PR_NODE, depth=1)["edges"]) == 1      # the edge exists ...
    checks, notes = r.checks(A, writers=frozenset({"key:0123456789ab"}))
    assert checks == [] and notes["untrusted"] == 1                                  # ... and the gate does not count it
    approve(r.store, KEY, A)
    r.svc.tick()
    assert not r.merged and "required check ci missing" in r.last_status()[4]


@pytest.mark.parametrize("section", [None, {}, {"evidence_source": {"enabled": False, "principal": WRITER,
                                                                    "repos": [{"repo": REPO}]}},
                                     {"evidence_source": {"principal": WRITER, "repos": [{"repo": REPO}]}}])
def test_off_by_default_writes_nothing(section):
    r = Rig(section)
    r.push(A)
    with pytest.raises(KeyError):
        r.cp.graph.neighborhood(READER, PR_NODE, depth=1)


def test_only_listed_repos_and_prs_are_written():
    only = {**SOURCE, "repos": [{"repo": REPO, "prs": [8]}]}
    r = Rig({**GATE, "evidence_source": only})
    r.push(A)                                         # PR 7 is not listed
    with pytest.raises(KeyError):
        r.cp.graph.neighborhood(READER, PR_NODE, depth=1)
    other = Rig({**GATE, "evidence_source": {**SOURCE, "repos": [{"repo": "github.com/acme/other"}]}})
    other.push(A)
    with pytest.raises(KeyError):
        other.cp.graph.neighborhood(READER, PR_NODE, depth=1)


def test_stale_ci_snapshot_and_unfinished_runs_write_nothing():
    r = Rig(GATE)
    enr = RepoEnrollment(repo=REPO)
    assert r.writer.observe(enr, pr(sha=A), [ci_row(A, "success")], True) == "skipped"
    assert r.writer.observe(enr, pr(sha=A), [ci_row(A, "running")], False) == "pending"
    assert r.writer.observe(enr, pr(sha=A), [ci_row(B, "success")], False) == "pending"      # rows are for another head
    assert r.writer.observe(enr, pr(sha="not-a-sha"), [ci_row(A, "success")], False) == "skipped"
    with pytest.raises(KeyError):
        r.cp.graph.neighborhood(READER, PR_NODE, depth=1)


def test_a_failing_ingest_never_breaks_the_gate_tick():
    r = Rig(GATE)
    r.writer._ingest = type("Boom", (), {"ingest": staticmethod(lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))})()
    r.push(A)                                          # the tick completes
    approve(r.store, KEY, A)
    r.svc.tick()
    assert not r.merged and r.writer.last_error                                     # no evidence: the gate waits


@pytest.mark.parametrize("raw", [
    {"enabled": True, "principal": "key:0123456789ab", "repos": [{"repo": REPO}]},     # an API-key id is refused
    {"enabled": True, "principal": "ci-evidence:*", "repos": [{"repo": REPO}]},
    {"enabled": True, "repos": [{"repo": REPO}]},                                       # principal required
    {"enabled": True, "principal": WRITER, "repos": []},                                # nothing listed
    {"enabled": True, "principal": WRITER, "repos": [{"repo": "acme/widgets"}]},        # not github.com/o/r
    {"enabled": True, "principal": WRITER, "repos": [{"repo": REPO, "prs": [0]}]},
    {"enabled": True, "principal": WRITER, "repos": [{"repo": REPO}, {"repo": REPO}]},
    {"enabld": True}, {"enabled": "yes"}, {"check": "bad name"}, "on"])
def test_startup_validation_is_strict(raw):
    with pytest.raises(ValueError):
        validate_evidence_source(raw)
    with pytest.raises(ValueError):                    # and the gate section's own startup check carries it
        validate_section({"enabled": False, "evidence_source": raw})


def test_validation_accepts_the_documented_block_and_defaults_to_off():
    assert validate_evidence_source(None) is None and evidence_config_from_gate_section({}) is None
    assert validate_evidence_source({"enabled": False}) is None
    cfg = validate_evidence_source(SOURCE)
    assert cfg.principal == WRITER and cfg.check == "ci" and cfg.wants(REPO, 7) and not cfg.wants("github.com/a/b", 7)


# ---- HTTP: the writer's edges are visible through the real server's graph API, and cannot be forged over HTTP ----

@pytest.fixture
def server():
    srv = ControlPlaneServer(ControlPlaneRepo(), api_keys={"op-key": "operator", "agent-key": "agent"})
    threading.Thread(target=srv.serve, daemon=True).start()
    srv.wait_until_ready()
    yield srv
    srv.shutdown()


def http(srv, path, key="op-key", body=None):
    req = urllib.request.Request(f"http://127.0.0.1:{srv.port}{path}", headers={"X-API-Key": key, "Content-Type": "application/json"},
                                 data=json.dumps(body).encode() if body is not None else None, method="POST" if body else "GET")
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def test_http_graph_shows_the_sha_bound_edge_and_the_http_ingest_cannot_pose_as_the_writer(server):
    writer = GraphEvidenceWriter(ingest=server.ingest, section_fn=lambda: GATE)
    assert writer.observe(RepoEnrollment(repo=REPO), pr(sha=A), [ci_row(A, "success")], False) == "written"
    code, view = http(server, "/api/v1/graph/nodes/" + urllib.parse.quote(PR_NODE, safe="") + "/neighborhood?depth=1")
    assert code == 200
    edge = next(e for e in view["edges"] if e["edge_type"] == "validated")
    assert edge["binding"]["commit"] == A and edge["attrs"]["conclusion"] == "success"
    assert edge["provenance"]["writer"] == WRITER and not edge["stale"]
    # an API key, however privileged, is `key:<hash>`; it can write its own validated edge but never as the evidence writer
    body = {"schema_version": 2, "event_id": "f1", "source": "forge", "source_sequence": 1, "entity_type": "lineage",
            "entity_id": "ci:forged", "event_type": "node.upsert", "visibility_scope": "org", "actor": {"id": WRITER},
            "payload": {"node_type": "ci", "node_id": "ci:forged", "attrs": {"commit": B}}}
    code, _ = http(server, "/api/v1/ingest", key="agent-key", body=body)
    assert code == 200
    forged = {"schema_version": 2, "event_id": "f2", "source": "forge", "source_sequence": 2, "entity_type": "lineage",
              "entity_id": "ci:forged", "event_type": "edge.add", "visibility_scope": "org",
              "payload": {"edge_type": "validated", "from_id": "ci:forged", "to_id": PR_NODE,
                          "attrs": {"commit": B, "conclusion": "success"}}}
    assert http(server, "/api/v1/ingest", key="agent-key", body=forged)[0] == 200
    checks, notes = graph_checks(server.repo.graph, READER, PR_NODE, B, frozenset({WRITER}))
    assert checks == [] and notes["untrusted"] == 1       # the forged pass is stamped with the key's own id, not the writer's


def test_http_ingest_replay_of_the_writers_events_is_a_duplicate(server):
    writer = GraphEvidenceWriter(ingest=server.ingest, section_fn=lambda: GATE)
    enr, rows = RepoEnrollment(repo=REPO), [ci_row(A, "success")]
    writer.observe(enr, pr(sha=A), rows, False)
    fresh = GraphEvidenceWriter(ingest=server.ingest, section_fn=lambda: GATE)      # no memory: goes through ingest again
    assert fresh.observe(enr, pr(sha=A), rows, False) == "written"
    _, view = http(server, "/api/v1/graph/nodes/" + urllib.parse.quote(PR_NODE, safe="") + "/neighborhood?depth=1")
    assert len([e for e in view["edges"] if e["edge_type"] == "validated"]) == 1

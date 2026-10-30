"""Deployment escalation and the repair ledger (deploy-4 Task 8): one round per fleet failure,
the attempt cap, the permit ``repair``, issues for software findings, the host report and
the Personal Assistant fallback. No secrets: tokens here are placeholders."""
from __future__ import annotations

import io
import json
import threading
import time
import urllib.error
from types import SimpleNamespace

import pytest

from herdr_engineering.control_plane import deployment_escalation as de
from herdr_engineering.control_plane.attention_inbox import _item
from herdr_engineering.control_plane.deployment_coordinator import (
    CoordinatorState,
    Halt,
    ReleaseEvidence,
    Replan,
)
from herdr_engineering.control_plane.deployment_diagnosis import DiagnosisConfig, Finding
from herdr_engineering.control_plane.deployment_escalation import (
    DeploymentEscalation,
    EscalationConfig,
    EscalationLedger,
    GitHubIssueSink,
    IssueRefused,
)

REPO = "InferWeave/inferweave"
TRACK = (REPO, "main", "ci/woodpecker/push/rust")
C0, C1 = "0" * 40, "1" * 40
SECRET = "ghp_" + "A" * 36          # a token-shaped placeholder, not a credential
GPU = "error: GPU 3 is not a card of this host"
SUPERSEDED = "control authentication failed: connection id mismatch"
FOLLOW = "2 node job(s) on this host do not follow a replacement gateway"


def _defn(component="hive-nodes", *, stage=1, order=1, host="hive",
          restart="process-restart-in-allocation", rollback="previous-version"):
    return SimpleNamespace(component=component, stage=stage, order=order, host=host,
                           restart=restart, rollback={"strategy": rollback}, track=TRACK,
                           hold_s=600, gpu=True)


class Monitor:
    def __init__(self):
        self.rows = {}
        self.n = 0

    def add(self, component, outcome, *, instance="job-1", to=C1, evidence="", rx=None,
            host="hive", ts=None, frm=C0):
        self.n += 1
        rx = rx if rx is not None else 1000.0 + self.n
        ev = {"id": f"{self.n:032x}", "host_ts": ts or f"2026-10-28T12:{self.n // 60:02d}:{self.n % 60:02d}Z",
              "host": f"{host}.box", "component": component, "step": "deployment.cycle",
              "outcome": outcome, "from": frm, "to": to if outcome != "rolled-back" else C0,
              "evidence": evidence, "instance": instance, "reported_by": host,
              "received_epoch": rx}
        if outcome == "rolled-back":
            ev["from"] = to
        self.rows.setdefault(component, {"timeline": []})["timeline"].append(ev)
        return ev

    def snapshot(self):
        return {"components": json.loads(json.dumps(self.rows))}


class World:
    def __init__(self, *, defs=None, cfg=None, dcfg=None, diagnose_fn=None, issue_sink=None,
                 assistant_sink=None, persist_required=None, clock=None, now=None):
        self.defs = defs or {"hive-nodes": _defn()}
        self.mon = Monitor()
        self.state = CoordinatorState()
        self.rows = []
        self.ledger = EscalationLedger(persist=self.rows.append,
                                       persist_required=persist_required or self.rows.append)
        self.alerts = []
        self.t = now or [5000.0]
        self.esc = DeploymentEscalation(
            ledger=lambda: self.ledger, state=lambda: self.state, monitor=lambda: self.mon,
            config=lambda: cfg or EscalationConfig(), diagnosis_config=lambda: dcfg or DiagnosisConfig(),
            diagnose_fn=diagnose_fn, issue_sink=issue_sink, assistant_sink=assistant_sink,
            alert=lambda c, r, p: self.alerts.append((c, r, p)), now=lambda: self.t[0],
            clock=clock)

    def tick(self, components=("hive-nodes",), target=C1, evidence="confirmed"):
        self.state.observe(self.mon.snapshot()["components"], self.defs)
        halt = Halt(stage=1, reason="halted", target_commit=target, generation=1,
                    components=tuple(components))
        if evidence == "confirmed":
            evidence = {TRACK: ReleaseEvidence(target, True)}
        return self.esc.run({TRACK: [halt]}, self.defs, evidence=evidence)

    def rounds(self, component="hive-nodes", release=C1):
        return (self.ledger.get(component, release) or {}).get("rounds", [])


class Counting:
    def __init__(self, finding):
        self.finding, self.calls = finding, []

    def __call__(self, evidence, *, config=None):
        self.calls.append((evidence, config))
        return self.finding


RETRY = Finding(kind="software", summary="the release broke", suggested_action="retry", source="model")


# ---------------------------------------------------------------- rounds and dedupe
def test_a_pattern_failure_records_one_round_and_repeated_ticks_change_nothing():
    w = World()
    w.mon.add("hive-nodes", "started")
    w.mon.add("hive-nodes", "failed", evidence=SUPERSEDED)
    w.tick()
    [r] = w.rounds()
    assert r["finding"]["source"] == "pattern:link-superseded"
    assert r["repair"]["action"] == "retry" and r["attempt"] == 1
    before = len(w.rows)
    for _ in range(5):
        w.tick()
    assert len(w.rounds()) == 1
    assert len(w.rows) == before                 # nothing re-persisted, nothing re-alerted
    assert len(w.alerts) == 1


def test_a_fleet_failing_in_one_tick_is_one_attempt_not_twenty():
    diag = Counting(RETRY)
    w = World(diagnose_fn=diag)
    for i in range(20):
        w.mon.add("hive-nodes", "failed", instance=f"job-{i}")
    w.tick()
    [r] = w.rounds()
    assert r["attempt"] == 1 and len(r["keys"]) == 20
    assert len(diag.calls) == 1                  # one diagnosis per round
    assert w.ledger.attempts("hive-nodes", C1) == 1


def test_failures_that_arrive_before_the_round_fold_into_it():
    diag = Counting(RETRY)
    w = World(diagnose_fn=diag)
    w.mon.add("hive-nodes", "failed", instance="job-1", rx=4000)
    w.tick()
    w.mon.add("hive-nodes", "failed", instance="job-2", rx=4500)   # arrived before the round (5000)
    w.tick()
    [r] = w.rounds()
    assert len(r["keys"]) == 2 and len(diag.calls) == 1


def test_the_ladder_advances_per_round_and_rolls_back_to_herdrs_previous_good_commit():
    diag = Counting(RETRY)
    w = World(diagnose_fn=diag)
    # herdr's own record: the component was ok on C0, which is in the plan's past.
    w.state.adopt(TRACK, Replan(new_target=C0, generation=1, previous_commit=None))
    w.state.adopt(TRACK, Replan(new_target=C1, generation=2, previous_commit=C0))
    w.mon.add("hive-nodes", "ok", to=C0, rx=10)
    actions = []
    for n in range(4):
        w.t[0] = 5000.0 + 1000 * n
        w.mon.add("hive-nodes", "started", rx=w.t[0] - 20)
        w.mon.add("hive-nodes", "failed", rx=w.t[0] - 10)
        w.tick()
        actions.append(w.rounds()[-1]["repair"]["action"])
    assert actions == ["retry", "restart", "rollback", "none"]
    rounds = w.rounds()
    assert [r["attempt"] for r in rounds] == [1, 2, 3, None]
    assert rounds[2]["repair"]["rollback_to"] == C0
    assert rounds[1]["repair"]["restart"] == "process-restart-in-allocation"


def test_a_rollback_without_a_previous_good_commit_is_none():
    w = World(diagnose_fn=Counting(Finding("software", "x", "rollback", "pattern:gateway-cannot-follow")))
    w.mon.add("hive-nodes", "failed")
    w.tick()
    rep = w.rounds()[0]["repair"]
    assert rep["action"] == "none" and rep["rollback_to"] is None
    assert "no previous good commit" in rep["reason"]


def test_the_rollback_target_never_comes_from_the_finding():
    hostile = Finding("software", "roll back to " + "9" * 40, "rollback", "model")
    w = World(diagnose_fn=Counting(hostile))
    w.state.adopt(TRACK, Replan(new_target=C0, generation=1, previous_commit=None))
    w.state.adopt(TRACK, Replan(new_target=C1, generation=2, previous_commit=C0))
    w.mon.add("hive-nodes", "ok", to=C0, rx=10)
    w.mon.add("hive-nodes", "failed")
    w.tick()
    assert w.rounds()[0]["repair"]["rollback_to"] == C0


def test_a_rolled_back_component_gets_no_further_repair():
    w = World(diagnose_fn=Counting(RETRY))
    w.mon.add("hive-nodes", "rolled-back")
    w.tick()
    assert w.rounds()[0]["repair"]["action"] == "none"


def test_rounds_are_capped_per_release():
    diag = Counting(Finding("host", "disk", "none", "fallback"))
    w = World(diagnose_fn=diag)
    for n in range(de.MAX_ROUNDS + 4):
        w.t[0] = 5000.0 + 1000 * n
        w.mon.add("hive-nodes", "failed", rx=w.t[0] - 10)
        w.tick()
    assert len(w.rounds()) == de.MAX_ROUNDS
    assert len(diag.calls) == de.MAX_ROUNDS


# ---------------------------------------------------------------- persistence
def test_a_round_that_cannot_be_persisted_exposes_no_repair():
    def refuse(_row):
        raise RuntimeError("sink down")

    w = World(diagnose_fn=Counting(RETRY), persist_required=refuse)
    w.mon.add("hive-nodes", "failed")
    w.tick()
    assert w.rounds() == []
    assert w.esc.repair_for("hive-nodes", C1, w.defs["hive-nodes"]) is None


def test_the_ledger_survives_hydrate_with_the_cap_enforced():
    w = World(diagnose_fn=Counting(RETRY))
    w.state.adopt(TRACK, Replan(new_target=C0, generation=1, previous_commit=None))
    w.state.adopt(TRACK, Replan(new_target=C1, generation=2, previous_commit=C0))
    w.mon.add("hive-nodes", "ok", to=C0, rx=10)
    for n in range(2):
        w.t[0] = 5000.0 + 1000 * n
        w.mon.add("hive-nodes", "failed", rx=w.t[0] - 10)
        w.tick()
    fresh = EscalationLedger()
    assert fresh.hydrate(w.rows) >= 1
    assert fresh.attempts("hive-nodes", C1) == 2
    w.ledger = fresh
    w.t[0] = 8000.0
    w.mon.add("hive-nodes", "failed", rx=7990)
    w.tick()
    assert w.rounds()[-1]["repair"]["action"] == "rollback" and w.rounds()[-1]["attempt"] == 3


def test_hydrate_skips_malformed_records():
    ledger = EscalationLedger()
    rows = [{"payload": {"kind": "escalation", "seq": 1, "record": {"component": "Bad Name"}}},
            {"payload": {"kind": "escalation", "seq": 2, "record": {
                "component": "hive-nodes", "release": C1, "rounds": [{"finding": {
                    "kind": "software", "summary": "x", "suggested_action": "scancel",
                    "source": "model"}, "repair": {"action": "retry"}, "keys": [], "at": 1}]}}},
            "garbage"]
    assert ledger.hydrate(rows) == 0


# ---------------------------------------------------------------- permit repair
def test_repair_for_is_given_while_the_failure_is_current_and_withdrawn_after():
    w = World(diagnose_fn=Counting(RETRY))
    w.mon.add("hive-nodes", "failed")
    w.tick()
    rep = w.esc.repair_for("hive-nodes", C1, w.defs["hive-nodes"])
    assert rep == {"id": f"hive-nodes:{C1}:1", "action": "retry", "attempt": 1,
                   "reason": "attempt 1 of 3: retry"}
    w.mon.add("hive-nodes", "started", rx=6000)          # the host acted on it
    w.state.observe(w.mon.snapshot()["components"], w.defs)
    assert w.esc.repair_for("hive-nodes", C1, w.defs["hive-nodes"]) is None


def test_repair_for_none_finding_and_unknown_component():
    w = World()
    w.mon.add("hive-nodes", "failed", evidence=GPU)
    w.tick()
    assert w.rounds()[0]["repair"]["action"] == "none"
    assert w.esc.repair_for("hive-nodes", C1, w.defs["hive-nodes"]) is None
    assert w.esc.repair_for("other", C1, _defn("other")) is None
    assert w.esc.repair_for("hive-nodes", None, w.defs["hive-nodes"]) is None


# ---------------------------------------------------------------- budget
def test_an_exhausted_budget_defers_the_diagnosis():
    clock = iter([0.0] + [100.0] * 10).__next__
    diag = Counting(RETRY)
    w = World(diagnose_fn=diag, clock=clock)
    w.mon.add("hive-nodes", "failed")
    stats = w.tick()
    assert stats["deferred"] == 1 and w.rounds() == [] and diag.calls == []


def test_the_model_timeout_is_clamped_to_the_budget():
    diag = Counting(RETRY)
    dcfg = DiagnosisConfig(enabled=True, endpoint="http://127.0.0.1:9/v1", model="m", timeout_s=60)
    w = World(diagnose_fn=diag, dcfg=dcfg, cfg=EscalationConfig(budget_s=10))
    w.mon.add("hive-nodes", "failed")
    w.tick()
    assert diag.calls[0][1].timeout_s <= 10


# ---------------------------------------------------------------- issues
class Sink:
    def __init__(self, answer=None, exc=None, delay=0):
        self.calls, self.answer, self.exc, self.delay = [], answer, exc, delay

    def __call__(self, repo, title, body, key, *, timeout=None):
        self.calls.append({"repo": repo, "title": title, "body": body, "key": key, "timeout": timeout})
        if self.delay:
            time.sleep(self.delay)
        if self.exc:
            raise self.exc
        return self.answer or {"url": f"https://github.com/{repo}/issues/42", "number": 42}


def _software(w, evidence=FOLLOW):
    w.mon.add("hive-nodes", "failed", evidence=evidence)
    w.tick()


def test_flags_off_means_no_external_calls():
    sink, assistant = Sink(), Sink()
    w = World(issue_sink=sink, assistant_sink=assistant)
    _software(w)
    w.mon.add("hive-gateway", "failed", evidence=GPU)
    w.defs["hive-gateway"] = _defn("hive-gateway", order=0, restart="gateway-cycle")
    w.tick(components=("hive-nodes", "hive-gateway"))
    assert sink.calls == [] and assistant.calls == []
    assert w.ledger.get("hive-nodes", C1)["issue"] is None


def test_a_software_finding_files_one_issue_across_ticks():
    sink = Sink()
    w = World(issue_sink=sink, cfg=EscalationConfig(issues=True))
    _software(w)
    for _ in range(3):
        w.tick()
    assert len(sink.calls) == 1
    call = sink.calls[0]
    assert call["repo"] == REPO and call["key"] == f"hive-nodes:{C1}"
    issue = w.ledger.get("hive-nodes", C1)["issue"]
    assert issue == {"state": "filed", "url": f"https://github.com/{REPO}/issues/42", "number": 42,
                     "reason": None}


def test_the_issue_body_is_redacted_labelled_and_mention_free():
    sink = Sink()
    model = Finding("software", "the gateway broke @octocat see #1", "none", "model")
    w = World(issue_sink=sink, cfg=EscalationConfig(issues=True), diagnose_fn=Counting(model))
    _software(w, evidence=f"token={SECRET}\nAuthorization: Bearer {SECRET}\nping @someone")
    body, title = sink.calls[0]["body"], sink.calls[0]["title"]
    assert SECRET not in body and SECRET not in title
    assert "Model output (AI-generated, unverified" in body
    assert "@octocat" not in body and "@someone" not in body
    assert "AI diagnosis" in title and "@" not in title


def test_a_claim_that_cannot_be_persisted_files_nothing():
    calls = {"n": 0}

    def flaky(row):
        calls["n"] += 1
        if (row["payload"]["record"].get("issue") or {}).get("state") == "claimed":
            raise RuntimeError("sink down")

    sink = Sink()
    w = World(issue_sink=sink, cfg=EscalationConfig(issues=True), persist_required=flaky)
    _software(w)
    assert sink.calls == []
    assert w.ledger.get("hive-nodes", C1)["issue"] is None


def test_an_issue_timeout_is_unknown_and_never_refiled():
    sink = Sink(delay=3)
    w = World(issue_sink=sink, cfg=EscalationConfig(issues=True, issue_timeout_s=1))
    _software(w)
    w.tick()
    assert len(sink.calls) == 1
    assert w.ledger.get("hive-nodes", C1)["issue"]["state"] == "unknown"


def test_a_refused_issue_is_recorded_not_retried():
    sink = Sink(exc=IssueRefused("GitHub refused the issue (HTTP 403)"))
    w = World(issue_sink=sink, cfg=EscalationConfig(issues=True))
    _software(w)
    w.tick()
    assert len(sink.calls) == 1
    issue = w.ledger.get("hive-nodes", C1)["issue"]
    assert issue["state"] == "failed" and "403" in issue["reason"]


def test_a_bad_issue_answer_is_not_stored():
    sink = Sink(answer={"url": "https://evil.example/issues/1", "number": 1})
    w = World(issue_sink=sink, cfg=EscalationConfig(issues=True))
    _software(w)
    issue = w.ledger.get("hive-nodes", C1)["issue"]
    assert issue["state"] == "unknown" and issue["url"] is None


def test_a_restart_turns_an_unanswered_claim_into_unknown_and_never_files_again():
    w = World(cfg=EscalationConfig(issues=True))
    _software(w)
    rec = w.ledger.get("hive-nodes", C1)
    rec["issue"] = {"state": "claimed", "url": None, "number": None, "reason": None}
    w.ledger.put(rec)
    fresh = EscalationLedger()
    fresh.hydrate(w.rows)
    assert fresh.get("hive-nodes", C1)["issue"]["state"] == "unknown"
    sink = Sink()
    w.ledger, w.esc.issue_sink = fresh, sink
    w.tick()
    assert sink.calls == []


def test_issue_url_ok():
    assert de.issue_url_ok(f"https://github.com/{REPO}/issues/7", REPO)
    assert de.issue_url_ok(f"https://github.com/{REPO.lower()}/issues/7", REPO)
    for bad in (f"http://github.com/{REPO}/issues/7", "https://github.com/other/repo/issues/7",
                f"https://github.com/{REPO}/pull/7", f"https://github.com/{REPO}/issues/7#x", None):
        assert not de.issue_url_ok(bad, REPO)


# ---------------------------------------------------------------- host findings
def test_a_host_finding_stores_a_full_report_and_takes_the_fallback():
    assistant = Sink()
    w = World(assistant_sink=assistant)
    w.mon.add("hive-nodes", "failed", evidence=f"{GPU}\npassword={SECRET}")
    w.tick()
    rec = w.ledger.get("hive-nodes", C1)
    report = rec["report"]
    assert set(report) == set(de._REPORT_KEYS)
    assert C1 in report["what_changed"] and C0 in report["what_changed"]
    assert "not a card of this host" in report["evidence"] and SECRET not in json.dumps(rec)
    assert report["tried"] and "halted" in report["current_state"]
    assert assistant.calls == [] and rec["assistant"] is None
    # the herdr notification: an attention item and one validated feed event
    [item] = de.attention_items(w.ledger, _item)
    assert item["kind"] == "deployment_escalation" and item["severity"] == "critical"
    assert item["href"] == "#/deployments" and SECRET not in json.dumps(item)
    [(comp, rel, payload)] = w.alerts
    assert (comp, rel) == ("hive-nodes", C1)
    assert set(payload) == {"component", "release", "kind", "source", "repair", "attempt"}


def test_the_assistant_gets_one_notification_with_the_dedupe_key():
    calls = []

    def assistant(*, dedupe_key, report):
        calls.append((dedupe_key, report))

    w = World(assistant_sink=assistant, cfg=EscalationConfig(assistant=True))
    w.mon.add("hive-nodes", "failed", evidence=GPU)
    for _ in range(3):
        w.tick()
    assert [k for k, _ in calls] == [f"hive-nodes:{C1}"]
    assert w.ledger.get("hive-nodes", C1)["assistant"]["state"] == "delivered"


def test_the_alert_closes_when_the_component_stops_failing():
    w = World()
    w.mon.add("hive-nodes", "failed", evidence=GPU)
    w.tick()
    assert len(w.ledger.alerts()) == 1
    w.esc.run({TRACK: []}, w.defs)
    assert w.ledger.alerts() == []
    assert w.ledger.snapshot()[0]["open"] is False


def test_one_component_failing_does_not_stop_the_others():
    def boom(evidence, *, config=None):
        if "hive-gateway" in json.dumps(evidence):
            raise RuntimeError("bad")
        return RETRY

    w = World(diagnose_fn=boom)
    w.defs["hive-gateway"] = _defn("hive-gateway", order=0, restart="gateway-cycle")
    w.mon.add("hive-gateway", "failed")
    w.mon.add("hive-nodes", "failed")
    w.tick(components=("hive-gateway", "hive-nodes"))
    assert w.rounds("hive-nodes") and not w.rounds("hive-gateway")


# ---------------------------------------------------------------- config
def test_config_defaults_off_and_validates():
    cfg = EscalationConfig.from_mapping(None)
    assert cfg.issues is False and cfg.assistant is False
    for bad in ({"issues": "yes"}, {"budget_s": 600}, {"unknown": 1}, ["x"]):
        with pytest.raises(ValueError):
            EscalationConfig.from_mapping(bad)


# ---------------------------------------------------------------- the GitHub sink
class Opener:
    def __init__(self, status=201, body=None):
        self.status, self.body, self.requests = status, body, []

    def open(self, request, timeout=None):
        self.requests.append((request, timeout))
        if self.status >= 400:
            raise urllib.error.HTTPError(request.full_url, self.status, "no", {}, io.BytesIO(
                ("leaked " + SECRET).encode()))
        return io.BytesIO(json.dumps(self.body or {"html_url": f"https://github.com/{REPO}/issues/9",
                                                   "number": 9}).encode())


def test_the_github_sink_posts_to_the_rest_api():
    opener = Opener()
    sink = GitHubIssueSink(token="placeholder-token", opener=opener)
    out = sink(REPO, "title", "body", "k", timeout=5)
    assert out == {"url": f"https://github.com/{REPO}/issues/9", "number": 9, "credential": "github_token"}
    request, timeout = opener.requests[0]
    assert request.full_url == f"https://api.github.com/repos/{REPO}/issues" and timeout == 5
    assert request.get_method() == "POST"
    assert request.headers["Authorization"] == "Bearer placeholder-token"
    assert json.loads(request.data) == {"title": "title", "body": "body"}


def test_the_github_sink_refuses_cleanly():
    with pytest.raises(IssueRefused) as exc:
        GitHubIssueSink(token="placeholder-token", opener=Opener(403))(REPO, "t", "b", "k")
    assert "403" in exc.value.reason and SECRET not in str(exc.value)
    with pytest.raises(RuntimeError) as exc:
        GitHubIssueSink(token="placeholder-token", opener=Opener(500))(REPO, "t", "b", "k")
    assert SECRET not in str(exc.value)
    with pytest.raises(IssueRefused):
        GitHubIssueSink(token="placeholder-token", opener=Opener())("../etc", "t", "b", "k")
    with pytest.raises(IssueRefused):
        GitHubIssueSink(token="placeholder-token", opener=Opener())("a/..", "t", "b", "k")
    with pytest.raises(IssueRefused):
        GitHubIssueSink(token=lambda: None, opener=Opener())(REPO, "t", "b", "k")


def test_bounded_drops_a_late_answer():
    gate = threading.Event()
    with pytest.raises(TimeoutError):
        de._bounded(lambda: gate.wait(5), 0.2)
    gate.set()


# ---------------------------------------------------------------- through the server
def _server_world(tmp_path, sink=None, escalation=None):
    import yaml

    from .test_deployment_actions import World as ServerWorld
    w = ServerWorld(tmp_path, sink=sink)
    if escalation is not None:
        doc = yaml.safe_load(w.path.read_text())
        doc["sections"]["control_plane"]["values"]["deployment_escalation"] = escalation
        w.path.write_text(yaml.safe_dump(doc) + f"# {time.time_ns()}\n")
    return w


def _fail_hive_nodes(w, evidence):
    w.confirm()
    w.svr.deployments_tick()
    w.event("hive-gateway", "started")
    w.event("hive-gateway", "ok")
    w.event("hive-nodes", "started")
    w._n += 1
    ev = {"id": f"{w._n:032x}", "ts": "2026-10-28T12:00:01Z", "host": "hive-nodes.box",
          "component": "hive-nodes", "step": "deployment.hive-nodes.cycle", "outcome": "failed",
          "from": "0" * 40, "to": "1" * 40, "evidence": evidence}
    status, _ = w.call("POST", "/api/deployments/events", ev, {"Authorization": "Bearer placeholder-host-hive"})
    assert status == 200
    w.svr.deployments_tick()


def test_a_halt_reaches_the_host_as_a_permit_repair(tmp_path):
    w = _server_world(tmp_path)
    try:
        _fail_hive_nodes(w, SUPERSEDED)
        assert w.plan()["current"]["type"] == "Halt"
        status, answer = w.permit("hive-nodes")
        assert status == 200 and answer["permitted"] is False
        assert answer["repair"] == {"id": f"hive-nodes:{C1}:1", "action": "retry", "attempt": 1,
                                    "reason": "attempt 1 of 3: retry"}
        # a component that did not fail gets no repair
        assert "repair" not in w.permit("hive-gateway")[1]
        view = w.svr.deployments_view()
        [rec] = view["escalations"]
        assert rec["component"] == "hive-nodes" and rec["open"] is True and rec["pr"] is None
        [row] = [p["components"]["hive-nodes"] for p in view["plans"]]
        assert row["repair_attempts"] == 1          # the ledger's count, next to the starts
        # one validated feed event per component:release, however many ticks
        w.svr.deployments_tick()
        feed = [e for k, s in w.repo._events.items() if k[0] == de.FEED_ENTITY for e in s]
        assert len(feed) == 1 and set(feed[0]["payload"]) == {
            "component", "release", "kind", "source", "repair", "attempt"}
    finally:
        w.close()


def test_the_attention_inbox_shows_an_open_escalation(tmp_path):
    w = _server_world(tmp_path)
    try:
        _fail_hive_nodes(w, GPU)
        snap = w.svr.attention.snapshot()
        items = [i for i in snap["items"] if i["kind"] == "deployment_escalation"]
        assert len(items) == 1 and items[0]["severity"] == "critical"
        assert items[0]["href"] == "#/deployments"
    finally:
        w.close()


def test_escalations_survive_a_restart_and_file_no_second_issue(tmp_path, acknowledged_sink):
    from herdr_engineering.control_plane.bridge import load_into
    from herdr_engineering.control_plane.repo import ControlPlaneRepo
    w = _server_world(tmp_path, sink=acknowledged_sink, escalation={"issues": True})
    sink = Sink()
    w.svr.deployment_escalation.issue_sink = sink
    try:
        _fail_hive_nodes(w, FOLLOW)
        assert len(sink.calls) == 1 and SECRET not in sink.calls[0]["body"]
    finally:
        w.close()

    class Source:
        def missions(self):
            return []

        sessions = services = hosts = missions

        def events(self):
            return list(acknowledged_sink.events)

    fresh = ControlPlaneRepo()
    load_into(fresh, Source())
    assert not any(k[0] == de.ENTITY_TYPE for k in fresh._events)      # private stream
    rec = fresh.deployment_escalations.get("hive-nodes", C1)
    assert rec["issue"]["state"] == "filed" and rec["issue"]["number"] == 42
    # FOLLOW suggests a rollback; with no previous good commit it is "none", so no attempt
    assert len(rec["rounds"]) == 1 and rec["rounds"][0]["repair"]["action"] == "none"
    assert fresh.deployment_escalations.attempts("hive-nodes", C1) == 0


def test_the_issue_flag_off_files_nothing_through_the_server(tmp_path):
    w = _server_world(tmp_path)
    sink = Sink()
    w.svr.deployment_escalation.issue_sink = sink
    try:
        _fail_hive_nodes(w, FOLLOW)
        assert sink.calls == []
        assert w.svr.deployments_view()["escalations"][0]["issue"] is None
    finally:
        w.close()


def test_the_budget_keeps_permits_fresh():
    from herdr_engineering.control_plane.api import DEPLOYMENTS_TICK_S
    from herdr_engineering.control_plane.deployment_actions import PERMIT_STALE_S
    assert de.MAX_BUDGET_S + DEPLOYMENTS_TICK_S < PERMIT_STALE_S
    with pytest.raises(ValueError):
        EscalationConfig(budget_s=de.MAX_BUDGET_S + 1)
    assert EscalationConfig(budget_s=de.MAX_BUDGET_S).budget_s == de.MAX_BUDGET_S


# ---------------------------------------------------------------- fix round 1
PHISH = ("see [x](https://phish.example/login) ![](https://t.example/p.png) "
         "<a href=\"https://evil.example\">click</a> <img src=x> https://bare.example/path ~~~ ```")


def _outside_fences(body):
    out, inside = [], False
    for line in body.splitlines():
        if line.startswith("~~~"):
            inside = not inside
            continue
        if not inside:
            out.append(line)
    assert not inside, "every fence is closed"
    return "\n".join(out)


@pytest.mark.parametrize("source", ["model", "fallback"])
def test_model_and_evidence_text_never_renders_markup_in_the_issue(source):
    sink = Sink()
    finding = Finding("software", PHISH, "none", source)
    w = World(issue_sink=sink, cfg=EscalationConfig(issues=True), diagnose_fn=Counting(finding))
    _software(w, evidence=PHISH)
    body = sink.calls[0]["body"]
    outside = _outside_fences(body)
    for bad in ("phish.example", "t.example", "evil.example", "bare.example", "<a ", "<img", "![", "]("):
        assert bad not in outside, bad
    # both texts are still there, inside the fences, with no fence marker that could close them
    assert body.count("phish.example") == 2
    inside = [ln for ln in body.splitlines() if "phish.example" in ln]
    assert all("~~~" not in ln and "```" not in ln for ln in inside)
    if source == "model":
        assert "Model output (AI-generated, unverified" in outside


def test_a_past_commit_without_an_ok_slot_is_never_the_rollback_target():
    C2 = "2" * 40
    w = World(diagnose_fn=Counting(Finding("software", "x", "rollback", "pattern:gateway-cannot-follow")))
    for gen, commit in enumerate((C2, C0, C1), start=1):
        w.state.adopt(TRACK, Replan(new_target=commit, generation=gen, previous_commit=None))
    assert w.state.plan(TRACK).past[:2] == (C0, C2)
    w.mon.add("hive-nodes", "failed", to=C0, rx=10)       # newest past commit: failed, never ok
    w.mon.add("hive-nodes", "ok", to=C2, rx=5)            # older: ok
    w.mon.add("hive-nodes", "failed")
    w.tick()
    assert w.rounds()[0]["repair"]["rollback_to"] == C2


def test_a_past_commit_with_only_failures_gives_no_rollback():
    w = World(diagnose_fn=Counting(Finding("software", "x", "rollback", "pattern:gateway-cannot-follow")))
    w.state.adopt(TRACK, Replan(new_target=C0, generation=1, previous_commit=None))
    w.state.adopt(TRACK, Replan(new_target=C1, generation=2, previous_commit=C0))
    w.mon.add("hive-nodes", "failed", to=C0, rx=10)
    w.mon.add("hive-nodes", "failed")
    w.tick()
    assert w.rounds()[0]["repair"]["action"] == "none"


def test_a_none_round_does_not_advance_the_attempt_count():
    findings = iter([Finding("host", "disk", "none", "fallback"), RETRY])
    w = World(diagnose_fn=lambda evidence, *, config=None: next(findings))
    for n in range(2):
        w.t[0] = 5000.0 + 1000 * n
        w.mon.add("hive-nodes", "failed", rx=w.t[0] - 10)
        w.tick()
    first, second = w.rounds()
    assert first["repair"]["action"] == "none" and first["attempt"] is None
    assert w.ledger.attempts("hive-nodes", C1) == 1
    assert second["repair"]["action"] == "retry" and second["attempt"] == 1


def test_model_text_is_labelled_in_the_host_report():
    w = World(diagnose_fn=Counting(Finding("host", "the disk is full", "none", "model")))
    w.mon.add("hive-nodes", "failed")
    w.tick()
    report = w.ledger.get("hive-nodes", C1)["report"]
    assert report["finding_source"] == "model"
    assert report["next_step"].startswith("Model output (AI-generated, unverified): the disk is full")


# ---------------------------------------------------------------- final review wave
@pytest.mark.parametrize("evidence", [None, {}, {TRACK: ReleaseEvidence(None, True)},
                                      {TRACK: ReleaseEvidence(C1, None)},
                                      {TRACK: ReleaseEvidence(C0, True)}])
def test_an_unconfirmed_release_is_never_escalated(evidence):
    diag, sink, assistant = Counting(RETRY), Sink(), Sink()
    w = World(diagnose_fn=diag, issue_sink=sink, assistant_sink=assistant,
              cfg=EscalationConfig(issues=True, assistant=True))
    w.mon.add("hive-nodes", "failed", evidence=FOLLOW)
    stats = w.tick(evidence=evidence)
    assert stats["unconfirmed"] == 1
    assert diag.calls == [] and sink.calls == [] and assistant.calls == []
    assert w.rounds() == [] and w.alerts == [] and w.rows == []


def test_an_open_alert_survives_an_unconfirmed_tick():
    w = World()
    w.mon.add("hive-nodes", "failed", evidence=GPU)
    w.tick()
    assert len(w.ledger.alerts()) == 1
    w.tick(evidence={})
    assert len(w.ledger.alerts()) == 1


def test_a_failing_ledger_write_is_warned_once_per_component_release(caplog):
    def refuse(_row):
        raise RuntimeError("legacy sink")

    w = World(diagnose_fn=Counting(RETRY), persist_required=refuse)
    w.mon.add("hive-nodes", "failed")
    with caplog.at_level("WARNING", logger="herdr.deployment_escalation"):
        for _ in range(5):
            w.tick()
    assert len([r for r in caplog.records if "escalation for hive-nodes" in r.getMessage()]) == 1


def test_a_hung_durable_write_fails_the_round_instead_of_blocking(monkeypatch):
    import herdr_engineering.control_plane.repo as repo_mod
    from herdr_engineering.control_plane.repo import ControlPlaneRepo
    monkeypatch.setattr(repo_mod, "DEPLOYMENT_DURABLE_WRITE_TIMEOUT_S", 0.2)
    gate = threading.Event()

    class Hung:
        def record(self, ev):
            pass

        def record_required(self, ev):
            gate.wait(5)

    repo = ControlPlaneRepo(sink=Hung())
    w = World(diagnose_fn=Counting(RETRY), persist_required=repo._persist_deployments_required)
    w.mon.add("hive-nodes", "failed")
    started = time.monotonic()
    w.tick()
    assert time.monotonic() - started < 2
    assert w.rounds() == [] and w.esc.repair_for("hive-nodes", C1, w.defs["hive-nodes"]) is None
    gate.set()


def test_the_server_escalates_only_a_confirmed_release(tmp_path):
    w = _server_world(tmp_path)
    try:
        _fail_hive_nodes(w, SUPERSEDED)
        assert w.svr.deployments_view()["escalations"]
        # a second component fails while the release is no longer confirmed: nothing new
        w.svr.deployment_evidence = lambda track, target, plan: ReleaseEvidence(None, True)
        w.event("hive-gateway", "failed")
        w.svr.deployments_tick()
        comps = [e["component"] for e in w.svr.deployments_view()["escalations"]]
        assert comps == ["hive-nodes"]
        # and the retry decided earlier is not exposed for the unconfirmed release
        assert "repair" not in w.permit("hive-nodes")[1]
    finally:
        w.close()


class _AppCreds:
    def __init__(self, enabled=True, reason="write_disabled"):
        self.enabled, self.reason, self.asked, self.invalidated = enabled, reason, [], []

    def capability_token_or_raise(self, owner, capability, repositories=None):
        from herdr_engineering.github_auth import GitHubAuthError
        self.asked.append((owner, capability, repositories))
        if not self.enabled:
            raise GitHubAuthError(self.reason)
        return "app-installation-token"

    def invalidate(self, owner=None):
        self.invalidated.append(owner)


def test_issue_sink_prefers_the_app_escalations_capability():
    opener = Opener()
    creds = _AppCreds()
    out = GitHubIssueSink(opener=opener, credentials=lambda: creds)(REPO, "t", "b", "k")
    assert out["credential"] == "github_app"
    assert creds.asked == [(REPO.split("/")[0], "escalations", [REPO.split("/")[1]])]
    assert opener.requests[0][0].headers["Authorization"] == "Bearer app-installation-token"


def test_issue_sink_falls_back_to_pat_only_when_capability_not_enabled(monkeypatch):
    monkeypatch.setattr("herdr_engineering.host_config.host_value", lambda key: "pat-token")
    opener = Opener()
    GitHubIssueSink(opener=opener, credentials=lambda: _AppCreds(enabled=False))(REPO, "t", "b", "k")
    assert opener.requests[0][0].headers["Authorization"] == "Bearer pat-token"


def test_issue_sink_never_falls_back_to_pat_when_enabled_capability_fails(monkeypatch):
    monkeypatch.setattr("herdr_engineering.host_config.host_value", lambda key: "pat-token")
    opener = Opener()
    with pytest.raises(IssueRefused):
        GitHubIssueSink(opener=opener, credentials=lambda: _AppCreds(enabled=False, reason="backoff"))(REPO, "t", "b", "k")
    assert not opener.requests


def test_issue_sink_401_drops_cached_app_tokens():
    creds = _AppCreds()
    with pytest.raises(IssueRefused):
        GitHubIssueSink(opener=Opener(401), credentials=lambda: creds)(REPO, "t", "b", "k")
    assert creds.invalidated == [REPO.split("/")[0]]

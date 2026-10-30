"""The learning ledger: YAML-listed low-risk actions run on their own, everything else asks; failures trip back to ask."""
from datetime import UTC, datetime, timedelta

from herdr_engineering.control_plane import project_intelligence as pi
from herdr_engineering.control_plane.intel_ledger import MAX_AUTO_PER_HOUR, PROMOTE_AFTER, Ledger
from herdr_engineering.control_plane.project_intelligence_api import IntelligenceRoutes

T = datetime(2026, 10, 30, 12, tzinfo=UTC)
REPO = "acme/app"
SECTION = {"enabled": True, "ingest": True, "suggest": True, "execute": True, "default_org_id": "o",
           "webhook_projects": {REPO: ["p1"]}, "assistant_viewers": {"t/u": {"org_id": "o", "projects": "*"}},
           "auto_actions": {"ci_rerun": [REPO]}}
PAYLOAD = {"project": "p1", "repo": REPO, "run": 7}


def rig(section=None, fail=False):
    sec, calls = dict(SECTION if section is None else section), []

    def rerun(repo, run):
        calls.append((repo, run))
        if fail:
            raise PermissionError("no")
    r = IntelligenceRoutes(pi.IntelligenceStore(clock=lambda: T), section_fn=lambda: sec)
    r.ci_rerun_fn = rerun
    return r, sec, calls


def test_policy_is_yaml_only_and_never_widens_to_risky_actions():
    assert Ledger.policy(SECTION, "ci_rerun", REPO) == "auto"
    assert Ledger.policy(SECTION, "ci_rerun", "acme/other") == "ask"
    for action in ("merge", "send_message", "run_shell", "request_ci_repair", "propose_issue"):
        assert Ledger.policy({"auto_actions": {action: [REPO]}}, action, REPO) == "ask"


def test_a_listed_rerun_runs_without_asking_and_is_recorded():
    r, _, calls = rig()
    out = r.assistant_ci_rerun("t", "u", PAYLOAD, "k1")
    assert calls == [(REPO, 7)] and r.board.get(out["id"], "o").state == "executed"
    assert r.board.get(out["id"], "o").approver == "ledger:auto"
    assert r.ledger.status(SECTION)["entries"][0]["auto_runs"] == 1


def test_unlisted_repo_execute_off_and_kill_switch_all_leave_a_proposal():
    for change in ({"auto_actions": {}}, {"execute": False}, {"kill_switch": True}):
        r, _, calls = rig(dict(SECTION, **change))
        out = r.assistant_ci_rerun("t", "u", PAYLOAD, "k")
        assert calls == [] and out["state"] == "awaiting_approval"


def test_a_failed_auto_run_trips_back_to_ask():
    r, _, calls = rig(fail=True)
    r.assistant_ci_rerun("t", "u", PAYLOAD, "k1")
    assert r.ledger.effective(SECTION, "ci_rerun", REPO) == "ask"
    out = r.assistant_ci_rerun("t", "u", dict(PAYLOAD, run=8), "k2")
    assert out["state"] == "awaiting_approval" and len(calls) == 1


def test_auto_runs_are_rate_limited():
    now = [T]
    led = Ledger(clock=lambda: now[0])
    assert all(led.may_auto_now("ci_rerun", REPO) for _ in range(MAX_AUTO_PER_HOUR))
    assert not led.may_auto_now("ci_rerun", REPO)
    now[0] = T + timedelta(hours=1, seconds=1)
    assert led.may_auto_now("ci_rerun", REPO)


def test_clean_approvals_suggest_a_promotion_and_a_failure_resets_it():
    led, ask = Ledger(), {"auto_actions": {}}
    for _ in range(PROMOTE_AFTER):
        led.record("ci_rerun", REPO, "executed")
    assert led.status(ask)["entries"][0]["suggest_promotion"] is True
    led.record("ci_rerun", REPO, "failed")
    assert led.status(ask)["entries"][0]["suggest_promotion"] is False
    for _ in range(PROMOTE_AFTER):
        led.record("link_entities", REPO, "executed")
    assert not [e for e in led.status(ask)["entries"] if e["action"] == "link_entities"][0]["suggest_promotion"]

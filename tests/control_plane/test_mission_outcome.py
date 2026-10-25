"""Durable mission outcome (design 2026-10-24 missions rework, step 1)."""
import pytest

from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.mission_outcome import (
    EVENT,
    MissionOutcomes,
    assemble,
    last_outcome,
    missions_config,
)
from herdr_engineering.control_plane.repo import ControlPlaneRepo

NOW = 1_800_000_000
HOUR = 3600


def _world(**cfg):
    clock = [NOW]
    repo = ControlPlaneRepo(now=lambda: clock[0])
    config = missions_config({"outcomes": cfg} if cfg else None)
    repo.mission_outcomes = MissionOutcomes(repo, config, now=lambda: clock[0])
    return repo, clock


def _outcomes(repo, key):
    return [e for e in repo.stream("mission", key) if e["event_type"] == EVENT]


def _full_mission(repo, key="m1"):
    repo.upsert_mission(key, title="Fix parser", purpose="make the parser faster password=hunter2hunter2", stage="implementation")
    repo.upsert_session("s1", host_id="beast", mission_id=key, agent_role="pi", model="qwen", repository_id="org/app", branch="fix/p")
    repo.upsert_session("s2", host_id="beast", mission_id=key, agent_role="pi")
    repo.upsert_test_run("t1", mission_id=key, status="failed", passed=1, failed=2, skipped=0, total=3)
    repo.upsert_test_run("t2", mission_id=key, status="passed", passed=3, failed=0, skipped=0, total=3)
    repo.upsert_review("r1", mission_id=key, reviewer="codex", status="done", verdict="approved")
    repo.upsert_review("r2", mission_id=key, reviewer="human", status="open")
    repo.upsert_pull_request("pr1", mission_id=key, repository="org/app", number=214, url="https://github.com/org/app/pull/214", status="merged", branch="fix/p")
    repo.observe_ci_pipeline({"repository": "org/app", "provider_repo_id": 7, "run_number": 11, "branch": "fix/p", "state": "failure", "finished_at": NOW + 60})
    repo.observe_ci_pipeline({"repository": "org/app", "provider_repo_id": 7, "run_number": 12, "branch": "fix/p", "state": "success", "finished_at": NOW + 120})
    repo.record_token_usage_row({"schema": 2, "project": "herdr", "model": "qwen", "route": "fabric", "stream": True, "usage_known": True,
                                 "status": 200, "session_id": "s1", "mission_id": key, "input_tokens": 50, "output_tokens": 200,
                                 "ttft_ms": 300, "gen_ms": 4000, "queue_ms": 0, "duration_ms": 4400})


def test_ending_a_mission_writes_one_outcome_assembled_from_what_is_held():
    repo, clock = _world()
    _full_mission(repo)
    clock[0] += 2 * HOUR
    repo.set_mission_stage("m1", "complete")
    [event] = _outcomes(repo, "m1")
    o = event["payload"]
    assert o["schema"] == 1 and o["final_stage"] == "complete" and o["inferred"] is False
    assert o["duration_s"] == 2 * HOUR
    assert "hunter2" not in o["objective"]
    assert {a["session_id"] for a in o["agents"]} == {"s1", "s2"} and o["agent_count"] == 2
    assert o["tests"] == {"runs": 2, "passed": 1, "failed": 1}
    assert o["reviews"] == {"open": 1, "approved": 1, "changes_requested": 0}
    assert o["prs"] == [{"repository": "org/app", "number": 214, "state": "merged", "url": "https://github.com/org/app/pull/214"}]
    assert o["ci"] == [{"repository": "org/app", "branch": "fix/p", "run": 12, "state": "success"}]   # latest per repo and branch
    assert o["usage"] == {"requests": 1, "input_tokens": 50, "output_tokens": 200, "lower_bound": o["usage"]["lower_bound"]}
    assert set(o["sources"]) >= {"mission", "sessions", "tests", "reviews", "prs", "ci", "usage"}
    assert event["event_type"] == EVENT and event["mission_id"] == "m1"


def test_an_unreadable_source_is_null_and_named_by_omission():
    repo, _ = _world()
    _full_mission(repo)
    repo.ci_reports = lambda: (_ for _ in ()).throw(RuntimeError("down"))
    repo.list_reviews = lambda **kw: (_ for _ in ()).throw(RuntimeError("down"))
    o = assemble(repo, "m1")
    assert o["ci"] is None and o["reviews"] is None
    assert "ci" not in o["sources"] and "reviews" not in o["sources"] and "tests" in o["sources"]


def test_nothing_is_invented_for_a_bare_mission():
    repo, _ = _world()
    repo.upsert_mission("bare", title="", stage="planning")
    repo.set_mission_stage("bare", "failed")
    o = _outcomes(repo, "bare")[0]["payload"]
    assert o["final_stage"] == "failed" and o["agents"] == [] and o["tests"] == {"runs": 0, "passed": 0, "failed": 0}
    assert o["usage"]["requests"] == 0 and o["ci"] == [] and o["last_activity"] is None


def test_agents_are_capped_and_the_payload_is_bounded():
    repo, _ = _world(max_agents=3)
    repo.upsert_mission("big", title="t", stage="implementation")
    for i in range(30):
        repo.upsert_session(f"s{i}", host_id="h", mission_id="big", agent_role="pi")
    repo.set_mission_stage("big", "complete")
    o = _outcomes(repo, "big")[0]["payload"]
    assert len(o["agents"]) == 3 and o["agent_count"] == 30


def test_the_write_is_idempotent_per_terminal_transition_and_a_reopen_writes_a_new_one():
    repo, clock = _world()
    repo.upsert_mission("m", title="t", stage="implementation")
    repo.set_mission_stage("m", "complete")
    repo.set_mission_stage("m", "complete")
    repo.set_mission_stage("m", "failed")                 # terminal to terminal: no second write
    assert len(_outcomes(repo, "m")) == 1
    clock[0] += HOUR
    repo.set_mission_stage("m", "implementation")         # restarts
    repo.set_mission_stage("m", "failed")
    events = _outcomes(repo, "m")
    assert [e["payload"]["final_stage"] for e in events] == ["complete", "failed"]
    assert last_outcome(repo, "m")["payload"]["final_stage"] == "failed"


def test_upsert_path_writes_too_and_namespaces_stay_exact():
    repo, _ = _world()
    repo.upsert_mission("X1", title="herdr one", stage="implementation")
    repo.upsert_mission("X1", title="pi one", stage="implementation", source="pi")
    repo.upsert_mission("X1", title="pi one", stage="complete", source="pi")
    assert len(_outcomes(repo, "pi:X1")) == 1 and _outcomes(repo, "X1") == []


def test_stale_close_marks_unknown_and_inferred_never_success_and_only_once():
    repo, clock = _world(stale_close_hours=72)
    repo.upsert_mission("quiet", title="quiet", stage="implementation")
    repo.upsert_mission("busy", title="busy", stage="implementation")
    clock[0] += 71 * HOUR
    repo.upsert_session("sb", host_id="h", mission_id="busy", agent_role="pi")
    assert repo.mission_outcomes.sweep(force=True) == 0           # nothing is 72 h silent yet
    clock[0] += 2 * HOUR
    assert repo.mission_outcomes.sweep(force=True) == 1           # "quiet" only; "busy" had a session 2 h ago
    [event] = _outcomes(repo, "quiet")
    assert event["payload"]["final_stage"] == "unknown" and event["payload"]["inferred"] is True
    assert event["payload"]["duration_s"] is None and _outcomes(repo, "busy") == []
    assert repo.mission_outcomes.sweep(force=True) == 0           # already closed
    repo.set_mission_stage("quiet", "implementation")             # it comes back to life...
    assert repo.mission_outcomes.sweep(force=True) == 0           # ...and is not closed again straight away


def test_the_sweep_is_throttled():
    repo, clock = _world()
    repo.upsert_mission("q", title="q", stage="implementation")
    clock[0] += 100 * HOUR
    assert repo.mission_outcomes.sweep() == 1
    repo.upsert_mission("q2", title="q2", stage="implementation")
    for stamp in ("created_at", "updated_at"):
        repo._missions["q2"][stamp] = "2020-01-01T00:00:00+00:00"
    repo._events[("mission", "q2")][-1]["source_timestamp"] = "2020-01-01T00:00:00+00:00"
    clock[0] += 60
    assert repo.mission_outcomes.sweep() == 0   # inside the throttle window
    clock[0] += 400
    assert repo.mission_outcomes.sweep() == 1


def test_the_event_replays_after_a_restart():
    repo, _ = _world()
    repo.upsert_mission("m", title="t", stage="implementation")
    repo.set_mission_stage("m", "complete")
    fresh = ControlPlaneRepo()
    fresh.import_state(repo.export_state())
    assert last_outcome(fresh, "m")["payload"]["final_stage"] == "complete"


def test_disabled_writes_nothing_and_a_failing_assembly_never_breaks_the_stage_change():
    repo, _ = _world(enabled=False)
    repo.upsert_mission("m", title="t", stage="implementation")
    repo.set_mission_stage("m", "complete")
    assert _outcomes(repo, "m") == []
    repo2, _ = _world()
    repo2.upsert_mission("m", title="t", stage="implementation")
    repo2.mission_outcomes._write = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    repo2.set_mission_stage("m", "complete")
    assert repo2.get_mission("m")["stage"] == "complete"


@pytest.mark.parametrize("raw,message", [
    ("x", "must be a mapping"), ({"bogus": 1}, "unknown keys"), ({"outcomes": {"enabled": "yes"}}, "true or false"),
    ({"outcomes": {"stale_close_hours": 0}}, "stale_close_hours"), ({"outcomes": {"max_agents": 99}}, "max_agents"),
    ({"story": {"max_events": 1}}, "max_events"), ({"outcomes": {"nope": 1}}, "unknown keys"),
])
def test_config_is_validated(raw, message):
    with pytest.raises(ValueError, match=message):
        missions_config(raw)


def test_config_defaults():
    c = missions_config(None)
    assert c["outcomes"] == {"enabled": True, "stale_close_hours": 72, "max_agents": 12}
    assert c["story"] == {"max_events": 200, "wait_span_seconds": 300}


def test_the_server_wires_outcomes_by_default_and_honours_yaml():
    repo = ControlPlaneRepo()
    svr = ControlPlaneServer(repo, host="127.0.0.1", port=0, allow_open=True)
    assert repo.mission_outcomes is svr.mission_outcomes and svr.mission_outcomes.enabled
    repo.upsert_mission("m", title="t", stage="implementation")
    repo.set_mission_stage("m", "complete")
    assert len(_outcomes(repo, "m")) == 1
    off = ControlPlaneRepo()
    ControlPlaneServer(off, host="127.0.0.1", port=0, allow_open=True, missions={"outcomes": {"enabled": False}})
    off.upsert_mission("m", title="t", stage="implementation")
    off.set_mission_stage("m", "complete")
    assert _outcomes(off, "m") == []

# ruff: noqa: F811  (the public-listener fixture is imported by name)
"""Mission story API (design 2026-10-24 missions rework, step 2)."""
import json
import threading
import urllib.error
import urllib.request
from datetime import UTC, datetime

import pytest

from herdr_engineering.control_plane import mission_story
from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.attention_inbox import AttentionInbox
from herdr_engineering.control_plane.mission_outcome import MissionOutcomes, missions_config
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from tests.control_plane.test_collab_public import stack  # noqa: F401
from tests.control_plane.test_mission_outcome import HOUR, NOW, _full_mission


def _iso(epoch):
    return datetime.fromtimestamp(epoch, UTC).isoformat()


def _world(**cfg):
    clock = [NOW]
    repo = ControlPlaneRepo(now=lambda: clock[0])
    config = missions_config({"outcomes": cfg} if cfg else None)
    outcomes = MissionOutcomes(repo, config, now=lambda: clock[0])
    repo.mission_outcomes = outcomes
    inbox = AttentionInbox(repo, ci_snapshot=lambda: {"status": "ok", "pipelines": {}}, now=lambda: clock[0])
    return repo, clock, outcomes, inbox, config


def _story(world, key="m1"):
    repo, _, outcomes, inbox, config = world
    return mission_story.build(repo, key, outcomes=outcomes, inbox=inbox, config=config)


def test_an_ended_mission_tells_its_story_from_the_stored_outcome():
    world = _world()
    repo, clock = world[0], world[1]
    _full_mission(repo)
    clock[0] += 2 * HOUR
    repo.set_mission_stage("m1", "complete")
    story = _story(world)
    h = story["header"]
    assert h["result"] == "succeeded" and h["duration_s"] == 2 * HOUR and h["agent_count"] == 2
    assert h["repository"] == "org/app" and h["branch"] == "fix/p" and h["hosts"] == ["beast"] and h["tokens_out"] == 200
    assert h["summary"] == {"pr": {"number": 214, "state": "merged"}, "ci": "success", "tests": {"runs": 2, "passed": 1, "failed": 1}}
    assert h["recorded_before_outcomes"] is False and h["inferred"] is False
    assert story["outcome"]["recorded"] is True and story["outcome"]["live"] is False
    assert {a["session_id"] for a in story["agents"]} == {"s1", "s2"} and story["usage"]["requests"] == 1
    kinds = [e["kind"] for e in story["events"]]
    for kind in ("start", "stage", "agent", "tests", "review", "pr", "ci", "end"):
        assert kind in kinds, kind
    assert [e["ts"] for e in story["events"]] == sorted(e["ts"] for e in story["events"])
    assert story["events"][-1]["text"] == "Ended: complete" or story["events"][-1]["kind"] in ("end", "stage", "agent")
    assert story["partial"] == []


def test_a_running_mission_assembles_the_same_shape_live():
    world = _world()
    repo, clock = world[0], world[1]
    _full_mission(repo)
    clock[0] += HOUR
    ended = _story(world)
    assert ended["header"]["result"] == "running" and ended["outcome"] == {**ended["outcome"], "recorded": False, "live": True}
    assert ended["header"]["duration_s"] == HOUR and ended["header"]["ended_at"] is None
    repo.set_mission_stage("m1", "complete")
    done = _story(world)
    assert set(ended) == set(done)
    assert set(ended["outcome"]) - {"recorded", "live"} == set(done["outcome"]) - {"recorded", "live"}


def test_a_mission_that_ended_before_outcomes_were_kept_says_so():
    world = _world()
    repo = world[0]
    repo.upsert_mission("old", title="old", stage="implementation")
    repo.mission_outcomes = None            # the stage change happens before outcomes exist
    repo.set_mission_stage("old", "complete")
    repo.mission_outcomes = world[2]
    story = _story(world, "old")
    assert story["header"]["recorded_before_outcomes"] is True and story["header"]["result"] == "succeeded"
    assert story["outcome"]["recorded"] is False


def test_failed_cancelled_and_stale_results():
    world = _world(stale_close_hours=1)
    repo, clock = world[0], world[1]
    for key, stage in (("f", "failed"), ("c", "cancelled")):
        repo.upsert_mission(key, title=key, stage="implementation")
        repo.set_mission_stage(key, stage)
    repo.upsert_mission("q", title="q", stage="implementation")
    clock[0] += 3 * HOUR
    repo.mission_outcomes.sweep(force=True)
    assert _story(world, "f")["header"]["result"] == "failed"
    assert _story(world, "c")["header"]["result"] == "cancelled"
    # stale-closed, but still reported as unknown, never success
    repo.mission_outcomes.on_stage  # noqa: B018
    q = _story(world, "q")
    assert q["header"]["result"] == "running"          # the mission record itself never reported an end


def test_a_reopened_mission_shows_running_not_the_old_outcome():
    world = _world()
    repo = world[0]
    repo.upsert_mission("m1", title="t", stage="implementation")
    repo.set_mission_stage("m1", "complete")
    repo.set_mission_stage("m1", "implementation")
    s = _story(world)
    assert s["header"]["result"] == "running" and s["outcome"]["recorded"] is False


def test_needs_come_from_the_attention_derivation():
    world = _world()
    repo = world[0]
    _full_mission(repo)
    repo._sessions["s1"]["status"] = "failed"
    story = _story(world)
    assert [n["kind"] for n in story["needs"]] == ["agent_failed"] and story["needs"][0]["href"].startswith("#/herd/session/")
    other = ControlPlaneRepo()
    other.upsert_mission("m2", title="m2", stage="implementation")
    other.upsert_session("sx", host_id="h", agent_role="pi")
    other._sessions["sx"]["status"] = "failed"        # an unrelated failed agent is not this mission's need
    assert mission_story.build(other, "m2", outcomes=MissionOutcomes(other, missions_config(None)),
                               inbox=AttentionInbox(other, ci_snapshot=lambda: {"status": "ok", "pipelines": {}}))["needs"] == []


def test_a_missing_attention_source_is_partial_not_empty():
    repo = ControlPlaneRepo()
    repo.upsert_mission("m", title="t", stage="implementation")
    story = mission_story.build(repo, "m", outcomes=None, inbox=None)
    assert "attention" in story["partial"]


def test_events_are_capped_newest_kept_with_a_more_count():
    world = _world(**{})
    repo = world[0]
    repo.upsert_mission("m1", title="t", stage="implementation")
    for i in range(60):
        repo.upsert_session(f"s{i}", host_id="h", mission_id="m1", agent_role="pi")
    story = mission_story.build(repo, "m1", outcomes=world[2], inbox=world[3], config={"story": {"max_events": 10, "wait_span_seconds": 300}})
    assert len(story["events"]) == 10 and story["more"] > 0


def test_a_long_wait_is_one_span_event_and_a_short_one_is_not():
    world = _world()
    repo = world[0]
    repo.upsert_mission("pi-w", title="w", stage="implementation", source="pi")
    repo.mission_activity.ingest("pi:pi-w", {
        "contract_version": 1, "host_id": "h", "instance_id": "i", "session_id": "s", "mission_revision": 1, "connection": "connected",
        "observed_at": _iso(NOW), "snapshot_at": _iso(NOW), "mission": {"id": "pi-w", "title": "w", "goal": "ship it", "revision": 1},
        "activity": {"currentObjective": "ship it", "currentActivity": "waiting on approval", "waitingReason": "approval of the plan",
                     "waitingSince": _iso(NOW - 900)}}, source="pi")
    story = _story(world, "pi:pi-w")
    waits = [e for e in story["events"] if e["kind"] == "wait"]
    assert len(waits) == 1 and waits[0]["span_s"] >= 900 and "approval of the plan" in waits[0]["text"]
    assert story["objective"] == "ship it"


def test_untrusted_text_is_redacted_and_bounded():
    world = _world()
    repo = world[0]
    repo.upsert_mission("m1", title="t" * 500, purpose="token=abcdef123456789 hello", stage="implementation")
    repo.upsert_session("s", host_id="h", mission_id="m1", agent_role="x" * 500)
    story = _story(world)
    assert len(story["header"]["title"]) <= 180 and "abcdef123456789" not in json.dumps(story)
    assert all(len(e["text"]) <= 200 for e in story["events"])


def test_pi_and_herdr_missions_with_one_raw_id_stay_separate():
    world = _world()
    repo = world[0]
    repo.upsert_mission("X", title="herdr x", stage="implementation")
    repo.upsert_mission("X", title="pi x", stage="implementation", source="pi")
    repo.upsert_session("sp", host_id="h", mission_id="X", agent_role="pi", source="pi")
    assert _story(world, "pi:X")["header"]["agent_count"] == 1 and _story(world, "X")["header"]["agent_count"] == 0
    assert _story(world, "nope") is None


def _serve(repo, **kw):
    svr = ControlPlaneServer(repo, host="127.0.0.1", port=0, **kw)
    threading.Thread(target=svr.serve, daemon=True).start()
    svr.wait_until_ready()
    return svr


def _get(svr, path):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{svr.port}{path}", timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def test_route_on_the_operator_listener():
    repo = ControlPlaneRepo()
    _full_mission(repo)
    svr = _serve(repo, allow_open=True)
    try:
        repo.set_mission_stage("m1", "complete")
        st, body = _get(svr, "/api/v1/missions/m1/story")
        assert st == 200 and body["header"]["result"] == "succeeded" and body["outcome"]["recorded"] is True
        assert _get(svr, "/api/v1/missions/nope/story")[0] == 404
        assert _get(svr, "/api/v1/missions/m1")[0] == 200 and _get(svr, "/api/v1/missions/m1/timeline")[0] == 200   # unchanged
    finally:
        svr.shutdown()


@pytest.mark.parametrize("path", ["/api/v1/missions/m1/story", "/api/v1/missions"])
def test_story_does_not_exist_on_the_public_listener(stack, path):
    status, _ = stack.pub.call("GET", path)
    assert status == 404


def test_outcome_summaries_feed_the_catalog_rows():
    from herdr_engineering.control_plane.mission_outcome import outcome_summaries
    world = _world(stale_close_hours=1)
    repo, clock = world[0], world[1]
    _full_mission(repo)
    clock[0] += HOUR
    repo.set_mission_stage("m1", "complete")
    repo.upsert_mission("bad", title="bad", stage="implementation")
    repo.set_mission_stage("bad", "failed")
    repo.upsert_mission("quiet", title="quiet", stage="implementation")
    repo.upsert_mission("running", title="running", stage="implementation")
    clock[0] += 3 * HOUR
    repo.mission_outcomes.sweep(force=True)
    out = outcome_summaries(repo)
    assert out["m1"] == {"result": "succeeded", "final_stage": "complete", "inferred": False, "ended_at": out["m1"]["ended_at"],
                         "duration_s": HOUR, "agent_count": 2, "repository": "org/app", "last_event": "PR #214 merged"}
    assert out["bad"]["result"] == "failed" and out["bad"]["last_event"] == "Ended: failed"
    assert out["quiet"]["result"] == "unknown" and out["quiet"]["inferred"] is True
    repo.set_mission_stage("m1", "implementation")                 # reopened: its old outcome no longer describes it
    assert "m1" not in outcome_summaries(repo)


def test_catalog_outcomes_route_and_public_404(stack):
    repo = ControlPlaneRepo()
    repo.upsert_mission("m", title="t", stage="implementation")
    svr = _serve(repo, allow_open=True)
    try:
        repo.set_mission_stage("m", "complete")
        st, body = _get(svr, "/api/v1/mission-outcomes")
        assert st == 200 and body["outcomes"]["m"]["result"] == "succeeded"
    finally:
        svr.shutdown()
    assert stack.pub.call("GET", "/api/v1/mission-outcomes")[0] == 404

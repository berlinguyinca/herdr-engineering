"""G1: GET /api/v1/work/weekly-events, a read-only cross-entity lifecycle window over the real repo."""
import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime

import pytest

from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.repo import ControlPlaneRepo


def epoch(iso):
    return datetime.fromisoformat(iso).astimezone(UTC).timestamp()


class Clock:
    def __init__(self, iso="2026-11-02T00:00:00+00:00"):
        self.t = epoch(iso)

    def __call__(self):
        return self.t

    def at(self, iso):
        self.t = epoch(iso)


def serve(repo, work_center=True, keys=None):
    server = ControlPlaneServer(repo, api_keys=keys or {"reader": "viewer", "writer": "ingest"},
                                work_center_config={"enabled": True} if work_center else None)
    thread = threading.Thread(target=server.serve, daemon=True)
    thread.start()
    server.wait_until_ready()
    return server, thread


@pytest.fixture
def world():
    clock = Clock()
    repo = ControlPlaneRepo(now=clock)
    server, thread = serve(repo)

    def get(query="", key="reader", path="/api/v1/work/weekly-events"):
        headers = {"X-API-Key": key} if key else {}
        request = urllib.request.Request(f"http://127.0.0.1:{server.port}{path}?{query}", headers=headers)
        try:
            response = urllib.request.urlopen(request)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, json.load(response)

    try:
        yield repo, clock, get
    finally:
        server.shutdown()
        thread.join(timeout=5)


WEEK = "since=2026-11-02T00:00:00Z&until=2026-11-09T00:00:00Z"


def kinds(body):
    return sorted((e["work_id"], e["kind"]) for e in body["events"])


def test_started_and_finished_missions_in_window(world):
    repo, clock, get = world
    clock.at("2026-11-03T10:00:00+00:00")
    repo.upsert_mission("m1", title="One", stage="implementation", source="pi")
    clock.at("2026-11-04T10:00:00+00:00")
    repo.set_mission_stage("pi:m1", "complete", source="herdr")
    status, body = get(WEEK)
    assert status == 200
    assert kinds(body) == [("mission:pi:m1", "finished"), ("mission:pi:m1", "started")]
    finished = next(e for e in body["events"] if e["kind"] == "finished")
    assert finished["occurred_at"] == "2026-11-04T10:00:00Z"
    assert finished["evidence"]["ref"] and finished["evidence"]["event_type"] == "MissionStageChanged"


def test_window_is_half_open(world):
    repo, clock, get = world
    clock.at("2026-11-02T00:00:00+00:00")           # exactly since: included
    repo.upsert_mission("edge-start", title="a", source="pi")
    clock.at("2026-11-09T00:00:00+00:00")           # exactly until: excluded
    repo.upsert_mission("edge-end", title="b", source="pi")
    clock.at("2026-11-01T23:59:59+00:00")           # just before since
    repo.upsert_mission("before", title="c", source="pi")
    _, body = get(WEEK)
    assert kinds(body) == [("mission:pi:edge-start", "started")]


def test_repeated_updates_do_not_duplicate_lifecycle(world):
    repo, clock, get = world
    clock.at("2026-11-03T10:00:00+00:00")
    repo.upsert_mission("m1", title="One", stage="implementation", source="pi")
    for hour in (11, 12, 13):                       # heartbeats
        clock.at(f"2026-11-03T{hour}:00:00+00:00")
        repo.upsert_mission("m1", title="One", stage="implementation", source="pi")
    clock.at("2026-11-04T10:00:00+00:00")
    repo.upsert_mission("m1", title="One", stage="complete", source="pi")
    clock.at("2026-11-05T10:00:00+00:00")
    repo.upsert_mission("m1", title="One", stage="complete", source="pi")   # still complete
    _, body = get(WEEK)
    assert kinds(body) == [("mission:pi:m1", "finished"), ("mission:pi:m1", "started")]
    assert next(e for e in body["events"] if e["kind"] == "finished")["occurred_at"] == "2026-11-04T10:00:00Z"


def test_cross_entity_sessions_and_missions_with_reassociation(world):
    repo, clock, get = world
    clock.at("2026-11-03T09:00:00+00:00")
    repo.upsert_mission("m1", title="One", source="pi")
    repo.upsert_session("s-owned", host_id="h1", mission_id="pi:m1", source="pi")
    repo.upsert_session("s-free", host_id="h1")
    _, body = get(WEEK)
    by_id = {e["work_id"]: e for e in body["events"]}
    assert "mission:pi:m1" in by_id
    assert by_id["session:h1/s-free"]["kind"] == "started"
    assert by_id["session:h1/s-free"].get("mission_key") is None
    assert by_id["session:h1/s-owned"]["mission_key"] == "pi:m1"   # client drops it: owned by the mission


def test_session_finished_from_terminal_status(world):
    repo, clock, get = world
    clock.at("2026-11-03T09:00:00+00:00")
    repo.upsert_session("s1", host_id="h1")
    clock.at("2026-11-03T11:00:00+00:00")
    repo.upsert_session("s1", host_id="h1", status="completed")
    _, body = get(WEEK)
    assert kinds(body) == [("session:h1/s1", "finished"), ("session:h1/s1", "started")]


def test_quota_pause_only_with_evidence(world):
    repo, clock, get = world
    clock.at("2026-11-03T09:00:00+00:00")
    repo.upsert_mission("m1", title="One", source="pi")
    repo.upsert_mission("m2", title="Two", source="pi")
    clock.at("2026-11-03T10:00:00+00:00")
    repo.record_event("mission", "pi:m1", "MissionUpdated", {"stage": "implementation", "status": "PAUSED_QUOTA"},
                      mission_id="m1")
    _, body = get(WEEK)
    paused = [e for e in body["events"] if e["kind"] == "paused_quota"]
    assert [e["work_id"] for e in paused] == ["mission:pi:m1"]
    assert paused[0]["evidence"]["event_type"] == "MissionUpdated"


def test_coverage_is_never_complete_and_reports_horizon(world):
    repo, clock, get = world
    _, empty = get(WEEK)
    assert empty["coverage"]["complete"] is False and empty["coverage"]["horizon"] is None
    clock.at("2026-10-20T00:00:00+00:00")
    repo.upsert_mission("old", title="old", source="pi")
    clock.at("2026-11-03T00:00:00+00:00")
    repo.upsert_mission("new", title="new", source="pi")
    _, body = get(WEEK)
    cov = body["coverage"]
    assert cov["complete"] is False                       # emission is not provable, even though horizon < since
    assert cov["horizon"] == "2026-10-20T00:00:00Z"
    assert cov["truncated"] is False
    assert cov["reasons"], "an incomplete answer must say why"
    _, late = get("since=2026-10-01T00:00:00Z&until=2026-10-08T00:00:00Z")
    assert late["coverage"]["complete"] is False and late["events"] == []


def test_limit_truncates_and_pages(world):
    repo, clock, get = world
    for i in range(5):
        clock.at(f"2026-11-03T0{i}:00:00+00:00")
        repo.upsert_mission(f"m{i}", title="t", source="pi")
    status, first = get(WEEK + "&limit=2")
    assert status == 200 and len(first["events"]) == 2
    assert first["coverage"]["truncated"] is True and first["next_cursor"]
    seen = [e["work_id"] for e in first["events"]]
    cursor = first["next_cursor"]
    while cursor:
        _, page = get(WEEK + "&limit=2&" + urllib.parse.urlencode({"after": cursor}))
        seen += [e["work_id"] for e in page["events"]]
        cursor = page.get("next_cursor")
        assert page["coverage"]["truncated"] is bool(cursor)
    assert seen == [f"mission:pi:m{i}" for i in range(5)]


@pytest.mark.parametrize("query", [
    "", "since=2026-11-02T00:00:00Z", "until=2026-11-09T00:00:00Z",
    "since=2026-11-02&until=2026-11-09",                              # no zone
    "since=2026-11-09T00:00:00Z&until=2026-11-02T00:00:00Z",          # inverted
    "since=2026-11-02T00:00:00Z&until=2026-11-02T00:00:00Z",          # empty
    "since=2026-01-01T00:00:00Z&until=2026-12-31T00:00:00Z",          # span > 31 days
    WEEK + "&limit=0", WEEK + "&limit=501", WEEK + "&limit=abc", WEEK + "&limit=1&limit=2",
    WEEK + "&after=!!", WEEK + "&since=2026-11-01T00:00:00Z",
])
def test_bad_parameters_are_400(world, query):
    status, body = world[2](query)
    assert status == 400 and "error" in body


def test_zone_offsets_are_honoured(world):
    repo, clock, get = world
    clock.at("2026-11-02T00:30:00+00:00")
    repo.upsert_mission("m1", title="One", source="pi")
    # 2026-11-01T17:00-08:00 == 2026-11-02T01:00Z: the 00:30Z start is before it
    _, body = get("since=2026-11-01T17:00:00-08:00&until=2026-11-08T17:00:00-08:00")
    assert body["events"] == []
    _, body = get("since=2026-11-01T16:00:00-08:00&until=2026-11-08T16:00:00-08:00")
    assert len(body["events"]) == 1


def test_auth_matches_events_endpoint(world):
    repo, clock, get = world
    ev = "/api/v1/events"
    for key in (None, "wrong"):
        assert get(WEEK, key=key)[0] == get("entity_type=mission&entity_id=x", key=key, path=ev)[0] == 401
    for key in ("reader", "writer"):                                     # roles allowed to read events can read this
        assert get(WEEK, key=key)[0] == get("entity_type=mission&entity_id=x", key=key, path=ev)[0] == 200


def test_post_is_not_supported(world):
    repo, clock, get = world
    status, _ = get(WEEK, path="/api/v1/work/weekly-events/extra")
    assert status == 404


def test_flag_off_is_not_found():
    repo = ControlPlaneRepo()
    server, thread = serve(repo, work_center=False)
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.port}/api/v1/work/weekly-events?{WEEK}", headers={"X-API-Key": "reader"})
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(request)
        assert err.value.code == 404
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_response_leaks_no_payload_fields(world):
    repo, clock, get = world
    clock.at("2026-11-03T09:00:00+00:00")
    repo.upsert_mission("m1", title="secret title", purpose="secret purpose", cwd="/home/x", source="pi")
    _, body = get(WEEK)
    raw = json.dumps(body)
    assert "secret" not in raw and "/home/x" not in raw

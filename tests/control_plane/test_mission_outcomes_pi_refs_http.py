"""G9: GET /api/v1/mission-outcomes with NON-EMPTY issue_refs/pr_refs from Pi links, through the real server, real
HTTP ingest and the real store. No mocks. Server-side there is no switch on this route: the work_center_v1 flag is
advertised in /health and read by the dashboard; the route itself is additive and always answers (same as G3)."""
import json
import threading
import urllib.error
import urllib.request
from datetime import UTC, datetime

import pytest

from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.repo import ControlPlaneRepo

KEYS = {"viewer-k": "viewer", "ingest-k": "ingest"}


def _call(server, route, key=None, body=None):
    headers = {"X-API-Key": key} if key else {}
    data = None
    if body is not None:
        data, headers["Content-Type"] = json.dumps(body).encode(), "application/json"
    req = urllib.request.Request(f"http://127.0.0.1:{server.port}{route}", data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, None


def _pi_snapshot(server, mission_id, links):
    now = datetime.now(UTC).isoformat()
    status, body = _call(server, "/api/v1/ingest", "ingest-k", {
        "entity_type": "mission_activity", "entity_id": f"pi:{mission_id}", "source": "pi",
        "event_type": "mission_activity.snapshot",
        "payload": {"contract_version": 1, "host_id": "h", "instance_id": "i", "session_id": "s", "observed_at": now,
                    "snapshot_at": now, "mission_revision": 1, "connection": "connected",
                    "mission": {"id": mission_id, "title": "t", "status": "RUNNING", "links": links},
                    "activity": {"health": "active"}}})
    assert status == 200 and body["entity"]["accepted"] is True, body


@pytest.fixture
def served():
    repo = ControlPlaneRepo()
    server = ControlPlaneServer(repo, host="127.0.0.1", port=0, api_keys=KEYS)
    threading.Thread(target=server.serve, daemon=True).start()
    server.wait_until_ready()
    # A Herdr-recorded mission with its own PR record plus Pi links to the same PR and an issue.
    repo.upsert_mission("MSN-1", title="one", stage="implementation", source="pi")
    repo.upsert_pull_request("p1", mission_id="pi:MSN-1", repository="acme/app", number=9, status="open")
    _pi_snapshot(server, "MSN-1", [
        {"kind": "issue", "repository": "acme/app", "number": 5},
        {"kind": "pull_request", "repository": "acme/app", "number": 9},      # duplicate of the Herdr record
        {"kind": "pull_request", "repository": "acme/app", "number": 11},
        {"kind": "branch", "repository": "acme/app", "number": 1},            # unknown kind: dropped
        {"kind": "issue", "repository": "acme/app", "number": 0}])            # invalid number: dropped
    # A Pi-only mission (no Herdr record) sharing issue 5, and one with its own issue.
    _pi_snapshot(server, "MSN-2", [{"kind": "issue", "repository": "acme/app", "number": 5},
                                   {"kind": "issue", "repository": "acme/app", "number": 6}])
    yield server
    server.shutdown()


def test_non_empty_issue_and_pr_refs_over_http(served):
    status, body = _call(served, "/api/v1/mission-outcomes", "viewer-k")
    assert status == 200 and set(body) == {"outcomes", "pr_refs", "issue_refs"}
    assert body["issue_refs"] == {
        "pi:MSN-1": [{"repository": "acme/app", "number": 5}],
        "pi:MSN-2": [{"repository": "acme/app", "number": 5}, {"repository": "acme/app", "number": 6}]}
    assert body["pr_refs"] == {"pi:MSN-1": [{"repository": "acme/app", "number": 9}, {"repository": "acme/app", "number": 11}]}
    assert body["outcomes"] == {}   # both missions are still running: refs do not make them ended


def test_issue_five_is_carried_by_two_missions_so_the_client_can_call_it_ambiguous(served):
    _, body = _call(served, "/api/v1/mission-outcomes", "viewer-k")
    carriers = [k for k, refs in body["issue_refs"].items() if {"repository": "acme/app", "number": 5} in refs]
    assert sorted(carriers) == ["pi:MSN-1", "pi:MSN-2"]
    assert [k for k, refs in body["issue_refs"].items() if {"repository": "acme/app", "number": 6} in refs] == ["pi:MSN-2"]


@pytest.mark.parametrize("key", [None, "wrong", ""])
def test_unauthenticated_requests_get_401_and_no_refs(served, key):
    assert _call(served, "/api/v1/mission-outcomes", key) == (401, None)


def test_work_center_flag_off_does_not_change_the_route_and_is_not_advertised(served):
    status, health = _call(served, "/api/v1/health", "viewer-k")
    assert status == 200 and "work_center_v1" not in health["capabilities"]
    status, body = _call(served, "/api/v1/mission-outcomes", "viewer-k")
    assert status == 200 and body["issue_refs"] and body["pr_refs"]   # always present, additive; the dashboard decides whether to read it


def test_flag_on_advertises_the_capability_and_returns_the_same_refs():
    repo = ControlPlaneRepo()
    server = ControlPlaneServer(repo, host="127.0.0.1", port=0, api_keys=KEYS, work_center_config={"enabled": True})
    threading.Thread(target=server.serve, daemon=True).start()
    server.wait_until_ready()
    try:
        _pi_snapshot(server, "MSN-7", [{"kind": "issue", "repository": "acme/app", "number": 7}])
        assert "work_center_v1" in _call(server, "/api/v1/health", "viewer-k")[1]["capabilities"]
        assert _call(server, "/api/v1/mission-outcomes", "viewer-k")[1]["issue_refs"] == {"pi:MSN-7": [{"repository": "acme/app", "number": 7}]}
    finally:
        server.shutdown()

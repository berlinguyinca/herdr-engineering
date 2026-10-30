"""ai_sync ingest + GET /api/v1/ai-sync: latest read-only config-sync status per host."""
import json
import threading
import urllib.error
import urllib.request

import pytest

from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.ingest import INGEST_ENTITY_TYPES
from herdr_engineering.control_plane.repo import ControlPlaneRepo


def payload(host="h1", **over):
    base = {"host_id": host, "ai_sync_version": "0.2.0", "envs_enabled": ["claude", "pi"],
            "last_sync_ts": "2026-10-30T10:00:00+00:00", "ahead": 1, "behind": 0, "branch": "main",
            "drifted_files_count": 2, "drift_sample": ["claude/CLAUDE.md", "claude/commands/sync.md"],
            "skipped_links_count": 0, "last_error": None, "checked_at": "2026-10-30T10:05:00+00:00"}
    return {**base, **over}


def envelope(host="h1", body=None, **over):
    return {"entity_type": "ai_sync", "entity_id": host, "event_type": "status",
            "payload": payload(host) if body is None else body, **over}


@pytest.fixture()
def server(acknowledged_sink):
    repo = ControlPlaneRepo(sink=acknowledged_sink)
    svr = ControlPlaneServer(repo, scheduler_admission=True)
    threading.Thread(target=svr.serve, daemon=True).start()
    svr.wait_until_ready()
    base = f"http://127.0.0.1:{svr.port}"

    def post(body):
        req = urllib.request.Request(base + "/api/v1/ingest", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def get(path="/api/v1/ai-sync"):
        try:
            with urllib.request.urlopen(base + path) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    yield post, get, repo
    svr.shutdown()


def test_ai_sync_is_an_ingest_entity_type():
    assert "ai_sync" in INGEST_ENTITY_TYPES


def test_ingest_then_get_returns_latest_per_host(server):
    post, get, _ = server
    assert get() == (200, {"hosts": []})
    assert post(envelope("beta"))[0] == 200
    assert post(envelope("alpha"))[0] == 200
    assert post(envelope("alpha", payload("alpha", ahead=3, checked_at="2026-10-30T11:00:00+00:00")))[0] == 200
    status, body = get()
    assert status == 200
    assert [h["host_id"] for h in body["hosts"]] == ["alpha", "beta"]
    alpha = body["hosts"][0]
    assert alpha["ahead"] == 3 and alpha["checked_at"] == "2026-10-30T11:00:00+00:00"
    assert alpha["envs_enabled"] == ["claude", "pi"] and alpha["drift_sample"][0] == "claude/CLAUDE.md"


def test_get_can_filter_one_host(server):
    post, get, _ = server
    post(envelope("alpha"))
    post(envelope("beta"))
    assert [h["host_id"] for h in get("/api/v1/ai-sync?host_id=beta")[1]["hosts"]] == ["beta"]


@pytest.mark.parametrize("mutate", [
    lambda p: {**p, "host_id": "other"},                         # payload must describe the entity
    lambda p: {**p, "file_contents": "secret"},                  # unknown key: nothing free-form is stored
    lambda p: {**p, "drift_sample": [f"f{n}" for n in range(21)]},
    lambda p: {**p, "drift_sample": ["a" * 301]},
    lambda p: {**p, "drift_sample": "claude/CLAUDE.md"},
    lambda p: {**p, "ahead": -1},
    lambda p: {**p, "ahead": "1"},
    lambda p: {**p, "drifted_files_count": True},
    lambda p: {**p, "envs_enabled": ["a"] * 65},
    lambda p: {**p, "envs_enabled": [1]},
    lambda p: {**p, "last_error": "x" * 501},
    lambda p: {**p, "ai_sync_version": 2},
    lambda p: {k: v for k, v in p.items() if k != "checked_at"},
])
def test_malformed_status_is_a_400_and_stores_nothing(server, mutate):
    post, get, _ = server
    status, body = post(envelope("h1", mutate(payload("h1"))))
    assert status == 400, body
    assert get() == (200, {"hosts": []})


def test_only_the_status_event_type_is_accepted(server):
    post, _, _ = server
    assert post({**envelope("h1"), "event_type": "pull"})[0] == 400


def test_status_survives_event_reload(server, acknowledged_sink):
    post, _, _ = server
    post(envelope("h1"))
    assert [e["entity_type"] for e in acknowledged_sink.events] == ["ai_sync"]
    fresh = ControlPlaneRepo()
    for event in acknowledged_sink.events:
        fresh.record_event(event["entity_type"], event["entity_id"], event["event_type"], event["payload"])
    assert [h["host_id"] for h in fresh.list_ai_sync()] == ["h1"]

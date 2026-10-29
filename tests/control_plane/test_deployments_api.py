"""Deployments read API: GET /api/v1/deployments and the coordinator tick (deploy-4 Task 4)."""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import urllib.error
import urllib.request

import pytest
import yaml

from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.deployment_coordinator import ReleaseEvidence
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from herdr_engineering.fleet_config import FleetConfigStore

from .test_deployment_definitions import C1, CI, STAGE1, _write

HIVE_TOKEN = "placeholder-host-hive"
HOSTILE = '<img src=x onerror="window.__pwned=1">'


def _sha(t):
    return hashlib.sha256(t.encode()).hexdigest()


def _server(tmp_path, control_plane, sink=None, *, loop=False, ci=None):
    """A served control plane. The tick loop is off unless ``loop``: tests tick by hand, so a
    background tick can never land mid-test (or reach the real CI poller before ``ci`` is set)."""
    path = tmp_path / "fleet.yaml"
    path.write_text(yaml.safe_dump({
        "version": 1,
        "sections": ({"control_plane": {"roles": ["control"], "values": control_plane}}
                     if control_plane is not None else {}),
        "hosts": {"hive": {"roles": ["node"], "token_sha256": _sha(HIVE_TOKEN)}},
    }))
    os.chmod(path, 0o600)
    repo = ControlPlaneRepo(sink=sink)
    svr = ControlPlaneServer(repo, api_keys={"opkey": "operator", "viewkey": "viewer"},
                             fleet_config=FleetConfigStore(path))
    if ci is not None:
        svr.ci_monitor.snapshot = lambda start=True: ci()
    if not loop:
        svr._deployments_stop.set()
    threading.Thread(target=svr.serve, daemon=True).start()
    svr.wait_until_ready()
    return svr, repo


def _get(port, path="/api/v1/deployments", headers=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=headers or {})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


def _post_event(port, body):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/deployments/events", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + HIVE_TOKEN},
        method="POST")
    with urllib.request.urlopen(req) as r:
        return r.status


_N = iter(range(1, 10**9))


def _event(component="hive-gateway", outcome="ok", evidence=""):
    return {"id": f"{next(_N):032x}", "ts": "2026-10-28T12:00:00Z", "host": "login2.hive",
            "component": component, "step": f"deployment.{component}.cycle",
            "outcome": outcome, "from": "0" * 40, "to": C1, "evidence": evidence}


VIEW = {"X-API-Key": "viewkey"}


@pytest.fixture()
def defined(tmp_path):
    d = _write(tmp_path / "pipelines", [doc for doc in STAGE1 if doc["host"] == "hive"])
    ci = CI()
    ci.add(10, C1)
    svr, repo = _server(tmp_path, {"deployment_definitions": str(d)}, ci=ci)
    yield svr, repo
    svr.shutdown()


def test_the_read_route_requires_a_key(defined):
    svr, _ = defined
    assert _get(svr.port)[0] == 401
    assert _get(svr.port, headers={"X-API-Key": "nope"})[0] == 401
    # A fleet host token authorizes event ingest only, never this read.
    assert _get(svr.port, headers={"Authorization": "Bearer " + HIVE_TOKEN})[0] == 401
    assert _get(svr.port, headers=VIEW)[0] == 200
    assert _get(svr.port, "/api/deployments", headers=VIEW)[0] == 200          # the spec's path
    assert _get(svr.port, "/api/deployments")[0] == 401
    assert _get(svr.port, "/api/v1/deployments/events", headers=VIEW)[0] == 404


def test_without_definitions_the_view_is_empty_and_the_page_is_not_advertised(tmp_path):
    svr, _ = _server(tmp_path, None)
    try:
        svr.deployments_tick()
        status, body = _get(svr.port, headers=VIEW)
        assert status == 200
        assert body["definitions"]["configured"] is False and body["plans"] == []
        assert body["components"] == {}
        assert body["coordinator"]["evidence"] == "unknown"
        assert "deployments_v1" not in svr._health()["capabilities"]
    finally:
        svr.shutdown()


def test_default_evidence_holds_release_not_confirmed_and_permits_nothing(defined):
    svr, _ = defined
    assert "deployments_v1" in svr._health()["capabilities"]
    svr.deployments_tick()
    status, body = _get(svr.port, headers=VIEW)
    assert status == 200
    [plan] = body["plans"]
    assert plan["track"] == {"repo": "InferWeave/inferweave", "branch": "main",
                             "ci_context": "ci/woodpecker/push/rust"}
    assert plan["target_commit"] == C1 and plan["generation"] == 0
    assert plan["current"]["type"] == "Hold" and plan["current"]["reason"] == "release not confirmed"
    assert plan["permitted"] == []
    assert set(plan["components"]) == {"hive-gateway", "hive-nodes"}
    assert body["coordinator"]["evidence"] == "unknown" and body["coordinator"]["error"] is None
    assert body["definitions"]["groups"] == [
        {"stage": 1, "order": 0, "components": ["hive-gateway"]},
        {"stage": 1, "order": 1, "components": ["hive-nodes"]}]


def test_a_confirmed_release_starts_and_the_component_view_follows_events(defined):
    svr, _ = defined
    svr.deployment_evidence = lambda track, target, plan: ReleaseEvidence(target, True)
    svr.deployments_tick()
    [plan] = _get(svr.port, headers=VIEW)[1]["plans"]
    assert [a["type"] for a in plan["actions"]] == ["Replan", "Start"]
    assert plan["generation"] == 1
    assert plan["current"]["stage"] == 1 and plan["current"]["components"] == ["hive-gateway"]
    assert plan["permitted"] == ["hive-gateway"]
    assert plan["components"]["hive-gateway"]["state"] == "pending"

    assert _post_event(svr.port, _event(outcome="started")) == 200
    assert _post_event(svr.port, _event(outcome="ok")) == 200
    svr.deployments_tick()
    body = _get(svr.port, headers=VIEW)[1]
    [plan] = body["plans"]
    gw = plan["components"]["hive-gateway"]
    assert gw["state"] == "healthy" and 595 <= gw["hold_remaining_s"] <= 600
    assert gw["attempts"] == 1 and gw["instances"] == 1
    assert plan["current"]["type"] == "Start" and plan["current"]["components"] == ["hive-nodes"]
    assert plan["permitted"] == ["hive-gateway", "hive-nodes"]
    assert body["components"]["hive-gateway"]["running"] == C1           # the monitor row too


def test_evidence_strings_are_returned_verbatim_as_data(defined):
    svr, _ = defined
    assert _post_event(svr.port, _event(outcome="failed", evidence=HOSTILE)) == 200
    status, body = _get(svr.port, headers=VIEW)
    assert status == 200
    [event] = body["components"]["hive-gateway"]["timeline"]
    assert event["evidence"] == HOSTILE                       # data, not markup: the page escapes it


def test_reads_never_tick_and_a_failed_tick_breaks_neither_the_page_nor_ingest(defined):
    svr, _ = defined
    svr.deployments_tick()
    good = _get(svr.port, headers=VIEW)[1]["plans"]
    calls = []

    def boom():
        calls.append(1)
        raise RuntimeError("coordinator exploded: secret detail")
    svr.deployment_coordinator.tick = boom
    for _ in range(3):
        assert _get(svr.port, headers=VIEW)[0] == 200
    assert calls == []                                          # GET only reads the last tick

    svr.deployments_tick()                                      # must not raise
    assert calls == [1]
    status, body = _get(svr.port, headers=VIEW)
    assert status == 200
    assert body["coordinator"]["error"] == "tick failed: RuntimeError"
    assert "secret" not in json.dumps(body["coordinator"])
    assert body["plans"] == good                                # the last good plan stays, flagged
    assert body["coordinator"]["stale"] is True
    assert _post_event(svr.port, _event(outcome="started")) == 200   # ingest unaffected


def test_the_server_ticks_on_its_own_and_stops_on_shutdown(tmp_path):
    d = _write(tmp_path / "pipelines", [doc for doc in STAGE1 if doc["host"] == "hive"])
    ci = CI()
    ci.add(10, C1)
    svr, _ = _server(tmp_path, {"deployment_definitions": str(d)}, loop=True, ci=ci)
    try:
        deadline = time.time() + 5
        while svr.deployments_view()["coordinator"]["ticked_at"] is None and time.time() < deadline:
            time.sleep(0.02)
        assert svr.deployments_view()["coordinator"]["ticked_at"] is not None
    finally:
        svr.shutdown()
    assert svr._deployments_stop.is_set()

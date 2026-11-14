"""control_plane.ci_deployments: components a CI pipeline (not a host updater) reports.

They are accepted and shown, but never planned, gated, permitted, escalated or forced.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import urllib.error
import urllib.request

import pytest
import yaml

from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.ci_deployments import MAX_CI_COMPONENTS, parse_ci_deployments
from herdr_engineering.control_plane.deployment_definitions import (
    DeploymentAuthorizationError,
    DeploymentDefinitions,
    DeploymentDefinitionsUnavailable,
)
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from herdr_engineering.fleet_config import FleetConfigStore

from .test_deployment_definitions import C1, CI, STAGE1, _write

HIVE_TOKEN = "placeholder-host-hive"
CI_TOKEN = "placeholder-host-woodpecker"
CI_HOST = "woodpecker-ci"
MAP = {"lc-binbase-frontend": CI_HOST, "lc-binbase-backend": CI_HOST}
OP = {"X-API-Key": "opkey"}
VIEW = {"X-API-Key": "viewkey"}
RUN_URL = "https://ci.metabolomics.us/repos/7/pipeline/412"


# ------------------------------------------------------------------ parser

def test_a_valid_mapping_parses_and_none_is_empty():
    assert parse_ci_deployments(MAP) == (MAP, [])
    assert parse_ci_deployments(None) == ({}, [])
    assert parse_ci_deployments({}) == ({}, [])


@pytest.mark.parametrize("value", [["a"], "a: b", 3, True])
def test_a_non_mapping_is_refused(value):
    assert parse_ci_deployments(value) == (
        {}, ["control_plane.ci_deployments must be a mapping of component to host id"])


@pytest.mark.parametrize("key, host", [
    ("Upper", CI_HOST), ("-lead", CI_HOST), ("a" * 65, CI_HOST), ("has space", CI_HOST), ("", CI_HOST),
    (5, CI_HOST), ("ok", ""), ("ok", 7), ("ok", None), ("ok", "two words"), ("ok", "h" * 300)])
def test_a_bad_entry_refuses_the_whole_map_without_echoing_unbounded_input(key, host):
    out, errors = parse_ci_deployments({key: host})
    assert out == {} and len(errors) == 1
    assert len(errors[0]) < 300 and "h" * 100 not in errors[0] and "a" * 100 not in errors[0]


def test_the_map_is_bounded():
    big = {f"c{i}": CI_HOST for i in range(MAX_CI_COMPONENTS + 1)}
    out, errors = parse_ci_deployments(big)
    assert out == {} and errors == [f"control_plane.ci_deployments allows at most {MAX_CI_COMPONENTS} components"]
    assert parse_ci_deployments({f"c{i}": CI_HOST for i in range(MAX_CI_COMPONENTS)})[1] == []


# ----------------------------------------------------------- authorization

def _store(tmp_path, ci_map, docs=STAGE1[:2]):
    d = _write(tmp_path / "pipelines", docs)
    return DeploymentDefinitions(lambda: str(d), ci_snapshot=CI(), ci_components=lambda: ci_map)


def test_an_allowlisted_pair_is_accepted_next_to_the_definitions_rule(tmp_path):
    store = _store(tmp_path, MAP)
    store.authorize("hive-gateway", "hive")                       # the definitions rule, unchanged
    store.authorize("lc-binbase-frontend", CI_HOST)
    store.authorize("lc-binbase-backend", CI_HOST)
    with pytest.raises(DeploymentAuthorizationError, match="not defined for host 'hive'"):
        store.authorize("lc-binbase-frontend", "hive")            # right component, wrong host
    with pytest.raises(DeploymentAuthorizationError):
        store.authorize("hive-gateway", CI_HOST)                  # CI host gets no definition component
    with pytest.raises(DeploymentAuthorizationError):
        store.authorize("other", CI_HOST)                         # and no component off the list


def test_without_definitions_configured_ingest_is_unchanged(tmp_path):
    store = DeploymentDefinitions(lambda: None, ci_components=lambda: MAP)
    store.authorize("anything", "any-host")
    store.authorize("lc-binbase-frontend", "hive")
    assert store.status()["ingest_restricted"] is False


def test_invalid_definitions_keep_refusing_even_an_allowlisted_component(tmp_path):
    # The conservative choice: an allowlisted CI pair is NOT a way around a broken definitions set.
    d = _write(tmp_path / "p", [dict(STAGE1[0], stage=7)])
    store = DeploymentDefinitions(lambda: str(d), ci_components=lambda: MAP)
    with pytest.raises(DeploymentDefinitionsUnavailable):
        store.authorize("lc-binbase-frontend", CI_HOST)
    with pytest.raises(DeploymentDefinitionsUnavailable):
        store.authorize("hive-gateway", "hive")


def test_a_clashing_entry_is_dropped_and_reported_and_the_rest_still_works(tmp_path):
    store = _store(tmp_path, {"hive-gateway": CI_HOST, "lc-binbase-frontend": CI_HOST})
    view = store.annotate({"components": {}})["definitions"]
    assert view["ci_errors"] == ["control_plane.ci_deployments: component 'hive-gateway' also has a "
                                 "deployment definition; remove one of them"]
    assert view["ci_components"] == ["lc-binbase-frontend"]
    store.authorize("hive-gateway", "hive")                       # the definition still rules
    with pytest.raises(DeploymentAuthorizationError):
        store.authorize("hive-gateway", CI_HOST)
    store.authorize("lc-binbase-frontend", CI_HOST)               # only the clash is dropped


def test_an_unreadable_or_invalid_ci_value_refuses_only_ci_components(tmp_path):
    def boom():
        raise RuntimeError("secret detail")
    d = _write(tmp_path / "p", STAGE1[:2])
    store = DeploymentDefinitions(lambda: str(d), ci_components=boom)
    store.authorize("hive-gateway", "hive")
    with pytest.raises(DeploymentAuthorizationError):
        store.authorize("lc-binbase-frontend", CI_HOST)
    view = store.annotate({"components": {}})["definitions"]
    assert view["ci_components"] == [] and "secret" not in json.dumps(view)


def test_without_loaded_definitions_no_ci_badge_or_component_list(tmp_path):
    store = DeploymentDefinitions(lambda: None, ci_components=lambda: MAP)        # not configured
    snap = {"components": {"lc-binbase-frontend": {"running": C1, "timeline": []}}}
    view = store.annotate(snap)
    assert "kind" not in view["components"]["lc-binbase-frontend"]
    assert view["definitions"]["ci_components"] == []
    broken = _write(tmp_path / "p", [dict(STAGE1[0], stage=7)])                   # configured, never valid
    view = DeploymentDefinitions(lambda: str(broken), ci_components=lambda: MAP).annotate(snap)
    assert "kind" not in view["components"]["lc-binbase-frontend"]


def test_the_snapshot_names_the_ci_components_and_marks_only_their_rows(tmp_path):
    store = _store(tmp_path, MAP)
    snap = {"components": {"lc-binbase-frontend": {"running": C1, "timeline": []},
                           "hive-gateway": {"running": C1, "timeline": []}}}
    view = store.annotate(snap)
    assert view["definitions"]["ci_components"] == ["lc-binbase-backend", "lc-binbase-frontend"]
    assert view["definitions"]["ci_errors"] == []
    assert view["components"]["lc-binbase-frontend"]["kind"] == "ci"
    assert "kind" not in view["components"]["hive-gateway"]
    assert "kind" not in snap["components"]["lc-binbase-frontend"]    # the input is not mutated


def test_the_ci_summary_carries_outcome_commits_time_and_a_bounded_evidence_link(tmp_path):
    store = _store(tmp_path, MAP)
    ev = {"outcome": "ok", "step": "deployment.lc-binbase-frontend.deploy", "from": "a" * 40, "to": C1,
          "received_at": "2026-11-09T10:00:00Z", "evidence": f"deployed, see {RUN_URL} for the run"}
    started = dict(ev, outcome="started", evidence="")
    snap = {"components": {"lc-binbase-frontend": {"timeline": [ev, started], "running": C1}}}
    ci = store.annotate(snap)["components"]["lc-binbase-frontend"]["ci"]
    assert ci == {"outcome": "ok", "at": "2026-11-09T10:00:00Z", "from": "a" * 40, "to": C1,
                  "evidence_url": RUN_URL, "detail": f"deployed, see {RUN_URL} for the run"}
    snap["components"]["lc-binbase-frontend"]["timeline"] = [dict(ev, evidence=f"(see {RUN_URL}).")]
    assert store.annotate(snap)["components"]["lc-binbase-frontend"]["ci"]["evidence_url"] == RUN_URL
    snap["components"]["lc-binbase-frontend"]["timeline"] = [dict(ev, evidence="no link http://x")]
    assert store.annotate(snap)["components"]["lc-binbase-frontend"]["ci"]["evidence_url"] is None


# -------------------------------------------------------------------- HTTP

def _sha(t):
    return hashlib.sha256(t.encode()).hexdigest()


@pytest.fixture()
def world(tmp_path):
    defs = _write(tmp_path / "pipelines", [d for d in STAGE1 if d["host"] == "hive"])
    path = tmp_path / "fleet.yaml"
    path.write_text(yaml.safe_dump({
        "version": 1,
        "sections": {"control_plane": {"roles": ["control"], "values": {
            "deployment_definitions": str(defs), "deployment_actions": True, "ci_deployments": MAP}}},
        "hosts": {"hive": {"roles": ["node"], "token_sha256": _sha(HIVE_TOKEN)},
                  CI_HOST: {"roles": ["ci"], "token_sha256": _sha(CI_TOKEN)}},
    }))
    os.chmod(path, 0o600)
    ci = CI()
    ci.add(10, C1)
    svr = ControlPlaneServer(ControlPlaneRepo(), api_keys={"opkey": "operator", "viewkey": "viewer"},
                             fleet_config=FleetConfigStore(path))
    svr.ci_monitor.snapshot = lambda start=True: ci()
    svr._deployments_stop.set()
    threading.Thread(target=svr.serve, daemon=True).start()
    svr.wait_until_ready()
    yield svr
    svr.shutdown()


def _call(svr, method, path, body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    hdrs = {"Content-Type": "application/json"} if data is not None else {}
    hdrs.update(headers or {})
    req = urllib.request.Request(f"http://127.0.0.1:{svr.port}{path}", data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


_N = iter(range(1, 10**9))


def _post(svr, token, component="lc-binbase-frontend", outcome="ok", evidence=""):
    body = {"id": f"{next(_N):032x}", "ts": "2026-11-09T10:00:00Z", "host": "ci-agent-1",
            "component": component, "step": f"deployment.{component}.deploy", "outcome": outcome,
            "from": "a" * 40, "to": C1, "evidence": evidence}
    return _call(svr, "POST", "/api/deployments/events", body, {"Authorization": "Bearer " + token})


def test_ci_events_are_accepted_from_the_ci_host_only(world):
    assert _post(world, CI_TOKEN)[0] == 200
    status, body = _post(world, HIVE_TOKEN)
    assert status == 403 and "lc-binbase-frontend" in body["error"] and HIVE_TOKEN not in json.dumps(body)
    assert _post(world, CI_TOKEN, component="unlisted")[0] == 403
    assert _post(world, HIVE_TOKEN, component="hive-gateway")[0] == 200       # definitions rule unchanged


def test_the_snapshot_shows_the_ci_row_with_its_badge_data(world):
    _post(world, CI_TOKEN, outcome="started")
    _post(world, CI_TOKEN, outcome="ok", evidence=f"pipeline {RUN_URL}")
    _post(world, HIVE_TOKEN, component="hive-gateway")
    status, body = _call(world, "GET", "/api/v1/deployments", headers=VIEW)
    assert status == 200
    assert body["definitions"]["ci_components"] == ["lc-binbase-backend", "lc-binbase-frontend"]
    row = body["components"]["lc-binbase-frontend"]
    assert row["kind"] == "ci" and row["ci"]["outcome"] == "ok" and row["ci"]["to"] == C1
    assert row["ci"]["evidence_url"] == RUN_URL
    assert "kind" not in body["components"]["hive-gateway"]


def test_the_coordinator_ignores_ci_components_entirely(world):
    _post(world, CI_TOKEN, outcome="started")
    _post(world, CI_TOKEN, outcome="failed", evidence="boom")
    _post(world, CI_TOKEN, component="lc-binbase-backend", outcome="ok")
    world.deployments_tick()
    body = _call(world, "GET", "/api/v1/deployments", headers=VIEW)[1]
    for plan in body["plans"]:
        assert not {"lc-binbase-frontend", "lc-binbase-backend"} & set(plan["components"])
        assert not {"lc-binbase-frontend", "lc-binbase-backend"} & set(plan["permitted"])
        assert not {"lc-binbase-frontend", "lc-binbase-backend"} & set(plan["missing"])
    assert body["escalations"] == [] and body["requests"] == []
    assert "lc-binbase-frontend" not in json.dumps(body["plans"])
    assert world.repo.deployment_plans is not None
    assert world.repo.deployment_requests.pending() == []


@pytest.mark.parametrize("path, body", [
    ("/api/deployments/force", {"component": "lc-binbase-frontend"}),
    ("/api/deployments/ack", {"component": "lc-binbase-frontend", "release": C1}),
])
def test_force_and_ack_refuse_a_ci_component(world, path, body):
    status, out = _call(world, "POST", path, body, OP)
    assert status == 404 and out == {"error": "unknown component"}
    assert world.repo.deployment_requests.pending() == []


def test_the_permit_gate_refuses_a_ci_component(world):
    _post(world, CI_TOKEN)
    status, out = _call(world, "GET", f"/api/deployments/permit?component=lc-binbase-frontend&commit={C1}",
                        headers={"Authorization": "Bearer " + CI_TOKEN})
    assert status == 403 and out == {"error": "component is not defined for this host"}


def test_the_allowlist_is_hot_reloaded(world, tmp_path):
    cfg = tmp_path / "fleet.yaml"
    doc = yaml.safe_load(cfg.read_text())
    doc["sections"]["control_plane"]["values"]["ci_deployments"] = {"lc-binbase-backend": CI_HOST}
    cfg.write_text(yaml.safe_dump(doc) + "# reload\n")
    os.chmod(cfg, 0o600)
    assert _post(world, CI_TOKEN, component="lc-binbase-frontend")[0] == 403
    assert _post(world, CI_TOKEN, component="lc-binbase-backend")[0] == 200


# ------------------------------------------ fail closed when the fleet file breaks

def _fleet_world(tmp_path, extra_values=None):
    defs = _write(tmp_path / "pipelines", [d for d in STAGE1 if d["host"] == "hive"])
    path = tmp_path / "fleet.yaml"
    values = {"deployment_definitions": str(defs), "ci_deployments": MAP, **(extra_values or {})}
    path.write_text(yaml.safe_dump({
        "version": 1,
        "sections": {"control_plane": {"roles": ["control"], "values": values}},
        "hosts": {"hive": {"roles": ["node"], "token_sha256": _sha(HIVE_TOKEN)},
                  CI_HOST: {"roles": ["ci"], "token_sha256": _sha(CI_TOKEN)}}}))
    os.chmod(path, 0o600)
    svr = ControlPlaneServer(ControlPlaneRepo(), api_keys={"opkey": "operator"},
                             fleet_config=FleetConfigStore(path))
    svr._deployments_stop.set()
    threading.Thread(target=svr.serve, daemon=True).start()
    svr.wait_until_ready()
    return svr, path


def _break(path):
    path.write_text("version: [unclosed\n")
    os.chmod(path, 0o600)
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))


def test_a_broken_fleet_file_fails_the_ci_allowlist_closed(tmp_path):
    svr, path = _fleet_world(tmp_path)
    try:
        assert _post(svr, CI_TOKEN)[0] == 200
        _break(path)
        status, body = _post(svr, CI_TOKEN)
        assert status == 403 and "lc-binbase-frontend" in body["error"]
        assert _post(svr, HIVE_TOKEN, component="hive-gateway")[0] == 200   # definitions keep last good
    finally:
        svr.shutdown()


def test_a_deleted_fleet_file_fails_the_ci_allowlist_closed(tmp_path):
    svr, path = _fleet_world(tmp_path)
    try:
        assert _post(svr, CI_TOKEN)[0] == 200
        path.unlink()
        assert _post(svr, CI_TOKEN)[0] == 403
    finally:
        svr.shutdown()


def test_last_good_mode_keeps_the_ci_allowlist_through_a_broken_file(tmp_path):
    svr, path = _fleet_world(tmp_path, {"config_failure_mode": "last_good"})
    try:
        assert _post(svr, CI_TOKEN)[0] == 200
        _break(path)
        assert _post(svr, CI_TOKEN)[0] == 200
    finally:
        svr.shutdown()


def test_the_allowlist_comes_back_when_the_file_is_repaired(tmp_path):
    svr, path = _fleet_world(tmp_path)
    try:
        good = path.read_text()
        _break(path)
        assert _post(svr, CI_TOKEN)[0] == 403
        path.write_text(good + "# repaired\n")
        os.chmod(path, 0o600)
        assert _post(svr, CI_TOKEN)[0] == 200
    finally:
        svr.shutdown()


def test_recent_problems_list_failed_events_newest_first_with_detail_link_and_recovery(tmp_path):
    store = _store(tmp_path, MAP)
    base = {"step": "deployment.lc-binbase-backend.deploy", "host": "ci-agent-1", "reported_by": CI_HOST}
    detail = f"deploy step failed: FAILED MODULES: go-assistant-mcp (pipeline #154) [{RUN_URL}]"
    backend = [dict(base, outcome="failed", received_at="2026-11-09T09:00:00Z", received_epoch=1, evidence="old"),
               dict(base, outcome="ok", received_at="2026-11-09T09:30:00Z", received_epoch=2, evidence=""),
               dict(base, outcome="failed", received_at="2026-11-10T09:00:00Z", received_epoch=3, evidence=detail)]
    frontend = [dict(base, step="deployment.lc-binbase-frontend.deploy", outcome="started",
                     received_at="2026-11-10T09:05:00Z", received_epoch=4, evidence="")]
    snap = {"components": {"lc-binbase-backend": {"timeline": backend}, "lc-binbase-frontend": {"timeline": frontend}}}
    problems = store.annotate(snap)["problems"]
    assert [p["at"] for p in problems] == ["2026-11-10T09:00:00Z", "2026-11-09T09:00:00Z"]
    assert problems[0] == {"component": "lc-binbase-backend", "step": "deployment.lc-binbase-backend.deploy",
                           "outcome": "failed", "at": "2026-11-10T09:00:00Z", "host": "ci-agent-1",
                           "reported_by": CI_HOST, "detail": detail, "evidence_url": RUN_URL, "recovered": False}
    assert problems[1]["recovered"] is True
    assert store.annotate({"components": {}})["problems"] == []


def test_recent_problems_are_bounded_and_the_detail_is_capped(tmp_path):
    store = _store(tmp_path, MAP)
    timeline = [{"step": "deployment.lc-binbase-backend.deploy", "outcome": "failed", "received_epoch": n,
                 "received_at": "t", "evidence": "x" * 5000} for n in range(50)]
    problems = store.annotate({"components": {"lc-binbase-backend": {"timeline": timeline}}})["problems"]
    assert len(problems) == 20 and len(problems[0]["detail"]) == 300

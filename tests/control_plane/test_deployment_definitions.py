"""Pipeline definitions, target resolution and host-authorized ingest (deploy-4 Task 2)."""
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
from herdr_engineering.control_plane.deployment_definitions import (
    DeploymentAuthorizationError,
    DeploymentDefinitions,
    DeploymentDefinitionsUnavailable,
    load_definitions,
    validate_definition,
    validate_dir,
)
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from herdr_engineering.fleet_config import FleetConfigStore

REPO = "InferWeave/inferweave"
CTX = "ci/woodpecker/push/rust"
C1, C2, C3 = "1" * 40, "2" * 40, "3" * 40


def _definition(component, *, stage=1, order=0, host=None, gpu=False,
                restart="gateway-cycle", hold_s=600, **over):
    doc = {
        "component": component, "stage": stage, "order": order,
        "host": host or component.split("-")[0],
        "updater": "deploy/hive/iw-node-pull-deploy",
        "follows": {"repo": REPO, "tag": "node-ci-latest", "asset": "inferweave",
                    "branch": "main", "ci_context": CTX},
        "restart": restart, "gpu": gpu,
        "health": {"probe": "served-request", "timeout_s": 600, "hold_s": hold_s},
        "rollback": {"strategy": "previous-version"},
    }
    doc.update(over)
    return doc


STAGE1 = [
    _definition("hive-gateway", order=0),
    _definition("hive-nodes", order=1, gpu=True, restart="process-restart-in-allocation"),
    _definition("whiteale-gateway", order=0),
    _definition("whiteale-laya", order=1, restart="process-restart-in-allocation"),
    _definition("farm-gateway", order=0),
    _definition("farm-nodes", order=1, gpu=True, restart="process-restart-in-allocation"),
]


def _write(d, docs):
    d.mkdir(parents=True, exist_ok=True)
    for doc in docs:
        (d / f"{doc['component']}.yaml").write_text(yaml.safe_dump(doc))
    return d


class CI:
    """A CIMonitor-shaped snapshot: {'pipelines': {group: [rows]}}."""

    def __init__(self):
        self.rows = []

    def add(self, run, commit, state="success", branch="main", event="push", repository=REPO):
        self.rows.append({"repository": repository, "run_number": run, "revision": commit,
                          "branch": branch, "event": event, "state": state})

    def __call__(self):
        return {"available": True, "pipelines": {REPO: list(self.rows)}}


def _defs(tmp_path, docs=STAGE1, ci=None):
    d = _write(tmp_path / "pipelines", docs)
    return DeploymentDefinitions(lambda: str(d), ci_snapshot=ci or CI())


# ---------------------------------------------------------------- validator

def test_valid_stage1_set_has_no_errors(tmp_path):
    assert validate_dir(str(_write(tmp_path / "p", STAGE1))) == []


@pytest.mark.parametrize("mutate, message", [
    (lambda d: d.update(stage=3), "stage must be 1 or 2"),
    (lambda d: d.update(stage=True), "stage must be 1 or 2"),
    (lambda d: d.update(order=-1), "order must be an integer >= 0"),
    (lambda d: d.update(component="other"), "component 'other' must match file name 'hive-gateway'"),
    (lambda d: d.update(colour="red"), "unknown key colour"),
    (lambda d: d.pop("updater"), "missing key updater"),
    (lambda d: d["follows"].update(branch="dev"), "follows.branch must be 'main'"),
    (lambda d: d["follows"].pop("ci_context"), "missing key follows.ci_context"),
    (lambda d: d.update(gpu=True, restart="scancel-resubmit"),
     "restart 'scancel-resubmit' releases a GPU; not allowed when gpu is true"),
    (lambda d: d.update(restart="reboot"), "restart 'reboot' must be one of"),
    (lambda d: d["health"].update(probe="catalog-warm"), "health.probe must be 'served-request'"),
    (lambda d: d["health"].update(hold_s="10m"), "health.hold_s must be an integer >= 0"),
    (lambda d: d.update(rollback={"strategy": "none"}),
     "rollback.reason is required when strategy is 'none'"),
    (lambda d: d.update(host=""), "host must be a non-empty string"),
])
def test_validator_messages_match_plan_one(mutate, message):
    doc = _definition("hive-gateway")
    mutate(doc)
    assert any(e.startswith(message) for e in validate_definition(doc, "hive-gateway")), \
        validate_definition(doc, "hive-gateway")


def test_invalid_definition_rejects_the_whole_set_with_the_validator_messages(tmp_path):
    bad = dict(_definition("hive-nodes", order=1, gpu=True), restart="job-cancel-resubmit")
    d = _write(tmp_path / "p", STAGE1[:1] + [bad])
    defs, errors = load_definitions(str(d))
    assert defs == {}
    assert "hive-nodes.yaml: restart 'job-cancel-resubmit' releases a GPU; not allowed when gpu is true" in errors
    store = DeploymentDefinitions(lambda: str(d))
    st = store.status()
    assert st["configured"] and not st["loaded"] and st["errors"] == errors
    assert store.definitions() == {}


def test_duplicate_host_stage_order_is_rejected(tmp_path):
    d = _write(tmp_path / "p", [_definition("hive-gateway"), _definition("hive-proxy")])
    errs = validate_dir(str(d))
    assert errs == ["hive-proxy.yaml: duplicate (host, stage, order) ('hive', 1, 0) also in hive-gateway.yaml"]


def test_not_a_directory_and_unparseable_file(tmp_path):
    assert validate_dir(str(tmp_path / "nope")) == [f"{tmp_path / 'nope'}: not a directory"]
    d = tmp_path / "p"
    d.mkdir()
    (d / "broken.yaml").write_text("component: [unclosed\n")
    errs = validate_dir(str(d))
    assert len(errs) == 1 and errs[0].startswith("broken.yaml: cannot parse: ")


def test_yaml_python_object_tags_are_never_constructed(tmp_path):
    sentinel = tmp_path / "pwned"
    d = tmp_path / "p"
    _write(d, STAGE1[:1])
    (d / "evil.yaml").write_text(
        f"!!python/object/apply:os.system [\"touch {sentinel}\"]\n")
    (d / "evil2.yaml").write_text("component: !!python/name:os.system ''\n")
    defs, errors = load_definitions(str(d))
    assert not sentinel.exists()
    assert defs == {}
    assert any(e.startswith("evil.yaml: cannot parse: ") for e in errors)
    assert any(e.startswith("evil2.yaml: cannot parse: ") for e in errors)


def test_oversize_definition_file_is_refused_without_parsing(tmp_path):
    d = _write(tmp_path / "p", STAGE1[:1])
    (d / "huge.yaml").write_text("# " + "x" * (70 * 1024) + "\n")
    defs, errors = load_definitions(str(d))
    assert defs == {} and any(e.startswith("huge.yaml: cannot parse: ") for e in errors)


# ----------------------------------------------------------------- grouping

def test_grouped_by_stage_then_order_gateways_first(tmp_path):
    stage2 = [_definition("zapp-console", stage=2, order=0), _definition("bender-node", stage=2, order=1)]
    store = _defs(tmp_path, STAGE1 + stage2)
    stages = store.stages()
    assert list(stages) == [1, 2]
    assert [(d.order, d.component) for d in stages[1]] == [
        (0, "farm-gateway"), (0, "hive-gateway"), (0, "whiteale-gateway"),
        (1, "farm-nodes"), (1, "hive-nodes"), (1, "whiteale-laya")]
    assert store.groups() == [
        {"stage": 1, "order": 0, "components": ["farm-gateway", "hive-gateway", "whiteale-gateway"]},
        {"stage": 1, "order": 1, "components": ["farm-nodes", "hive-nodes", "whiteale-laya"]},
        {"stage": 2, "order": 0, "components": ["zapp-console"]},
        {"stage": 2, "order": 1, "components": ["bender-node"]},
    ]
    d = store.definitions()["hive-nodes"]
    assert d.host == "hive" and d.gpu is True and d.hold_s == 600
    assert d.follows["ci_context"] == CTX


def test_definitions_reload_when_a_file_changes(tmp_path):
    d = _write(tmp_path / "pipelines", STAGE1[:2])
    store = DeploymentDefinitions(lambda: str(d))
    assert set(store.definitions()) == {"hive-gateway", "hive-nodes"}
    _write(d, [_definition("farm-gateway")])
    assert set(store.definitions()) == {"hive-gateway", "hive-nodes", "farm-gateway"}


def test_a_broken_edit_keeps_the_last_good_set_and_reports_it(tmp_path):
    d = _write(tmp_path / "pipelines", STAGE1[:2])
    store = DeploymentDefinitions(lambda: str(d))
    assert store.status()["loaded"]
    # stage 99 changes the size too: a same-size rewrite within one mtime tick keeps the signature
    (d / "hive-gateway.yaml").write_text(yaml.safe_dump(dict(_definition("hive-gateway"), stage=99)))
    st = store.status()
    assert st["loaded"] and st["errors"] == ["hive-gateway.yaml: stage must be 1 or 2"]
    assert set(store.definitions()) == {"hive-gateway", "hive-nodes"}   # still authorizing


# ----------------------------------------------------------------- inert

@pytest.mark.parametrize("value", [None, ""])
def test_no_definitions_configured_is_inert(value):
    store = DeploymentDefinitions(lambda: value, ci_snapshot=lambda: 1 / 0)
    assert store.definitions() == {} and store.stages() == {} and store.groups() == []
    assert store.target_for("hive-gateway") is None
    assert store.plans() == {}
    store.authorize("anything", "any-host")     # accept-all, as before Task 2
    st = store.status()
    assert st == {"configured": False, "loaded": False, "errors": [], "ingest_restricted": False,
                  "warning": "no deployment definitions loaded: any fleet host may report any component"}
    view = store.annotate({"components": {}})
    assert view["components"] == {} and view["definitions"]["ingest_restricted"] is False
    assert view["definitions"]["warning"]


def test_a_relative_path_is_a_configuration_error(tmp_path):
    store = DeploymentDefinitions(lambda: "deploy/pipelines")
    st = store.status()
    assert st["configured"] and not st["loaded"]
    assert st["errors"] == ["control_plane.deployment_definitions must be an absolute directory path"]


# ------------------------------------------------------------ authorization

def test_authorize_only_the_defined_host(tmp_path):
    store = _defs(tmp_path)
    store.authorize("hive-gateway", "hive")
    with pytest.raises(DeploymentAuthorizationError, match="not defined for host 'whiteale'"):
        store.authorize("hive-gateway", "whiteale")
    with pytest.raises(DeploymentAuthorizationError, match="not defined"):
        store.authorize("zapp-console", "hive")
    assert store.status()["ingest_restricted"] is True and store.status()["warning"] is None


def test_configured_but_never_valid_fails_closed(tmp_path):
    d = _write(tmp_path / "p", [dict(_definition("hive-gateway"), stage=7)])
    store = DeploymentDefinitions(lambda: str(d))
    with pytest.raises(DeploymentDefinitionsUnavailable):
        store.authorize("hive-gateway", "hive")
    assert store.status()["ingest_restricted"] is True


# ---------------------------------------------------------- target + replan

def test_target_is_the_newest_green_main_push(tmp_path):
    ci = CI()
    ci.add(10, C1)
    ci.add(11, C2, state="failure")
    ci.add(12, C3, branch="feature")
    ci.add(13, C3, event="pull_request")
    ci.add(14, C3, repository="InferWeave/other")
    store = _defs(tmp_path, ci=ci)
    t = store.target_for("hive-gateway")
    assert t.commit == C1 and t.run_number == 10 and t.repository == REPO
    assert t.tag == "node-ci-latest" and t.asset == "inferweave" and t.ci_context == CTX
    assert store.target_for("no-such-component") is None


def test_no_green_release_yet_means_no_target(tmp_path):
    ci = CI()
    ci.add(1, C1, state="running")
    assert _defs(tmp_path, ci=ci).target_for("hive-gateway") is None


def test_a_new_green_release_mid_roll_restarts_the_plan_from_the_newest_commit(tmp_path):
    ci = CI()
    ci.add(10, C1)
    store = _defs(tmp_path, ci=ci)
    first = store.plan()
    assert first.target_commit == C1 and first.generation == 1 and not first.restarted
    assert store.plan() == first                           # stable while nothing changes
    ci.add(11, C2, state="running")
    assert store.plan().generation == 1                    # not green yet: no replan
    ci.rows[-1]["state"] = "success"
    second = store.plan()
    assert second.target_commit == C2 and second.generation == 2
    assert second.restarted and second.previous_commit == C1
    assert store.target_for("farm-nodes").commit == C2
    assert store.target_for("farm-nodes").generation == 2
    assert [g["components"] for g in second.groups][0] == ["farm-gateway", "hive-gateway", "whiteale-gateway"]


def test_a_rerun_of_an_old_commit_never_moves_the_target_back(tmp_path):
    ci = CI()
    ci.add(10, C1)
    ci.add(11, C2)
    store = _defs(tmp_path, ci=ci)
    assert store.plan().target_commit == C2
    ci.add(12, C1)                                         # Woodpecker restart of the old commit
    plan = store.plan()
    assert plan.target_commit == C2 and plan.generation == 1


def test_a_ci_outage_keeps_the_target_and_generation(tmp_path):
    ci = CI()
    ci.add(10, C1)
    state = {"down": False}

    def snapshot():
        if state["down"]:
            return {"available": False, "pipelines": {}}
        return ci()
    store = _defs(tmp_path, ci=snapshot)
    assert store.plan().target_commit == C1
    state["down"] = True
    plan = store.plan()
    assert plan.target_commit == C1 and plan.generation == 1


def test_a_failing_ci_snapshot_does_not_raise(tmp_path):
    store = _defs(tmp_path, ci=lambda: 1 / 0)
    assert store.target_for("hive-gateway") is None


# -------------------------------------------------------- ingest over HTTP

HIVE_TOKEN, WHITEALE_TOKEN = "placeholder-host-hive", "placeholder-host-whiteale"


def _sha(t):
    return hashlib.sha256(t.encode()).hexdigest()


def _server(tmp_path, sink, control_plane):
    path = tmp_path / "fleet.yaml"
    path.write_text(yaml.safe_dump({
        "version": 1,
        "sections": ({"control_plane": {"roles": ["control"], "values": control_plane}}
                     if control_plane is not None else {}),
        "hosts": {"hive": {"roles": ["node"], "token_sha256": _sha(HIVE_TOKEN)},
                  "whiteale": {"roles": ["node"], "token_sha256": _sha(WHITEALE_TOKEN)}},
    }))
    os.chmod(path, 0o600)
    repo = ControlPlaneRepo(sink=sink)
    svr = ControlPlaneServer(repo, api_keys={"opkey": "operator"}, fleet_config=FleetConfigStore(path))
    threading.Thread(target=svr.serve, daemon=True).start()
    svr.wait_until_ready()
    return svr, repo


_N = iter(range(1, 10**9))


def _event(component="hive-gateway", outcome="ok"):
    return {"id": f"{next(_N):032x}", "ts": "2026-10-28T12:00:00Z", "host": "login2.hive",
            "component": component, "step": f"deployment.{component}.cycle",
            "outcome": outcome, "from": C1, "to": C2, "evidence": ""}


def _post(port, body, token):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/deployments/events", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + token}, method="POST")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


def _deployment_rows(sink):
    return [e for e in sink.events if e.get("entity_type") == "deployment"]


@pytest.fixture()
def defined_server(tmp_path, acknowledged_sink):
    d = _write(tmp_path / "pipelines", STAGE1)
    svr, repo = _server(tmp_path, acknowledged_sink, {"deployment_definitions": str(d)})
    yield svr, repo, acknowledged_sink
    svr.shutdown()


def test_ingest_is_accepted_for_the_defined_host(defined_server):
    svr, repo, sink = defined_server
    assert _post(svr.port, _event("hive-gateway"), HIVE_TOKEN) == (200, {"ok": True})
    assert "hive-gateway" in repo.deployments.snapshot()["components"]
    view = svr.deployments_snapshot()
    assert view["definitions"]["ingest_restricted"] is True and view["definitions"]["warning"] is None
    assert len(_deployment_rows(sink)) == 1


def test_ingest_is_refused_for_another_hosts_token(defined_server):
    svr, repo, sink = defined_server
    status, body = _post(svr.port, _event("hive-gateway"), WHITEALE_TOKEN)
    assert status == 403 and "not defined for host 'whiteale'" in body["error"]
    status, _ = _post(svr.port, _event("zapp-console"), HIVE_TOKEN)        # undefined component
    assert status == 403
    assert repo.deployments.snapshot() == {"components": {}}
    assert _deployment_rows(sink) == []                                    # nothing recorded as accepted


def test_ingest_without_definitions_keeps_accept_all_and_warns(tmp_path, acknowledged_sink):
    svr, repo = _server(tmp_path, acknowledged_sink, None)
    try:
        assert _post(svr.port, _event("hive-gateway"), WHITEALE_TOKEN)[0] == 200
        view = svr.deployments_snapshot()
        assert "hive-gateway" in view["components"]
        assert view["definitions"]["ingest_restricted"] is False
        assert "any fleet host may report any component" in view["definitions"]["warning"]
    finally:
        svr.shutdown()


def test_ingest_with_broken_definitions_fails_closed(tmp_path, acknowledged_sink):
    d = _write(tmp_path / "pipelines", [dict(_definition("hive-gateway"), restart="reboot")])
    svr, repo = _server(tmp_path, acknowledged_sink, {"deployment_definitions": str(d)})
    try:
        status, body = _post(svr.port, _event("hive-gateway"), HIVE_TOKEN)
        assert status == 503 and "definitions" in body["error"]
        assert _deployment_rows(acknowledged_sink) == []
    finally:
        svr.shutdown()


def test_server_target_reads_the_ci_monitor_snapshot(defined_server):
    svr, _, _ = defined_server
    ci = CI()
    ci.add(5, C3)
    calls = []
    svr.ci_monitor.snapshot = lambda start=True: calls.append(start) or ci()
    assert svr.deployment_definitions.target_for("hive-gateway").commit == C3
    assert calls and all(calls)        # start=True: the poller runs even without the review gate


def test_no_definitions_never_reads_or_starts_ci(tmp_path, acknowledged_sink):
    svr, _ = _server(tmp_path, acknowledged_sink, None)
    try:
        svr.ci_monitor.snapshot = lambda start=True: pytest.fail("CI read without definitions")
        assert svr.deployment_definitions.target_for("hive-gateway") is None
        assert svr.deployment_definitions.plans() == {}
    finally:
        svr.shutdown()


# ------------------------------------------------ hostile input (review round 1)

def _bomb(depth=6):
    lines = ["a0: &a0 [x,x,x,x,x,x,x,x,x,x]"]
    for i in range(1, depth + 1):
        lines.append(f"a{i}: &a{i} [" + ",".join([f"*a{i - 1}"] * 10) + "]")
    lines.append(f"restart: *a{depth}")
    return "\n".join(lines) + "\n"


def test_deep_nesting_is_a_parse_error_and_keeps_the_last_good_set(tmp_path):
    d = _write(tmp_path / "pipelines", STAGE1[:2])
    store = DeploymentDefinitions(lambda: str(d))
    assert store.status()["loaded"]
    (d / "deep.yaml").write_text("restart: " + "[" * 5000 + "]" * 5000 + "\n")
    st = store.status()
    assert st["loaded"] and len(st["errors"]) == 1
    assert st["errors"][0].startswith("deep.yaml: cannot parse: ")
    store.authorize("hive-gateway", "hive")                   # still the last good set
    assert store.status() == st                               # stable, no repeated failure


def test_deep_nesting_on_first_load_fails_closed_not_crashes(tmp_path):
    d = tmp_path / "p"
    d.mkdir()
    (d / "deep.yaml").write_text("restart: " + "[" * 5000 + "]" * 5000 + "\n")
    store = DeploymentDefinitions(lambda: str(d))
    assert store.status()["errors"][0].startswith("deep.yaml: cannot parse: ")
    with pytest.raises(DeploymentDefinitionsUnavailable):
        store.authorize("hive-gateway", "hive")


def test_yaml_aliases_and_anchors_are_refused(tmp_path):
    d = tmp_path / "p"
    d.mkdir()
    (d / "bomb.yaml").write_text(_bomb())
    doc = dict(_definition("hive-gateway"))
    text = yaml.safe_dump(doc).replace("follows:\n", "follows: &f\n")
    (d / "hive-gateway.yaml").write_text(text)
    errs = validate_dir(str(d))
    assert [e.split(": cannot parse: ")[0] for e in errs] == ["bomb.yaml", "hive-gateway.yaml"]
    assert all("alias" in e or "anchor" in e for e in errs)
    assert sum(map(len, errs)) < 1000


def test_long_values_are_capped_in_messages_and_the_error_list_is_bounded(tmp_path):
    long = "x" * 100_000
    doc = _definition("hive-gateway", restart=long)
    doc["component"] = long
    doc[long] = 1
    errs = validate_definition(doc, "hive-gateway")
    assert errs and max(map(len, errs)) < 300
    d = tmp_path / "p"
    d.mkdir()
    for i in range(200):
        (d / f"c{i}.yaml").write_text(yaml.safe_dump({"x" * 1000 + str(j): 1 for j in range(5)}))
    errs = validate_dir(str(d))
    assert len(errs) <= 101 and errs[-1].startswith("... ") and sum(map(len, errs)) < 40_000


def test_parse_errors_do_not_echo_file_content(tmp_path):
    d = tmp_path / "p"
    d.mkdir()
    (d / "leak.yaml").write_text("component: ok\nsecret_value: SUPERSECRET-123 : : [\n")
    errs = validate_dir(str(d))
    assert len(errs) == 1 and errs[0].startswith("leak.yaml: cannot parse: ")
    assert "SUPERSECRET" not in errs[0] and "line" in errs[0] and len(errs[0]) < 300


def test_same_stem_yaml_and_yml_is_a_duplicate(tmp_path):
    d = _write(tmp_path / "p", STAGE1[:1])
    (d / "hive-gateway.yml").write_text(yaml.safe_dump(_definition("hive-gateway", order=5)))
    errs = validate_dir(str(d))
    assert "hive-gateway.yml: duplicate component 'hive-gateway' also in hive-gateway.yaml" in errs


def test_symlinks_fifos_and_non_regular_entries_are_refused(tmp_path):
    d = _write(tmp_path / "p", STAGE1[:1])
    target = tmp_path / "elsewhere.yaml"
    target.write_text(yaml.safe_dump(_definition("hive-nodes", order=1)))
    os.symlink(target, d / "hive-nodes.yaml")
    os.mkfifo(d / "fifo.yaml")
    errs = validate_dir(str(d))          # a FIFO must not block the loader
    assert "hive-nodes.yaml: not a regular file" in errs
    assert "fifo.yaml: not a regular file" in errs


def test_group_or_world_writable_directory_or_file_is_refused(tmp_path):
    d = _write(tmp_path / "p", STAGE1[:2])
    os.chmod(d / "hive-nodes.yaml", 0o666)
    assert "hive-nodes.yaml: must not be writable by group or other (chmod go-w)" in validate_dir(str(d))
    os.chmod(d / "hive-nodes.yaml", 0o644)
    os.chmod(d, 0o777)
    try:
        errs = validate_dir(str(d))
        assert errs == [f"{d}: must not be writable by group or other (chmod go-w)"]
        store = DeploymentDefinitions(lambda: str(d))
        assert not store.status()["loaded"] and store.status()["errors"] == errs
    finally:
        os.chmod(d, 0o755)


def test_the_lock_is_not_held_during_file_io(tmp_path):
    d = _write(tmp_path / "p", STAGE1[:1])
    store = DeploymentDefinitions(lambda: str(d))
    store.definitions()
    seen = []
    import herdr_engineering.control_plane.deployment_definitions as mod
    real = mod.load_definitions

    def spy(path):
        acquired = store._lock.acquire(blocking=False)
        if acquired:
            store._lock.release()
        # another thread must be able to take the lock while we read
        t = threading.Thread(target=lambda: seen.append(store._lock.acquire(timeout=1) and
                                                         (store._lock.release() or True)))
        t.start()
        t.join()
        return real(path)
    mod.load_definitions = spy
    try:
        _write(d, [_definition("farm-gateway")])
        store.definitions()
    finally:
        mod.load_definitions = real
    assert seen == [True]

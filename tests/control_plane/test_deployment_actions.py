"""Deployment actions: audited force redeploy, the host permit gate and the operator ack (deploy-4 Task 5)."""
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

from herdr_engineering.control_plane import deployment_actions as da
from herdr_engineering.control_plane import deployment_coordinator as dc
from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.bridge import load_into
from herdr_engineering.control_plane.deployment_coordinator import CoordinatorState, ReleaseEvidence
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from herdr_engineering.fleet_config import FleetConfigStore

from .test_deployment_definitions import C1, C2, CI, _definition, _write

HIVE_TOKEN = "placeholder-host-hive"
WHITEALE_TOKEN = "placeholder-host-whiteale"
OP = {"X-API-Key": "opkey"}
VIEW = {"X-API-Key": "viewkey"}
OP_DIGEST = "operator:key:" + hashlib.sha256(b"opkey").hexdigest()[:12]
DOCS = [_definition("hive-gateway", order=0), _definition("hive-nodes", order=1, gpu=True,
                                                          restart="process-restart-in-allocation"),
        _definition("whiteale-gateway", stage=2, order=0)]


def _sha(t):
    return hashlib.sha256(t.encode()).hexdigest()


class World:
    def __init__(self, tmp_path, *, actions=True, sink=None, defs=True, keyed=True):
        self.tmp = tmp_path
        self.defs = str(_write(tmp_path / "pipelines", DOCS)) if defs else None
        self.path = tmp_path / "fleet.yaml"
        self.write_config(actions)
        self.ci = CI()
        self.ci.add(10, C1)
        self.repo = ControlPlaneRepo(sink=sink)
        self.svr = ControlPlaneServer(
            self.repo, api_keys=({"opkey": "operator", "viewkey": "viewer", "agentkey": "agent"}
                                 if keyed else None),
            fleet_config=FleetConfigStore(self.path))
        self.svr.ci_monitor.snapshot = lambda start=True: self.ci()
        self.svr._deployments_stop.set()              # tick by hand
        threading.Thread(target=self.svr.serve, daemon=True).start()
        self.svr.wait_until_ready()
        self.port = self.svr.port
        self._n = 0

    def write_config(self, actions):
        values = {}
        if self.defs:
            values["deployment_definitions"] = self.defs
        if actions is not None:
            values["deployment_actions"] = actions
        self.path.write_text(yaml.safe_dump({
            "version": 1,
            "sections": {"control_plane": {"roles": ["control"], "values": values}},
            "hosts": {"hive": {"roles": ["node"], "token_sha256": _sha(HIVE_TOKEN)},
                      "whiteale": {"roles": ["node"], "token_sha256": _sha(WHITEALE_TOKEN)}},
        }) + f"# {time.time_ns()}\n")
        os.chmod(self.path, 0o600)

    # ---------------------------------------------------------------- http
    def call(self, method, path, body=None, headers=None, raw=None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        hdrs = {"Content-Type": "application/json"} if data is not None else {}
        hdrs.update(headers or {})
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data,
                                     headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"null")

    def force(self, body, headers=OP, path="/api/deployments/force"):
        return self.call("POST", path, body, headers)

    def ack(self, body, headers=OP):
        return self.call("POST", "/api/deployments/ack", body, headers)

    def requests(self, token=HIVE_TOKEN, query="?host=hive"):
        return self.call("GET", "/api/deployments/requests" + query,
                         headers={"Authorization": "Bearer " + token})

    def clear(self, rid, token=HIVE_TOKEN):
        return self.call("DELETE", f"/api/deployments/requests/{rid}",
                         headers={"Authorization": "Bearer " + token})

    def permit(self, component, commit=C1, token=HIVE_TOKEN, extra=""):
        return self.call("GET", f"/api/deployments/permit?component={component}&commit={commit}{extra}",
                         headers={"Authorization": "Bearer " + token})

    def event(self, component, outcome, *, token=HIVE_TOKEN, to=C1, instance=None, ts=None):
        self._n += 1
        ev = {"id": f"{self._n:032x}", "ts": ts or "2026-10-28T12:00:00Z",
              "host": f"{component}.box", "component": component,
              "step": f"deployment.{component}.cycle", "outcome": outcome, "from": "0" * 40,
              "to": to, "evidence": ""}
        if instance:
            ev["instance"] = instance
        status, _ = self.call("POST", "/api/deployments/events", ev,
                              {"Authorization": "Bearer " + token})
        assert status == 200

    # ---------------------------------------------------------------- state
    def audits(self):
        return [e for (t, _), stream in self.repo._events.items() if t == da.AUDIT_ENTITY
                for e in stream]

    def refused(self):
        return self.svr.deployments_view()["actions"]["refused"]

    def plan(self):
        [plan] = self.svr.deployments_view()["plans"]
        return plan

    def confirm(self):
        self.svr.deployment_evidence = lambda track, target, plan: ReleaseEvidence(target, True)

    def stage1_holding(self):
        """Stage 1 healthy and on its hold timer: whiteale-gateway (stage 2) is not permitted."""
        self.confirm()
        self.svr.deployments_tick()
        for c in ("hive-gateway", "hive-nodes"):
            self.event(c, "started")
            self.event(c, "ok")
        self.svr.deployments_tick()
        cur = self.plan()["current"]
        assert cur["type"] == "Hold" and cur["stage"] == 1 and cur["remaining_s"] > 0
        assert "whiteale-gateway" not in self.plan()["permitted"]

    def close(self):
        self.svr.shutdown()


@pytest.fixture()
def world(tmp_path):
    w = World(tmp_path)
    yield w
    w.close()


@pytest.fixture()
def world_off(tmp_path):
    w = World(tmp_path, actions=None)
    yield w
    w.close()


def _refused(audits):
    return [a for a in audits if a["payload"]["outcome"] == "refused"]


def _accepted(audits):
    return [a for a in audits if a["payload"]["outcome"] == "accepted"]


# ------------------------------------------------------------------ force: refusals
def test_force_is_refused_without_the_flag(world_off):
    w = world_off
    w.svr.deployments_tick()
    status, body = w.force({"component": "hive-gateway"})
    assert status == 403 and "deployment_actions" in body["error"]
    assert w.repo.deployment_requests.pending() == []
    [row] = w.audits()
    assert row["payload"]["outcome"] == "refused" and row["payload"]["actor"] == OP_DIGEST
    assert row["payload"]["target"] == {"component": "hive-gateway"}
    assert w.requests()[1]["requests"] == []


def test_the_flag_must_be_exactly_true(tmp_path):
    w = World(tmp_path, actions="yes")
    try:
        w.svr.deployments_tick()
        assert w.force({"component": "hive-gateway"})[0] == 403
    finally:
        w.close()


def test_force_needs_the_operator_role_and_auth_refusals_are_never_accepted(world):
    w = world
    w.svr.deployments_tick()
    assert w.force({"component": "hive-gateway"}, headers={})[0] == 401
    assert w.force({"component": "hive-gateway"}, headers={"X-API-Key": "nope"})[0] == 401
    assert w.force({"component": "hive-gateway"}, headers=VIEW)[0] == 403
    assert w.force({"component": "hive-gateway"}, headers={"X-API-Key": "agentkey"})[0] == 403
    assert w.ack({"component": "hive-nodes", "instance": "job-1"}, headers=VIEW)[0] == 403
    assert w.repo.deployment_requests.pending() == []
    assert w.audits() == []                         # refused before authentication: no row ...
    assert w.refused() == {"force: unauthorized": 2, "force: operator role required": 2,
                           "ack: operator role required": 1}      # ... only a counter


def test_a_host_token_cannot_post_force(world):
    w = world
    w.svr.deployments_tick()
    bearer = {"Authorization": "Bearer " + HIVE_TOKEN}
    assert w.force({"component": "hive-gateway"}, headers=bearer)[0] == 401
    assert w.force({"component": "hive-gateway"}, headers={"X-Host-Token": HIVE_TOKEN})[0] == 401
    # Even beside an operator key, a host credential on the request is refused.
    assert w.force({"component": "hive-gateway"}, headers={**OP, **bearer})[0] == 403
    assert w.repo.deployment_requests.pending() == [] and w.audits() == []
    assert w.refused() == {"force: unauthorized": 2, "force: a host token grants no operator action": 1}


def test_a_thousand_unauthenticated_refusals_write_no_rows(world, caplog):
    w = world
    with caplog.at_level("WARNING", logger="herdr_engineering.control_plane.api"):
        for _ in range(1000):
            w.svr.deployment_refused("force", None, 401, "unauthorized", auth=True)
        assert w.force({"component": "hive-gateway"}, headers={})[0] == 401
    assert w.audits() == [] and w.repo._event_order == []
    assert w.refused() == {"force: unauthorized": 1001}
    assert len([r for r in caplog.records if "refused before authentication" in r.getMessage()]) == 1


@pytest.mark.parametrize("headers", [
    {},
    {"Host": "evil.example", "Origin": "http://evil.example"},          # DNS rebinding
    {"Origin": "null"},
    {"Origin": "https://127.0.0.1"},
])
def test_open_mode_refuses_deployment_actions(tmp_path, headers):
    w = World(tmp_path, keyed=False)
    try:
        w.svr.deployments_tick()
        status, body = w.force({"component": "hive-gateway"}, headers=headers)
        assert status == 403 and body["error"] == "deployment actions require API keys (keyed mode)"
        assert w.ack({"component": "hive-nodes", "instance": "job-1"}, headers=headers)[0] == 403
        assert w.repo.deployment_requests.pending() == [] and w.audits() == []
        assert w.refused() == {"force: deployment actions require API keys (keyed mode)": 1,
                               "ack: deployment actions require API keys (keyed mode)": 1}
        # The host routes are unaffected by the mode.
        assert w.permit("hive-gateway")[0] == 200
    finally:
        w.close()


def test_force_refuses_cross_origin_and_non_json(world):
    w = world
    w.svr.deployments_tick()
    here = f"127.0.0.1:{w.port}"
    cases = [
        ({"Origin": "http://evil.example"}, "cross-origin deployment action refused"),
        ({"Origin": "null"}, "cross-origin deployment action refused"),
        ({"Origin": ""}, "cross-origin deployment action refused"),           # present but empty
        ({"Origin": f"ftp://{here}"}, "cross-origin deployment action refused"),
        ({"Host": f"evil.example:{w.port}", "Origin": f"http://evil.example:{w.port}"},
         "deployment actions need a dashboard host"),                               # rebinding
        ({"Sec-Fetch-Site": "cross-site"}, "cross-site deployment action refused"),
        ({"Sec-Fetch-Site": "same-site", "Origin": f"http://{here}"}, "cross-site deployment action refused"),
    ]
    for extra, _ in cases:
        assert w.force({"component": "hive-gateway"}, headers={**OP, **extra})[0] == 403, extra
    status, _ = w.call("POST", "/api/deployments/force", raw=b"component=hive-gateway",
                       headers={**OP, "Content-Type": "application/x-www-form-urlencoded"})
    assert status == 415
    assert w.repo.deployment_requests.pending() == []
    audits = w.audits()
    assert _accepted(audits) == []
    assert [a["payload"]["reason"] for a in audits] == [r for _, r in cases] + [
        "deployment actions require application/json"]
    assert all(a["payload"]["actor"] == OP_DIGEST and a["payload"]["outcome"] == "refused"
               for a in audits)
    # Refusal rows stay out of the shared feed.
    assert not any(t == da.AUDIT_ENTITY for t, _, _ in w.repo._event_order)
    # A same-origin browser request is fine.
    status, _ = w.force({"component": "hive-gateway"},
                        headers={**OP, "Origin": f"http://{here}", "Sec-Fetch-Site": "same-origin"})
    assert status == 200


@pytest.mark.parametrize("body,status,target", [
    ({"component": "no-such-thing"}, 404, {"component": "no-such-thing"}),
    ({"stage": 7}, 404, {"stage": 7}),
    ({"component": "Bad Name!"}, 400, None),
    ({"component": "x" * 65}, 400, None),
    ({"stage": True}, 400, None),
    ({"stage": "1"}, 400, None),
    ({"component": "hive-gateway", "stage": 1}, 400, None),
    ({}, 400, None),
    ({"component": "hive-gateway", "extra": 1}, 400, None),
    (["hive-gateway"], 400, None),
])
def test_force_validates_its_body_against_the_definitions(world, body, status, target):
    w = world
    w.svr.deployments_tick()
    assert w.force(body)[0] == status
    assert w.repo.deployment_requests.pending() == []
    [row] = w.audits()
    assert row["payload"]["outcome"] == "refused" and row["payload"]["target"] == target


def test_force_caps_its_body_size(world):
    w = world
    w.svr.deployments_tick()
    raw = json.dumps({"component": "hive-gateway", "pad": "x" * da.MAX_BODY_BYTES}).encode()
    status, _ = w.call("POST", "/api/deployments/force", raw=raw, headers=OP)
    assert status == 413
    assert w.call("POST", "/api/deployments/force", raw=b"{not json", headers=OP)[0] == 400
    assert len(_refused(w.audits())) == 2


def test_force_without_a_target_release_is_refused(tmp_path):
    w = World(tmp_path)
    try:
        w.ci.rows.clear()
        w.svr.deployments_tick()
        status, body = w.force({"component": "hive-gateway"})
        assert status == 409 and "target" in body["error"]
    finally:
        w.close()


def test_force_without_loaded_definitions_is_refused(tmp_path):
    w = World(tmp_path, defs=False)
    try:
        w.svr.deployments_tick()
        assert w.force({"component": "hive-gateway"})[0] == 503
        assert _refused(w.audits())
    finally:
        w.close()


def test_an_audit_that_cannot_be_written_refuses_the_request(tmp_path):
    class Broken:
        def record(self, ev):
            pass

        def record_required(self, ev):
            raise OSError("disk full")

    w = World(tmp_path, sink=Broken())
    try:
        w.svr.deployments_tick()
        status, body = w.force({"component": "hive-gateway"})
        assert status == 503 and body["error"] == "audit_persistence_failed"
        assert w.repo.deployment_requests.pending() == []
    finally:
        w.close()


# ------------------------------------------------------------------ force: accepted
def test_an_accepted_force_is_audited_with_actor_target_and_time(world):
    w = world
    w.svr.deployments_tick()
    before = time.time()
    status, body = w.force({"component": "hive-gateway"}, path="/api/v1/deployments/force")
    assert status == 200 and body["created"] is True
    rid = body["request"]["id"]
    assert body["request"]["components"] == {"hive-gateway": {
        "target_commit": C1, "generation": 0, "cleared_at": None, "cleared_by": None}}
    [row] = w.audits()
    p = row["payload"]
    assert row["event_type"] == "DeploymentForceRequested"
    assert p["outcome"] == "accepted" and p["actor"] == OP_DIGEST
    assert p["target"] == {"component": "hive-gateway"} and p["request_id"] == rid
    assert p["components"] == ["hive-gateway"]
    assert before - 1 <= p["at_epoch"] <= time.time() + 1 and p["at"].endswith("Z")
    # An identical pending request is not duplicated, but the ask is still audited.
    status, again = w.force({"component": "hive-gateway"})
    assert status == 200 and again["created"] is False and again["request"]["id"] == rid
    assert len(w.repo.deployment_requests.pending()) == 1
    assert len(_accepted(w.audits())) == 2 and w.audits()[-1]["payload"]["existing"] is True
    # The operator's page lists it.
    view = w.svr.deployments_view()
    assert view["actions"] == {"enabled": True, "keyed": True, "refused": {}}
    assert [r["id"] for r in view["requests"]] == [rid]


def test_hosts_see_only_requests_for_their_own_components(world):
    w = world
    w.svr.deployments_tick()
    status, body = w.force({"stage": 1})
    assert status == 200 and set(body["request"]["components"]) == {"hive-gateway", "hive-nodes"}
    w.force({"component": "whiteale-gateway"})
    status, hive = w.requests()
    assert status == 200 and hive["host"] == "hive"
    assert sorted(r["component"] for r in hive["requests"]) == ["hive-gateway", "hive-nodes"]
    assert all(r["target_commit"] == C1 and "actor" not in r for r in hive["requests"])
    status, white = w.requests(WHITEALE_TOKEN, "?host=whiteale")
    assert [r["component"] for r in white["requests"]] == ["whiteale-gateway"]
    assert w.requests(query="")[1]["host"] == "hive"                    # host defaults to the token's
    assert w.requests(query="?host=whiteale")[0] == 403                 # another host's list
    assert w.call("GET", "/api/deployments/requests?host=hive")[0] == 401
    assert w.call("GET", "/api/deployments/requests?host=hive", headers=OP)[0] == 401
    assert w.call("GET", "/api/v1/deployments/requests?host=hive",
                  headers={"Authorization": "Bearer " + HIVE_TOKEN})[0] == 200
    # The host token grants nothing else.
    assert w.call("GET", "/api/v1/deployments",
                  headers={"Authorization": "Bearer " + HIVE_TOKEN})[0] == 401


def test_only_the_owning_host_clears_a_request(world):
    w = world
    w.svr.deployments_tick()
    stage1 = w.force({"stage": 1})[1]["request"]["id"]
    white = w.force({"component": "whiteale-gateway"})[1]["request"]["id"]
    assert w.clear(white, HIVE_TOKEN)[0] == 404                         # not hive's
    assert w.clear(stage1, WHITEALE_TOKEN)[0] == 404
    assert w.call("DELETE", f"/api/deployments/requests/{stage1}")[0] == 401
    assert w.call("DELETE", f"/api/deployments/requests/{stage1}", headers=OP)[0] == 401
    status, body = w.clear(stage1)
    assert status == 200 and sorted(body["cleared"]) == ["hive-gateway", "hive-nodes"]
    assert w.requests()[1]["requests"] == []
    assert [r["component"] for r in w.requests(WHITEALE_TOKEN, "?host=whiteale")[1]["requests"]] == [
        "whiteale-gateway"]
    assert w.clear(stage1)[0] == 404                                    # cleared once
    assert w.clear("force-not-an-id")[0] == 404
    pickups = [a for a in w.audits() if a["event_type"] == "DeploymentForcePickedUp"]
    assert len(pickups) == 1 and pickups[0]["payload"]["host"] == "hive"


def test_a_cleared_request_can_be_asked_for_again(world):
    w = world
    w.svr.deployments_tick()
    first = w.force({"component": "hive-gateway"})[1]["request"]["id"]
    w.clear(first)
    status, body = w.force({"component": "hive-gateway"})
    assert body["created"] is True and body["request"]["id"] != first


def test_force_requests_survive_a_restart(tmp_path, acknowledged_sink):
    w = World(tmp_path, sink=acknowledged_sink)
    try:
        w.svr.deployments_tick()
        stage1 = w.force({"stage": 1})[1]["request"]["id"]
        w.clear(stage1)
        keep = w.force({"component": "whiteale-gateway"})[1]["request"]["id"]
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
    assert not any(k[0] == da.ENTITY_TYPE for k in fresh._events)
    assert [r["id"] for r in fresh.deployment_requests.pending()] == [keep]
    # The audit trail is ordinary durable history.
    assert any(k[0] == da.AUDIT_ENTITY for k in fresh._events)


def test_the_request_store_is_bounded_by_count_and_age():
    clock = [1000.0]
    store = da.ForceRequests(now=lambda: clock[0])
    for i in range(da.MAX_PENDING):
        store.add("component", f"c{i}", {f"c{i}": (C1, 1)}, "operator:x")
    with pytest.raises(da.ActionRefused) as exc:
        store.add("component", "one-more", {"one-more": (C1, 1)}, "operator:x")
    assert exc.value.status == 429
    clock[0] += da.REQUEST_TTL_S + 1
    assert store.pending() == []
    store.add("component", "c0", {"c0": (C1, 1)}, "operator:x")
    assert len(store.pending()) == 1


def test_hydrate_skips_malformed_and_expired_requests():
    clock = [da.REQUEST_TTL_S + 5000.0]
    store = da.ForceRequests(now=lambda: clock[0])
    good = {"kind": "request", "id": "force-" + "a" * 16, "type": "component", "target": "hive-nodes",
            "requested_at": clock[0] - 10, "actor": "operator:x",
            "components": {"hive-nodes": {"target_commit": C1, "generation": 1}}}
    rows = [{"payload": good, "sequence": 1},
            {"payload": dict(good, id="force-" + "b" * 16, requested_at=1.0), "sequence": 2},
            {"payload": dict(good, id="nope"), "sequence": 3},
            {"payload": dict(good, id="force-" + "c" * 16, components={"Bad Name": {}}), "sequence": 4},
            {"payload": dict(good, id="force-" + "d" * 16, requested_at=True), "sequence": 5},
            {"payload": "junk"}]
    assert store.hydrate(rows) == 1
    assert [r["id"] for r in store.pending()] == ["force-" + "a" * 16]


# ------------------------------------------------------------------ permit
def test_permit_follows_the_newest_tick(world):
    w = world
    w.svr.deployments_tick()                                            # default evidence
    status, body = w.permit("hive-gateway")
    assert status == 200 and body["permitted"] is False
    assert body["reason"] == "not permitted: Hold release not confirmed"
    assert body["target_commit"] == C1 and body["generation"] == 0 and body["forced"] is False
    w.confirm()
    w.svr.deployments_tick()
    assert w.permit("hive-gateway")[1] == {
        "permitted": True, "reason": "permitted by the plan", "generation": 1,
        "target_commit": C1, "forced": False, "request_id": None}
    assert w.permit("hive-nodes")[1]["permitted"] is False             # not its turn yet
    body = w.permit("hive-gateway", commit=C2)[1]
    assert body["permitted"] is False and body["reason"] == "commit is not the plan target"
    assert w.permit("hive-gateway", commit=C1[:12])[1]["permitted"] is True


def test_permit_is_for_the_hosts_own_components_only(world):
    w = world
    w.svr.deployments_tick()
    assert w.permit("whiteale-gateway")[0] == 403                       # hive asks for whiteale's
    assert w.permit("whiteale-gateway", token=WHITEALE_TOKEN)[0] == 200
    assert w.permit("no-such-thing")[0] == 403
    assert w.call("GET", f"/api/deployments/permit?component=hive-gateway&commit={C1}")[0] == 401
    assert w.call("GET", f"/api/deployments/permit?component=hive-gateway&commit={C1}",
                  headers=OP)[0] == 401
    assert w.call("GET", "/api/deployments/permit?component=hive-gateway&commit=zz",
                  headers={"Authorization": "Bearer " + HIVE_TOKEN})[0] == 400
    assert w.call("GET", "/api/deployments/permit?component=Bad!&commit=" + C1,
                  headers={"Authorization": "Bearer " + HIVE_TOKEN})[0] == 400


def test_a_stale_or_missing_tick_permits_nothing(world):
    w = world
    body = w.permit("hive-gateway")[1]                                  # no tick yet
    assert body["permitted"] is False and body["reason"] == "no coordinator tick yet"
    w.confirm()
    w.svr.deployments_tick()
    assert w.permit("hive-gateway")[1]["permitted"] is True
    w.svr._deployments_last["ticked_at"] -= da.PERMIT_STALE_S + 1
    body = w.permit("hive-gateway")[1]
    assert body["permitted"] is False and body["reason"] == "the coordinator tick is stale"
    w.svr.deployments_tick()
    w.svr._deployments_last["stale"] = True                              # a failed tick
    assert w.permit("hive-gateway")[1]["permitted"] is False


def test_permit_without_definitions_is_false(tmp_path):
    w = World(tmp_path, defs=False)
    try:
        w.svr.deployments_tick()
        body = w.permit("hive-gateway")[1]
        assert body["permitted"] is False and body["reason"] == "no deployment definitions loaded"
        assert w.requests()[1]["requests"] == []
    finally:
        w.close()


def test_a_force_overrides_the_hold_timer_once(world):
    w = world
    w.stage1_holding()
    assert w.permit("whiteale-gateway", token=WHITEALE_TOKEN)[1]["permitted"] is False
    rid = w.force({"component": "whiteale-gateway"})[1]["request"]["id"]
    body = w.permit("whiteale-gateway", token=WHITEALE_TOKEN)[1]
    assert body["permitted"] is True and body["forced"] is True and body["request_id"] == rid
    assert body["reason"] == "forced redeploy"
    assert w.permit("whiteale-gateway", commit=C2, token=WHITEALE_TOKEN)[1]["permitted"] is False
    assert w.clear(rid, WHITEALE_TOKEN)[0] == 200                       # picked up: spent
    assert w.permit("whiteale-gateway", token=WHITEALE_TOKEN)[1]["permitted"] is False


def test_a_force_overrides_a_stage_halt(world):
    w = world
    w.confirm()
    w.svr.deployments_tick()
    w.event("hive-gateway", "started")
    w.event("hive-gateway", "ok")
    w.event("hive-nodes", "started", instance="job-1")
    w.event("hive-nodes", "failed", instance="job-1")
    w.svr.deployments_tick()
    cur = w.plan()["current"]
    assert cur["type"] == "Halt" and cur["stage"] == 1 and w.plan()["permitted"] == []
    assert w.permit("hive-nodes")[1]["permitted"] is False
    rid = w.force({"stage": 1})[1]["request"]["id"]
    for c in ("hive-gateway", "hive-nodes"):
        body = w.permit(c)[1]
        assert body["permitted"] is True and body["forced"] is True and body["request_id"] == rid
    assert w.permit("whiteale-gateway", token=WHITEALE_TOKEN)[1]["permitted"] is False
    # A halt in an EARLIER stage blocks a later stage's force.
    w.force({"component": "whiteale-gateway"})
    body = w.permit("whiteale-gateway", token=WHITEALE_TOKEN)[1]
    assert body["permitted"] is False and body["forced"] is False
    assert body["reason"].startswith("a forced redeploy cannot override: Halt")


def test_permit_checks_the_generation_when_given(world):
    w = world
    w.confirm()
    w.svr.deployments_tick()
    assert w.permit("hive-gateway", extra="&generation=1")[1]["permitted"] is True
    body = w.permit("hive-gateway", extra="&generation=0")[1]
    assert body["permitted"] is False and body["reason"] == "generation changed"
    assert w.permit("hive-gateway", extra="&generation=-1")[0] == 400
    assert w.permit("hive-gateway", extra="&generation=x")[0] == 400
    assert w.permit("hive-gateway", extra="&bogus=1")[0] == 400


def test_one_pickup_spends_overlapping_forces(world):
    w = world
    w.stage1_holding()
    w.svr.deployments_tick()
    # A component force and a stage force cover hive-nodes at the same commit.
    single = w.force({"component": "hive-nodes"})[1]["request"]["id"]
    stage = w.force({"stage": 1})[1]["request"]["id"]
    assert single != stage
    status, body = w.clear(single)
    assert status == 200 and body["cleared"] == ["hive-nodes"]
    # The stage request's hive-nodes entry is spent too; its hive-gateway entry is not.
    assert [r["component"] for r in w.requests()[1]["requests"]] == ["hive-gateway"]
    assert w.repo.deployment_requests.active("hive-nodes", C1) is None


def test_a_pickup_that_cannot_be_persisted_answers_503_and_stays_pending(tmp_path):
    class Flaky:
        fail = False
        events = []

        def record(self, ev):
            self.events.append(ev)

        def record_required(self, ev):
            if self.fail:
                raise OSError("disk full")
            self.events.append(ev)

    sink = Flaky()
    w = World(tmp_path, sink=sink)
    try:
        w.stage1_holding()
        rid = w.force({"component": "whiteale-gateway"})[1]["request"]["id"]
        sink.fail = True
        status, body = w.clear(rid, WHITEALE_TOKEN)
        assert status == 503 and "do not redeploy" in body["error"]
        assert w.permit("whiteale-gateway", token=WHITEALE_TOKEN)[1]["forced"] is True
        assert len(w.requests(WHITEALE_TOKEN, "?host=whiteale")[1]["requests"]) == 1
        sink.fail = False
        assert w.clear(rid, WHITEALE_TOKEN)[0] == 200
        assert w.permit("whiteale-gateway", token=WHITEALE_TOKEN)[1]["forced"] is False
    finally:
        w.close()


def test_a_force_never_overrides_release_not_confirmed(world):
    w = world
    w.svr.deployments_tick()                                            # default evidence: unknown
    w.force({"component": "hive-gateway"})
    body = w.permit("hive-gateway")[1]
    assert body["permitted"] is False and body["forced"] is False
    assert body["reason"].startswith("a forced redeploy cannot override")
    # A timer hold over an unpublished release (ancestry known) is not overridable either.
    w.stage1_holding()
    w.force({"component": "whiteale-gateway"})
    assert w.permit("whiteale-gateway", token=WHITEALE_TOKEN)[1]["permitted"] is True
    w.svr.deployment_evidence = lambda track, target, plan: ReleaseEvidence(None, True)
    w.svr.deployments_tick()
    assert w.plan()["current"]["type"] == "Hold" and w.plan()["release_confirmed"] is False
    assert w.permit("whiteale-gateway", token=WHITEALE_TOKEN)[1]["permitted"] is False


def test_a_force_never_overrides_target_off_main(world):
    w = world
    w.stage1_holding()
    w.force({"stage": 1})
    w.force({"component": "whiteale-gateway"})
    w.svr.deployment_evidence = lambda track, target, plan: ReleaseEvidence(target, False)
    w.svr.deployments_tick()
    cur = w.plan()["current"]
    assert cur["type"] == "Halt" and cur["stage"] is None
    for c, token in (("hive-gateway", HIVE_TOKEN), ("whiteale-gateway", WHITEALE_TOKEN)):
        body = w.permit(c, token=token)[1]
        assert body["permitted"] is False
        assert "target no longer on main" in body["reason"]


def test_a_replan_voids_an_old_force(world):
    w = world
    w.stage1_holding()
    w.force({"component": "whiteale-gateway"})
    w.ci.add(11, C2)
    w.svr.deployments_tick()
    assert w.plan()["target_commit"] == C2
    body = w.permit("whiteale-gateway", commit=C2, token=WHITEALE_TOKEN)[1]
    assert body["permitted"] is False and body["forced"] is False


def test_turning_the_flag_off_disables_pending_forces(world):
    w = world
    w.stage1_holding()
    w.force({"component": "whiteale-gateway"})
    assert w.permit("whiteale-gateway", token=WHITEALE_TOKEN)[1]["permitted"] is True
    w.write_config(False)
    assert w.permit("whiteale-gateway", token=WHITEALE_TOKEN)[1]["permitted"] is False
    assert w.requests(WHITEALE_TOKEN, "?host=whiteale")[1]["requests"] == []
    assert w.svr.deployments_view()["actions"]["enabled"] is False
    # The plan's own permission does not depend on the flag.
    assert w.permit("hive-gateway")[1]["permitted"] is True


def test_permit_and_requests_have_no_side_effects(world):
    w = world
    w.stage1_holding()
    w.force({"component": "whiteale-gateway"})
    n = len(w.audits())
    for _ in range(3):
        assert w.permit("whiteale-gateway", token=WHITEALE_TOKEN)[1]["permitted"] is True
        w.requests(WHITEALE_TOKEN, "?host=whiteale")
    assert len(w.audits()) == n and len(w.repo.deployment_requests.pending()) == 1


# ------------------------------------------------------------------ ack
def _halted_on_job1(w):
    w.confirm()
    w.svr.deployments_tick()
    w.event("hive-gateway", "started")
    w.event("hive-gateway", "ok")
    w.event("hive-nodes", "started", instance="job-1", ts="2026-10-28T12:00:00Z")
    w.event("hive-nodes", "failed", instance="job-1", ts="2026-10-28T12:00:01Z")
    w.event("hive-nodes", "started", instance="job-2", ts="2026-10-28T12:00:02Z")
    w.event("hive-nodes", "ok", instance="job-2", ts="2026-10-28T12:00:03Z")
    w.svr.deployments_tick()
    assert w.plan()["current"]["type"] == "Halt"


def test_an_ack_lifts_a_halt_from_a_stale_failed_instance(world):
    w = world
    _halted_on_job1(w)
    attempts = w.repo.deployment_plans.attempts("hive-nodes", C1)
    status, body = w.ack({"component": "hive-nodes", "instance": "job-1"})
    assert status == 200 and body["acknowledged"] == [C1]
    for _ in range(2):            # the failure is still in the timeline and is re-observed
        w.svr.deployments_tick()
        assert w.plan()["current"]["type"] != "Halt"
        assert w.plan()["current"]["type"] == "Hold" and w.plan()["current"]["stage"] == 1
    assert w.repo.deployment_plans.attempts("hive-nodes", C1) == attempts
    assert w.repo.deployment_plans.attempts("hive-nodes", C1, instance="job-1") == 1
    [row] = [a for a in w.audits() if a["event_type"] == "DeploymentInstanceAcknowledged"]
    assert row["payload"]["actor"] == OP_DIGEST and row["payload"]["outcome"] == "accepted"
    assert row["payload"]["target"] == {"component": "hive-nodes", "instance": "job-1",
                                        "release": C1}
    assert row["payload"]["acknowledged"] == [
        {"release": C1, "outcome": "failed", "host_ts": "2026-10-28T12:00:01Z"}]


def test_an_ack_does_not_hide_a_newer_failure_of_the_same_instance(world):
    w = world
    _halted_on_job1(w)
    w.ack({"component": "hive-nodes", "instance": "job-1"})
    w.svr.deployments_tick()
    assert w.plan()["current"]["type"] != "Halt"
    w.event("hive-nodes", "failed", instance="job-1", ts="2026-10-28T12:05:00Z")
    w.svr.deployments_tick()
    assert w.plan()["current"]["type"] == "Halt"


def test_ack_refusals_are_audited(world_off):
    w = world_off
    assert w.ack({"component": "hive-nodes", "instance": "job-1"})[0] == 403          # flag off
    w.write_config(True)
    assert w.ack({"component": "hive-nodes", "instance": "job-9"})[0] == 409          # no tick: no target
    w.svr.deployments_tick()
    assert w.ack({"component": "hive-nodes", "instance": "job-9"})[0] == 404          # unknown instance
    assert w.ack({"component": "hive-nodes", "instance": "bad id!"})[0] == 400
    assert w.ack({"component": "hive-nodes"})[0] == 400                               # no instance, no release
    assert w.ack({"component": "nope", "instance": "job-1"})[0] == 404
    assert w.ack({"component": "hive-nodes", "instance": "job-1", "release": "zz"})[0] == 400
    audits = w.audits()
    assert len(audits) == 7 and _accepted(audits) == []


def test_an_ack_survives_a_restart(tmp_path, acknowledged_sink):
    w = World(tmp_path, sink=acknowledged_sink)
    try:
        _halted_on_job1(w)
        assert w.ack({"component": "hive-nodes", "instance": "job-1"})[0] == 200
        w.svr.deployments_tick()
        assert w.plan()["current"]["type"] != "Halt"
        timeline = w.repo.deployments
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
    fresh.deployments = timeline          # the old failure is still in the monitor's window
    defs = w.svr.deployment_definitions.definitions()
    fresh.deployment_plans.observe(timeline.snapshot()["components"], defs)
    slots = fresh.deployment_plans.slots_for(C1, defs)
    assert set(slots["hive-nodes"]["instances"]) == {"job-2"}


# ------------------------------------------------------------------ CoordinatorState.acknowledge
def _slot(outcome, ts="2026-10-28T12:00:00Z", rx=1.0):
    return {"outcome": outcome, "host_ts": ts, "rx": rx}


def test_state_ack_tombstones_the_slot_and_keeps_attempts():
    records = []
    st = CoordinatorState(persist=records.append)
    st._emit(st._put_slot("hive-nodes", C1, "job-1", _slot("failed")))
    st._add_attempt("hive-nodes", C1, "job-1", "k1")
    assert st.acknowledge("hive-nodes", C1, "job-1") == [C1]
    assert st.slots_for(C1, {"hive-nodes": None})["hive-nodes"]["instances"] == {}
    assert st.attempts("hive-nodes", C1) == 1
    assert st._put_slot("hive-nodes", C1, "job-1", _slot("failed")) == []         # the same event again
    st._emit(st._put_slot("hive-nodes", C1, "job-1", _slot("ok", "2026-10-28T12:00:09Z", 2.0)))
    assert st.acknowledge("hive-nodes", C1, "job-404") == []
    assert st.acknowledge("hive-nodes", C2, "job-1") == []
    assert records[-1]["payload"]["kind"] == "slot"
    replay = CoordinatorState()
    replay.hydrate(records)
    assert replay.slots_for(C1, {"hive-nodes": None})["hive-nodes"]["instances"]["job-1"]["outcome"] == "ok"


def test_state_ack_lifts_a_failure_evicted_release(monkeypatch):
    monkeypatch.setattr(dc, "INSTANCES_PER_RELEASE", 2)
    records = []
    st = CoordinatorState(persist=lambda r: records.append(r))
    for i in range(3):
        st._put_slot("hive-nodes", C1, f"job-{i}", _slot("failed", rx=float(i)))
    assert st.slots_for(C1, {"hive-nodes": None})["hive-nodes"]["failure_evicted"] is True
    assert st.acknowledge("hive-nodes", C1, None) == [C1]
    assert st.slots_for(C1, {"hive-nodes": None})["hive-nodes"]["failure_evicted"] is False
    assert st.acknowledge("hive-nodes", C1, None) == []                            # nothing left


def test_state_ack_replays_in_sequence_order():
    records = []
    st = CoordinatorState(persist=records.append)
    st._emit(st._put_slot("hive-nodes", C1, "job-1", _slot("failed")))
    st.acknowledge("hive-nodes", C1, "job-1")
    assert [r["payload"]["kind"] for r in records] == ["slot", "ack"]
    fresh = CoordinatorState()
    assert fresh.hydrate(list(reversed(records))) == 2
    assert fresh.slots_for(C1, {"hive-nodes": None})["hive-nodes"]["instances"] == {}
    # the old event observed again after a restart stays acknowledged
    assert fresh._put_slot("hive-nodes", C1, "job-1", _slot("failed")) == []
    bad = dict(records[1], payload=dict(records[1]["payload"], outcome="exploded"))
    assert CoordinatorState().hydrate([bad]) == 0


@pytest.mark.parametrize("current,ok", [
    ({"type": "Start", "stage": 2, "order": 0}, True),
    ({"type": "Hold", "stage": 1, "remaining_s": 30, "reason": "holding stage 1"}, True),
    ({"type": "Halt", "stage": 2, "reason": "stage 2 halted: x: failed"}, True),
    ({"type": "Halt", "stage": 1, "reason": "stage 1 halted: x: failed"}, False),   # an earlier stage
    ({"type": "Hold", "stage": None, "remaining_s": None, "reason": "target ancestry not confirmed"}, False),
    ({"type": "Hold", "stage": 1, "remaining_s": None, "reason": "release not confirmed"}, False),
    ({"type": "Halt", "stage": None, "reason": "target no longer on main"}, False),
    ({"type": "Idle", "reason": "no green release"}, False),
])
def test_a_force_overrides_staging_only_even_with_confirmed_evidence(current, ok):
    """Defence in depth: the gate shape alone refuses, whatever the evidence flags say."""
    now = 1000.0
    view = {"target_commit": C1, "generation": 3, "permitted": [], "current": current,
            "release_confirmed": True, "target_on_main": True}
    meta = {"ticked_at": now, "stale": False, "error": None}
    out = da.permit_decision(meta, view, "whiteale-gateway", C1, "force-" + "e" * 16, now=now,
                             component_stage=2)
    assert out["permitted"] is ok and out["forced"] is ok and out["generation"] == 3

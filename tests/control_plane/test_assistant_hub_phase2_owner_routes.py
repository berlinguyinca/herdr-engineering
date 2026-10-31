"""Owner routes and CLI for action requests, grants and worker scopes (Phase 2, Task 8).

A real ControlPlaneServer and a real HubServer share ONE WorkerRegistry and ONE store; nothing is mocked."""
import hashlib
import json
import os
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from herdr_engineering import cli
from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.assistant import AssistantService, assistant_config
from herdr_engineering.control_plane.assistant_hub.actiongrants import ActionGrant, GrantState
from herdr_engineering.control_plane.assistant_hub.canonical import RawAction, derive
from herdr_engineering.control_plane.assistant_hub.mcp import Hub
from herdr_engineering.control_plane.assistant_hub.policy import activate_grant
from herdr_engineering.control_plane.assistant_hub.server import HubServer
from herdr_engineering.control_plane.assistant_hub.workers import WorkerRegistry
from herdr_engineering.control_plane.repo import ControlPlaneRepo

KEYS = {"alice": "p2-route-alice-key", "bea": "p2-route-bea-key", "viewer": "p2-route-viewer-key"}
ROLES = {KEYS["alice"]: "operator", KEYS["bea"]: "operator", KEYS["viewer"]: "viewer"}
PUSH = "git push origin main"
SECRET_CMD = "frobnicate --all --token=hunter2-CANARY"
ACT = ["ask", "report", "read", "act"]


def pid(name):
    return "key:" + hashlib.sha256(KEYS[name].encode()).hexdigest()[:12]


def call(base, method, path, body=None, headers=None, raw=None):
    h = {"Content-Type": "application/json", **(headers or {})}
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(base + path, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        raw_body = e.read()
        try:
            return e.code, json.loads(raw_body or b"null")
        except ValueError:
            return e.code, raw_body


def owner(base, method, path, body=None, who="alice", raw=None):
    return call(base, method, path, body, {"X-API-Key": KEYS[who]}, raw=raw)


@pytest.fixture()
def world(acknowledged_sink):
    long_ago = (datetime.now(UTC) - timedelta(days=60)).isoformat()
    section = {"enabled": True, "tenancy": {"principals": {pid("alice"): "acme", pid("bea"): "globex"}},
               "grants": {"shadow_started_at": long_ago},
               "hub": {"enabled": True, "host": "100.64.0.9", "port": 9483, "allowed_networks": ["127.0.0.0/8"]}}
    svc = AssistantService(config=assistant_config({"personal_assistant": section}))
    registry = WorkerRegistry(svc.store, enrollment_ttl=timedelta(minutes=10), worker_ttl=timedelta(days=30))
    hub = HubServer(Hub(registry=registry, service=svc, store=svc.store), host="127.0.0.1", port=0,
                    allowed_networks=["127.0.0.0/8"], allowed_hosts=[])
    hub.start()
    hub.wait_until_ready()
    svr = ControlPlaneServer(ControlPlaneRepo(sink=acknowledged_sink), assistant_service=svc, api_keys=ROLES,
                             assistant_workers=registry)
    threading.Thread(target=svr.serve, daemon=True).start()
    svr.wait_until_ready()
    yield f"http://127.0.0.1:{svr.port}", f"http://127.0.0.1:{hub.port}", svc, registry
    svr.shutdown()
    hub.shutdown()


def new_worker(base, hub_base, scopes=None, who="alice", device="dev1"):
    body = {"name": "box", "harness": "claude"}
    if scopes is not None:
        body["scopes"] = scopes
    status, created = owner(base, "POST", "/api/v1/assistant-workers", body, who)
    assert status == 201, created
    status, red = call(hub_base, "POST", "/assistant/enroll", {"code": created["code"], "device_id": device})
    assert status == 200, red
    return red["worker_id"], red["credential"], device


def mcp(hub_base, cred, device, method, params=None):
    msg = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        msg["params"] = params
    return call(hub_base, "POST", "/assistant/mcp", msg, {"Authorization": f"Worker {cred}", "X-Herdr-Device": device})


def action_request(hub_base, worker, command, session="s1", task="t1"):
    wid, cred, device = worker
    status, body = mcp(hub_base, cred, device, "tools/call", {"name": "action_request", "arguments": {
        "tool": "Bash", "command": command, "repo": "acme/repo", "session_id": session, "task_id": task}})
    assert status == 200, body
    text = body["result"]["content"][0]["text"]
    assert body["result"]["isError"] is False, text
    return json.loads(text.split("\n\n", 1)[1] if "\n\n" in text else text[text.index("{"):])


def put_grant(svc, worker_id, tenant="acme", user=None, state=GrantState.PROPOSED, cmd=PUSH, activate=False):
    user = user or pid("alice")
    key = derive(RawAction(tool="Bash", command=cmd, repo="acme/repo")).key
    gid = "ag_" + key.key_id()[:8] + worker_id[-4:]
    cfg = svc.config["grants"]
    now = svc.now()
    svc.store.put_action_grant(ActionGrant(
        grant_id=gid, tenant_id=tenant, user_id=user, worker_id=worker_id, resource_scope=key.resource_scope,
        key_id=key.key_id(), verb=key.verb.value, key_canonical=key.canonical(), state=state, created_at=now,
        proposed_at=now, expires_at=now + timedelta(days=30), policy_version=cfg["policy_version"]))
    if activate:
        activate_grant(svc.store, cfg, now, tenant, user, gid, confirmed_by="owner", judge_gate_satisfied=True)
    return gid


# --------------------------------------------------------------------------------------------- auth / role split

def _all_routes(wid="w", rid="r", gid="g"):
    return [("GET", "/api/v1/assistant-action-requests", None),
            ("POST", f"/api/v1/assistant-action-requests/{rid}/decision", {"approve": True}),
            ("GET", "/api/v1/assistant-grants", None),
            ("DELETE", f"/api/v1/assistant-grants/{gid}", None),
            ("POST", f"/api/v1/assistant-grants/{gid}/activate", {"confirm": True}),
            ("POST", f"/api/v1/assistant-workers/{wid}/scopes", {"scopes": ["ask"]})]


def test_no_key_401_and_viewer_403_on_every_route(world):
    base, _, _, _ = world
    for method, path, body in _all_routes():
        assert call(base, method, path, body)[0] == 401, (method, path)
        assert owner(base, method, path, body, who="viewer")[0] == 403, (method, path)


def test_worker_credential_rejected_on_every_route(world):
    base, hub_base, _, _ = world
    wid, cred, device = new_worker(base, hub_base)
    for method, path, body in _all_routes(wid):
        for headers in ({"Authorization": f"Worker {cred}", "X-Herdr-Device": device}, {"X-API-Key": cred}):
            assert call(base, method, path, body, headers)[0] in (401, 403), (method, path)
    status, _ = owner(base, "GET", "/api/v1/assistant-workers")
    assert status == 200


def test_cross_origin_mutations_refused(world):
    base, _, _, _ = world
    for method, path, body in _all_routes():
        if method == "GET":
            continue
        status, _ = call(base, method, path, body, {"X-API-Key": KEYS["alice"], "Origin": "http://evil.example"})
        assert status == 403, (method, path)


def test_disabled_503(acknowledged_sink):
    svr = ControlPlaneServer(ControlPlaneRepo(sink=acknowledged_sink), api_keys=ROLES)
    threading.Thread(target=svr.serve, daemon=True).start()
    svr.wait_until_ready()
    try:
        base = f"http://127.0.0.1:{svr.port}"
        for method, path, body in _all_routes():
            status, out = owner(base, method, path, body)
            assert status == 503 and out["code"] == "unavailable", (method, path, status)
    finally:
        svr.shutdown()


# ------------------------------------------------------------------------------------------------- action requests

def test_list_requests_returns_display_to_owner_only_and_filters(world):
    base, hub_base, svc, _ = world
    worker = new_worker(base, hub_base, scopes=ACT)
    pending = action_request(hub_base, worker, SECRET_CMD)
    vetoed = action_request(hub_base, worker, "git push --force origin main", task="t2")
    assert pending["outcome"] == "pending" and "display" not in pending and "display" not in vetoed
    assert "hunter2-CANARY" not in json.dumps(pending)

    status, out = owner(base, "GET", "/api/v1/assistant-action-requests")
    assert status == 200
    rows = {r["request_id"]: r for r in out["requests"]}
    assert set(rows) == {pending["request_id"], vetoed["request_id"]}
    row = rows[pending["request_id"]]
    assert set(row) == {"request_id", "worker_id", "session_id", "task_id", "verb", "risk_class", "status", "reason",
                        "display", "created_at", "decided_at", "decided_by", "key_id"}
    assert row["worker_id"] == worker[0] and row["status"] == "pending" and "frobnicate" in row["display"]
    assert "hunter2-CANARY" not in row["display"]
    assert "key_canonical" not in json.dumps(out)
    only = owner(base, "GET", "/api/v1/assistant-action-requests?status=pending")[1]["requests"]
    assert [r["request_id"] for r in only] == [pending["request_id"]]
    assert owner(base, "GET", "/api/v1/assistant-action-requests?status=bogus")[0] == 400
    assert owner(base, "GET", "/api/v1/assistant-action-requests", who="bea")[1] == {"requests": []}


@pytest.mark.parametrize("approve,expected", [(True, "approved"), (False, "denied")])
def test_decision_records_authenticated_principal(world, approve, expected):
    base, hub_base, svc, _ = world
    worker = new_worker(base, hub_base, scopes=ACT)
    rid = action_request(hub_base, worker, "frobnicate --all")["request_id"]
    status, body = owner(base, "POST", f"/api/v1/assistant-action-requests/{rid}/decision", {"approve": approve})
    assert status == 200 and body["status"] == expected and body["decided_by"] == pid("alice")
    assert "key_canonical" not in body
    row = svc.store.get_action_request(rid, tenant_id="acme", user_id=pid("alice"))
    assert row.status == expected and row.decided_by == pid("alice")
    # twice => conflict, whichever way
    for again in (True, False):
        status, body = owner(base, "POST", f"/api/v1/assistant-action-requests/{rid}/decision", {"approve": again})
        assert status == 409 and body["code"] == "conflict"


def test_decision_other_tenant_and_unknown_404(world):
    base, hub_base, _, _ = world
    worker = new_worker(base, hub_base, scopes=ACT)
    rid = action_request(hub_base, worker, "frobnicate --all")["request_id"]
    assert owner(base, "POST", f"/api/v1/assistant-action-requests/{rid}/decision", {"approve": True}, who="bea")[0] == 404
    assert owner(base, "POST", "/api/v1/assistant-action-requests/nope/decision", {"approve": True})[0] == 404
    status, listing = owner(base, "GET", "/api/v1/assistant-action-requests")
    assert listing["requests"][0]["status"] == "pending"            # bea's attempt changed nothing


def test_vetoed_request_cannot_be_decided(world):
    base, hub_base, _, _ = world
    worker = new_worker(base, hub_base, scopes=ACT)
    rid = action_request(hub_base, worker, "git push --force origin main")["request_id"]
    status, body = owner(base, "POST", f"/api/v1/assistant-action-requests/{rid}/decision", {"approve": True})
    assert status == 409 and body["code"] == "conflict"
    assert owner(base, "GET", "/api/v1/assistant-action-requests?status=vetoed")[1]["requests"][0]["status"] == "vetoed"


@pytest.mark.parametrize("body", [{}, {"approve": "yes"}, {"approve": 1}, {"approve": None},
                                  {"approve": True, "decided_by": "mallory"}, [True], "x"])
def test_decision_body_validation_400(world, body):
    base, hub_base, _, _ = world
    worker = new_worker(base, hub_base, scopes=ACT)
    rid = action_request(hub_base, worker, "frobnicate --all")["request_id"]
    assert owner(base, "POST", f"/api/v1/assistant-action-requests/{rid}/decision", body)[0] == 400
    assert owner(base, "GET", "/api/v1/assistant-action-requests?status=pending")[1]["requests"]


def test_oversize_and_malformed_body_400(world):
    base, hub_base, _, _ = world
    worker = new_worker(base, hub_base, scopes=ACT)
    rid = action_request(hub_base, worker, "frobnicate --all")["request_id"]
    path = f"/api/v1/assistant-action-requests/{rid}/decision"
    # declare 5 MiB but send only a little: the server must refuse on the declared size without waiting for the rest
    import http.client
    conn = http.client.HTTPConnection(base.split("//")[1], timeout=10)
    conn.putrequest("POST", path)
    for k, v in (("Content-Type", "application/json"), ("X-API-Key", KEYS["alice"]),
                 ("Content-Length", str(5 * 1024 * 1024))):
        conn.putheader(k, v)
    conn.endheaders(b'{"approve": true, "pad": "xxxx')
    assert conn.getresponse().status == 400
    conn.close()
    assert owner(base, "POST", path, raw=b"{not json")[0] == 400
    assert owner(base, "GET", "/api/v1/assistant-action-requests?status=pending")[1]["requests"]


# ----------------------------------------------------------------------------------------------------------- grants

def test_grants_list_effective_state_and_tenant_scoping(world):
    base, hub_base, svc, _ = world
    worker = new_worker(base, hub_base, scopes=ACT)
    gid = put_grant(svc, worker[0], activate=True)
    expired = put_grant(svc, worker[0], cmd="git push origin dev", state=GrantState.ACTIVE)
    stored = svc.store.get_action_grant(expired, tenant_id="acme", user_id=pid("alice"))
    stored.expires_at = svc.now() - timedelta(days=1)
    svc.store.put_action_grant(stored)
    status, out = owner(base, "GET", "/api/v1/assistant-grants")
    assert status == 200
    rows = {g["grant_id"]: g for g in out["grants"]}
    assert rows[gid]["state"] == "active" and rows[gid]["confirmed_by"] == "owner"
    assert rows[expired]["state"] == "expired"                       # effective state, not the stored one
    assert set(rows[gid]) == {"grant_id", "worker_id", "verb", "resource_scope", "key_id", "state", "policy_version",
                              "created_at", "proposed_at", "activated_at", "expires_at", "revoked_at", "confirmed_by"}
    assert "key_canonical" not in json.dumps(out)
    assert owner(base, "GET", "/api/v1/assistant-grants", who="bea")[1] == {"grants": []}


def test_activate_refused_judge_gate_not_satisfied(world):
    base, hub_base, svc, _ = world
    worker = new_worker(base, hub_base, scopes=ACT)
    gid = put_grant(svc, worker[0])
    status, body = owner(base, "POST", f"/api/v1/assistant-grants/{gid}/activate", {"confirm": True})
    assert status == 409 and body["code"] == "judge_gate_not_satisfied" and body["error"]
    assert owner(base, "GET", "/api/v1/assistant-grants")[1]["grants"][0]["state"] == "proposed"


def test_activate_not_proposed_and_not_found(world):
    base, hub_base, svc, _ = world
    worker = new_worker(base, hub_base, scopes=ACT)
    gid = put_grant(svc, worker[0], state=GrantState.SHADOW)
    status, body = owner(base, "POST", f"/api/v1/assistant-grants/{gid}/activate", {"confirm": True})
    assert status == 409 and body["code"] == "not_proposed"
    assert owner(base, "POST", "/api/v1/assistant-grants/nope/activate", {"confirm": True})[0] == 404
    assert owner(base, "POST", f"/api/v1/assistant-grants/{gid}/activate", {"confirm": True}, who="bea")[0] == 404


@pytest.mark.parametrize("body", [{}, {"confirm": False}, {"confirm": "true"}, {"confirm": True, "x": 1}, [True]])
def test_activate_body_validation_400(world, body):
    base, hub_base, svc, _ = world
    gid = put_grant(svc, new_worker(base, hub_base, scopes=ACT)[0])
    assert owner(base, "POST", f"/api/v1/assistant-grants/{gid}/activate", body)[0] == 400


def test_revoke_takes_effect_on_next_action_request_and_is_idempotent(world):
    base, hub_base, svc, _ = world
    worker = new_worker(base, hub_base, scopes=ACT)
    gid = put_grant(svc, worker[0], activate=True)
    first = action_request(hub_base, worker, PUSH)
    assert (first["outcome"], first["grant_id"]) == ("authorized", gid)
    status, body = owner(base, "DELETE", f"/api/v1/assistant-grants/{gid}")
    assert status == 200 and body["state"] == "revoked" and body["revoked_at"]
    second = action_request(hub_base, worker, PUSH)
    assert second["outcome"] == "pending" and second["grant_id"] is None
    again = owner(base, "DELETE", f"/api/v1/assistant-grants/{gid}")
    assert again[0] == 200 and again[1]["state"] == "revoked"
    assert owner(base, "DELETE", "/api/v1/assistant-grants/nope")[0] == 404
    assert owner(base, "DELETE", f"/api/v1/assistant-grants/{gid}", who="bea")[0] == 404


# ----------------------------------------------------------------------------------------------------------- scopes

def tool_names(hub_base, worker):
    status, body = mcp(hub_base, worker[1], worker[2], "tools/list")
    assert status == 200
    return {t["name"] for t in body["result"]["tools"]}


def test_enroll_with_scopes_then_redeem_shows_act_tools(world):
    base, hub_base, _, registry = world
    plain = new_worker(base, hub_base, device="d0")
    assert "action_request" not in tool_names(hub_base, plain)
    status, created = owner(base, "POST", "/api/v1/assistant-workers",
                            {"name": "a", "harness": "claude", "scopes": ACT})
    assert status == 201 and created["scopes"] == ACT
    status, red = call(hub_base, "POST", "/assistant/enroll", {"code": created["code"], "device_id": "d1"})
    worker = (red["worker_id"], red["credential"], "d1")
    assert "action_request" in tool_names(hub_base, worker)
    listed = owner(base, "GET", "/api/v1/assistant-workers")[1]["workers"]
    assert {w["worker_id"]: w["scopes"] for w in listed}[worker[0]] == ACT


def test_enroll_default_scopes_in_response_and_bad_scopes_400(world):
    base, _, _, _ = world
    status, created = owner(base, "POST", "/api/v1/assistant-workers", {"name": "a", "harness": "claude"})
    assert status == 201 and created["scopes"] == ["ask", "report"]
    for bad in (["root"], [], "act", [1]):
        assert owner(base, "POST", "/api/v1/assistant-workers", {"name": "a", "harness": "claude", "scopes": bad})[0] == 400


def test_scope_change_visible_on_next_mcp_call(world):
    base, hub_base, _, _ = world
    worker = new_worker(base, hub_base)
    assert "action_request" not in tool_names(hub_base, worker)
    status, body = owner(base, "POST", f"/api/v1/assistant-workers/{worker[0]}/scopes", {"scopes": ACT})
    assert status == 200 and body["scopes"] == ACT and body["worker_id"] == worker[0]
    assert "secret_digest" not in body
    assert "action_request" in tool_names(hub_base, worker)
    owner(base, "POST", f"/api/v1/assistant-workers/{worker[0]}/scopes", {"scopes": ["ask"]})
    assert "action_request" not in tool_names(hub_base, worker)


def test_scope_change_errors(world):
    base, hub_base, _, _ = world
    worker = new_worker(base, hub_base)
    path = f"/api/v1/assistant-workers/{worker[0]}/scopes"
    for bad in ({"scopes": ["root"]}, {"scopes": []}, {"scopes": "act"}, {}, {"scopes": ["ask"], "x": 1}):
        assert owner(base, "POST", path, bad)[0] == 400, bad
    assert owner(base, "POST", "/api/v1/assistant-workers/nope/scopes", {"scopes": ["ask"]})[0] == 404
    assert owner(base, "POST", path, {"scopes": ["ask"]}, who="bea")[0] == 404
    owner(base, "DELETE", f"/api/v1/assistant-workers/{worker[0]}")
    assert owner(base, "POST", path, {"scopes": ["ask"]})[0] == 400          # revoked worker


# ------------------------------------------------------------------------------------------------------------- CLI

@pytest.fixture()
def keyfile(tmp_path):
    p = tmp_path / "k"
    p.write_text(KEYS["alice"] + "\n")
    return p


def run_cli(capsys, base, keyfile, *argv):
    capsys.readouterr()
    rc = cli.main(["control-plane", *argv, "--control-plane", base, "--api-key-file", str(keyfile)])
    out = capsys.readouterr()
    assert KEYS["alice"] not in out.out + out.err
    return rc, out.out, out.err


def test_cli_action_list_approve_deny(world, keyfile, capsys):
    base, hub_base, svc, _ = world
    worker = new_worker(base, hub_base, scopes=ACT)
    r1 = action_request(hub_base, worker, SECRET_CMD)["request_id"]
    r2 = action_request(hub_base, worker, "frobnicate --other", task="t2")["request_id"]
    rc, out, _ = run_cli(capsys, base, keyfile, "assistant-action", "list", "--status", "pending")
    assert rc == 0 and r1 in out and r2 in out and "pending" in out and worker[0] in out and "hunter2-CANARY" not in out
    assert run_cli(capsys, base, keyfile, "assistant-action", "approve", r1)[0] == 0
    assert run_cli(capsys, base, keyfile, "assistant-action", "deny", r2)[0] == 0
    store = svc.store
    assert store.get_action_request(r1, tenant_id="acme", user_id=pid("alice")).status == "approved"
    assert store.get_action_request(r2, tenant_id="acme", user_id=pid("alice")).status == "denied"
    rc, _, err = run_cli(capsys, base, keyfile, "assistant-action", "approve", r1)
    assert rc == 1 and len(err.strip().splitlines()) == 1 and "409" in err
    assert run_cli(capsys, base, keyfile, "assistant-action", "approve")[0] == 2


def test_cli_action_list_truncates_display(world, keyfile, capsys):
    base, hub_base, _, _ = world
    worker = new_worker(base, hub_base, scopes=ACT)
    action_request(hub_base, worker, "frobnicate " + "a" * 250)
    rc, out, _ = run_cli(capsys, base, keyfile, "assistant-action", "list")
    assert rc == 0 and "a" * 100 in out and "a" * 150 not in out


def test_cli_grant_list_revoke_activate(world, keyfile, capsys):
    base, hub_base, svc, _ = world
    worker = new_worker(base, hub_base, scopes=ACT)
    active = put_grant(svc, worker[0], activate=True)
    proposed = put_grant(svc, worker[0], cmd="git push origin dev")
    rc, out, _ = run_cli(capsys, base, keyfile, "assistant-grant", "list")
    assert rc == 0 and active in out and proposed in out and "active" in out and "proposed" in out
    rc, out, err = run_cli(capsys, base, keyfile, "assistant-grant", "activate", proposed)
    assert rc == 1 and "judge_gate_not_satisfied" in out + err and len((out + err).strip().splitlines()) == 1
    rc, out, _ = run_cli(capsys, base, keyfile, "assistant-grant", "revoke", active)
    assert rc == 0 and "revoked" in out
    assert action_request(hub_base, worker, PUSH)["outcome"] == "pending"
    assert run_cli(capsys, base, keyfile, "assistant-grant", "revoke", "nope")[0] == 1
    assert run_cli(capsys, base, keyfile, "assistant-grant", "revoke")[0] == 2


def test_cli_worker_scopes_and_enroll_scopes(world, keyfile, capsys):
    base, hub_base, _, _ = world
    worker = new_worker(base, hub_base)
    rc, out, _ = run_cli(capsys, base, keyfile, "assistant-worker", "scopes", worker[0], "--scopes", "ask,report,read,act")
    assert rc == 0 and "act" in out
    assert "action_request" in tool_names(hub_base, worker)
    assert run_cli(capsys, base, keyfile, "assistant-worker", "scopes", worker[0], "--scopes", "root")[0] == 1
    assert run_cli(capsys, base, keyfile, "assistant-worker", "scopes", worker[0])[0] == 2
    assert run_cli(capsys, base, keyfile, "assistant-worker", "scopes", "--scopes", "ask")[0] == 2
    rc, out, _ = run_cli(capsys, base, keyfile, "assistant-worker", "enroll", "--name", "n", "--harness", "claude",
                         "--scopes", "ask,act")
    assert rc == 0
    code = next(line.split()[-1] for line in out.splitlines() if line.startswith("code:"))
    _, red = call(hub_base, "POST", "/assistant/enroll", {"code": code, "device_id": "dz"})
    assert "action_request" in tool_names(hub_base, (red["worker_id"], red["credential"], "dz"))


def test_cli_subprocess_approve_in_throwaway_home(world, tmp_path):
    base, hub_base, svc, _ = world
    worker = new_worker(base, hub_base, scopes=ACT)
    rid = action_request(hub_base, worker, "frobnicate --all")["request_id"]
    key = tmp_path / "key"
    key.write_text(KEYS["alice"])
    home = tmp_path / "home"
    home.mkdir()
    repo = Path(__file__).resolve().parents[2]
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8",
           "PYTHONPATH": os.pathsep.join([str(repo), *[p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p]])}
    done = subprocess.run([sys.executable, "-m", "herdr_engineering.cli", "control-plane", "assistant-action",
                           "approve", rid, "--control-plane", base, "--api-key-file", str(key)],
                          env=env, capture_output=True, text=True, timeout=60, cwd=str(tmp_path))
    assert done.returncode == 0, done.stderr
    assert KEYS["alice"] not in done.stdout + done.stderr
    assert svc.store.get_action_request(rid, tenant_id="acme", user_id=pid("alice")).status == "approved"

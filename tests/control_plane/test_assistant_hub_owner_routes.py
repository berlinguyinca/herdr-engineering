"""Owner enrollment routes (/api/v1/assistant-workers) and CLI. A real ControlPlaneServer and a real HubServer share
ONE WorkerRegistry; nothing is mocked."""
import json
import threading
import urllib.error
import urllib.request
from datetime import timedelta

import pytest

from herdr_engineering import cli
from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.assistant import AssistantService, assistant_config
from herdr_engineering.control_plane.assistant_hub.mcp import Hub
from herdr_engineering.control_plane.assistant_hub.server import HubServer
from herdr_engineering.control_plane.assistant_hub.workers import WorkerRegistry
from herdr_engineering.control_plane.repo import ControlPlaneRepo

KEYS = {"alice": "owner-route-alice-key", "bea": "owner-route-bea-key", "viewer": "owner-route-viewer-key"}
ROLES = {KEYS["alice"]: "operator", KEYS["bea"]: "operator", KEYS["viewer"]: "viewer"}


def pid(name):
    import hashlib
    return "key:" + hashlib.sha256(KEYS[name].encode()).hexdigest()[:12]


def call(base, method, path, body=None, headers=None):
    h = {"Content-Type": "application/json", **(headers or {})}
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw or b"null")
        except ValueError:
            return e.code, raw


def owner(base, method, path, body=None, who="alice"):
    return call(base, method, path, body, {"X-API-Key": KEYS[who]})


@pytest.fixture()
def world(acknowledged_sink):
    section = {"enabled": True, "tenancy": {"principals": {pid("alice"): "acme", pid("bea"): "globex"}},
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
    yield f"http://127.0.0.1:{svr.port}", f"http://127.0.0.1:{hub.port}", registry, svr
    svr.shutdown()
    hub.shutdown()


def enroll(base, name="box", harness="claude", who="alice"):
    return owner(base, "POST", "/api/v1/assistant-workers", {"name": name, "harness": harness}, who)


def redeem(hub_base, code, device="dev1"):
    status, body = call(hub_base, "POST", "/assistant/enroll", {"code": code, "device_id": device})
    assert status == 200, body
    return body


def mcp(hub_base, cred, method, device="dev1"):
    return call(hub_base, "POST", "/assistant/mcp", {"jsonrpc": "2.0", "id": 1, "method": method},
                {"Authorization": f"Worker {cred}", "X-Herdr-Device": device})


def test_create_enrollment_requires_operator_key(world):
    base, _, _, _ = world
    assert call(base, "POST", "/api/v1/assistant-workers", {"name": "b", "harness": "claude"})[0] == 401
    assert call(base, "POST", "/api/v1/assistant-workers", {"name": "b", "harness": "claude"},
                {"X-API-Key": "wrong"})[0] == 401
    assert enroll(base, who="viewer")[0] == 403
    status, body = enroll(base)
    assert status == 201
    assert set(body) == {"enrollment_id", "code", "expires_at", "setup"}
    assert body["code"] and body["enrollment_id"]
    assert body["setup"] == {
        "supported": True, "harness": "claude",
        "redeem": {"method": "POST", "path": "/assistant/enroll",
                   "body": {"code": "<CODE>", "device_id": "<DEVICE>"}},
        "mcp_add": 'claude mcp add --transport http herdr-assistant http://100.64.0.9:9483/assistant/mcp '
                   '--header "Authorization: Worker <CREDENTIAL>" --header "X-Herdr-Device: <DEVICE>"'}
    assert body["code"] not in body["setup"]["mcp_add"]
    assert owner(base, "GET", "/api/v1/assistant-workers", who="viewer")[0] == 403


def test_worker_credential_cannot_reach_owner_routes(world):
    base, hub_base, _, _ = world
    redeemed = redeem(hub_base, enroll(base)[1]["code"])
    cred = redeemed["credential"]
    h = {"Authorization": f"Worker {cred}", "X-Herdr-Device": "dev1"}
    for method, path, body in (("GET", "/api/v1/assistant-workers", None),
                               ("POST", "/api/v1/assistant-workers", {"name": "x", "harness": "claude"}),
                               ("DELETE", "/api/v1/assistant-workers/anything", None)):
        assert call(base, method, path, body, h)[0] in (401, 403)
    assert call(base, "GET", "/api/v1/assistant-workers", None, {"X-API-Key": cred})[0] in (401, 403)


def test_list_never_returns_secrets(world):
    base, hub_base, _, _ = world
    created = enroll(base)[1]
    redeemed = redeem(hub_base, created["code"])
    status, listing = owner(base, "GET", "/api/v1/assistant-workers")
    assert status == 200
    text = json.dumps(listing)
    for secret in (created["code"], redeemed["credential"]):
        assert secret not in text
    assert "digest" not in text and "secret" not in text
    (w,) = listing["workers"]
    assert w["name"] == "box" and w["status"] == "active" and w["worker_id"] == redeemed["worker_id"]
    assert owner(base, "GET", "/api/v1/assistant-workers", who="bea")[1] == {"workers": []}


def test_revoke_then_mcp_401(world):
    base, hub_base, _, _ = world
    redeemed = redeem(hub_base, enroll(base)[1]["code"])
    assert mcp(hub_base, redeemed["credential"], "ping")[0] == 200
    status, body = owner(base, "DELETE", f"/api/v1/assistant-workers/{redeemed['worker_id']}")
    assert status == 200 and body["status"] == "revoked" and body["worker_id"] == redeemed["worker_id"]
    assert mcp(hub_base, redeemed["credential"], "ping")[0] == 401
    assert owner(base, "DELETE", "/api/v1/assistant-workers/nope")[0] == 404


def test_feature_disabled_503(acknowledged_sink):
    for kwargs in ({}, {"assistant_config": {"personal_assistant": {"enabled": True}}}):     # no registry / hub off
        svr = ControlPlaneServer(ControlPlaneRepo(sink=acknowledged_sink), api_keys=ROLES, **kwargs)
        threading.Thread(target=svr.serve, daemon=True).start()
        svr.wait_until_ready()
        try:
            base = f"http://127.0.0.1:{svr.port}"
            for method, path, body in (("POST", "/api/v1/assistant-workers", {"name": "b", "harness": "claude"}),
                                       ("GET", "/api/v1/assistant-workers", None),
                                       ("DELETE", "/api/v1/assistant-workers/x", None)):
                status, out = owner(base, method, path, body)
                assert status == 503 and out["code"] == "unavailable"
        finally:
            svr.shutdown()


def test_assistant_disabled_with_registry_503(acknowledged_sink):
    svc = AssistantService(config=assistant_config({}))
    registry = WorkerRegistry(svc.store, enrollment_ttl=timedelta(minutes=1), worker_ttl=timedelta(days=1))
    svr = ControlPlaneServer(ControlPlaneRepo(sink=acknowledged_sink), assistant_service=svc, api_keys=ROLES,
                             assistant_workers=registry)
    threading.Thread(target=svr.serve, daemon=True).start()
    svr.wait_until_ready()
    try:
        assert enroll(f"http://127.0.0.1:{svr.port}")[0] == 503
    finally:
        svr.shutdown()


def test_cli_enroll_prints_code_once(world, tmp_path, capsys):
    base, _, _, _ = world
    keyfile = tmp_path / "k"
    keyfile.write_text(KEYS["alice"] + "\n")
    common = ["--control-plane", base, "--api-key-file", str(keyfile)]
    capsys.readouterr()
    assert cli.main(["control-plane", "assistant-worker", "enroll", "--name", "laptop", "--harness", "claude",
                     *common]) == 0
    out = capsys.readouterr().out
    assert "will not be shown again" in out
    assert "claude mcp add --transport http herdr-assistant http://100.64.0.9:9483/assistant/mcp" in out
    code = next(line.split()[-1] for line in out.splitlines() if line.startswith("code:"))
    assert len(code) >= 8
    assert KEYS["alice"] not in out
    assert cli.main(["control-plane", "assistant-worker", "list", *common]) == 0
    listing = capsys.readouterr().out
    assert code not in listing and KEYS["alice"] not in listing
    assert "laptop" not in listing                                  # enrolled but not yet redeemed: no worker row yet


def test_cli_list_and_revoke(world, tmp_path, capsys):
    base, hub_base, _, _ = world
    keyfile = tmp_path / "k"
    keyfile.write_text(KEYS["alice"] + "\n")
    common = ["--control-plane", base, "--api-key-file", str(keyfile)]
    worker = redeem(hub_base, enroll(base, name="desk")[1]["code"])
    capsys.readouterr()
    assert cli.main(["control-plane", "assistant-worker", "list", *common]) == 0
    out = capsys.readouterr().out
    assert "desk" in out and worker["worker_id"] in out and worker["credential"] not in out
    assert cli.main(["control-plane", "assistant-worker", "revoke", worker["worker_id"], *common]) == 0
    assert "revoked" in capsys.readouterr().out
    assert cli.main(["control-plane", "assistant-worker", "revoke", "nope", *common]) != 0
    err = capsys.readouterr().err
    assert len(err.strip().splitlines()) == 1 and KEYS["alice"] not in err
    keyfile.write_text("bad-key\n")
    assert cli.main(["control-plane", "assistant-worker", "list", *common]) != 0
    assert "bad-key" not in capsys.readouterr().err


def test_enrollment_end_to_end(world):
    base, hub_base, _, _ = world
    created = enroll(base)[1]
    redeemed = redeem(hub_base, created["code"])
    status, init = mcp(hub_base, redeemed["credential"], "initialize")
    assert status == 200 and init["result"]["serverInfo"]["name"] == "herdr-assistant"
    status, tools = mcp(hub_base, redeemed["credential"], "tools/list")
    assert status == 200 and {t["name"] for t in tools["result"]["tools"]} >= {"assistant_ask", "report"}
    # the one-time code is gone for good
    assert call(hub_base, "POST", "/assistant/enroll", {"code": created["code"], "device_id": "dev1"})[0] == 401


def test_other_tenant_cannot_revoke(world):
    base, hub_base, _, _ = world
    redeemed = redeem(hub_base, enroll(base)[1]["code"])
    assert owner(base, "DELETE", f"/api/v1/assistant-workers/{redeemed['worker_id']}", who="bea")[0] == 404
    assert mcp(hub_base, redeemed["credential"], "ping")[0] == 200


def test_unknown_harness_400(world):
    base, _, _, _ = world
    assert enroll(base, harness="emacs")[0] == 400
    assert owner(base, "POST", "/api/v1/assistant-workers", {"name": "x"})[0] == 400
    assert owner(base, "POST", "/api/v1/assistant-workers", {"harness": "claude"})[0] == 400


@pytest.mark.parametrize("harness", ["pi", "codex", "opencode"])
def test_codex_pi_opencode_setup_unsupported(world, harness):
    base, _, _, _ = world
    status, body = enroll(base, harness=harness)
    assert status == 201 and body["setup"] == {"supported": False, "harness": harness}


def test_hub_listener_404_for_owner_paths(world):
    base, hub_base, _, _ = world
    for method in ("GET", "POST", "DELETE"):
        status, _ = call(hub_base, method, "/api/v1/assistant-workers", {"name": "x", "harness": "claude"}
                         if method == "POST" else None, {"X-API-Key": KEYS["alice"]})
        assert status == 404


def test_server_uses_the_shared_registry(world):
    _, _, registry, svr = world
    assert svr.assistant_workers is registry

"""Owner enrollment routes (/api/v1/assistant-workers) and CLI. A real ControlPlaneServer and a real HubServer share
ONE WorkerRegistry; nothing is mocked."""
import json
import re
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
        "supported": True, "harness": "claude", "host_is_placeholder": False,
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


# ---------------------------------------------------------------- fix round 1
def _server_with_hub(sink, hub_cfg):
    section = {"enabled": True, "hub": {"enabled": True, "port": 9483, **hub_cfg}}
    svc = AssistantService(config=assistant_config({"personal_assistant": section}))
    registry = WorkerRegistry(svc.store, enrollment_ttl=timedelta(minutes=10), worker_ttl=timedelta(days=30))
    svr = ControlPlaneServer(ControlPlaneRepo(sink=sink), assistant_service=svc, api_keys=ROLES,
                             assistant_workers=registry)
    threading.Thread(target=svr.serve, daemon=True).start()
    svr.wait_until_ready()
    return svr


def _setup_for(sink, hub_cfg):
    svr = _server_with_hub(sink, hub_cfg)
    try:
        return enroll(f"http://127.0.0.1:{svr.port}")[1]["setup"]
    finally:
        svr.shutdown()


@pytest.mark.parametrize("hub_cfg,host,placeholder", [
    ({"host": "0.0.0.0"}, "<HUB_HOST>", True),
    ({"host": "::"}, "<HUB_HOST>", True),
    ({"host": "127.0.0.1"}, "<HUB_HOST>", True),
    ({"host": "::1"}, "<HUB_HOST>", True),
    ({"host": "100.64.0.9"}, "100.64.0.9", False),
    ({"host": "fd7a:115c:a1e0::1"}, "[fd7a:115c:a1e0::1]", False),
    ({"host": "0.0.0.0", "allowed_hosts": ["localhost", "box.tail1234.ts.net"]}, "box.tail1234.ts.net", False),
])
def test_setup_url_host(acknowledged_sink, hub_cfg, host, placeholder):
    setup = _setup_for(acknowledged_sink, hub_cfg)
    assert re.search(r"herdr-assistant (\S+) --header", setup["mcp_add"]).group(1) == f"http://{host}:9483/assistant/mcp"
    assert setup["host_is_placeholder"] is placeholder


def test_cli_warns_when_host_is_placeholder(acknowledged_sink, tmp_path, capsys):
    svr = _server_with_hub(acknowledged_sink, {"host": "0.0.0.0"})
    try:
        keyfile = tmp_path / "k"
        keyfile.write_text(KEYS["alice"])
        capsys.readouterr()
        assert cli.main(["control-plane", "assistant-worker", "enroll", "--name", "n", "--harness", "claude",
                         "--control-plane", f"http://127.0.0.1:{svr.port}", "--api-key-file", str(keyfile)]) == 0
        assert "replace <HUB_HOST> with this machine's tailnet name or address" in capsys.readouterr().out
    finally:
        svr.shutdown()


def test_delete_with_hostile_origin_refused(world):
    base, _, _, _ = world
    status, _ = call(base, "DELETE", "/api/v1/assistant-workers/x", None,
                     {"X-API-Key": KEYS["alice"], "Origin": "http://evil.example"})
    assert status == 403


def test_control_character_name_400(world):
    base, _, _, _ = world
    assert enroll(base, name="bad\x1b[31mname")[0] == 400
    assert enroll(base, name="line\nbreak")[0] == 400


def test_cli_non_utf8_key_file(world, tmp_path, capsys):
    base, _, _, _ = world
    keyfile = tmp_path / "k"
    keyfile.write_bytes(b"\xff\xfe\x80secret")
    capsys.readouterr()
    assert cli.main(["control-plane", "assistant-worker", "list", "--control-plane", base,
                     "--api-key-file", str(keyfile)]) != 0
    err = capsys.readouterr().err
    assert len(err.strip().splitlines()) == 1 and "secret" not in err


def test_cli_enroll_unsupported_harness(world, tmp_path, capsys):
    base, _, _, _ = world
    keyfile = tmp_path / "k"
    keyfile.write_text(KEYS["alice"])
    capsys.readouterr()
    rc = cli.main(["control-plane", "assistant-worker", "enroll", "--name", "n", "--harness", "emacs",
                   "--control-plane", base, "--api-key-file", str(keyfile)])
    assert rc != 0
    err = capsys.readouterr().err
    assert len(err.strip().splitlines()) == 1 and "400" in err
    rc = cli.main(["control-plane", "assistant-worker", "enroll", "--name", "n", "--harness", "codex",
                   "--control-plane", base, "--api-key-file", str(keyfile)])
    out = capsys.readouterr().out
    assert rc == 0 and "not supported yet" in out and "will not be shown again" in out


def test_cli_connection_refused(tmp_path, capsys):
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    keyfile = tmp_path / "k"
    keyfile.write_text("sekrit-key")
    capsys.readouterr()
    assert cli.main(["control-plane", "assistant-worker", "list", "--control-plane", f"http://127.0.0.1:{port}",
                     "--api-key-file", str(keyfile)]) != 0
    err = capsys.readouterr().err
    assert len(err.strip().splitlines()) == 1 and "sekrit-key" not in err and "Traceback" not in err


def test_cp_web_wiring_shares_the_hub_registry(acknowledged_sink):
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    svc = AssistantService(config=assistant_config({"personal_assistant": {
        "enabled": True, "hub": {"enabled": True, "host": "127.0.0.1", "port": port,
                                 "allowed_networks": ["127.0.0.0/8"]}}}))
    svr = ControlPlaneServer(ControlPlaneRepo(sink=acknowledged_sink), assistant_service=svc, api_keys=ROLES)
    hub = cli._wire_assistant_hub(svr)
    try:
        assert hub is not None and svr.assistant_workers is hub.hub.registry
    finally:
        hub.shutdown()
        svr.shutdown()
    off = ControlPlaneServer(ControlPlaneRepo(sink=acknowledged_sink), api_keys=ROLES)
    assert cli._wire_assistant_hub(off) is None and off.assistant_workers is None
    off.shutdown()


@pytest.mark.parametrize("entries,host,placeholder", [
    (["[fd7a::1]:9483"], "[fd7a::1]", False),
    (["box.ts.net:9483"], "box.ts.net", False),
    (["BOX.Ts.Net"], "box.ts.net", False),
    (["localhost:9483"], "<HUB_HOST>", True),
    (["[::1]:9483"], "<HUB_HOST>", True),
    (["127.0.0.2:1"], "<HUB_HOST>", True),
    (["http://x"], "<HUB_HOST>", True),
    (["x; touch /tmp/pwn #"], "<HUB_HOST>", True),
    (["$(id)"], "<HUB_HOST>", True),
    (["a b"], "<HUB_HOST>", True),
    (["a_b.example"], "<HUB_HOST>", True),
    (["http://x", "$(id)", "good.ts.net:1"], "good.ts.net", False),
    (["localhost", "good.ts.net", "other.ts.net"], "good.ts.net", False),
])
def test_setup_url_host_from_allowed_hosts(acknowledged_sink, entries, host, placeholder):
    setup = _setup_for(acknowledged_sink, {"host": "0.0.0.0", "allowed_hosts": entries})
    url = re.search(r"herdr-assistant (\S+) --header", setup["mcp_add"]).group(1)
    assert url == f"http://{host}:9483/assistant/mcp"
    assert setup["host_is_placeholder"] is placeholder
    assert setup["mcp_add"].endswith('--header "Authorization: Worker <CREDENTIAL>" --header "X-Herdr-Device: <DEVICE>"')

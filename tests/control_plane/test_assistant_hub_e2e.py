"""End-to-end: a real ControlPlaneServer and a real HubServer share ONE WorkerRegistry on 127.0.0.1 port 0. No mocks.

Every subprocess (including the real ``claude`` binary) runs with HOME set to a tmp_path and a minimal environment:
nothing here may read or write the real ~/.claude, ~/.claude.json, ~/.codex, ~/.config/opencode, ~/.pi or ~/.config/herdr.
Note: /api/v1 routes need asyncpg importable (pre-existing sandbox gap, see docs/operations/assistant-hub.md)."""
import hashlib
import json
import shlex
import shutil
import subprocess
import threading
import urllib.error
import urllib.request
from argparse import Namespace
from datetime import timedelta
from pathlib import Path

import pytest

from herdr_engineering.cli import _cmd_cp_assistant_worker
from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.assistant import AssistantService, assistant_config
from herdr_engineering.control_plane.assistant_hub.events import list_events
from herdr_engineering.control_plane.assistant_hub.mcp import UNTRUSTED, Hub
from herdr_engineering.control_plane.assistant_hub.server import HubServer
from herdr_engineering.control_plane.assistant_hub.workers import WorkerRegistry
from herdr_engineering.control_plane.repo import ControlPlaneRepo

OWNER_KEY = "e2e-owner-key-0001"
OWNER = "key:" + hashlib.sha256(OWNER_KEY.encode()).hexdigest()[:12]
TENANT = "acme"


def http(method, url, body=None, headers=None):
    h = {"Content-Type": "application/json", **(headers or {})}
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = r.read()
            return r.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw or b"null")
        except ValueError:
            return e.code, raw


def clean_env(home: Path, **extra: str) -> dict[str, str]:
    return {"HOME": str(home), "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", **extra}


@pytest.fixture()
def world(acknowledged_sink):
    section = {"enabled": True, "tenancy": {"principals": {OWNER: TENANT}},
               "hub": {"enabled": True, "host": "127.0.0.1", "port": 9483, "allowed_networks": ["127.0.0.0/8"]}}
    svc = AssistantService(config=assistant_config({"personal_assistant": section}))
    registry = WorkerRegistry(svc.store, enrollment_ttl=timedelta(minutes=10), worker_ttl=timedelta(days=30))
    hub = HubServer(Hub(registry=registry, service=svc, store=svc.store), host="127.0.0.1", port=0,
                    allowed_networks=["127.0.0.0/8"], allowed_hosts=[])
    hub.start()
    hub.wait_until_ready()
    svr = ControlPlaneServer(ControlPlaneRepo(sink=acknowledged_sink), assistant_service=svc,
                             api_keys={OWNER_KEY: "operator"}, assistant_workers=registry)
    threading.Thread(target=svr.serve, daemon=True).start()
    svr.wait_until_ready()
    try:
        yield Namespace(api=f"http://127.0.0.1:{svr.port}", hub=f"http://127.0.0.1:{hub.port}", hub_port=hub.port,
                        svc=svc, registry=registry)
    finally:
        try:
            svr.shutdown()
        finally:
            hub.shutdown()


def owner(world, method, path, body=None):
    return http(method, world.api + path, body, {"X-API-Key": OWNER_KEY})


def mcp(world, cred, device, method, params=None, rid=1):
    msg = {"jsonrpc": "2.0", "id": rid, "method": method}
    if params is not None:
        msg["params"] = params
    return http("POST", world.hub + "/assistant/mcp", msg,
                {"Authorization": f"Worker {cred}", "X-Herdr-Device": device})


def tool(world, cred, device, name, args):
    status, body = mcp(world, cred, device, "tools/call", {"name": name, "arguments": args})
    assert status == 200, body
    result = body["result"]
    text = result["content"][0]["text"]
    assert result["isError"] is False, text
    return json.loads(text.split("\n\n", 1)[1] if UNTRUSTED in text else text[text.index("{"):])


def events(world, **kw):
    return list_events(world.svc.store, tenant_id=TENANT, user_id=OWNER, **kw)


def test_full_flow_enroll_use_revoke_audit(world):
    # 1. owner creates an enrollment
    status, created = owner(world, "POST", "/api/v1/assistant-workers", {"name": "laptop", "harness": "claude"})
    assert status == 201, created
    code = created["code"]
    assert created["setup"]["supported"] is True and "claude mcp add --transport http" in created["setup"]["mcp_add"]

    # 2. the worker redeems it on the hub (single use)
    status, redeemed = http("POST", world.hub + "/assistant/enroll", {"code": code, "device_id": "dev1"})
    assert status == 200, redeemed
    cred, wid = redeemed["credential"], redeemed["worker_id"]
    assert http("POST", world.hub + "/assistant/enroll", {"code": code, "device_id": "dev1"})[0] == 401

    # 3. MCP
    status, body = mcp(world, cred, "dev1", "initialize")
    assert status == 200 and body["result"]["serverInfo"]["name"] == "herdr-assistant"
    status, body = mcp(world, cred, "dev1", "tools/list")
    assert {"assistant_ask", "report", "memory_search"} <= {t["name"] for t in body["result"]["tools"]}

    ask = tool(world, cred, "dev1", "assistant_ask", {"question": "Delete the cache?", "options": ["yes", "no"],
                                                       "session_id": "s1"})
    assert ask["status"] == "no_decision" and ask["mode"] == "shadow" and ask["event_id"].startswith("evt_")
    assert set(ask) == {"status", "mode", "event_id"}

    rep = tool(world, cred, "dev1", "report", {"kind": "outcome", "text": "built the thing", "session_id": "s1"})
    (reported,) = events(world, kind="report")
    assert reported.provenance == "worker_reported" and reported.worker_id == wid
    assert reported.event_id in json.dumps(rep)

    status, body = mcp(world, cred, "dev1", "tools/call", {"name": "memory_search", "arguments": {"query": "x"}})
    assert body["result"]["isError"] is True and "no assistant" in body["result"]["content"][0]["text"]
    world.svc.create_assistant(tenant_id=TENANT, user_id=OWNER, name="mine")
    found = tool(world, cred, "dev1", "memory_search", {"query": "anything"})
    assert isinstance(found, dict)

    status, posted = http("POST", world.hub + "/assistant/hook-events",
                          {"kind": "observation", "session_id": "s1",
                           "payload": {"hook_event": "PostToolUse", "tool_name": "Bash", "outcome": "ran"}},
                          {"Authorization": f"Worker {cred}", "X-Herdr-Device": "dev1"})
    assert status == 200, posted
    (observed,) = events(world, kind="observation")
    assert (observed.provenance, observed.event_id) == ("harness_attested", posted["event_id"])
    assert {e.kind: e.provenance for e in events(world)} == {
        "ask": "worker_reported", "report": "worker_reported", "observation": "harness_attested"}

    # 4. the owner listing exposes nothing secret
    status, listing = owner(world, "GET", "/api/v1/assistant-workers")
    assert status == 200
    text = json.dumps(listing)
    assert cred not in text and code not in text and "digest" not in text and "secret" not in text
    (row,) = listing["workers"]
    assert row["worker_id"] == wid and row["status"] == "active"

    # 5. revoke -> the same credential is refused everywhere
    status, gone = owner(world, "DELETE", f"/api/v1/assistant-workers/{wid}")
    assert status == 200 and gone["status"] == "revoked"
    assert mcp(world, cred, "dev1", "tools/list")[0] == 401
    assert http("POST", world.hub + "/assistant/hook-events", {"kind": "observation", "session_id": "s1",
                                                              "payload": {"hook_event": "PostToolUse"}},
                {"Authorization": f"Worker {cred}", "X-Herdr-Device": "dev1"})[0] == 401
    assert len(events(world)) == 3     # the refused calls recorded nothing
    revoked = [a for a in world.svc.store.audit
               if a["action"] == "worker.revoked" and a["subject_id"] == wid and a["tenant_id"] == TENANT]
    assert len(revoked) == 1 and revoked[0]["actor"] == OWNER

    # 6. the audit chain verifies and neither secret appears anywhere durable
    assert world.svc.store.verify_audit(TENANT) is True
    audit_text = json.dumps(world.svc.store.audit, default=str)
    snapshot_text = json.dumps(world.svc.store.snapshot(), default=str)
    for blob in (audit_text, snapshot_text):
        assert cred not in blob and code not in blob
    assert audit_text  # not vacuous


def test_cli_redeem_install_hooks_and_installed_command_delivers_observation(world, tmp_path, capsys):
    home = tmp_path / "home"
    home.mkdir()
    status, created = owner(world, "POST", "/api/v1/assistant-workers", {"name": "cli-box", "harness": "claude"})
    assert status == 201
    worker_file = home / ".config" / "herdr" / "assistant-worker.json"
    rc = _cmd_cp_assistant_worker(Namespace(action="redeem", url=world.hub, code=created["code"],
                                            device_id="dev9", out=str(worker_file), force=False))
    out = capsys.readouterr().out
    assert rc == 0
    assert (worker_file.stat().st_mode & 0o777) == 0o600
    cred = json.loads(worker_file.read_text())["credential"]
    assert f'--header "Authorization: Worker {cred}"' in out

    settings = home / ".claude" / "settings.json"
    settings.parent.mkdir()
    rc = _cmd_cp_assistant_worker(Namespace(action="install-hooks", harness="claude", settings=str(settings),
                                            yes=True))
    capsys.readouterr()
    assert rc == 0
    hooks = json.loads(settings.read_text())["hooks"]
    command = hooks["PostToolUse"][0]["hooks"][0]["command"]

    event = {"session_id": "cli-s1", "hook_event_name": "PostToolUse", "tool_name": "Bash",
             "tool_input": {"command": "echo hi"}, "tool_use_id": "tu1", "tool_response": {"stdout": "hi"}}
    run = subprocess.run(command, shell=True, input=json.dumps(event), capture_output=True, text=True, timeout=30,
                         env=clean_env(home, HERDR_ASSISTANT_WORKER_FILE=str(worker_file)))
    assert (run.returncode, run.stdout, run.stderr) == (0, "", "")
    (obs,) = events(world, kind="observation")
    assert obs.provenance == "harness_attested" and obs.payload["tool_name"] == "Bash"
    assert obs.payload["hook_event"] == "PostToolUse" and obs.session_id == "cli-s1"
    assert world.svc.store.verify_audit(TENANT) is True
    assert cred not in json.dumps(world.svc.store.snapshot(), default=str)


@pytest.mark.integration
@pytest.mark.skipif(shutil.which("claude") is None, reason="the `claude` CLI is not on PATH")
def test_real_claude_mcp_add_writes_server_entry_in_throwaway_home(world, tmp_path):
    """Runs the owner API's `setup.mcp_add` line with the REAL claude binary, HOME=tmp_path and a clean environment
    (no Anthropic credentials, so nothing can be sent anywhere). Asserts on the written config, not on a health check."""
    home = tmp_path / "home"
    home.mkdir()
    status, created = owner(world, "POST", "/api/v1/assistant-workers", {"name": "claude-box", "harness": "claude"})
    assert status == 201
    status, redeemed = http("POST", world.hub + "/assistant/enroll", {"code": created["code"], "device_id": "dev-c"})
    assert status == 200
    line = (created["setup"]["mcp_add"].replace("<CREDENTIAL>", redeemed["credential"])
            .replace("<DEVICE>", redeemed["device_id"]))
    argv = shlex.split(line)
    argv[0] = shutil.which("claude")
    url_at = next(i for i, a in enumerate(argv) if a.startswith("http://") and a.endswith("/assistant/mcp"))
    argv[url_at] = world.hub + "/assistant/mcp"     # the test hub's real ephemeral port
    env = clean_env(home, PATH=f"{Path(argv[0]).parent}:/usr/bin:/bin", NO_COLOR="1", DISABLE_AUTOUPDATER="1",
                    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1")
    added = subprocess.run(argv, cwd=home, env=env, capture_output=True, text=True, timeout=60)
    assert added.returncode == 0, (added.stdout, added.stderr)
    written = json.loads((home / ".claude.json").read_text())
    servers = written.get("mcpServers") or next(
        (p["mcpServers"] for p in written.get("projects", {}).values() if p.get("mcpServers")), {})
    entry = servers["herdr-assistant"]
    assert entry["type"] == "http" and entry["url"] == world.hub + "/assistant/mcp"
    assert entry["headers"]["Authorization"] == f"Worker {redeemed['credential']}"
    assert entry["headers"]["X-Herdr-Device"] == "dev-c"
    got = subprocess.run([argv[0], "mcp", "get", "herdr-assistant"], cwd=home, env=env, capture_output=True,
                         text=True, timeout=60)
    assert "herdr-assistant" in got.stdout + got.stderr

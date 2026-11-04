from __future__ import annotations

import copy
import hashlib
import json
import stat
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from herdr_engineering import cli
from herdr_engineering.assistant_cli import (
    AssistantClient,
    AssistantConfig,
    ConfigError,
)
from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.collab import CollabService, StaticTokenAuthenticator
from herdr_engineering.control_plane.repo import ControlPlaneRepo

TOKEN = "tok-alice-0123456789"
OTHER_TOKEN = "tok-bob-0123456789abc"


class Sink:
    def __init__(self):
        self.events = []

    def record(self, event):
        self.record_required(event)

    def record_required(self, event):
        self.events.append(copy.deepcopy(event))


@pytest.fixture
def collab_server():
    sink = Sink()
    auth = StaticTokenAuthenticator({
        "alice": hashlib.sha256(TOKEN.encode()).hexdigest(),
        "bob": hashlib.sha256(OTHER_TOKEN.encode()).hexdigest(),
    })
    service = CollabService(auth, sink=sink)
    server = ControlPlaneServer(ControlPlaneRepo(sink=sink), host="127.0.0.1", port=0,
                                collab=service)
    thread = threading.Thread(target=server.serve, daemon=True)
    thread.start()
    server.wait_until_ready()
    yield server, service
    server.shutdown()
    thread.join(timeout=5)


def raw_call(port, method, path, body=None, token=TOKEN):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Authorization": f"Bearer {token}"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data,
                                     headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def seed_world(server):
    port = server.port
    status, project = raw_call(port, "POST", "/api/v1/collab/projects", {"name": "p"})
    assert status == 201
    status, room = raw_call(port, "POST", f"/api/v1/collab/projects/{project['project_id']}/rooms",
                            {"name": "r"})
    assert status == 201
    status, start = raw_call(port, "POST", "/api/v1/collab/agents/enrollment/start",
                             {"device_id": "dev-1", "harness_type": "pi"})
    assert status == 201
    status, agent = raw_call(port, "POST", "/api/v1/collab/agents/enrollment/complete",
                             {"code": start["code"], "device_id": "dev-1", "instance_id": "i-1"})
    assert status == 201
    agent["device_id"] = "dev-1"
    status, _ = raw_call(port, "POST", f"/api/v1/collab/rooms/{room['room_id']}/agents",
                         {"agent_id": agent["agent_id"],
                          "actions": ["read", "execute", "instruct"]})
    assert status == 201
    return room["room_id"], agent


def config(port, *, token=TOKEN, auth_kind="user", device=None):
    return AssistantConfig(base_url=f"http://127.0.0.1:{port}", token=token,
                           auth_kind=auth_kind, device_id=device)


def test_config_rejects_plain_http_off_loopback():
    with pytest.raises(ConfigError, match="HTTPS"):
        AssistantConfig(base_url="http://example.com", token=TOKEN, auth_kind="user")
    with pytest.raises(ConfigError, match="origin"):
        AssistantConfig(base_url="https://example.com/prefix", token=TOKEN, auth_kind="user")
    with pytest.raises(ConfigError, match="port"):
        AssistantConfig(base_url="https://example.com:not-a-port", token=TOKEN,
                        auth_kind="user")
    assert TOKEN not in repr(AssistantConfig(base_url="https://example.com", token=TOKEN,
                                             auth_kind="user"))


@pytest.mark.parametrize("url", [
    "https://example.com?",
    "https://example.com#",
    "https://example.com/?",
    "https://example.com/#",
    "https://@example.com",
    "https://example.com:",
])
def test_config_rejects_empty_url_delimiters_and_userinfo(url):
    with pytest.raises(ConfigError, match="origin"):
        AssistantConfig(base_url=url, token=TOKEN, auth_kind="user")


def test_config_reads_private_token_file_and_rejects_open_permissions(tmp_path, monkeypatch):
    token_file = tmp_path / "token"
    token_file.write_text(TOKEN + "\n")
    token_file.chmod(stat.S_IRUSR | stat.S_IWUSR)
    monkeypatch.setenv("HERDR_ASSISTANT_URL", "https://herdr.example")
    monkeypatch.setenv("HERDR_ASSISTANT_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("HERDR_ASSISTANT_AUTH_KIND", "user")
    assert AssistantConfig.from_env().token == TOKEN
    token_file.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP)
    with pytest.raises(ConfigError, match="permissions"):
        AssistantConfig.from_env()
    link = tmp_path / "token-link"
    link.symlink_to(token_file)
    monkeypatch.setenv("HERDR_ASSISTANT_TOKEN_FILE", str(link))
    with pytest.raises(ConfigError, match="securely open"):
        AssistantConfig.from_env()


def test_agents_and_capabilities_use_authenticated_server_scope(collab_server):
    server, _ = collab_server
    room, agent = seed_world(server)
    client = AssistantClient(config(server.port))
    agents = client.agents_list()
    assert [row["agent_id"] for row in agents["agents"]] == [agent["agent_id"]]
    capabilities = client.capabilities(room)
    assert capabilities["role_registry_available"] is False
    assert capabilities["agents"][0]["actions"] == ["execute", "instruct", "read"]

    other = AssistantClient(config(server.port, token=OTHER_TOKEN))
    with pytest.raises(Exception) as denied:
        other.capabilities(room)
    assert getattr(denied.value, "status", None) == 404


def test_task_create_is_idempotent_and_task_get_filters_room(collab_server):
    server, _ = collab_server
    room, agent = seed_world(server)
    client = AssistantClient(config(server.port))
    first = client.task_create(room, agent["agent_id"], "research venues", "task-key-1")
    replay = client.task_create(room, agent["agent_id"], "research venues", "task-key-1")
    assert replay == {"task_id": first["task_id"], "status": "pending", "replayed": True}
    assert client.task_get(room, first["task_id"])["text"] == "research venues"
    with pytest.raises(Exception) as missing:
        client.task_get(room, "grant_missing")
    assert getattr(missing.value, "status", None) == 404


def test_agent_inbox_and_update_use_agent_auth(collab_server):
    server, _ = collab_server
    room, agent = seed_world(server)
    user = AssistantClient(config(server.port))
    task = user.task_create(room, agent["agent_id"], "research venues", "task-key-2")
    worker = AssistantClient(config(server.port, token=agent["credential"], auth_kind="agent",
                                    device=agent["device_id"]))
    assert worker.task_inbox()["tasks"][0]["task_id"] == task["task_id"]
    assert worker.task_update(task["task_id"], "accepted")["status"] == "accepted"
    assert worker.task_update(task["task_id"], "done", "draft ready")["status"] == "done"


def test_cli_prints_versioned_success_and_error_envelopes(collab_server, monkeypatch, capsys):
    server, _ = collab_server
    seed_world(server)
    monkeypatch.setenv("HERDR_ASSISTANT_URL", f"http://127.0.0.1:{server.port}")
    monkeypatch.setenv("HERDR_ASSISTANT_TOKEN", TOKEN)
    monkeypatch.setenv("HERDR_ASSISTANT_AUTH_KIND", "user")
    assert cli.main(["assistant", "agents", "list", "--json"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["schema_version"] == 1 and output["ok"] is True

    monkeypatch.setenv("HERDR_ASSISTANT_TOKEN", "bad-token")
    assert cli.main(["assistant", "agents", "list", "--json"]) == 1
    error = json.loads(capsys.readouterr().out)
    assert error["schema_version"] == 1 and error["ok"] is False
    assert error["error"]["status"] == 401


def test_cli_task_create_accepts_json_file(collab_server, tmp_path, monkeypatch, capsys):
    server, _ = collab_server
    room, agent = seed_world(server)
    request_file = tmp_path / "task.json"
    request_file.write_text(json.dumps({"room_id": room, "target_agent_id": agent["agent_id"],
                                        "text": "compare venues",
                                        "idempotency_key": "fixture-task-1"}))
    monkeypatch.setenv("HERDR_ASSISTANT_URL", f"http://127.0.0.1:{server.port}")
    monkeypatch.setenv("HERDR_ASSISTANT_TOKEN", TOKEN)
    monkeypatch.setenv("HERDR_ASSISTANT_AUTH_KIND", "user")
    assert cli.main(["assistant", "tasks", "create", "--file", str(request_file), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["result"]["status"] == "pending"


def test_approval_request_uses_pending_proposal_contract(collab_server):
    server, _ = collab_server
    room, agent = seed_world(server)
    client = AssistantClient(config(server.port))
    task = client.task_create(room, agent["agent_id"], "contact venue", "task-key-3")
    created = client.approval_request(
        room, {"task_id": task["task_id"], "kind": "external_contact",
               "summary": "Contact one venue", "details": {"recipients": 1},
               "idempotency_key": "approval-1"})
    assert created["proposal"]["state"] == "pending"
    assert "granted" not in created["proposal"] and "approved" not in created["proposal"]
    assert client.approvals_list(room)["proposals"][0]["task_id"] == task["task_id"]


def test_redirect_is_not_followed_and_authorization_is_not_forwarded():
    received = []

    class Capture(BaseHTTPRequestHandler):
        def do_GET(self):
            received.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"agents":[]}')

        def log_message(self, *_args):
            pass

    capture = ThreadingHTTPServer(("127.0.0.1", 0), Capture)
    capture_thread = threading.Thread(target=capture.serve_forever, daemon=True)
    capture_thread.start()

    class Redirect(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{capture.server_address[1]}/steal")
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":"redirect refused"}')

        def log_message(self, *_args):
            pass

    redirect = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
    redirect_thread = threading.Thread(target=redirect.serve_forever, daemon=True)
    redirect_thread.start()
    try:
        client = AssistantClient(config(redirect.server_address[1]))
        with pytest.raises(Exception) as refused:
            client.agents_list()
        assert getattr(refused.value, "status", None) == 302
        assert received == []
    finally:
        redirect.shutdown()
        capture.shutdown()
        redirect_thread.join(timeout=5)
        capture_thread.join(timeout=5)


def test_cli_rejects_forged_scope_fields_before_http(tmp_path, monkeypatch, capsys):
    payload = tmp_path / "task.json"
    payload.write_text(json.dumps({"room_id": "room_1", "target_agent_id": "agent_1",
                                   "text": "work", "idempotency_key": "key-1",
                                   "owner_user_id": "mallory"}))
    monkeypatch.setenv("HERDR_ASSISTANT_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("HERDR_ASSISTANT_TOKEN", TOKEN)
    assert cli.main(["assistant", "tasks", "create", "--file", str(payload), "--json"]) == 1
    error = json.loads(capsys.readouterr().out)
    assert error["error"]["code"] == "invalid_request"
    assert "unknown field" in error["error"]["message"]


def test_missing_credential_and_bad_arguments_are_json_errors(monkeypatch, capsys):
    monkeypatch.delenv("HERDR_ASSISTANT_TOKEN", raising=False)
    monkeypatch.delenv("HERDR_ASSISTANT_TOKEN_FILE", raising=False)
    assert cli.main(["assistant", "agents", "list", "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "invalid_request"
    assert cli.main(["assistant", "tasks", "update", "task-1", "--json"]) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "argument_error"


def test_real_process_bad_arguments_return_json():
    result = subprocess.run(
        [sys.executable, "-c",
         "from herdr_engineering.cli import main; raise SystemExit(main())",
         "assistant", "tasks", "update", "task-1", "--json"],
        check=False, capture_output=True, text=True,
    )
    assert result.returncode == 2
    parsed = json.loads(result.stdout)
    assert parsed["error"]["code"] == "argument_error"
    assert "--status" in parsed["error"]["message"]


def test_transport_error_does_not_echo_credential(monkeypatch):
    client = AssistantClient(config(9))

    def fail(*_args, **_kwargs):
        raise urllib.error.URLError(f"connection failed with {TOKEN}")

    monkeypatch.setattr(client._opener, "open", fail)
    with pytest.raises(Exception) as error:
        client.agents_list()
    assert TOKEN not in str(error.value)

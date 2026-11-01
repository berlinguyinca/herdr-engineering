from __future__ import annotations

import json
import time

import pytest

from herdr_engineering import cli
from herdr_engineering.assistant_cli import AssistantClient
from herdr_engineering.cli import _build_parser
from tests.test_assistant_cli import OTHER_TOKEN, TOKEN, config, seed_world
from tests.test_assistant_packages import DIGEST_B, manifest

pytest_plugins = ("tests.test_assistant_cli",)


def client_with_capture(monkeypatch):
    client = AssistantClient(config(9))
    calls = []

    def request(method, path, body=None):
        calls.append((method, path, body))
        if method == "GET" and path.endswith("/assistant-hosts"):
            return {"hosts": []}
        if method == "GET" and "/assistant-runtimes/" in path:
            return {"runtime": {"runtime_id": "runtime_1", "status": "queued",
                                "package_id": "package_1", "package_digest": "sha256:x",
                                "host_id": "host_1", "agent_id": None,
                                "cancel_requested": False, "checkpoint": None,
                                "error_code": None, "lease_expires_at": None,
                                "updated_at": "2026-10-30T00:00:00Z"}}
        if method == "GET":
            return {"runtimes": []}
        return {"runtime": {"runtime_id": "runtime_1", "status": "queued"}}

    monkeypatch.setattr(client, "_request", request)
    return client, calls


def test_runtime_client_uses_exact_owner_routes_and_creation_body(monkeypatch):
    client, calls = client_with_capture(monkeypatch)
    assert client.hosts_list("room_1") == {"hosts": []}
    client.runtime_create("room_1", "package_1", "host_1", "venue-agent", "create-1",
                          selected_model="qwen3-32b")
    client.runtimes_list("room_1")
    client.runtime_get("room_1", "runtime_1")
    client.runtime_cancel("room_1", "runtime_1")
    assert calls == [
        ("GET", "/api/v1/collab/rooms/room_1/assistant-hosts", None),
        ("POST", "/api/v1/collab/rooms/room_1/assistant-runtimes", {
            "package_id": "package_1", "host_id": "host_1", "name": "venue-agent",
            "idempotency_key": "create-1", "selected_model": "qwen3-32b",
        }),
        ("GET", "/api/v1/collab/rooms/room_1/assistant-runtimes", None),
        ("GET", "/api/v1/collab/rooms/room_1/assistant-runtimes/runtime_1", None),
        ("POST", "/api/v1/collab/rooms/room_1/assistant-runtimes/runtime_1/cancel", {}),
    ]


def test_runtime_create_omits_optional_model_instead_of_sending_null(monkeypatch):
    client, calls = client_with_capture(monkeypatch)
    client.runtime_create("room_1", "package_1", "host_1", "venue-agent", "create-1")
    assert calls[0][2] == {
        "package_id": "package_1", "host_id": "host_1", "name": "venue-agent",
        "idempotency_key": "create-1",
    }


def test_runtime_status_is_a_bounded_projection(monkeypatch):
    client, _ = client_with_capture(monkeypatch)
    assert client.runtime_status("room_1", "runtime_1") == {
        "runtime_id": "runtime_1", "status": "queued", "package_id": "package_1",
        "package_digest": "sha256:x", "host_id": "host_1", "agent_id": None,
        "cancel_requested": False, "checkpoint": None, "error_code": None,
        "lease_expires_at": None, "updated_at": "2026-10-30T00:00:00Z",
    }


def test_parser_exposes_hosts_and_runtime_commands_without_execution_fields():
    parser = _build_parser()
    hosts = parser.parse_args(["assistant", "hosts", "list", "--room", "room_1", "--json"])
    assert hosts.assistant_operation == "hosts-list"
    create = parser.parse_args([
        "assistant", "agents", "create", "--package", "package_1", "--room", "room_1",
        "--host", "host_1", "--name", "venue-agent", "--idempotency-key", "create-1",
        "--model", "qwen3-32b", "--json",
    ])
    assert create.assistant_operation == "agents-create"
    assert vars(create).keys() >= {
        "package_id", "room", "host_id", "name", "idempotency_key", "selected_model",
    }
    assert not vars(create).keys() & {
        "image", "path", "env", "grant", "owner_user_id", "credential",
    }
    for operation in ("runtime-inspect", "runtime-status", "runtime-cancel"):
        parsed = parser.parse_args([
            "assistant", "agents", operation, "runtime_1", "--room", "room_1", "--json",
        ])
        assert parsed.assistant_operation == "agents-" + operation


def attest(service, package, *, expires_in=240, models=("qwen3-32b", "llama-4-scout")):
    now = time.time()
    body = {
        "schema_version": 1, "host_id": "local-linux", "profile": "task-restricted-v1",
        "profile_digest": "sha256:" + "c" * 64,
        "runtime": {"kind": "pi",
                    "artifact_digest": package["manifest"]["runtime"]["artifact"]["digest"],
                    "entrypoint_id": "pi-fixed-v1"},
        "enforcement": {"bubblewrap": True, "cgroup_v2": True, "broker_only": True,
                        "task_only": True, "no_new_privileges": True,
                        "limits": package["manifest"]["sandbox"]["limits"]},
        "approved_models": list(models), "issued_at": now, "expires_at": now + expires_in,
        "nonce": "nonce-runtime-cli-01234567",
        "role_bindings": package["manifest"]["references"]["procedures"],
    }
    status, result = service.dispatch(
        "POST", ["fleet", "assistant-runtime-hosts"], {}, body, "", None, operator="alice")
    assert status == 201, result
    return body


def registered_package(client, room, *, name="venue-coordinator", digest=None):
    source = manifest()
    source["name"] = name
    if digest:
        source["runtime"]["artifact"]["digest"] = digest
    return client.package_register(room, source)["package"]


def test_real_server_runtime_cli_success_replay_status_cancel_and_json(
        collab_server, monkeypatch, capsys):
    server, service = collab_server
    room, _ = seed_world(server)
    owner = AssistantClient(config(server.port))
    package = registered_package(owner, room)
    attest(service, package)
    hosts = owner.hosts_list(room)
    assert hosts["hosts"][0]["status"] == "ready"
    first = owner.runtime_create(
        room, package["package_id"], "local-linux", "venue-agent", "runtime-cli-1")
    assert first["runtime"]["status"] == "queued"
    replay = owner.runtime_create(
        room, package["package_id"], "local-linux", "venue-agent", "runtime-cli-1")
    assert replay == {"runtime": first["runtime"], "replayed": True}
    runtime_id = first["runtime"]["runtime_id"]
    assert owner.runtime_status(room, runtime_id)["status"] == "queued"
    assert "credential" not in json.dumps(owner.runtime_get(room, runtime_id))
    cancelled = owner.runtime_cancel(room, runtime_id)["runtime"]
    assert cancelled["status"] == "cancelled" and cancelled["cancel_requested"] is True

    monkeypatch.setenv("HERDR_ASSISTANT_URL", f"http://127.0.0.1:{server.port}")
    monkeypatch.setenv("HERDR_ASSISTANT_TOKEN", TOKEN)
    monkeypatch.setenv("HERDR_ASSISTANT_AUTH_KIND", "user")
    assert cli.main(["assistant", "hosts", "list", "--room", room, "--json"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["schema_version"] == 1 and output["result"]["hosts"][0]["status"] == "ready"
    assert cli.main([
        "assistant", "agents", "create", "--package", package["package_id"],
        "--room", room, "--host", "local-linux", "--name", "venue-agent-two",
        "--idempotency-key", "runtime-cli-json-2", "--json",
    ]) == 0
    created = json.loads(capsys.readouterr().out)
    assert created["schema_version"] == 1
    assert created["result"]["runtime"]["status"] == "queued"
    cli_runtime_id = created["result"]["runtime"]["runtime_id"]
    assert cli.main([
        "assistant", "agents", "runtime-status", cli_runtime_id, "--room", room, "--json",
    ]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["result"]["runtime_id"] == cli_runtime_id
    assert status["result"]["status"] == "queued"
    assert cli.main([
        "assistant", "agents", "runtime-inspect", cli_runtime_id, "--room", room, "--json",
    ]) == 0
    inspected = json.loads(capsys.readouterr().out)
    assert inspected["result"]["access"] == ["execute", "read"]
    assert cli.main([
        "assistant", "agents", "runtime-cancel", cli_runtime_id, "--room", room, "--json",
    ]) == 0
    cancelled_json = json.loads(capsys.readouterr().out)
    assert cancelled_json["result"]["runtime"]["status"] == "cancelled"


def test_real_server_runtime_cli_denies_nonowner_agent_unattested_mismatch_and_model(
        collab_server):
    server, service = collab_server
    room, agent = seed_world(server)
    owner = AssistantClient(config(server.port))
    package = registered_package(owner, room)
    with pytest.raises(Exception) as no_host:
        owner.runtime_create(
            room, package["package_id"], "local-linux", "venue-agent", "no-host")
    assert getattr(no_host.value, "status", None) == 409

    attest(service, package)
    mismatched = registered_package(owner, room, name="other-package", digest=DIGEST_B)
    with pytest.raises(Exception) as mismatch:
        owner.runtime_create(
            room, mismatched["package_id"], "local-linux", "other-agent", "mismatch")
    assert getattr(mismatch.value, "status", None) == 409
    with pytest.raises(Exception) as model:
        owner.runtime_create(
            room, package["package_id"], "local-linux", "venue-agent", "bad-model",
            selected_model="not-approved")
    assert getattr(model.value, "status", None) == 409

    unrelated = AssistantClient(config(server.port, token=OTHER_TOKEN))
    with pytest.raises(Exception) as hidden:
        unrelated.hosts_list(room)
    assert getattr(hidden.value, "status", None) in {403, 404}
    worker = AssistantClient(config(
        server.port, token=agent["credential"], auth_kind="agent", device=agent["device_id"]))
    with pytest.raises(Exception) as forbidden:
        worker.runtime_create(
            room, package["package_id"], "local-linux", "venue-agent", "agent-attempt")
    assert getattr(forbidden.value, "status", None) in {403, 404}


def test_real_server_runtime_cli_rejects_stale_host(collab_server):
    server, service = collab_server
    room, _ = seed_world(server)
    owner = AssistantClient(config(server.port))
    package = registered_package(owner, room)
    attestation = attest(service, package, expires_in=1)
    service._now = lambda: attestation["expires_at"] + 1
    assert owner.hosts_list(room)["hosts"][0]["status"] == "stale"
    with pytest.raises(Exception) as stale:
        owner.runtime_create(
            room, package["package_id"], "local-linux", "venue-agent", "stale-host")
    assert getattr(stale.value, "status", None) == 409


def test_ready_runtime_agent_accepts_existing_task_contract(collab_server):
    server, service = collab_server
    room, _ = seed_world(server)
    owner = AssistantClient(config(server.port))
    package = registered_package(owner, room)
    attestation = attest(service, package)
    runtime = owner.runtime_create(
        room, package["package_id"], "local-linux", "venue-agent", "task-ready-run")["runtime"]
    status, claimed = service.dispatch(
        "POST", ["fleet", "assistant-runtimes", runtime["runtime_id"], "claim"], {},
        {"host_id": "local-linux", "attestation_nonce": attestation["nonce"]},
        "", None, operator="alice")
    assert status == 200
    launch = {
        "profile_digest": attestation["profile_digest"],
        "runtime_artifact_digest": attestation["runtime"]["artifact_digest"],
        "enforcement": attestation["enforcement"],
        "role_bindings": package["manifest"]["references"]["procedures"],
    }
    report = {
        "claim_token": claimed["claim_token"], "attestation_nonce": attestation["nonce"],
        "status": "ready", "checkpoint": {"sequence": 1, "phase": "ready",
                                             "output_sha256": None},
        "launch_attestation": launch, "instance_id": "pi-runtime-cli-test",
    }
    status, _ = service.dispatch(
        "POST", ["fleet", "assistant-runtimes", runtime["runtime_id"], "report"], {}, report,
        "", None, operator="alice")
    assert status == 200
    ready = owner.runtime_get(room, runtime["runtime_id"])
    task = owner.task_create(room, ready["agent_id"], "Return the harmless fixture summary.",
                             "runtime-task-1")
    worker = AssistantClient(config(
        server.port, token=claimed["credential"], auth_kind="agent",
        device=claimed["device_id"]))
    assert worker.task_inbox()["tasks"][0]["task_id"] == task["task_id"]

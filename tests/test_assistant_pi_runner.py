from __future__ import annotations

import hashlib
import json
import os
import subprocess

import pytest

from herdr_engineering.assistant_pi_runner import (
    RunnerError,
    cancel_process,
    fixed_pi_command,
    launch_worker,
    load_role_registry,
    prepare_activation,
    profile_digest,
    run_activation,
    stage_runtime,
    tree_digest,
    validate_claim,
    verified_role_refs,
)

LIMITS = {
    "memory_bytes": 536870912, "process_count": 64,
    "cpu_quota_percent": 100, "wall_time_seconds": 300,
}


def fake_runtime(tmp_path):
    source = tmp_path / "source"
    (source / "dist/bundle").mkdir(parents=True)
    (source / "package.json").write_text(json.dumps({
        "name": "@earendil-works/pi-coding-agent", "version": "1.4.0",
    }))
    (source / "dist/bundle/cli.js").write_text("process.stdout.write('ok')")
    return source


def claim(digest):
    return {
        "runtime": {"host_id": "bender", "attestation_nonce": "nonce-1"},
        "credential": "hca_private", "device_id": "dev-1", "claim_token": "claim-private",
        "lease_expires_at": 2_000_000_000, "selected_model": "approved-model",
        "actions": ["execute", "read"],
        "package": {
            "package_id": "package_1", "digest": "sha256:" + "1" * 64,
            "runtime_artifact_digest": digest, "profile": "task-restricted-v1",
            "limits": LIMITS, "required_role_bindings": [],
        },
    }


def test_runtime_is_staged_content_addressed_and_read_only(tmp_path):
    staged, digest = stage_runtime(fake_runtime(tmp_path), tmp_path / "artifacts")
    assert tree_digest(staged) == digest
    assert staged.name == digest.removeprefix("sha256:")
    assert staged.stat().st_mode & 0o222 == 0
    assert (staged / "dist/bundle/cli.js").stat().st_mode & 0o222 == 0


def test_runtime_ignores_package_bin_links_and_rejects_other_links(tmp_path):
    source = fake_runtime(tmp_path)
    (source / "node_modules/.bin").mkdir(parents=True)
    (source / "node_modules/.bin/tool").symlink_to(source / "package.json")
    staged, _ = stage_runtime(source, tmp_path / "artifacts")
    assert not (staged / "node_modules/.bin").exists()
    (source / "linked").symlink_to(source / "package.json")
    with pytest.raises(RunnerError, match="untrusted link"):
        stage_runtime(source, tmp_path / "other")


def test_copy_race_preserves_link_for_postcopy_rejection(tmp_path, monkeypatch):
    import shutil

    import herdr_engineering.assistant_pi_runner as runner

    source = fake_runtime(tmp_path)
    original = shutil.copytree

    def raced_copytree(src, dst, *args, **kwargs):
        result = original(src, dst, *args, **kwargs)
        dst_path = type(tmp_path)(dst)
        if dst_path.name == "runtime":
            (dst_path / "raced-link").symlink_to(tmp_path / "outside-secret")
        return result

    monkeypatch.setattr(runner.shutil, "copytree", raced_copytree)
    with pytest.raises(RunnerError, match="link or special"):
        stage_runtime(source, tmp_path / "artifacts")
    assert not list((tmp_path / "artifacts").glob("stage-*"))


def test_claim_is_exact_and_bound_to_attestation():
    value = claim("sha256:" + "2" * 64)
    assert validate_claim(value, "bender", "nonce-1") is value
    with pytest.raises(RunnerError, match="attestation"):
        validate_claim(value, "beast", "nonce-1")
    value["command"] = "sh"
    with pytest.raises(RunnerError, match="invalid claim"):
        validate_claim(value, "bender", "nonce-1")


def test_activation_uses_fixed_pi_argv_and_private_files(tmp_path):
    staged, digest = stage_runtime(fake_runtime(tmp_path), tmp_path / "artifacts")
    value = claim(digest)
    sandbox = tmp_path / "sandbox.sh"; sandbox.write_text("#!/bin/sh\n")
    worker = tmp_path / "worker.sh"; worker.write_text(
        "BASE='__HERDR_BASE__'\n"
        "  call tasks_wait '{\"seconds\":20}' > \"$TMP/r.json\" || { sleep 5; continue; }\n"
    )
    sock = tmp_path / "egress.sock"; sock.write_text("")
    activation, env, roles = prepare_activation(
        tmp_path / "runs", value, staged, sandbox, worker,
        "https://collab.example", sock,
    )
    assert env["HERDR_AGENT_CMD"] == fixed_pi_command("approved-model")
    assert "/opt/herdr-pi/dist/bundle/cli.js" in env["HERDR_AGENT_CMD"]
    assert "--mode json" not in env["HERDR_AGENT_CMD"]
    assert env["HERDR_SANDBOX_RUNTIME_DIR"] == str(staged)
    assert env["HERDR_SANDBOX_PROFILE"] == "task-restricted-v1"
    assert "hca_private" not in json.dumps(env)
    assert (activation / "agent.json").stat().st_mode & 0o077 == 0
    models = json.loads((activation / "harness/pi/models.json").read_text())
    assert models["providers"]["herdr"]["apiKey"] == "broker-bound"
    assert models["providers"]["herdr"]["models"] == [{"id": "approved-model"}]
    assert roles == []


def test_model_cannot_become_shell_arguments():
    with pytest.raises(RunnerError, match="invalid model"):
        fixed_pi_command("ok; touch /tmp/pwn")


@pytest.mark.parametrize("origin", [
    "http://127.0.0.1:123@evil.example", "http://127.0.0.1:8080?", "http://127.0.0.1:8080#",
    "https://127.0.0.1:8080", "http://localhost:8080", "http://127.0.0.1",
])
def test_operator_origin_is_exact_loopback(origin):
    from herdr_engineering.assistant_pi_runner import OperatorClient
    with pytest.raises(RunnerError, match="loopback"):
        OperatorClient(origin)


def test_secretary_role_is_digest_bound_and_uses_static_prompt_path(tmp_path):
    source = fake_runtime(tmp_path)
    staged, digest = stage_runtime(source, tmp_path / "artifacts")
    role = tmp_path / "secretary.md"; role.write_text("Secretary role\n"); role.chmod(0o600)
    role_digest = "sha256:" + hashlib.sha256(role.read_bytes()).hexdigest()
    registry_file = tmp_path / "roles.json"
    registry_file.write_text(json.dumps({"roles": [{
        "id": "procedure:secretary-system", "version": "1.0.0",
        "digest": role_digest, "path": str(role),
    }]})); registry_file.chmod(0o600)
    registry = load_role_registry(registry_file)
    assert verified_role_refs(registry) == [{
        "id": "procedure:secretary-system", "version": "1.0.0", "digest": role_digest,
    }]
    value = claim(digest)
    value["package"]["required_role_bindings"] = [{
        "id": "procedure:secretary-system", "version": "1.0.0", "digest": role_digest,
    }]
    sandbox = tmp_path / "sandbox"; sandbox.write_text("x")
    worker = tmp_path / "worker"; worker.write_text(
        "BASE='__HERDR_BASE__'\n"
        "  call tasks_wait '{\"seconds\":20}' > \"$TMP/r.json\" || { sleep 5; continue; }\n")
    activation, env, verified = prepare_activation(
        tmp_path / "runs", value, staged, sandbox, worker,
        "https://collab.example", tmp_path / "sock", registry)
    assert verified == value["package"]["required_role_bindings"]
    assert (activation / "harness/pi/role-00.md").read_text() == "Secretary role\n"
    assert (activation / "harness/pi/role-00.md").stat().st_mode & 0o777 == 0o400
    assert '--append-system-prompt "$PI_CODING_AGENT_DIR/role-00.md"' in env["HERDR_AGENT_CMD"]
    assert "Secretary role" not in env["HERDR_AGENT_CMD"]
    role.write_text("tampered"); role.chmod(0o600)
    with pytest.raises(RunnerError, match="digest"):
        verified_role_refs(registry)


def test_role_registry_rejects_tamper_links_and_hardlinks(tmp_path):
    role = tmp_path / "role"; role.write_text("role"); role.chmod(0o600)
    digest = "sha256:" + hashlib.sha256(role.read_bytes()).hexdigest()
    from herdr_engineering.assistant_pi_runner import read_verified_role
    assert read_verified_role(role, digest) == "role"
    role.write_text("tampered")
    with pytest.raises(RunnerError, match="digest"):
        read_verified_role(role, digest)
    link = tmp_path / "link"; link.symlink_to(role)
    with pytest.raises(OSError):
        read_verified_role(link, digest)
    hard = tmp_path / "hard"; os.link(role, hard)
    with pytest.raises(RunnerError, match="private regular"):
        read_verified_role(role, "sha256:" + hashlib.sha256(role.read_bytes()).hexdigest())


def test_multiple_roles_are_sorted_and_appended_in_fixed_order(tmp_path):
    source = fake_runtime(tmp_path); staged, digest = stage_runtime(source, tmp_path / "artifacts")
    registry = {}; required = []
    for role_id, text in (("procedure:z-role", "z"), ("procedure:a-role", "a")):
        path = tmp_path / (role_id.split(":")[1] + ".md"); path.write_text(text); path.chmod(0o600)
        role_digest = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
        key = (role_id, "1.0.0", role_digest); registry[key] = path
        required.append({"id": role_id, "version": "1.0.0", "digest": role_digest})
    value = claim(digest); value["package"]["required_role_bindings"] = required
    sandbox = tmp_path / "sandbox"; sandbox.write_text("x")
    worker = tmp_path / "worker"; worker.write_text(
        "BASE='__HERDR_BASE__'\n"
        "  call tasks_wait '{\"seconds\":20}' > \"$TMP/r.json\" || { sleep 5; continue; }\n")
    activation, env, verified = prepare_activation(
        tmp_path / "runs", value, staged, sandbox, worker,
        "https://collab.example", tmp_path / "sock", registry)
    assert [row["id"] for row in verified] == ["procedure:a-role", "procedure:z-role"]
    assert (activation / "harness/pi/role-00.md").read_text() == "a"
    assert (activation / "harness/pi/role-01.md").read_text() == "z"
    command = env["HERDR_AGENT_CMD"]
    assert command.index("role-00.md") < command.index("role-01.md")


def test_cancel_terminates_the_process_group():
    process = subprocess.Popen(
        ["bash", "-c", "sleep 60 & wait"], start_new_session=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    cancel_process(process)
    assert process.poll() is not None


def test_worker_does_not_inherit_host_secrets(tmp_path, monkeypatch):
    activation = tmp_path
    (activation / "worker.sh").write_text("#!/bin/sh\n")
    (activation / "agent.json").write_text("{}")
    seen = {}

    class Process:
        pass

    def fake_popen(*args, **kwargs):
        seen.update(kwargs["env"])
        return Process()

    monkeypatch.setenv("SHOULD_NOT_REACH_WORKER", "secret")
    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    launch_worker(activation, {"HOME": "/private/run"})
    assert "SHOULD_NOT_REACH_WORKER" not in seen
    assert seen["HOME"] == "/private/run"


def test_pre_ready_failure_reports_terminal_receipt_and_cleans_up(tmp_path, monkeypatch):
    import time

    import herdr_engineering.assistant_pi_runner as runner

    value = claim("sha256:" + "2" * 64)
    value["lease_expires_at"] = time.time() + 60
    activation = tmp_path / "activation"; activation.mkdir()
    (tmp_path / "sandbox").write_text("#!/bin/sh\n")
    reports = []

    class Client:
        def report(self, _runtime_id, body):
            reports.append(body)

    class Process:
        pid = 123
        def poll(self): return 1

    monkeypatch.setattr(runner, "prepare_activation", lambda *args: (
        activation, {"HERDR_SANDBOX_UNIT": "herdr-test"}, []))
    monkeypatch.setattr(runner, "launch_worker", lambda *args: Process())
    monkeypatch.setattr(runner, "cancel_process", lambda process: None)
    monkeypatch.setattr(runner, "stop_sandbox_unit", lambda unit: None)

    with pytest.raises(RunnerError, match="task polling"):
        profile_hash = profile_digest(tmp_path / "sandbox", LIMITS)
        run_activation(Client(), "runtime_1", "bender", "nonce-1", value,
                       tmp_path, tmp_path, tmp_path / "sandbox", tmp_path / "worker",
                       "https://collab.example", tmp_path / "sock", {}, profile_hash)
    assert [body["status"] for body in reports] == ["starting", "failed"]
    assert reports[-1]["error_code"] == "runner_failed"
    assert reports[-1]["checkpoint"]["sequence"] == 2


def test_unverifiable_scope_stop_emits_no_terminal_receipt(tmp_path, monkeypatch):
    import time

    import herdr_engineering.assistant_pi_runner as runner

    value = claim("sha256:" + "2" * 64); value["lease_expires_at"] = time.time() + 60
    activation = tmp_path / "activation"; activation.mkdir()
    sandbox = tmp_path / "sandbox"; sandbox.write_text("x")
    reports = []

    class Client:
        def report(self, _runtime_id, body): reports.append(body)

    class Process:
        pid = 123
        def poll(self): return 1

    monkeypatch.setattr(runner, "prepare_activation", lambda *args: (
        activation, {"HERDR_SANDBOX_UNIT": "herdr-test"}, []))
    monkeypatch.setattr(runner, "launch_worker", lambda *args: Process())
    monkeypatch.setattr(runner, "cancel_process", lambda process: None)
    monkeypatch.setattr(runner, "stop_sandbox_unit", lambda unit: (_ for _ in ()).throw(
        RunnerError("sandbox scope stop could not be verified")))
    with pytest.raises(RunnerError, match="could not be verified"):
        run_activation(Client(), "runtime_1", "bender", "nonce-1", value, tmp_path, tmp_path,
                       sandbox, tmp_path / "worker", "https://collab.example", tmp_path / "sock",
                       {}, profile_digest(sandbox, LIMITS))
    assert [body["status"] for body in reports] == ["starting"]

"""Fail-closed contract for the opt-in task-restricted Pi sandbox profile."""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PACKAGED = ROOT / "packages/collab-scripts/sandbox.sh"
DEPLOYED = ROOT / "deploy/control-plane/collab-sandbox.sh"
PROFILE = "task-restricted-v1"


@pytest.fixture
def strict_env(tmp_path):
    short_root = Path(tempfile.mkdtemp(prefix="herdr-strict-", dir="/tmp"))
    home = short_root / "home"
    work = home / "herdr-agents/work/a1"
    harness = short_root / "harness"
    broker = short_root / "broker"
    runtime = short_root / "runtime"
    for directory in (work, harness, broker, runtime):
        directory.mkdir(parents=True, mode=0o700)
        directory.chmod(0o700)
    work.parent.chmod(0o700)
    sock_path = broker / "egress.sock"
    sock = socket.socket(socket.AF_UNIX)
    sock.bind(str(sock_path))
    env = {
        **os.environ,
        "HOME": str(home),
        "HERDR_SANDBOX_PROFILE": PROFILE,
        "HERDR_SANDBOX_HARNESS_DIR": str(harness),
        "HERDR_SANDBOX_RUNTIME_DIR": str(runtime),
        "HERDR_EGRESS_SOCK": str(sock_path),
        "HERDR_SANDBOX_MEM_BYTES": "1073741824",
        "HERDR_SANDBOX_TASKS": "128",
        "HERDR_SANDBOX_TIMEOUT_SECONDS": "900",
        "HERDR_SANDBOX_CPU_PERCENT": "100",
        "HERDR_SANDBOX_UNIT": "herdr-test-agent",
    }
    try:
        yield env, work, harness, sock_path
    finally:
        sock.close()
        shutil.rmtree(short_root)


def run_box(env, work, command="echo should-not-run"):
    return subprocess.run(
        ["bash", str(DEPLOYED), str(work), command], env=env,
        capture_output=True, text=True, timeout=15,
    )


def test_script_copies_remain_identical_and_parse():
    assert PACKAGED.read_bytes() == DEPLOYED.read_bytes()
    assert subprocess.run(["bash", "-n", str(PACKAGED),], capture_output=True).returncode == 0


def test_unknown_profile_refuses_before_running(strict_env):
    env, work, _, _ = strict_env
    env["HERDR_SANDBOX_PROFILE"] = "almost-strict"
    result = run_box(env, work)
    assert result.returncode == 126
    assert "unknown sandbox profile" in result.stderr
    assert "should-not-run" not in result.stdout


@pytest.mark.parametrize(
    ("missing", "message"),
    [
        ("HERDR_SANDBOX_HARNESS_DIR", "requires a private harness directory"),
        ("HERDR_SANDBOX_RUNTIME_DIR", "requires a pinned runtime directory"),
        ("HERDR_EGRESS_SOCK", "requires broker-only network egress"),
        ("HERDR_SANDBOX_MEM_BYTES", "HERDR_SANDBOX_MEM_BYTES"),
        ("HERDR_SANDBOX_TASKS", "HERDR_SANDBOX_TASKS"),
        ("HERDR_SANDBOX_TIMEOUT_SECONDS", "HERDR_SANDBOX_TIMEOUT_SECONDS"),
        ("HERDR_SANDBOX_CPU_PERCENT", "HERDR_SANDBOX_CPU_PERCENT"),
        ("HERDR_SANDBOX_UNIT", "safe sandbox unit"),
    ],
)
def test_strict_profile_requires_every_control(strict_env, missing, message):
    env, work, _, _ = strict_env
    env.pop(missing)
    result = run_box(env, work)
    assert result.returncode == 126
    assert message in result.stderr


@pytest.mark.parametrize("variable", ["HERDR_SANDBOX_RO_BINDS", "HERDR_SANDBOX_SSH_DIR"])
def test_strict_profile_forbids_optional_host_mounts(strict_env, variable):
    env, work, _, _ = strict_env
    env[variable] = "/tmp/forbidden"
    result = run_box(env, work)
    assert result.returncode == 126
    assert "does not permit SSH or extra host mounts" in result.stderr


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("HERDR_SANDBOX_MEM_BYTES", "268435455"),
        ("HERDR_SANDBOX_MEM_BYTES", "17179869185"),
        ("HERDR_SANDBOX_MEM_BYTES", "1G"),
        ("HERDR_SANDBOX_MEM_BYTES", "999999999999999999999999999999"),
        ("HERDR_SANDBOX_TASKS", "15"),
        ("HERDR_SANDBOX_TASKS", "0128"),
        ("HERDR_SANDBOX_TASKS", "1025"),
        ("HERDR_SANDBOX_TIMEOUT_SECONDS", "29"),
        ("HERDR_SANDBOX_TIMEOUT_SECONDS", "86401"),
        ("HERDR_SANDBOX_CPU_PERCENT", "9"),
        ("HERDR_SANDBOX_CPU_PERCENT", "801"),
    ],
)
def test_strict_profile_bounds_numeric_limits(strict_env, variable, value):
    env, work, _, _ = strict_env
    env[variable] = value
    result = run_box(env, work)
    assert result.returncode == 126
    assert variable in result.stderr


def test_strict_profile_rejects_unsafe_broker_socket_parent(strict_env):
    env, work, _, sock_path = strict_env
    sock_path.parent.chmod(0o777)
    result = run_box(env, work)
    assert result.returncode == 126
    assert "egress socket directory must be yours with mode 0700" in result.stderr


def test_strict_profile_rejects_operator_config_overlap(strict_env):
    env, work, _, _ = strict_env
    operator_config = Path(env["HOME"]) / ".pi"
    operator_config.mkdir(mode=0o700)
    Path(env["HOME"]).chmod(0o700)
    env["HERDR_SANDBOX_HARNESS_DIR"] = env["HOME"]
    result = run_box(env, work)
    assert result.returncode == 126
    assert "must not contain the operator's .pi" in result.stderr


def test_strict_profile_rejects_unrelated_harness_content(strict_env):
    env, work, harness, _ = strict_env
    (harness / "operator-notes.txt").write_text("private")
    result = run_box(env, work)
    assert result.returncode == 126
    assert "may contain only claude, codex and pi directories" in result.stderr


def test_strict_profile_rejects_preexisting_host_sockets_in_workspace(strict_env):
    env, work, _, _ = strict_env
    work_socket = socket.socket(socket.AF_UNIX)
    work_socket.bind(str(work / "docker.sock"))
    try:
        result = run_box(env, work)
    finally:
        work_socket.close()
    assert result.returncode == 126
    assert "workspace must not contain host-linked or special files" in result.stderr


@pytest.mark.parametrize(("target", "message"), [
    ("work", "workspace must be yours with mode 0700"),
    ("area", "work root must be yours with mode 0700"),
])
def test_strict_profile_rejects_group_writable_work_paths(strict_env, target, message):
    env, work, _, _ = strict_env
    path = work if target == "work" else work.parent
    path.chmod(0o770)
    result = run_box(env, work)
    assert result.returncode == 126
    assert message in result.stderr


def test_strict_profile_rejects_workspace_hardlinks(strict_env, tmp_path):
    env, work, _, _ = strict_env
    outside = tmp_path / "outside-secret"
    outside.write_text("secret")
    os.link(outside, work / "linked-secret")
    result = run_box(env, work)
    assert result.returncode == 126
    assert "workspace must not contain host-linked or special files" in result.stderr


def test_strict_profile_refuses_when_resource_scope_is_unavailable(strict_env, tmp_path):
    env, work, _, _ = strict_env
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / "bwrap").write_text("#!/bin/sh\nexit 99\n")
    (tools / "bwrap").chmod(0o755)
    (tools / "systemd-run").write_text("#!/bin/sh\nexit 1\n")
    (tools / "systemd-run").chmod(0o755)
    env["PATH"] = f"{tools}:{env['PATH']}"
    result = run_box(env, work)
    assert result.returncode != 0
    assert "should-not-run" not in result.stdout


def test_strict_profile_passes_exact_limits_to_enforced_scope(strict_env, tmp_path):
    env, work, _, _ = strict_env
    tools = tmp_path / "tools"
    tools.mkdir()
    trace = tmp_path / "systemd.args"
    (tools / "bwrap").write_text("#!/bin/sh\nexit 99\n")
    (tools / "bwrap").chmod(0o755)
    (tools / "systemd-run").write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' CALL \"$@\" >> \"$STRICT_TRACE\"\n"
    )
    (tools / "systemd-run").chmod(0o755)
    env.update(PATH=f"{tools}:{env['PATH']}", STRICT_TRACE=str(trace))
    result = run_box(env, work)
    assert result.returncode == 0, result.stderr
    args = trace.read_text().splitlines()
    assert args.count("CALL") == 1
    assert "--run-strict" in args
    assert "TasksMax=128" in args
    assert "MemoryMax=1073741824" in args
    assert "CPUQuota=100%" in args
    assert any(item.endswith("/timeout") for item in args)
    assert "900" in args
    assert "--unshare-net" in args


def test_profile_is_opt_in_instead_of_changing_legacy_defaults():
    text = PACKAGED.read_text()
    assert 'HERDR_SANDBOX_PROFILE:-legacy' in text
    assert "running without a process/memory cap" in text
    assert 'case "/$rel/" in */../*|*/./*) exit 1' in text


def test_strict_wrapper_refuses_to_run_outside_an_enforced_cgroup(tmp_path):
    if Path("/sys/fs/cgroup/cgroup.controllers").exists():
        pytest.skip("host may already place the test process in an enforced cgroup v2 scope")
    marker = tmp_path / "ran"
    result = subprocess.run(
        ["bash", str(PACKAGED), "--run-strict", "128", "1073741824", "100", "900",
         "/usr/bin/true", "bash", "-c", f"touch {marker}"],
        capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert not marker.exists()

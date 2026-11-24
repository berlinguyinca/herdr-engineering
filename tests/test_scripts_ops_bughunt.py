"""Operator shell scripts and ansible roles, run for real against temp dirs and throwaway containers.

PATH-local helpers are real programs that only supply an exit code or record their argv; nothing here reaches a
real host, sudo, tailscale or the live compose stack.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
HAVE_DOCKER = shutil.which("docker") is not None
BASE_PATH = os.environ["PATH"]


def _helper(bindir: Path, name: str, body: str) -> Path:
    bindir.mkdir(parents=True, exist_ok=True)
    path = bindir / name
    path.write_text("#!/usr/bin/env bash\n" + body)
    path.chmod(0o755)
    return path


def _env(tmp_path: Path, bindir: Path | None = None, **extra: str) -> dict[str, str]:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("HERDR_CP_", "GATEWAY_", "HERDR_GATEWAY"))
    }
    env.update(HOME=str(home), PATH=f"{bindir}:{BASE_PATH}" if bindir else BASE_PATH)
    env.update(extra)
    return env


# --------------------------------------------------------------------------- 20 gateway monitor

MONITOR = ROOT / "scripts" / "monitor-fabric-gateway.sh"


def _gateway_container(health_cmd: str) -> str:
    name = f"herdr-bughunt-gw-{uuid.uuid4().hex[:8]}"
    args = shlex.split(
        subprocess.run(
            ["herdr-eng", "cleanup", "--print-run-args"], capture_output=True, text=True, check=True
        ).stdout
    )
    subprocess.run(
        [
            "docker",
            "run",
            "-d",
            *args,
            "--name",
            name,
            "--health-cmd",
            health_cmd,
            "--health-interval",
            "1s",
            "--health-retries",
            "1",
            "python:3.13-slim",
            "sleep",
            "120",
        ],
        check=True,
        capture_output=True,
    )
    return name


def _wait_health(name: str, want: str) -> None:
    for _ in range(40):
        got = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Health.Status}}", name],
            capture_output=True,
            text=True,
        ).stdout.strip()
        if got == want:
            return
        time.sleep(0.5)
    raise AssertionError(f"{name} never became {want}")


def _monitor(tmp_path: Path, name: str, *flags: str) -> subprocess.CompletedProcess[str]:
    gates = tmp_path / "state" / "build-gates"
    gates.mkdir(parents=True, exist_ok=True)
    (gates / "telemetry.jsonl").write_text(
        '{"event": "acquired"}\n'
    )  # exists, zero contention events
    env = _env(tmp_path, HERDR_STATE_DIR=str(tmp_path / "state"), HERDR_GATEWAY_CONTAINER=name)
    return subprocess.run(
        ["bash", str(MONITOR), *flags], env=env, capture_output=True, text=True, timeout=60
    )


@pytest.mark.skipif(not HAVE_DOCKER, reason="needs docker")
def test_monitor_reports_an_unhealthy_container_as_degraded_in_json(tmp_path):
    name = _gateway_container("false")
    try:
        _wait_health(name, "unhealthy")
        r = _monitor(tmp_path, name, "--json")
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
    row = json.loads(
        r.stdout.strip().splitlines()[-1]
    )  # must be one valid JSON line (no '0\n0' literal)
    assert row["degraded"] == 1 and "gateway-not-healthy" in row["reasons"]
    assert row["gate_contention_24h"] == 0
    assert r.returncode == 1


@pytest.mark.skipif(not HAVE_DOCKER, reason="needs docker")
def test_monitor_does_not_flag_a_healthy_container(tmp_path):
    name = _gateway_container("true")
    try:
        _wait_health(name, "healthy")
        r = _monitor(tmp_path, name, "--json")
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
    row = json.loads(r.stdout.strip().splitlines()[-1])
    assert "gateway-not-healthy" not in row["reasons"]


def test_monitor_counts_only_contention_events_inside_each_window(tmp_path):
    from datetime import UTC, datetime, timedelta

    def line(event: str, age: timedelta) -> str:
        ts = (datetime.now(UTC) - age).isoformat(timespec="milliseconds")
        return json.dumps({"event": event, "ts": ts, "host": "h"}, sort_keys=True)

    gates = tmp_path / "state" / "build-gates"
    gates.mkdir(parents=True)
    (gates / "telemetry.jsonl").write_text(
        "\n".join(
            [
                line("contention", timedelta(minutes=10)),  # in both windows
                line("contention", timedelta(hours=5)),  # 24h only
                line("contention", timedelta(hours=40)),  # neither
                line("acquired", timedelta(minutes=1)),  # not contention
                line("released", timedelta(minutes=2)),
            ]
        )
        + "\n"
    )
    env = _env(tmp_path, HERDR_STATE_DIR=str(tmp_path / "state"))
    env["HERDR_GATEWAY_CONTAINER"] = "herdr-no-such-gateway"
    r = subprocess.run(
        ["bash", str(MONITOR), "--json"], env=env, capture_output=True, text=True, timeout=60
    )
    row = json.loads(r.stdout.strip().splitlines()[-1])
    assert (row["gate_contention_1h"], row["gate_contention_24h"]) == (1, 2), row


# --------------------------------------------------------------------------- 21 fabric-stack


def _stack_tree(tmp_path: Path) -> Path:
    tree = tmp_path / "tree"
    (tree / "scripts").mkdir(parents=True)
    shutil.copy(ROOT / "scripts" / "fabric-stack.sh", tree / "scripts")
    shutil.copytree(ROOT / "deploy" / "fabric-stack", tree / "deploy" / "fabric-stack")
    (tree / "deploy" / "control-plane").mkdir(parents=True)
    shutil.copy(
        ROOT / "deploy" / "control-plane" / "compose.yaml", tree / "deploy" / "control-plane"
    )
    return tree


def _stack_run(tmp_path: Path, *args: str) -> tuple[subprocess.CompletedProcess[str], Path]:
    tree = _stack_tree(tmp_path)
    bindir = tmp_path / "bin"
    calls = tmp_path / "calls.log"
    _helper(bindir, "tailscale", "echo 100.64.0.9\n")
    _helper(
        bindir, "systemctl", f'echo "systemctl $*" >> {calls}\nexit 0\n'
    )  # "is-active" would say yes
    _helper(bindir, "pgrep", f'echo "pgrep $*" >> {calls}\nexit 0\n')
    _helper(bindir, "pkill", f'echo "pkill $*" >> {calls}\nexit 0\n')
    r = subprocess.run(
        ["bash", str(tree / "scripts" / "fabric-stack.sh"), *args],
        env=_env(tmp_path, bindir),
        capture_output=True,
        text=True,
        timeout=120,
    )
    return r, calls


@pytest.mark.skipif(not HAVE_DOCKER, reason="needs docker")
def test_up_replace_checks_compose_before_stopping_the_old_services(tmp_path):
    r, calls = _stack_run(tmp_path, "up-replace")
    assert r.returncode != 0
    assert "deploy.sh" in r.stderr
    assert (
        not calls.exists() or "pkill" not in calls.read_text() and "stop" not in calls.read_text()
    )


@pytest.mark.skipif(not HAVE_DOCKER, reason="needs docker")
def test_status_surfaces_a_compose_error_instead_of_stack_not_running(tmp_path):
    r, _ = _stack_run(tmp_path, "status")
    assert r.returncode != 0
    assert "stack not running" not in r.stdout


def _stack_with_docker(tmp_path: Path, docker_body: str, *args: str):
    """Run fabric-stack.sh with a recording `docker` helper in place of the real one."""
    tree = _stack_tree(tmp_path)
    bindir = tmp_path / "bin"
    calls = tmp_path / "calls.log"
    _helper(bindir, "docker", f'echo "docker $*" >> {calls}\n{docker_body}\n')
    _helper(bindir, "tailscale", "echo 100.64.0.9\n")
    _helper(bindir, "systemctl", f'echo "systemctl $*" >> {calls}\nexit 0\n')
    _helper(bindir, "pgrep", f'echo "pgrep $*" >> {calls}\nexit 0\n')
    _helper(bindir, "pkill", f'echo "pkill $*" >> {calls}\nexit 0\n')
    _helper(bindir, "curl", f'echo "curl $*" >> {calls}\nexit 0\n')
    _helper(bindir, "dig", f'echo "dig $*" >> {calls}\nexit 0\n')
    r = subprocess.run(
        ["bash", str(tree / "scripts" / "fabric-stack.sh"), *args],
        env=_env(tmp_path, bindir),
        capture_output=True,
        text=True,
        timeout=120,
    )
    return r, calls.read_text() if calls.exists() else ""


def test_up_replace_builds_the_image_before_stopping_the_old_services(tmp_path):
    r, calls = _stack_with_docker(tmp_path, "exit 0", "up-replace")
    assert r.returncode == 0, r.stderr
    lines = calls.splitlines()
    build = next(i for i, c in enumerate(lines) if c.startswith("docker") and " build " in c + " ")
    stop = next(i for i, c in enumerate(lines) if c.startswith(("pkill", "systemctl --user stop")))
    assert build < stop, calls


def test_status_goes_on_to_healthz_and_dns_when_compose_cannot_render(tmp_path):
    r, calls = _stack_with_docker(
        tmp_path, 'case "$*" in *" ps"*) echo "compose render error" >&2; exit 1;; esac', "status"
    )
    assert r.returncode != 0
    assert "compose render error" in r.stderr
    assert "curl" in calls and "dig" in calls, calls  # the other checks still ran


def test_up_never_removes_orphans_and_names_its_services():
    text = (ROOT / "scripts" / "fabric-stack.sh").read_text()
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    assert "--remove-orphans" not in code
    assert re.search(r"up -d[^\n]*coredns fabric-gateway herdr-web", text)


# --------------------------------------------------------------------------- 23 install.sh


def test_install_passes_the_tailscale_key_by_file_not_argv():
    text = (ROOT / "scripts" / "install.sh").read_text()
    assert '--authkey="$TS_KEY"' not in text
    assert re.search(r'--auth-key="?file:', text)


# --------------------------------------------------------------------------- 24 deploy-fleet-agents

AGENTS = ROOT / "deploy" / "deploy-fleet-agents.sh"


def test_a_failing_restart_restores_the_symlink_and_the_roll_goes_on(tmp_path):
    bindir = tmp_path / "bin"
    log = tmp_path / "ssh.log"
    # ssh/scp run the command here as bash under a temp HOME (a "local host" reached the way ssh would).
    _helper(
        bindir,
        "ssh",
        'while [ "${1#-}" != "$1" ]; do [ "$1" = -o ] && shift; shift; done\n'
        f'host="$1"; shift; echo "$host: $*" >> {log}\nexec bash -c "$*"\n',
    )
    _helper(
        bindir,
        "scp",
        'while [ "${1#-}" != "$1" ]; do [ "$1" = -o ] && shift; shift; done\n'
        'src="$1"; dest="${2#*:}"; exec cp "$src" "$dest"\n',
    )
    # restart of the plugin unit fails on host alpha only (HOME differs per host: HOSTMARK file).
    home = tmp_path / "home"
    cfg = home / ".config" / "herdr-engineering"
    cfg.mkdir(parents=True)
    (cfg / "host.json").write_text("{}")
    (home / ".fail-restart").write_text("x")
    system_python = shutil.which("python3", path="/usr/bin:/bin")
    if system_python is None:
        pytest.skip("needs a system python3 (the dev venv may lack pip)")
    (bindir / "python3").symlink_to(system_python)
    # Both targets share one HOME, so the failure is one-shot: the first restart consumes the marker (alpha fails).
    _helper(
        bindir,
        "systemctl",
        'if [ "$2" = restart ] && [ -e "$HOME/.fail-restart" ]; then rm -f "$HOME/.fail-restart"; exit 1; fi\nexit 0\n',
    )
    hosts = tmp_path / "hosts.txt"
    hosts.write_text("alpha|alpha.test|systemd\nbeta|beta.test|systemd\n")
    cur = home / ".local/share/herdr-cp-feed-plugin-current"
    cur.parent.mkdir(parents=True)
    prev = home / ".local/share/herdr-cp-feed-plugin-releases/previous"
    prev.mkdir(parents=True)
    cur.symlink_to(prev)
    # A pinned PATH and no agent config roots: the roll must not reach the developer's real herdr/claude/codex/pi.
    env = _env(tmp_path, bindir, HERDR_FLEET_HOSTS=str(hosts))
    env["PATH"] = f"{bindir}:/usr/bin:/bin"
    for name in ("CODEX_HOME", "CLAUDE_CONFIG_DIR", "PI_CODING_AGENT_DIR"):
        env.pop(name, None)
    r = subprocess.run(
        ["bash", str(AGENTS), "--no-fetch", "--ref", "HEAD"],
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        cwd=ROOT,
    )
    assert r.returncode != 0  # alpha failed
    assert "plugin: NOT active, restoring" in r.stderr, (
        r.stderr
    )  # ...because its restart failed, nothing else
    out = log.read_text() if log.exists() else r.stderr + r.stdout
    assert "beta.test" in out and "beta.test: mkdir" in out, (
        r.stderr
    )  # the roll reached the next host
    assert re.search(r"alpha\.test: ln -sfn \S*/previous ", out), (
        out
    )  # alpha was put back where it was


def test_a_failed_ssh_for_the_feed_dir_copies_nothing_to_the_host_root(tmp_path):
    bindir = tmp_path / "bin"
    scp_log = tmp_path / "scp.log"
    _helper(
        bindir,
        "ssh",
        'while [ "${1#-}" != "$1" ]; do [ "$1" = -o ] && shift; shift; done\n'
        'host="$1"; shift\n'
        'case "$*" in "echo "*herdr-service-metadata/releases/*) exit 255;; esac\n'
        'exec bash -c "$*"\n',
    )
    _helper(
        bindir,
        "scp",
        'while [ "${1#-}" != "$1" ]; do [ "$1" = -o ] && shift; shift; done\n'
        f'echo "$2" >> {scp_log}\n'
        'src="$1"; dest="${2#*:}"; [ "$dest" != / ] || exit 1; exec cp "$src" "$dest"\n',
    )
    _helper(bindir, "systemctl", "exit 0\n")
    (bindir / "python3").symlink_to("/usr/bin/python3")
    home = tmp_path / "home"
    cfg = home / ".config" / "herdr-engineering"
    cfg.mkdir(parents=True)
    (cfg / "host.json").write_text("{}")
    (home / ".local/share/herdr-service-metadata/releases").mkdir(parents=True)
    hosts = tmp_path / "hosts.txt"
    hosts.write_text("alpha|alpha.test|systemd\n")
    env = _env(tmp_path, bindir, HERDR_FLEET_HOSTS=str(hosts))
    env["PATH"] = f"{bindir}:/usr/bin:/bin"
    for name in ("CODEX_HOME", "CLAUDE_CONFIG_DIR", "PI_CODING_AGENT_DIR"):
        env.pop(name, None)
    r = subprocess.run(
        ["bash", str(AGENTS), "--no-fetch", "--ref", "HEAD"],
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        cwd=ROOT,
    )
    scp = scp_log.read_text() if scp_log.exists() else ""
    assert "alpha.test:/" not in scp.splitlines(), scp  # nothing is sent to the host root
    assert "ownership feed: FAILED" in r.stderr, r.stderr
    assert r.returncode != 0


# --------------------------------------------------------------------------- 45 collab-sync

SYNC = ROOT / "deploy" / "control-plane" / "collab-sync.sh"


def test_a_failed_once_leaves_no_temp_dir_in_the_sync_dir(tmp_path):
    bindir = tmp_path / "bin"
    _helper(bindir, "curl", "exit 22\n")  # the AWS endpoint is down
    cfg = tmp_path / "collab.json"
    cfg.write_text(
        json.dumps(
            {
                "sections": {
                    "control_plane": {
                        "values": {
                            "collab": {
                                "cognito_pool": "us-west-2_TESTPOOL",
                                "cognito_group": "team.test",
                            }
                        }
                    }
                }
            }
        )
    )
    sync = tmp_path / "sync"
    sync.mkdir()
    (sync / ".tmp.STALE1").mkdir()  # left by an earlier run of the old script
    env = _env(tmp_path, bindir)
    for _ in range(2):
        r = subprocess.run(
            [
                "bash",
                str(SYNC),
                "once",
                "--config",
                str(cfg),
                "--dir",
                str(sync),
                "--data-dir",
                str(tmp_path / "data"),
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert r.returncode != 0
    assert list(sync.glob(".tmp.*")) == []


# --------------------------------------------------------------------------- 46 ansible runtime tasks


@pytest.mark.parametrize("role", ["herdr_linux", "herdr_macos"])
@pytest.mark.parametrize("task_name,binary", [("Verify Herdr", "herdr"), ("Verify Pi", "pi")])
def test_a_missing_runtime_is_reported_as_drift_and_never_fails_the_host(
    tmp_path, role, task_name, binary
):
    tasks = yaml.safe_load((ROOT / "ansible" / "roles" / role / "tasks" / "main.yml").read_text())
    task = next(t for t in tasks if t["name"].startswith(task_name))
    assert task["failed_when"] is False and task["changed_when"] is False
    script = task["ansible.builtin.shell"].replace("{{ herdr_pinned_version }}", "0.9.1")
    empty = tmp_path / "empty"
    empty.mkdir()
    for tool in ("bash", "grep"):  # a PATH with the shell tools but without herdr/pi
        (empty / tool).symlink_to(shutil.which(tool))
    r = subprocess.run(
        [str(empty / "bash"), "-c", script],
        env={"PATH": str(empty)},
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0 and "DRIFT" in r.stderr, (r.returncode, r.stderr)


@pytest.mark.parametrize("role", ["herdr_linux", "herdr_macos"])
def test_another_herdr_version_is_drift_and_the_pinned_one_is_quiet(tmp_path, role):
    tasks = yaml.safe_load((ROOT / "ansible" / "roles" / role / "tasks" / "main.yml").read_text())
    task = next(t for t in tasks if t["name"].startswith("Verify Herdr"))
    script = task["ansible.builtin.shell"].replace("{{ herdr_pinned_version }}", "0.9.1")
    bindir = tmp_path / "bin"
    for tool in ("bash", "grep"):
        bindir.mkdir(exist_ok=True)
        (bindir / tool).symlink_to(shutil.which(tool))
    out = {}
    for version in ("0.1.0", "0.9.1"):
        _helper(bindir, "herdr", f'echo "herdr {version}"\n')
        r = subprocess.run(
            [str(bindir / "bash"), "-c", script],
            env={"PATH": str(bindir)},
            capture_output=True,
            text=True,
        )
        out[version] = (r.returncode, "DRIFT" in r.stderr)
    assert out == {"0.1.0": (0, True), "0.9.1": (0, False)}


# --------------------------------------------------------------------------- 48 fleet-join


def test_fleet_join_keeps_user_and_counts_zero_leases_once(tmp_path):
    tree = tmp_path / "tree"
    (tree / "scripts").mkdir(parents=True)
    for f in ("fleet-join.sh", "fleet_inventory.py"):
        shutil.copy(ROOT / "scripts" / f, tree / "scripts")
    (tree / "ansible" / "playbooks").mkdir(parents=True)
    (tree / "ansible" / "inventory").mkdir(parents=True)
    (tree / "herdr_engineering").symlink_to(ROOT / "herdr_engineering")
    (tree / "ansible" / "inventory" / "hosts.yml").write_text(
        "all:\n  children:\n    linux:\n      hosts: {}\n    macos:\n      hosts: {}\n"
    )
    bindir = tmp_path / "bin"
    seen = tmp_path / "ansible-user.txt"
    _helper(bindir, "ansible-playbook", f'echo "USER=[$USER]" >> {seen}\n')
    _helper(bindir, "herdr-eng", 'case "$1" in --version) echo 0;; *) echo "[]";; esac\n')
    env = _env(tmp_path, bindir, USER="controller")
    r = subprocess.run(
        [
            "bash",
            str(tree / "scripts" / "fleet-join.sh"),
            "newbox",
            "--ansible-host",
            "newbox.test",
            "--user",
            "remote",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert r.returncode == 0, r.stderr
    assert "USER=[controller]" in seen.read_text() and "USER=[remote]" not in seen.read_text()
    assert "integer expression expected" not in r.stderr
    assert re.search(r"leases visible to the gateway: 0\n  \(0 is fine", r.stdout)


# --------------------------------------------------------------------------- 62 collab-worker

WORKER = ROOT / "deploy" / "control-plane" / "collab-worker.sh"


def _serve(replies: list[dict], seen: list[str]):
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["content-length"])))
            name = body["params"]["name"]
            seen.append(name)
            reply = (
                replies.pop(0)
                if name == "tasks_wait" and replies
                else {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {"content": [{"type": "text", "text": json.dumps({"tasks": []})}]},
                }
            )
            data = json.dumps(reply).encode()
            self.send_response(200)
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _tasks_reply(tasks: list[dict]) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"content": [{"type": "text", "text": json.dumps({"tasks": tasks})}]},
    }


def _run_worker(tmp_path: Path, srv, secs: float) -> subprocess.CompletedProcess[str]:
    script = tmp_path / "worker.sh"
    script.write_text(
        WORKER.read_text().replace("__HERDR_BASE__", f"http://127.0.0.1:{srv.server_port}")
    )
    tmpdir = tmp_path / "tmpdir"
    tmpdir.mkdir()
    agent = tmp_path / "agent.json"
    agent.write_text(json.dumps({"credential": "c", "device_id": "d", "name": "tester"}))
    proc = subprocess.Popen(
        ["bash", str(script), str(agent)],
        env=_env(tmp_path, HERDR_TASK_TIMEOUT="20", TMPDIR=str(tmpdir)),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        out, err = proc.communicate(timeout=secs)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
        proc.returncode = -9
    return subprocess.CompletedProcess(proc.args, proc.returncode, out, err)


def test_worker_survives_a_json_rpc_error_reply(tmp_path):
    seen: list[str] = []
    srv = _serve(
        [
            {"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": "boom"}},
            _tasks_reply([{"task_id": "t1", "text": "hello", "attempt": 1}]),
        ],
        seen,
    )
    try:
        r = _run_worker(tmp_path, srv, 12)
    finally:
        srv.shutdown()
    assert r.returncode == -9, f"worker exited early: {r.stderr}"  # still looping, not dead
    assert "t1 done" in r.stdout


def test_worker_refuses_a_task_id_that_is_not_a_plain_token(tmp_path):
    seen: list[str] = []
    srv = _serve(
        [
            _tasks_reply([{"task_id": "../../escaped", "text": "x", "attempt": 1}]),
            _tasks_reply([{"task_id": "ok-1", "text": "x", "attempt": 1}]),
        ],
        seen,
    )
    try:
        r = _run_worker(tmp_path, srv, 12)
    finally:
        srv.shutdown()
    assert not list(
        (tmp_path / "tmpdir").glob("escaped*")
    )  # "../../escaped" resolves to TMPDIR/escaped
    assert "ok-1 done" in r.stdout and "escaped" not in r.stdout


def test_worker_runs_good_tasks_beside_a_malformed_one(tmp_path):
    seen: list[str] = []
    srv = _serve(
        [
            _tasks_reply(
                [
                    {"task_id": "bad-1", "text": None, "attempt": 1},
                    {"task_id": "good-1", "text": "x", "attempt": 1},
                    {"task_id": "../evil", "text": "x", "attempt": 1},
                ]
            ),
        ],
        seen,
    )
    try:
        r = _run_worker(tmp_path, srv, 12)
    finally:
        srv.shutdown()
    assert "good-1 done" in r.stdout, (r.stdout, r.stderr)
    assert "bad-1" in r.stderr  # the skipped task is named, not dropped silently
    assert "../evil" in r.stderr or "evil" in r.stderr

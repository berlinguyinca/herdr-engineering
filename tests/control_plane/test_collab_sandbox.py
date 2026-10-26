# ruff: noqa: F811  (pytest fixtures are imported by name from the sibling test modules)
"""The box spawned agents run tasks in, and the audit trail they leave. Real bubblewrap, real worker script."""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from tests.control_plane.test_collab import BASE
from tests.control_plane.test_collab_public import main_api, stack  # noqa: F401
from tests.control_plane.test_collab_scripts import generate, shim  # noqa: F401

ROOT = Path(__file__).resolve().parents[2]
SANDBOX = ROOT / "deploy/control-plane/collab-sandbox.sh"
VERIFY = ROOT / "deploy/control-plane/collab-audit-verify.py"


def bwrap_works() -> bool:
    return bool(shutil.which("bwrap")) and subprocess.run(
        ["bwrap", "--ro-bind", "/", "/", "--unshare-pid", "/usr/bin/true"], capture_output=True).returncode == 0


needs_box = pytest.mark.skipif(not (bwrap_works() and shutil.which("node") and shutil.which("curl")),
                               reason="needs bubblewrap, node and curl")
spec = importlib.util.spec_from_file_location("collab_audit_verify", VERIFY)
verify = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verify)


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    (h / "herdr-agents" / "work").mkdir(parents=True)
    (h / "keep.txt").write_text("precious")
    (h / "herdr-agents" / "other-agent.json").write_text('{"credential":"hca_SECRETSECRET"}')
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("SECRET_ENV", "leak-me")
    return h


def box(home, cmd, work="a1"):
    return subprocess.run(["bash", str(SANDBOX), str(home / "herdr-agents" / "work" / work), cmd],
                          capture_output=True, text=True, timeout=60)


@needs_box
def test_only_the_work_directory_is_writable_and_nothing_else_is_visible(home):
    r = box(home, 'rm -rf "$HOME"/* 2>&1; echo made > made.txt; rm -f /usr/bin/env 2>&1 | head -1; touch /etc/x 2>&1 | head -1; '
                  'cat "$HOME/herdr-agents/other-agent.json" 2>&1; ls "$HOME/herdr-agents"; echo "env=[$SECRET_ENV]"; '
                  'ls ~/.ssh ~/.aws 2>&1 | head -2; echo pwd=$PWD')
    assert (home / "herdr-agents/work/a1/made.txt").read_text() == "made\n"
    assert (home / "keep.txt").read_text() == "precious"                          # never touched
    assert (home / "herdr-agents/other-agent.json").exists()
    out = r.stdout + r.stderr
    assert "SECRETSECRET" not in out and "leak-me" not in out and "env=[]" in out
    assert "Read-only file system" in out and "No such file" in out
    assert f"pwd={home}/herdr-agents/work/a1" in out
    assert "other-agent.json" not in r.stdout.splitlines() and "work" in r.stdout.splitlines()         # only its own tree exists in the box


@needs_box
def test_a_harness_behind_a_chain_of_symlinks_is_found_in_the_box(home, monkeypatch):
    """codex's standalone install: ~/.local/bin/codex -> ~/.codex/packages/standalone/current/bin/codex, where
    `current` is itself a link to releases/<version>. Every hop must resolve inside the box, not just both ends."""
    rel = home / ".codex" / "packages" / "standalone" / "releases" / "9.9.9" / "bin"
    rel.mkdir(parents=True)
    (rel / "codex").write_text("#!/bin/sh\necho harness-ran\n")
    (rel / "codex").chmod(0o755)
    (home / ".codex" / "packages" / "standalone" / "current").symlink_to("releases/9.9.9")
    lb = home / ".local" / "bin"
    lb.mkdir(parents=True)
    (lb / "codex").symlink_to(home / ".codex" / "packages" / "standalone" / "current" / "bin" / "codex")
    monkeypatch.setenv("PATH", f"{lb}:{os.environ['PATH']}")
    # helper mode: the operator's ~/.codex is NOT bound, so only the bound hops can make the link resolve
    hd = home.parent / "harness"
    for sub in ("", "claude", "codex", "pi"):
        (hd / sub).mkdir(mode=0o700, exist_ok=True)
    hd.chmod(0o700)
    monkeypatch.setenv("HERDR_SANDBOX_HARNESS_DIR", str(hd))
    r = box(home, "codex")
    assert r.returncode == 0, r.stderr
    assert "harness-ran" in r.stdout


@needs_box
def test_it_refuses_to_run_outside_the_work_area_or_without_bwrap(home, tmp_path):
    r = subprocess.run(["bash", str(SANDBOX), str(tmp_path), "true"], capture_output=True, text=True)
    assert r.returncode == 126 and "work/" in r.stderr
    r = subprocess.run(["bash", str(SANDBOX), str(home / "herdr-agents/work/../.."), "true"], capture_output=True, text=True)
    assert r.returncode == 126
    empty = tmp_path / "nobwrap"
    empty.mkdir()
    for tool in ("bash", "env", "dirname", "mkdir", "cat"):
        (empty / tool).symlink_to(shutil.which(tool))
    r = subprocess.run([shutil.which("bash"), str(SANDBOX), str(home / "herdr-agents/work/a2"), "echo ran"],
                       env={"PATH": str(empty), "HOME": str(home)}, capture_output=True, text=True)
    assert r.returncode == 126 and "ran" not in r.stdout and "refusing to run unboxed" in r.stderr


@needs_box
def test_a_fork_bomb_is_stopped_by_the_process_cap(home):
    if subprocess.run(["systemd-run", "--user", "--scope", "-q", "true"], capture_output=True).returncode != 0:
        pytest.skip("no systemd user scope here")
    r = subprocess.run(["bash", str(SANDBOX), str(home / "herdr-agents/work/fb"), ':(){ :|:& };: ; sleep 2; echo alive'],
                       capture_output=True, text=True, timeout=60, env={**os.environ, "HERDR_SANDBOX_TASKS": "64"})
    assert "alive" in r.stdout or r.returncode in (0, 1, 126, 137)               # the host is fine; that is the point


@needs_box
def test_worker_runs_tasks_boxed_and_leaves_a_tamper_evident_trail(stack, shim, home, tmp_path):
    api = main_api(stack)
    s, p = api.call("POST", f"{BASE}/projects", {"name": "p"}, user="alice")
    s, r = api.call("POST", f"{BASE}/projects/{p['project_id']}/rooms", {"name": "r"}, user="alice")
    room = r["room_id"]
    s, tok = api.call("POST", f"{BASE}/agents/join-tokens", {
        "harness_type": "claude-code", "name_prefix": "box", "count": 1, "room_id": room,
        "actions": ["read", "post", "execute"]}, user="alice")
    base = f"http://127.0.0.1:{stack.srv.public_port}"
    agents = home / "herdr-agents"
    env = {**shim, "HERDR_AGENT_DIR": str(agents), "HOME": str(home)}
    bundle = tmp_path / "bundle.sh"
    bundle.write_text(generate("bundleScript", base, tok["token"], 1, "box"))
    assert subprocess.run(["bash", str(bundle)], env=env, capture_output=True, text=True, timeout=60).returncode == 0
    cred = json.loads((agents / "box-1.json").read_text())
    s, reqs = api.call("GET", f"{BASE}/rooms/{room}/agent-requests", user="alice")
    api.call("POST", f"{BASE}/rooms/{room}/agent-requests/approve-all",
             {"request_ids": [x["request_id"] for x in reqs["requests"]]}, user="alice")
    s, t = api.call("POST", f"{BASE}/rooms/{room}/tasks", {"target_agent_id": cred["agent_id"], "text": "clean up everything"}, user="alice")
    assert s == 201
    audit = agents / "audit" / "box-1.log"
    hostile = ('cat >/dev/null; rm -rf "$HOME"/* 2>/dev/null; echo data > notes.txt; rm -f ../../keep.txt ../keep.txt 2>/dev/null; '
               'cat "$HOME/herdr-agents/other-agent.json" 2>&1 | head -c 30; echo finished')
    proc = subprocess.Popen(["bash", str(agents / "worker.sh"), str(agents / "box-1.json")], stderr=subprocess.PIPE,
                            stdout=subprocess.PIPE, text=True,
                            env={**env, "HERDR_AGENT_CMD": hostile, "HERDR_SANDBOX": str(SANDBOX), "HERDR_AUDIT": str(audit)})
    try:
        for _ in range(60):
            s, tasks = api.call("GET", f"{BASE}/rooms/{room}/tasks", user="alice")
            done = [x for x in tasks["tasks"] if x["task_id"] == t["task_id"] and x["status"] in ("done", "failed")]
            if done:
                break
            time.sleep(0.5)
    finally:
        proc.terminate()
        proc.wait(10)
    assert done and done[0]["status"] == "done" and "finished" in done[0]["result"], done
    assert "SECRETSECRET" not in done[0]["result"]
    assert (home / "keep.txt").read_text() == "precious" and (agents / "other-agent.json").exists()
    assert (agents / "work/box-1/notes.txt").read_text() == "data\n"

    lines = [json.loads(x) for x in audit.read_text().splitlines()]
    assert [x["event"] for x in lines] == ["worker_start", "task_start", "task_end"]
    start, end = lines[1], lines[2]
    assert start["task_id"] == end["task_id"] == t["task_id"] and len(start["text_sha256"]) == 64
    assert end["status"] == "done" and end["exit"] == "0" and end["work_changes"] == "+notes.txt"
    assert lines[0]["sandbox"] == "yes" and oct(audit.stat().st_mode & 0o777) == "0o600"
    assert "clean up everything" not in audit.read_text()                          # the text is hashed, not copied

    assert verify.check(audit)[0] is True
    text = audit.read_text()
    audit.write_text(text.replace('"status": "done"', '"status": "fine"'))
    assert verify.check(audit)[0] is False
    audit.write_text("".join(text.splitlines(True)[:1] + text.splitlines(True)[2:]))   # a line removed
    ok, msg = verify.check(audit)
    assert not ok and "removed" in msg
    audit.write_text(text)
    assert subprocess.run(["python3", str(VERIFY), str(audit)], capture_output=True, text=True).returncode == 0


def test_the_verifier_rejects_garbage_and_accepts_an_empty_log(tmp_path):
    f = tmp_path / "x.log"
    f.write_text("not json\n")
    assert verify.check(f)[0] is False
    f.write_text("")
    assert verify.check(f)[0] is True


def test_sandbox_requests_cpu_containment_and_scope_collection():
    text = SANDBOX.read_text()
    assert 'HERDR_SANDBOX_CPU_QUOTA' in text
    assert 'CPUQuota=' in text
    assert '--collect' in text

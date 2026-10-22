# ruff: noqa: F811  (pytest fixtures are imported by name from the sibling test modules)
"""The copy-paste bash fleet scripts, run for real against a live listener."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from tests.control_plane.test_collab import BASE
from tests.control_plane.test_collab_public import H, main_api, stack  # noqa: F401

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(not (shutil.which("node") and shutil.which("curl") and shutil.which("bash")),
                                reason="needs node, curl and bash")


def generate(kind: str, base: str, token: str = "hcj_x", count: int = 2, prefix: str = "fleet") -> str:
    js = ("import('./packages/collab-scripts.js').then(m=>process.stdout.write(m[process.argv[1]]"
          "({base:process.argv[2],token:process.argv[3],count:+process.argv[4],prefix:process.argv[5]})))")
    return subprocess.run(["node", "-e", js, kind, base, token, str(count), prefix], cwd=ROOT,
                          capture_output=True, text=True, check=True, timeout=30).stdout


@pytest.fixture
def shim(tmp_path):
    """The test listener demands the TLS-proxy header the real edge adds; give curl a wrapper that sends it."""
    d = tmp_path / "bin"
    d.mkdir()
    real = shutil.which("curl")
    (d / "curl").write_text(f"#!/bin/sh\nexec {real} -H 'X-Forwarded-Proto: https' \"$@\"\n")
    (d / "curl").chmod(0o755)
    return {**os.environ, "PATH": f"{d}:{os.environ['PATH']}"}


@pytest.mark.parametrize("kind", ["bundleScript", "registerScript", "workerScript", "delegateScript", "agentsScript"])
def test_every_script_parses_and_hides_nothing_but_the_token(kind, tmp_path):
    text = generate(kind, "https://localdev.example.org", token="hcj_SECRET")
    f = tmp_path / "s.sh"
    f.write_text(text)
    subprocess.run(["bash", "-n", str(f)], check=True)
    assert ("hcj_SECRET" in text) == (kind in ("bundleScript", "registerScript"))


def test_quoting_survives_hostile_values(tmp_path):
    text = generate("registerScript", "https://x.example/'; touch /tmp/pwned; '", token="t'k")
    f = tmp_path / "s.sh"
    f.write_text(text)
    subprocess.run(["bash", "-n", str(f)], check=True)
    assert "'\\''" in text and not Path("/tmp/pwned").exists()


def test_fleet_end_to_end_register_approve_work_and_delegate(stack, shim, tmp_path):
    api = main_api(stack)
    s, p = api.call("POST", f"{BASE}/projects", {"name": "p"}, user="alice")
    s, r = api.call("POST", f"{BASE}/projects/{p['project_id']}/rooms", {"name": "r"}, user="alice")
    room = r["room_id"]
    s, tok = api.call("POST", f"{BASE}/agents/join-tokens", {
        "harness_type": "claude-code", "name_prefix": "fleet", "count": 2, "room_id": room,
        "actions": ["read", "post", "execute", "instruct"]}, user="alice")
    assert s == 201, tok
    base = f"http://127.0.0.1:{stack.srv.public_port}"
    env = {**shim, "HERDR_AGENT_DIR": str(tmp_path / "agents")}
    bundle = tmp_path / "bundle.sh"
    bundle.write_text(generate("bundleScript", base, tok["token"], 2, "fleet"))
    out = subprocess.run(["bash", str(bundle)], env=env, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    files = sorted((tmp_path / "agents").glob("fleet-*.json"))
    assert [f.name for f in files] == ["fleet-1.json", "fleet-2.json"]
    assert oct(files[0].stat().st_mode & 0o777) == "0o600"
    ids = {f.stem: json.loads(f.read_text())["agent_id"] for f in files}

    # nothing works until the owner approves
    first = subprocess.run(["bash", str(tmp_path / "agents" / "agents.sh"), str(files[0])], env=env,
                           capture_output=True, text=True, timeout=30)
    assert json.loads(json.loads(first.stdout)["result"]["content"][0]["text"].split("\n\n", 1)[1])["rooms"] == []
    s, ap = api.call("POST", f"{BASE}/rooms/{room}/agent-requests/approve-all", {}, user="alice")
    assert s == 200 and ap["approved"] == 2, ap

    # a person gives fleet-1 a task; the worker script does it and reports
    s, t = api.call("POST", f"{BASE}/rooms/{room}/tasks", {"target_agent_id": ids["fleet-1"], "text": "say hi"}, user="alice")
    assert s == 201
    worker_env = {**env, "HERDR_AGENT_CMD": "echo got:$(cat)"}
    proc = subprocess.Popen(["bash", str(tmp_path / "agents" / "worker.sh"), str(files[0])], env=worker_env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        for _ in range(40):
            s, tasks = api.call("GET", f"{BASE}/rooms/{room}/tasks", user="alice")
            done = [x for x in tasks["tasks"] if x["task_id"] == t["task_id"] and x["status"] == "done"]
            if done:
                break
            time.sleep(0.5)
        assert done and done[0]["result"].strip() == "got:say hi"
    finally:
        proc.terminate()
        proc.wait(5)

    # fleet-1 delegates part of that task to fleet-2 with delegate.sh
    s, t2 = api.call("POST", f"{BASE}/rooms/{room}/tasks", {"target_agent_id": ids["fleet-1"], "text": "lead"}, user="alice")
    cred = json.loads(files[0].read_text())
    stack.svc.update_task(stack.svc.authenticate(f"Agent {cred['credential']}", cred["device_id"]), t2["task_id"],
                          {"status": "accepted"})
    d = subprocess.run(["bash", str(tmp_path / "agents" / "delegate.sh"), str(files[0]), room, ids["fleet-2"],
                        t2["task_id"], "sub-task; $(touch /tmp/pwned2)"], env=env, capture_output=True, text=True, timeout=30)
    assert d.returncode == 0, d.stderr
    s, tasks = api.call("GET", f"{BASE}/rooms/{room}/tasks", user="alice")
    sub = [x for x in tasks["tasks"] if x["target_agent_id"] == ids["fleet-2"]]
    assert sub and sub[0]["text"] == "sub-task; $(touch /tmp/pwned2)" and sub[0]["requested_by"]["type"] == "agent"
    assert not Path("/tmp/pwned2").exists()

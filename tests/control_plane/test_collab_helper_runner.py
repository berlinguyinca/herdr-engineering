# ruff: noqa: F811  (pytest fixtures are imported by name from the sibling test modules)
"""Helpers on the host: the sandbox's extra read-only views, the room secrets CLI, and the runner's
helper lifecycle (claim once, place, run boxed behind the egress proxy, finish, tear down)."""
from __future__ import annotations

import base64
import json
import os
import shutil
import stat
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from herdr_engineering.control_plane import collab
from tests.control_plane.test_collab_spawn import WORKER, calls, rig, runner  # noqa: F401
from tests.control_plane.test_collab_tasks import svc  # noqa: F401

ROOT = Path(__file__).resolve().parents[2]
SANDBOX = ROOT / "deploy/control-plane/collab-sandbox.sh"
SECRETS = ROOT / "deploy/control-plane/collab-secrets.py"


def bwrap_works() -> bool:
    return bool(shutil.which("bwrap")) and subprocess.run(
        ["bwrap", "--ro-bind", "/", "/", "--unshare-pid", "/usr/bin/true"], capture_output=True).returncode == 0


needs_box = pytest.mark.skipif(not bwrap_works(), reason="needs bubblewrap")


# ------------------------------------------------------------------ sandbox: read-only binds and ~/.ssh
@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    (h / "herdr-agents" / "work").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(h))
    return h


def box(home, cmd, work="helper-1", **env):
    e = {k: v for k, v in os.environ.items() if not k.startswith("HERDR_")} | env
    return subprocess.run(["bash", str(SANDBOX), str(home / "herdr-agents" / "work" / work), cmd],
                          capture_output=True, text=True, timeout=60, env=e)


@needs_box
def test_a_read_only_bind_is_visible_at_its_path_and_cannot_be_written(home):
    parent = home / "herdr-agents/work/alice-1"
    parent.mkdir()
    (parent / "code.py").write_text("print(1)\n")
    r = box(home, f'cat {parent}/code.py; touch {parent}/x 2>&1 | head -1; echo done', HERDR_SANDBOX_RO_BINDS=str(parent))
    assert "print(1)" in r.stdout and "done" in r.stdout and "Read-only" in r.stdout
    assert not (parent / "x").exists()
    plain = box(home, f'ls {parent} 2>&1; echo done')            # without the variable the parent is invisible
    assert "code.py" not in plain.stdout


@needs_box
@pytest.mark.parametrize("bad", ["outside", "dotdot", "symlink", "relative", "missing"])
def test_a_read_only_bind_outside_the_work_area_fails_closed(home, tmp_path, bad):
    (tmp_path / "secret").mkdir()
    (home / "herdr-agents/work/alice-1").mkdir()
    (home / "herdr-agents/work/link").symlink_to(tmp_path / "secret")
    path = {"outside": str(tmp_path / "secret"), "dotdot": str(home / "herdr-agents/work/alice-1/../../..") ,
            "symlink": str(home / "herdr-agents/work/link"), "relative": "herdr-agents/work/alice-1",
            "missing": str(home / "herdr-agents/work/nobody")}[bad]
    r = box(home, "echo ran", HERDR_SANDBOX_RO_BINDS=path)
    assert r.returncode == 126 and "ran" not in r.stdout


@needs_box
def test_the_ssh_dir_shows_up_read_only_as_dot_ssh(home, tmp_path):
    ssh = tmp_path / "priv" / "ssh"
    ssh.mkdir(parents=True, mode=0o700)
    ssh.chmod(0o700)
    (ssh / "deploy_key").write_text("KEY")
    (ssh / "deploy_key").chmod(0o600)
    r = box(home, 'cat ~/.ssh/deploy_key; echo; touch ~/.ssh/x 2>&1 | head -1', HERDR_SANDBOX_SSH_DIR=str(ssh))
    assert r.stdout.startswith("KEY") and "Read-only" in r.stdout, r.stderr
    ssh.chmod(0o755)
    r = box(home, "echo ran", HERDR_SANDBOX_SSH_DIR=str(ssh))
    assert r.returncode == 126 and "ran" not in r.stdout
    r = box(home, "echo ran", HERDR_SANDBOX_SSH_DIR="")
    assert r.returncode == 126 and "ran" not in r.stdout


def test_both_sandbox_copies_are_identical():
    assert SANDBOX.read_bytes() == (ROOT / "packages/collab-scripts/sandbox.sh").read_bytes()


# ------------------------------------------------------------------ collab-secrets.py
def secrets_cli(d, *args, stdin=""):
    return subprocess.run([sys.executable, str(SECRETS), "--dir", str(d), *args], input=stdin,
                          capture_output=True, text=True, timeout=30)


def test_secrets_cli_writes_0600_in_a_0700_dir_and_never_echoes(tmp_path):
    d = tmp_path / "collab-secrets"
    r = secrets_cli(d, "set", "room_abc", "openai", stdin="Bearer sk-VERYSECRET\n")
    assert r.returncode == 0 and "VERYSECRET" not in r.stdout + r.stderr
    f = d / "room_abc.json"
    assert stat.S_IMODE(d.stat().st_mode) == 0o700 and stat.S_IMODE(f.stat().st_mode) == 0o600
    assert json.loads(f.read_text()) == {"openai": "Bearer sk-VERYSECRET"}
    secrets_cli(d, "set", "room_abc", "deploy_key", "--base64", stdin="-----KEY-----\n")
    assert base64.b64decode(json.loads(f.read_text())["deploy_key"]) == b"-----KEY-----\n"
    r = secrets_cli(d, "list", "room_abc")
    assert r.stdout.split() == ["deploy_key", "openai"] and "VERYSECRET" not in r.stdout
    r = secrets_cli(d, "rm", "room_abc", "openai")
    assert r.returncode == 0 and "openai" not in json.loads(f.read_text())
    assert stat.S_IMODE(f.stat().st_mode) == 0o600


@pytest.mark.parametrize("args", [("set", "../x", "n"), ("set", "room_a", "../n"), ("set", "room_a", ".hidden"),
                                  ("set", "room_a", "a b"), ("list", "a/b"), ("rm", "room_a", "")])
def test_secrets_cli_refuses_bad_room_or_name(tmp_path, args):
    r = secrets_cli(tmp_path / "s", *args, stdin="v")
    assert r.returncode != 0 and not list((tmp_path).rglob("*.json"))


def test_secrets_cli_refuses_an_empty_value_and_a_value_in_argv(tmp_path):
    assert secrets_cli(tmp_path / "s", "set", "room_a", "n", stdin="").returncode != 0
    assert secrets_cli(tmp_path / "s", "set", "room_a", "n", "the-value", stdin="x").returncode != 0


# ------------------------------------------------------------------ the runner runs helpers
# The real collab service behind the fake HTTP front of test_collab_spawn, its stateful fake herdr, the real
# sandbox and egress proxy, and a fake `claude` that reports what it saw (argv, env, stdin, ~/.ssh) on stdout.

FAKE_HARNESS = r'''#!/usr/bin/python3
import json, os, re, socket, sys, time
task = sys.stdin.read()
if "SLEEP" in task:
    time.sleep(60)
    print("woke up")
connect = None
if "CONNECT" in task:
    s = socket.create_connection(("127.0.0.1", 3128), timeout=10)
    s.sendall(b"CONNECT blocked.example.com:443 HTTP/1.1\r\nHost: blocked.example.com:443\r\n\r\n")
    connect = s.recv(200).decode(errors="replace").split("\r\n")[0]
m = re.search(r"read-only at (\S+)", task)
view = sorted(os.listdir(m.group(1))) if m and os.path.isdir(m.group(1)) else None
ssh = os.path.expanduser("~/.ssh")
info = {"argv": sys.argv, "env": dict(os.environ), "stdin": task, "cwd": os.getcwd(), "connect": connect,
        "ssh": sorted(os.listdir(ssh)) if os.path.isdir(ssh) else None, "view": view,
        "keys": {n: open(os.path.join(ssh, n)).read() for n in os.listdir(ssh)} if os.path.isdir(ssh) else {}}
print("RESULT-JSON " + json.dumps(info))
if "BIG" in task:
    print("x" * 50000)
sys.exit(3 if "FAIL" in task else 0)
'''
TASK = "please review zebra_quokka.py carefully"


@pytest.fixture
def hr(rig, tmp_path, monkeypatch):
    w = rig.w
    w.s.set_room_profile(w.alice, w.room, {"allowed_families": ["claude-code", "codex"]})
    w.root, w.root_info = w.agent("alice", actions=("read", "post", "spawn_helpers"), name="lead")
    fake = rig.home / ".local" / "bin"
    fake.mkdir(parents=True)
    (fake / "claude").write_text(FAKE_HARNESS)
    (fake / "claude").chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake}:{os.environ['PATH']}")
    monkeypatch.setenv("COLLAB_TEST_SECRET", "env-secret-XYZ")
    said: list[str] = []

    def mk(**kw):
        r = runner.Runner(rig.r.url, tmp_path / "state", rig.r.herdr, rig.r.ssh, WORKER, "bender",
                          say=said.append, sandbox=SANDBOX, **kw)
        r.families = {"bender": ["claude-code", "codex", "pi"]}
        return r

    rig.mk, rig.said, rig.fake = mk, said, fake
    rig.state = tmp_path / "state" / "helpers"
    return rig


def ask(hr, agent=None, room=None, **body):
    return hr.w.s.request_helper(agent or hr.w.root, {"room_id": room or hr.w.room, "role": "worker", "task": TASK, **body})


def cycle(hr, **kw):
    hr.mk(**kw).helpers_cycle()


def view(hr, hid, agent=None):
    return hr.w.s.helper_result(agent or hr.w.root, hid)


def until(hr, pred, timeout=45.0, **kw):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        cycle(hr, **kw)
        if pred():
            return
        time.sleep(0.3)
    raise AssertionError(f"timed out; runner said: {hr.said[-5:]}")


def finished(hr, hid, agent=None):
    return lambda: view(hr, hid, agent)["status"] in ("done", "failed", "cancelled", "expired")


def report(hr, hid, agent=None):
    res = view(hr, hid, agent)["result"] or ""
    line = next(x for x in res.splitlines() if x.startswith("RESULT-JSON "))
    return json.loads(line[len("RESULT-JSON "):]), res


def procs_with(token: str) -> list[int]:
    out = []
    for p in Path("/proc").iterdir():
        if p.name.isdigit():
            try:
                if token.encode() in (p / "cmdline").read_bytes():
                    st = (p / "stat").read_text().rsplit(")", 1)[1].split()
                    if st[0] != "Z":
                        out.append(int(p.name))
            except OSError:
                pass
    return out


@needs_box
def test_a_helper_runs_boxed_with_the_task_on_stdin_only_and_its_output_comes_back(hr):
    h = ask(hr)
    until(hr, finished(hr, h["helper_id"]))
    v = view(hr, h["helper_id"])
    assert v["status"] == "done", v
    info, _ = report(hr, h["helper_id"])
    assert TASK in info["stdin"] and info["env"]["HERDR_SANDBOX"] == "1"
    assert all(TASK not in a for a in info["argv"]) and all(TASK not in x for x in info["env"].values())
    assert "env-secret-XYZ" not in json.dumps(info["env"]) and "HTTPS_PROXY" in info["env"]
    assert info["cwd"].endswith(f"herdr-agents/work/helper-{h['helper_id']}")
    assert "--tools" in info["argv"]                                   # text-only: the runner has no --allow-tools
    # the task never reached a command line or the runner's log, and everything private is gone
    assert all(TASK not in line for line in calls(hr) + hr.said)
    assert not list(hr.state.iterdir()) and not Path(info["cwd"]).exists()
    assert any(" pane close " in f" {line} " for line in calls(hr))


@needs_box
def test_claim_happens_once_and_a_restarted_runner_never_starts_a_claimed_helper_again(hr, monkeypatch):
    h = ask(hr)

    class Crash(BaseException):
        pass

    def crash(self, *a, **k):
        raise Crash()
    monkeypatch.setattr(runner.Runner, "start_helper", crash)
    with pytest.raises(Crash):
        cycle(hr)
    monkeypatch.undo()
    assert view(hr, h["helper_id"])["status"] == "claimed"
    assert hr.mk().api("POST", f"fleet/helpers/{h['helper_id']}/claim", {})[0] == 409     # once only
    runs = len([x for x in calls(hr) if " pane run " in f" {x} "])
    for _ in range(3):
        cycle(hr)                                                      # a new process each time
    assert view(hr, h["helper_id"])["status"] == "claimed"
    assert len([x for x in calls(hr) if " pane run " in f" {x} "]) == runs
    assert procs_with(str(hr.state)) == []
    monkeypatch.setattr(runner, "HELPER_CLAIM_STALE", 0.0)
    cycle(hr)
    v = view(hr, h["helper_id"])
    assert v["status"] == "failed" and "restarted" in (v["error_text"] or "")
    assert not list(hr.state.iterdir())


@needs_box
def test_the_pane_lands_in_the_parents_tab(hr):
    out = json.loads(subprocess.run([hr.r.herdr, "workspace", "create", "--label", "lead"], capture_output=True,
                                    text=True, check=True).stdout)
    pane = out["result"]["root_pane"]["pane_id"]
    hr.w.s.link_agent(hr.w.alice, hr.w.root_info["agent_id"], {"instance_id": f"pane:bender:{pane}"})
    h = ask(hr)
    until(hr, finished(hr, h["helper_id"]))
    assert any(line.startswith(f"pane split {pane} ") for line in calls(hr))
    assert not any(line.startswith("workspace create --label herd-") for line in calls(hr))


@needs_box
def test_an_unknown_parent_gets_its_own_tab_and_never_splits_anyone_elses_pane(hr):
    w = hr.w
    out = json.loads(subprocess.run([hr.r.herdr, "workspace", "create", "--label", "mine"], capture_output=True,
                                    text=True, check=True).stdout)
    pane = out["result"]["root_pane"]["pane_id"]
    pid = w.s.create_project(w.bob, {"name": "bp"})["project_id"]
    broom = w.s.create_room(w.bob, pid, {"name": "br"})["room_id"]
    w.s.set_room_profile(w.bob, broom, {"allowed_families": ["claude-code"]})
    bagent, binfo = w.agent("bob", actions=("read", "post", "spawn_helpers"), room=broom, name="bobby")
    w.s.link_agent(w.bob, binfo["agent_id"], {"instance_id": f"pane:bender:{pane}"})   # bob points at alice's pane
    h = ask(hr, agent=bagent, room=broom)
    until(hr, finished(hr, h["helper_id"], bagent))
    assert view(hr, h["helper_id"], bagent)["status"] == "done"
    assert not any(line.startswith("pane split") for line in calls(hr))
    assert any(line.startswith("workspace create --label herd-bobby") for line in calls(hr))


def write_secrets(hr, room, data, mode=0o600):
    d = hr.home / ".config/herdr-engineering/collab-secrets"
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    f = d / f"{room}.json"
    f.write_text(json.dumps(data))
    f.chmod(mode)
    return f


@needs_box
def test_ssh_keys_are_present_only_when_listed_and_secret_values_never_reach_the_env(hr):
    w = hr.w
    w.s.set_room_profile(w.alice, w.room, {
        "allowed_families": ["claude-code"], "ssh_secret_names": ["deploy_key"],
        "services": [{"name": "api", "upstream_url": "https://api.example.com/v1", "header": "Authorization",
                      "secret_name": "api_token"}]})
    write_secrets(hr, w.room, {"deploy_key": base64.b64encode(b"KEY-ONE").decode(),
                               "other_key": base64.b64encode(b"KEY-TWO").decode(),
                               "api_token": "Bearer sk-SVCSECRET"})
    h = ask(hr)
    until(hr, finished(hr, h["helper_id"]))
    info, res = report(hr, h["helper_id"])
    assert info["ssh"] == ["deploy_key"] and info["keys"] == {"deploy_key": "KEY-ONE"}
    assert "SVCSECRET" not in res and "KEY-TWO" not in res
    assert info["env"]["SVC_API_URL"].endswith("/svc/api")
    # without ssh names there is no ~/.ssh at all
    w.s.set_room_profile(w.alice, w.room, {"allowed_families": ["claude-code"]})
    h = ask(hr)
    until(hr, finished(hr, h["helper_id"]))
    assert report(hr, h["helper_id"])[0]["ssh"] is None


def test_a_secrets_file_with_a_loose_mode_or_a_link_is_ignored(hr):
    f = write_secrets(hr, hr.w.room, {"k": "v"}, mode=0o644)
    r = hr.mk()
    assert r.room_secrets(hr.w.room) == {} and any("ignored" in m for m in hr.said)
    f.chmod(0o600)
    assert r.room_secrets(hr.w.room) == {"k": "v"}
    link = f.with_name("room_link.json")
    link.symlink_to(f)
    assert r.room_secrets("room_link") == {}
    assert r.room_secrets("../etc") == {}


@needs_box
def test_a_reviewer_sees_its_parents_work_read_only_and_a_worker_does_not(hr):
    parent = hr.home / "herdr-agents/work/lead"
    parent.mkdir(parents=True)
    (parent / "main.py").write_text("x = 1\n")
    hr.w.s.link_agent(hr.w.alice, hr.w.root_info["agent_id"], {"instance_id": "pane:beast:w9:p1"})   # an own agent
    h = ask(hr, role="reviewer")
    until(hr, finished(hr, h["helper_id"]))
    assert report(hr, h["helper_id"])[0]["view"] == ["main.py"]
    h = ask(hr, role="worker")
    until(hr, finished(hr, h["helper_id"]))
    assert report(hr, h["helper_id"])[0]["view"] is None


@needs_box
def test_the_time_limit_kills_the_helper_and_marks_it_failed(hr, monkeypatch):
    monkeypatch.setattr(collab, "HELPER_TTL", 4.0)
    h = ask(hr, task="SLEEP for a long time")
    until(hr, finished(hr, h["helper_id"]), timeout=40)
    v = view(hr, h["helper_id"])
    assert v["status"] == "failed" and v["error"] == "timeout", v
    assert "woke up" not in (v["result"] or "")
    assert procs_with(str(hr.fake / "claude")) == []


def running(hr, hid):
    f = hr.state / hid / "state.json"
    return lambda: f.exists() and json.loads(f.read_text()).get("phase") == "running"


@needs_box
@pytest.mark.parametrize("how", ["pause", "revoke", "cancel"])
def test_pause_revoke_or_cancel_stop_a_running_helper_within_one_cycle(hr, how):
    h = ask(hr, task="SLEEP until stopped")
    until(hr, running(hr, h["helper_id"]))
    start = time.monotonic() + 30                                      # the box may take a while under load
    while not procs_with(str(hr.fake / "claude")) and time.monotonic() < start:
        time.sleep(0.2)
    assert procs_with(str(hr.fake / "claude"))                         # it is really running
    r = hr.mk()
    if how == "pause":
        r.ok("POST", "fleet/helpers/pause", {"paused": True})
    elif how == "revoke":
        hr.w.s.revoke_agent(hr.w.alice, hr.w.root_info["agent_id"])
    else:
        hr.w.s.cancel_helper(hr.w.root, h["helper_id"])
    cycle(hr)                                                          # ONE cycle
    deadline = time.monotonic() + 5
    while procs_with(str(hr.fake / "claude")) and time.monotonic() < deadline:
        time.sleep(0.2)
    assert procs_with(str(hr.fake / "claude")) == [] and procs_with(str(hr.state)) == []
    assert not list(hr.state.iterdir())
    assert not (hr.home / f"herdr-agents/work/helper-{h['helper_id']}").exists()


@needs_box
def test_a_blocked_host_becomes_one_egress_request(hr):
    h = ask(hr, task="CONNECT somewhere please")
    until(hr, finished(hr, h["helper_id"]))
    info, res = report(hr, h["helper_id"])
    assert info["connect"].startswith("HTTP/1.1 403")
    reqs = [e for e in hr.mk().ok("GET", "fleet/egress")["requests"] if e["host_port"] == "blocked.example.com:443"]
    assert len(reqs) == 1 and reqs[0]["room_id"] == hr.w.room


def test_blocked_log_lines_are_requested_once_and_forbidden_addresses_only_noted(hr, tmp_path):
    r = hr.mk()
    d = tmp_path / "hd"
    d.mkdir()
    lines = [{"host": "a.example.com", "port": 443, "ts": 1}, {"suppressed": 5, "ts": 2},
             {"host": "a.example.com", "port": 443, "ts": 3}, {"host": "inner.example.com", "port": 443, "ts": 4,
                                                                "reason": "forbidden-address"},
             {"host": "b.example.com", "port": 8443, "ts": 5}]
    (d / "blocked.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines) + '{"host": "c.exa')
    sent = []
    r.api = lambda m, p, b=None: (sent.append((p, b)), (200, {"state": "open"}))[1]
    st = {"room_id": hr.w.room, "agent_id": "agent_x"}
    r.drain_blocked(d, st)
    r.drain_blocked(d, st)                                             # nothing new: nothing sent
    assert [(b["host"], b["port"]) for _, b in sent] == [("a.example.com", 443), ("b.example.com", 8443)]
    assert all(p == "fleet/egress/request" and b["room_id"] == hr.w.room for p, b in sent)
    assert st["forbidden"] == ["inner.example.com:443"]
    with open(d / "blocked.jsonl", "a") as fh:                        # the partial line completes later
        fh.write('mple.com", "port": 443, "ts": 6}\n')
    r.drain_blocked(d, st)
    assert sent[-1][1]["host"] == "c.example.com" and len(sent) == 3


def test_model_ids_go_through_the_familys_flag_and_odd_ones_are_refused(hr):
    r = hr.mk(allow_tools=True)
    prof = {"tools_allowed": True}
    cmd, notes = r.helper_command({"family": "pi", "model": "qwen3:30b", "profile": prof})
    assert cmd.startswith("pi --model qwen3:30b -p ") and cmd.endswith('-- "Task from a room member: $(cat)"')
    cmd, _ = r.helper_command({"family": "codex", "model": "gpt-5", "profile": prof})
    assert cmd == "codex exec --model gpt-5 --skip-git-repo-check -"
    cmd, _ = r.helper_command({"family": "claude-code", "model": None, "profile": {"tools_allowed": False}})
    assert cmd == runner.FAMILY_CMD_TEXT_ONLY["claude-code"]            # the room does not allow tools
    for bad in ("a b", "x;rm -rf /", "$(id)", "m" * 129, "", "q\n"):
        with pytest.raises(runner.Failure):
            r.helper_command({"family": "claude-code", "model": bad, "profile": prof})
    with pytest.raises(runner.Failure):                                # codex has no text-only mode
        hr.mk().helper_command({"family": "codex", "model": None, "profile": prof})


@needs_box
def test_an_invalid_model_id_is_refused_with_a_failed_finish(hr):
    h = ask(hr)
    hr.w.s._helpers[h["helper_id"]]["model"] = "bad model;id"
    cycle(hr)
    v = view(hr, h["helper_id"])
    assert v["status"] == "failed" and "model" in (v["error_text"] or "")
    assert not any(" pane run " in f" {x} " for x in calls(hr))


@needs_box
def test_big_output_is_truncated_with_a_marker(hr):
    h = ask(hr, task="BIG output please")
    until(hr, finished(hr, h["helper_id"]))
    res = view(hr, h["helper_id"])["result"]
    assert "[output truncated at 20 KB]" in res and len(res) < 22000


def test_nothing_is_claimed_without_a_sandbox(hr):
    h = ask(hr)
    r = runner.Runner(hr.r.url, hr.state.parent, hr.r.herdr, hr.r.ssh, WORKER, "bender", say=hr.said.append)
    r.families = {"bender": ["claude-code"]}
    r.helpers_cycle()
    assert view(hr, h["helper_id"])["status"] == "requested"


def test_runner_rows_carry_the_profile_slice_and_the_service_clock(hr):
    w = hr.w
    w.s.set_room_profile(w.alice, w.room, {"allowed_families": ["claude-code"], "tools_allowed": True,
                                           "ssh_secret_names": ["k1"]})
    h = ask(hr)
    r = hr.mk()
    row = next(x for x in r.ok("GET", "fleet/helpers")["helpers"] if x["helper_id"] == h["helper_id"])
    assert row["profile"] == {"tools_allowed": True, "services": [], "ssh_secret_names": ["k1"]}
    c = r.ok("POST", f"fleet/helpers/{h['helper_id']}/claim", {})
    assert c["profile"] == row["profile"] and c["now"] == w.s.clock.t and c["expires_epoch"] - c["now"] > 0


# ------------------------------------------------------------------ the model catalogue
GATEWAY_MODELS = {"data": [
    {"id": "qwen3-coder:30b", "owned_by": "qwen", "context_length": 262144, "local": True},
    {"id": "glm-5v", "owned_by": "zhipu", "capabilities": ["vision", "tools"], "price_out_per_mtok": 2.5},
    {"id": "org/with-slash", "owned_by": "x"},                          # not a plain id: skipped, not fatal
    {"id": "has space"}, {"id": 7}, {"nothing": True}, "junk",
    {"id": "qwen3-coder:30b", "owned_by": "dupe"},                     # a duplicate: the first one counts
    {"id": "big", "context_length": 10**12, "price_out_per_mtok": -1},  # absurd numbers: dropped, entry kept
]}


@pytest.fixture
def gateway():
    hits = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            hits.append(self.path)
            data = json.dumps(GATEWAY_MODELS).encode() if self.path == "/v1/models" else b"{}"
            self.send_response(200 if self.path == "/v1/models" else 404)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/v1", hits
    srv.shutdown()


def test_the_catalogue_is_posted_normalized_and_skips_odd_ids(hr, gateway):
    url, hits = gateway
    hr.mk(gateway=url).catalogue_cycle()
    cat = {e["id"]: e for e in hr.w.s._catalogue}
    assert set(cat) == {"qwen3-coder:30b", "glm-5v", "big"}
    assert cat["qwen3-coder:30b"] == {"id": "qwen3-coder:30b", "family": "qwen", "vision": False,
                                      "price_out_per_mtok": None, "context": 262144, "local": True}
    assert cat["glm-5v"]["vision"] is True and cat["glm-5v"]["price_out_per_mtok"] == 2.5
    assert cat["big"]["context"] is None and cat["big"]["price_out_per_mtok"] is None
    hr.mk(gateway=url).catalogue_cycle()                               # a new process, within ten minutes
    assert hits == ["/v1/models"]


def test_without_a_gateway_nothing_is_posted(hr):
    hr.mk().catalogue_cycle()
    assert hr.w.s._catalogue == [] and any("no gateway" in m for m in hr.said)


@needs_box
def test_someone_elses_reviewer_named_like_an_operator_agent_gets_no_view(hr):
    w = hr.w
    pid = w.s.create_project(w.bob, {"name": "bp"})["project_id"]
    broom = w.s.create_room(w.bob, pid, {"name": "br"})["room_id"]
    w.s.set_room_profile(w.bob, broom, {"allowed_families": ["claude-code"]})
    bagent, _ = w.agent("bob", actions=("read", "post", "spawn_helpers"), room=broom, name="bobby")
    (hr.home / "herdr-agents/work/bobby").mkdir(parents=True)          # e.g. an operator agent of that name
    (hr.home / "herdr-agents/work/bobby/private.txt").write_text("operator work")
    h = ask(hr, agent=bagent, room=broom, role="reviewer")
    until(hr, finished(hr, h["helper_id"], bagent))
    info, res = report(hr, h["helper_id"], bagent)
    assert info["view"] is None and "operator work" not in res


@needs_box
def test_a_paused_helper_stops_even_when_the_fleet_path_fails(hr, monkeypatch):
    h = ask(hr, task="SLEEP until stopped")
    until(hr, running(hr, h["helper_id"]))
    r = hr.mk()
    r.ok("POST", "fleet/helpers/pause", {"paused": True})
    real = r.ok

    def broken(method, path, body=None):
        if path.startswith("fleet/spawns") or path == "config":
            raise runner.Failure("GET config: 500")
        return real(method, path, body)
    monkeypatch.setattr(r, "ok", broken)
    with pytest.raises(runner.Failure):
        r.once()
    assert not list(hr.state.iterdir()) and procs_with(str(hr.state)) == []

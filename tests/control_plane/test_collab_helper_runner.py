# ruff: noqa: F811  (pytest fixtures are imported by name from the sibling test modules)
"""Helpers on the host: the sandbox's extra read-only views, the room secrets CLI, and the runner's
helper lifecycle (claim once, place, run boxed behind the egress proxy, finish, tear down)."""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

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
    import json
    assert json.loads(f.read_text()) == {"openai": "Bearer sk-VERYSECRET"}
    secrets_cli(d, "set", "room_abc", "deploy_key", "--base64", stdin="-----KEY-----\n")
    import base64
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

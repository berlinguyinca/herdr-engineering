"""`herdr-eng fleet host-apply` (spec docs/specs/herdr-fleet-rollout-watcher-20261104, step 4).

No mocks: real git repos (a bare origin plus clones) in tmp dirs, real subprocesses (install/stage/smoke commands are
real scripts reached through the documented HERDR_FLEET_*_CMD seam), real flock contention, real symlinks and a real
long-lived process that must survive a Pi switch. HOME and XDG_STATE_HOME are throwaway; nothing touches a real host.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import pytest

from herdr_engineering.control_plane.fleet_rollout import (
    CHECK_NAMES,
    HOST_ORDER,
    LOG_KINDS,
    REASONS,
    STATUSES,
)
from herdr_engineering.fleet_watch import host_apply, logs

RID = "0123456789abcdef"
HEX40 = re.compile(r"[0-9a-f]{40}")
HEX64 = re.compile(r"[0-9a-f]{64}")


def git(cwd: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
    out = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, env=env)
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


def script(path: Path, body: str) -> Path:
    path.write_text("#!/bin/sh\n" + body + "\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def assert_closed(result: dict) -> None:
    """Spec section 14: every string in a result is an enum, hex, a timestamp or a fixed key - never free text."""
    enums = {"precheck", "apply", "health", "rollback", "all", *STATUSES, *REASONS, *HOST_ORDER, *LOG_KINDS,
             *CHECK_NAMES, "ok", "failed", "skipped", "autospec", "pi-engineering", "herdr-engineering",
             "ai-sync", "claude-config"}

    def walk(value, key=""):
        if isinstance(value, dict):
            for k, v in value.items():
                assert re.fullmatch(r"[a-z0-9_]{1,24}", k), k
                walk(v, k)
        elif isinstance(value, list):
            for v in value:
                walk(v, key)
        elif isinstance(value, str):
            if key in ("started_at", "finished_at"):
                assert datetime.fromisoformat(value).utcoffset().total_seconds() == 0
            else:
                assert value in enums or HEX40.fullmatch(value) or HEX64.fullmatch(value) or value == RID, \
                    f"free text in {key}: {value!r}"
        else:
            assert isinstance(value, (int, bool)) or value is None
    walk(result)


class World:
    """A bare origin, a developer clone that publishes commits, and the host's deployment checkout."""

    def __init__(self, root: Path):
        self.root = root
        self.origin = root / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.origin)], check=True)
        self.dev = root / "dev"
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.dev)], check=True)
        git(self.dev, "remote", "add", "origin", str(self.origin))
        self.publish("one")
        self.checkout = root / "checkout"
        subprocess.run(["git", "clone", "-q", str(self.origin), str(self.checkout)], check=True)
        self.home = root / "home"
        self.home.mkdir()
        self.marker = root / "installed.marker"
        install = script(root / "install.sh", f'echo "$HERDR_FLEET_TARGET_SHA" > "{self.marker}"')
        ok = script(root / "ok.sh", "exit 0")
        self.env = {
            "PATH": os.environ["PATH"], "HOME": str(self.home), "XDG_STATE_HOME": str(self.home / "state"),
            "HERDR_ENGINEERING_REPO": str(self.checkout),
            "HERDR_FLEET_HERDR_INSTALL_CMD": str(install), "HERDR_FLEET_HERDR_AGENT_CMD": "",
            "HERDR_FLEET_HERDR_SERVICE_CMD": str(ok), "HERDR_FLEET_PARITY_CMD": str(ok),
            "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
        }

    def publish(self, name: str) -> str:
        (self.dev / name).write_text(name)
        git(self.dev, "add", name)
        git(self.dev, "commit", "-q", "-m", name)
        git(self.dev, "push", "-q", "origin", "main")
        return git(self.dev, "rev-parse", "HEAD")

    def head(self) -> str:
        return git(self.checkout, "rev-parse", "HEAD")

    def run(self, phase, repo="herdr-engineering", sha=None, **kw) -> dict:
        return host_apply.run_phase(phase, rollout_id=RID, repo=repo, sha=sha or self.head(), host="bender",
                                    env=kw.pop("env", self.env), busy_wait_s=0.05, **kw)


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


# --------------------------------------------------------------------------- D6 gate

def test_dirty_checkout_is_blocked_with_counts_and_paths_only_in_the_log(world):
    target = world.publish("two")
    (world.checkout / "one").write_text("edited")                    # tracked change
    (world.checkout / "stray-secret-name.txt").write_text("x")      # untracked
    out = world.run("precheck", sha=target)
    assert (out["status"], out["reason"]) == ("blocked", "dirty_checkout")
    assert out["counts"]["dirty_paths"] == 2 and out["counts"]["unpublished_commits"] == 0
    assert "stray-secret-name" not in json.dumps(out)                # no paths on stdout (section 14)
    assert_closed(out)
    text = logs.LogStore(world.home / "state/herdr-engineering/fleet-watch/logs").read_bytes(RID, "bender", "updater")
    assert b"stray-secret-name.txt" in text                         # ... but readable on the host
    assert hashlib.sha256(text).hexdigest() == out["log"]["sha256"]
    # never cleaned, stashed or reset
    assert (world.checkout / "one").read_text() == "edited" and (world.checkout / "stray-secret-name.txt").exists()
    assert git(world.checkout, "stash", "list") == ""


def test_dirty_path_examples_are_capped_at_eight_in_the_log(world):
    target = world.publish("two")
    for i in range(12):
        (world.checkout / f"junk{i}").write_text("x")
    out = world.run("precheck", sha=target)
    assert out["counts"]["dirty_paths"] == 12
    text = logs.LogStore(world.home / "state/herdr-engineering/fleet-watch/logs").read_bytes(
        RID, "bender", "updater").decode()
    assert text.count("dirty: ") == 8


def test_not_on_main_is_blocked(world):
    target = world.publish("two")
    git(world.checkout, "checkout", "-q", "-b", "feature")
    out = world.run("precheck", sha=target)
    assert (out["status"], out["reason"]) == ("blocked", "not_on_main")
    assert git(world.checkout, "branch", "--show-current") == "feature"


def test_unpublished_commits_are_blocked_with_the_count(world):
    target = world.publish("two")
    (world.checkout / "local").write_text("l")
    git(world.checkout, "add", "local")
    git(world.checkout, "commit", "-q", "-m", "local only")
    out = world.run("precheck", sha=target)
    assert (out["status"], out["reason"]) == ("blocked", "unpublished_commits")
    assert out["counts"]["unpublished_commits"] == 1
    assert_closed(out)


def test_a_sha_that_is_not_on_origin_main_is_not_applied(world):
    git(world.dev, "checkout", "-q", "-b", "side")
    (world.dev / "side").write_text("s")
    git(world.dev, "add", "side")
    git(world.dev, "commit", "-q", "-m", "side")
    git(world.dev, "push", "-q", "origin", "side")
    side = git(world.dev, "rev-parse", "HEAD")
    before = world.head()
    out = world.run("apply", sha=side)
    assert (out["status"], out["reason"]) == ("blocked", "not_published")
    assert world.head() == before and not world.marker.exists()


# --------------------------------------------------------------------------- apply + idempotency

def test_apply_fast_forwards_installs_and_passes_health(world):
    target = world.publish("two")
    out = world.run("all", sha=target)
    assert out["status"] == "ok" and out["after_sha"] == target and out["no_change"] is False
    assert world.head() == target and world.marker.read_text().strip() == target
    health = world.run("health", sha=target)
    names = {c["name"]: c["status"] for c in health["checks"]}
    assert names["version_matches"] == "ok" and names["service_active"] == "ok" and names["parity_ok"] == "ok"
    assert names["gateway_registered"] == "skipped"
    assert_closed(out) and assert_closed(health)


def test_second_apply_is_no_change_and_runs_nothing(world):
    target = world.publish("two")
    assert world.run("all", sha=target)["status"] == "ok"
    world.marker.unlink()
    again = world.run("apply", sha=target)
    assert (again["status"], again["no_change"]) == ("ok", True)
    assert not world.marker.exists()                                  # the installer did not run again
    assert world.run("precheck", sha=target)["no_change"] is True


def test_failed_install_is_apply_failed_and_herdr_has_no_automatic_rollback(world):
    target = world.publish("two")
    world.env["HERDR_FLEET_HERDR_INSTALL_CMD"] = str(script(world.root / "bad.sh", "echo boom; exit 3"))
    before = world.head()
    out = world.run("apply", sha=target)
    assert (out["status"], out["reason"]) == ("failed", "apply_failed")
    back = world.run("rollback", sha=target, before_sha=before)
    assert back["status"] == "skipped" and back["not_applicable"] is True
    assert "boom" in logs.LogStore(world.home / "state/herdr-engineering/fleet-watch/logs").read_bytes(
        RID, "bender", "updater").decode()
    assert "boom" not in json.dumps(out)


def test_timeout_is_failed_timeout_and_kills_the_process_group(world, monkeypatch):
    target = world.publish("two")
    pidfile = world.root / "child.pid"
    world.env["HERDR_FLEET_HERDR_INSTALL_CMD"] = str(script(
        world.root / "slow.sh", f'sleep 60 &\necho $! > "{pidfile}"\nsleep 60'))
    monkeypatch.setattr(host_apply, "COMMAND_TIMEOUT_S", 1)
    began = time.monotonic()
    out = world.run("apply", sha=target)
    assert (out["status"], out["reason"]) == ("failed", "timeout")
    assert time.monotonic() - began < 20
    time.sleep(0.2)
    with pytest.raises(ProcessLookupError):
        os.kill(int(pidfile.read_text()), 0)                          # the background child died too


def test_failed_health_check_is_reported(world):
    target = world.publish("two")
    world.env["HERDR_FLEET_HERDR_SERVICE_CMD"] = str(script(world.root / "down.sh", "exit 1"))
    out = world.run("all", sha=target)
    assert (out["status"], out["reason"]) == ("failed", "health_failed")
    assert {c["name"]: c["status"] for c in out["checks"]}["service_active"] == "failed"


def test_phase2_repos_are_skipped_not_applicable(world):
    for repo in ("ai-sync", "claude-config"):
        out = world.run("all", repo=repo, sha="a" * 40)
        assert (out["status"], out["not_applicable"]) == ("skipped", True)
        assert_closed(out)


def test_bad_arguments_are_refused(world):
    with pytest.raises(ValueError):
        host_apply.run_phase("all", rollout_id="nope", repo="autospec", sha="a" * 40, env=world.env)
    with pytest.raises(ValueError):
        host_apply.run_phase("all", rollout_id=RID, repo="autospec", sha="short", env=world.env)
    with pytest.raises(ValueError):
        host_apply.run_phase("all", rollout_id=RID, repo="mystery", sha="a" * 40, env=world.env)
    with pytest.raises(ValueError):
        host_apply.run_phase("all", rollout_id=RID, repo="autospec", sha="a" * 40, host="laptop", env=world.env)


# --------------------------------------------------------------------------- lock (spec 6.3)

def _hold(world) -> object:
    path = host_apply.lock_path(world.env)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a")
    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return handle


def test_busy_lock_blocks_after_the_retries_and_changes_nothing(world):
    target = world.publish("two")
    held = _hold(world)
    try:
        began = time.monotonic()
        out = world.run("apply", sha=target, busy_retries=3)
        assert (out["status"], out["reason"]) == ("blocked", "busy")
        assert time.monotonic() - began >= 0.15                       # it retried 3 times, 0.05 s apart
        assert world.head() != target and not world.marker.exists()
    finally:
        held.close()


def test_busy_lock_is_retried_and_then_succeeds(world):
    target = world.publish("two")
    held = _hold(world)
    threading.Timer(0.3, held.close).start()
    out = world.run("apply", sha=target, busy_retries=20)
    assert out["status"] == "ok" and world.head() == target


# --------------------------------------------------------------------------- pi-engineering (D7)

PI_SHAS = [f"{i:x}" * 40 for i in range(1, 7)]


def pi_stage_script(world) -> Path:
    root = world.home / ".local/share/herdr-engineering/pi"
    return script(world.root / "stage.sh", f'''set -e
r="{root}/releases/$HERDR_FLEET_TARGET_SHA"
mkdir -p "$r/node_modules" "{root}/receipts"
echo '{{"name":"pi-engineering-runtime"}}' > "$r/package.json"
echo '{{}}' > "{root}/receipts/$HERDR_FLEET_TARGET_SHA.json"''')


def make_release(world, sha: str, age: int = 0) -> Path:
    root = world.home / ".local/share/herdr-engineering/pi"
    release = root / "releases" / sha
    (release / "node_modules").mkdir(parents=True)
    (release / "package.json").write_text("{}")
    (root / "receipts").mkdir(exist_ok=True)
    (root / "receipts" / f"{sha}.json").write_text("{}")
    os.utime(release, (time.time() - age, time.time() - age))
    return release


def pi_env(world, smoke_ok=True) -> dict:
    return {**world.env, "HERDR_FLEET_PI_STAGE_CMD": str(pi_stage_script(world)),
            "HERDR_FLEET_PI_SMOKE_CMD": str(script(world.root / "smoke.sh", "exit 0" if smoke_ok else "exit 1"))}


def test_pi_stages_switches_current_atomically_and_reports_checks(world):
    root = world.home / ".local/share/herdr-engineering/pi"
    out = world.run("all", repo="pi-engineering", sha=PI_SHAS[0], env=pi_env(world))
    assert out["status"] == "ok" and out["after_sha"] == PI_SHAS[0]
    assert os.readlink(root / "current") == f"releases/{PI_SHAS[0]}"
    assert {c["name"]: c["status"] for c in out["checks"]} == {
        "release_complete": "ok", "current_symlink": "ok", "version_smoke": "ok"}
    assert_closed(out)


def test_pi_keeps_three_releases_and_never_prunes_current_or_previous(world):
    for age, sha in zip((500, 400, 300, 200), PI_SHAS[:4], strict=True):
        make_release(world, sha, age)
    root = world.home / ".local/share/herdr-engineering/pi"
    os.symlink(f"releases/{PI_SHAS[0]}", root / "current")            # the OLDEST is active
    out = world.run("apply", repo="pi-engineering", sha=PI_SHAS[4], env=pi_env(world))
    assert out["status"] == "ok" and out["before_sha"] == PI_SHAS[0]
    kept = sorted(p.name for p in (root / "releases").iterdir())
    assert len(kept) == 3
    assert PI_SHAS[4] in kept and PI_SHAS[0] in kept                  # new one + the one it replaced (rollback)
    assert not (root / "receipts" / f"{PI_SHAS[1]}.json").exists()    # a pruned release loses its receipt


def test_pi_switch_never_exposes_a_missing_current_and_never_signals_a_live_session(world):
    old, new = make_release(world, PI_SHAS[0]), make_release(world, PI_SHAS[1])
    root = world.home / ".local/share/herdr-engineering/pi"
    os.symlink(f"releases/{PI_SHAS[0]}", root / "current")
    session = subprocess.Popen(["sleep", "30"], cwd=old)               # a live "Pi session" inside the old release
    try:
        env = {**world.env, "HOME": str(world.home)}
        ctx = host_apply.Context(RID, "pi-engineering", PI_SHAS[1], "bender", env=env)
        seen: list[str] = []
        stop = threading.Event()

        def watch():
            while not stop.is_set():
                try:
                    seen.append(os.readlink(root / "current"))
                    assert (root / "current").resolve().is_dir()
                except OSError as exc:                               # pragma: no cover - this IS the failure
                    seen.append(f"ERR {exc}")
        thread = threading.Thread(target=watch)
        thread.start()
        pi = host_apply.Pi()
        for i in range(300):
            pi.switch(ctx, PI_SHAS[i % 2 ^ 1] if i % 2 else PI_SHAS[0])
        stop.set()
        thread.join()
        assert seen and not [s for s in seen if s.startswith("ERR")]
        assert set(seen) <= {f"releases/{PI_SHAS[0]}", f"releases/{PI_SHAS[1]}"}
        out = world.run("apply", repo="pi-engineering", sha=PI_SHAS[2], env=pi_env(world))
        assert out["status"] == "ok"
        assert session.poll() is None                                  # the session was never signalled
        assert new.exists() and old.exists()
    finally:
        session.kill()
        session.wait()


def test_pi_failed_smoke_is_rolled_back_by_a_symlink_flip(world):
    make_release(world, PI_SHAS[0])
    root = world.home / ".local/share/herdr-engineering/pi"
    os.symlink(f"releases/{PI_SHAS[0]}", root / "current")
    env = pi_env(world, smoke_ok=False)
    out = world.run("all", repo="pi-engineering", sha=PI_SHAS[1], env=env)
    assert (out["status"], out["reason"]) == ("failed", "health_failed")
    assert os.readlink(root / "current") == f"releases/{PI_SHAS[1]}"
    back = world.run("rollback", repo="pi-engineering", sha=PI_SHAS[1], before_sha=PI_SHAS[0], env=env)
    assert (back["status"], back["after_sha"]) == ("rolled_back", PI_SHAS[0])
    assert os.readlink(root / "current") == f"releases/{PI_SHAS[0]}"


def test_pi_already_current_is_no_change_and_stage_does_not_run(world):
    make_release(world, PI_SHAS[0])
    root = world.home / ".local/share/herdr-engineering/pi"
    os.symlink(f"releases/{PI_SHAS[0]}", root / "current")
    marker = world.root / "staged"
    env = {**pi_env(world), "HERDR_FLEET_PI_STAGE_CMD": str(script(world.root / "m.sh", f'touch "{marker}"'))}
    out = world.run("apply", repo="pi-engineering", sha=PI_SHAS[0], env=env)
    assert (out["status"], out["no_change"]) == ("ok", True) and not marker.exists()


def test_pi_incomplete_stage_is_failed(world):
    env = {**pi_env(world), "HERDR_FLEET_PI_STAGE_CMD": "true"}
    out = world.run("apply", repo="pi-engineering", sha=PI_SHAS[0], env=env)
    assert (out["status"], out["reason"]) == ("failed", "apply_failed")
    assert not (world.home / ".local/share/herdr-engineering/pi/current").exists()


def test_pi_refuses_to_replace_a_real_current_directory(world):
    root = world.home / ".local/share/herdr-engineering/pi"
    (root / "current").mkdir(parents=True)
    out = world.run("apply", repo="pi-engineering", sha=PI_SHAS[0], env=pi_env(world))
    assert out["status"] == "failed" and (root / "current").is_dir() and not (root / "current").is_symlink()


# --------------------------------------------------------------------------- autospec

def autospec_world(world) -> dict:
    gens = world.home / ".autospec/runtime-generations"
    gens.mkdir(parents=True)
    install = script(world.root / "as-install.sh", f'''set -e
g="{gens}/gen-$HERDR_FLEET_TARGET_SHA"
mkdir -p "$g"
printf 'schema=4\\nhead=%s\\n' "$HERDR_FLEET_TARGET_SHA" > "$g/receipt"''')
    return {**world.env, "HERDR_FLEET_AUTOSPEC_CHECKOUT": str(world.checkout),
            "HERDR_FLEET_AUTOSPEC_GENERATIONS": str(gens), "HERDR_FLEET_AUTOSPEC_INSTALL_CMD": str(install),
            "HERDR_FLEET_AUTOSPEC_SMOKE_CMD": str(world.root / "ok.sh")}


def test_autospec_applies_and_checks_the_generation_receipt(world):
    env = autospec_world(world)
    target = world.publish("two")
    out = world.run("all", repo="autospec", sha=target, env=env)
    assert out["status"] == "ok" and out["after_sha"] == target
    assert {c["name"]: c["status"] for c in out["checks"]} == {"generation_receipt": "ok", "version_smoke": "ok"}
    assert world.run("apply", repo="autospec", sha=target, env=env)["no_change"] is True


def test_autospec_dirty_checkout_blocks_and_failed_refresh_leaves_previous_generation(world):
    env = autospec_world(world)
    first = world.publish("two")
    assert world.run("all", repo="autospec", sha=first, env=env)["status"] == "ok"
    second = world.publish("three")
    env["HERDR_FLEET_AUTOSPEC_INSTALL_CMD"] = str(script(world.root / "bad.sh", "exit 1"))
    out = world.run("apply", repo="autospec", sha=second, env=env)
    assert out["status"] == "failed"
    back = world.run("rollback", repo="autospec", sha=second, before_sha=first, env=env)
    assert back["status"] == "rolled_back"                              # the previous generation is still active
    third = world.publish("four")
    (world.checkout / "dirty").write_text("d")
    assert world.run("precheck", repo="autospec", sha=third, env=env)["reason"] == "dirty_checkout"


# --------------------------------------------------------------------------- the real CLI

def test_cli_prints_one_closed_json_object_and_sets_the_exit_code(world):
    target = world.publish("two")
    env = {**world.env, "PYTHONPATH": str(Path(__file__).resolve().parent.parent)}
    base = [sys.executable, "-I", "-c", "import sys; from herdr_engineering.cli import main; sys.exit(main())",
            "fleet", "host-apply", "--rollout", RID, "--repo", "herdr-engineering", "--sha", target,
            "--host", "bender", "--busy-wait", "0.05"]
    env_i = {k: v for k, v in env.items() if k != "PYTHONPATH"}
    out = subprocess.run(base, capture_output=True, text=True, env=env_i, cwd="/", timeout=120)
    # -I ignores PYTHONPATH, but the venv has the package installed (editable): the installed-build path.
    assert out.returncode == 0, out.stderr
    result = json.loads(out.stdout)
    assert result["status"] == "ok" and out.stdout.count("\n") == 1
    assert_closed(result)
    (world.checkout / "x").write_text("x")
    third = world.publish("three")
    blocked = subprocess.run([*base[:-6], "--sha", third, "--host", "bender", "--phase", "precheck"],
                             capture_output=True, text=True, env=env_i, cwd="/", timeout=120)
    assert blocked.returncode == 1 and json.loads(blocked.stdout)["reason"] == "dirty_checkout"
    bad = subprocess.run([*base[:7], "nothex", *base[8:]], capture_output=True, text=True, env=env_i, cwd="/")
    assert bad.returncode == 2 and bad.stdout == ""

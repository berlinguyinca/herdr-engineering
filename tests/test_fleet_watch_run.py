"""`fleet-watch run` (spec section 6, step 4): the serial rollout runner.

No mocks. The planner talks to a real local HTTP "GitHub" and real bare git repos (tests/test_fleet_watch.py). Each
fleet host is a real directory tree with a real clone, reached through a real executable ssh stand-in
(HERDR_FLEET_WATCH_SSH) that parses ssh's own option syntax, exits 255 for an unreachable host, hangs on request, or
kills the runner process outright to simulate a crash, and otherwise runs the host's installed `herdr-eng` in a
throwaway HOME. Nothing here can reach a real host.
"""
from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

import pytest

from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.fleet_rollout import (
    CHECK_NAMES,
    HOST_ORDER,
    LOG_KINDS,
    REASONS,
    STATUSES,
    validate_fleet_rollout,
)
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from herdr_engineering.fleet_watch import core, events, run, state
from herdr_engineering.service_metadata_feed import MetadataClient
from tests.test_fleet_watch import FakeGitHub, Forge, git

CHECKOUT_ROOT = Path(__file__).resolve().parent.parent
#: Run THIS checkout's code (not whatever happens to be installed in the venv), isolated like the real entry point.
BOOT = (f"import sys; sys.path.insert(0, {str(CHECKOUT_ROOT)!r}); "
        "from herdr_engineering.cli import main; sys.exit(main())")
HE = "berlinguyinca/herdr-engineering"
PI = "berlinguyinca/pi-engineering"
HEX = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}|[0-9a-f]{16}")

FAKE_SSH = """#!/bin/sh
while [ "$1" = "-o" ]; do shift 2; done
[ "$1" = "-T" ] && shift
host=$1; shift
[ "$1" = "--" ] && shift
echo "$host $*" >> "$FAKE_SSH_DIR/calls.log"
mode=$(cat "$FAKE_SSH_DIR/$host.mode" 2>/dev/null || echo ok)
case "$mode" in
  down) exit 255 ;;
  hang) sleep 60; exit 0 ;;
  hang_apply) case "$*" in *"--phase apply"*) sleep 60; exit 0 ;; esac ;;
  die) kill -9 $PPID; sleep 1; exit 1 ;;
esac
export HOME="$FAKE_SSH_DIR/home-$host"
exec sh -c "$*"
"""


def script(path: Path, body: str) -> Path:
    path.write_text("#!/bin/sh\n" + body + "\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


class Fleet:
    def __init__(self, root: Path, forge: Forge, gh: FakeGitHub):
        self.root, self.forge, self.gh = root, forge, gh
        self.dir = root / "fleet"
        self.dir.mkdir()
        self.runner_home = root / "runner-home"
        self.runner_home.mkdir()
        self.ssh = script(root / "fake-ssh", FAKE_SSH.split("\n", 1)[1])
        self.lock = root / "fleet.lock.yaml"
        self.shims: dict[str, Path] = {}
        for host in HOST_ORDER:
            self.add_host(host)

    def home(self, host: str) -> Path:
        return self.dir / f"home-{host}"

    def add_host(self, host: str) -> None:
        home = self.home(host)
        (home / ".local/bin").mkdir(parents=True)
        checkout = home / "checkout"
        subprocess.run(["git", "clone", "-q", str(self.forge.root / f"{HE}.git"), str(checkout)], check=True)
        marker = home / "installed.marker"
        install = script(home / "install.sh", f'echo "$HERDR_FLEET_TARGET_SHA" > "{marker}"')
        ok = script(home / "ok.sh", "exit 0")
        pi_root = home / ".local/share/herdr-engineering/pi"
        stage = script(home / "stage.sh", f'''set -e
r="{pi_root}/releases/$HERDR_FLEET_TARGET_SHA"
mkdir -p "$r/node_modules" "{pi_root}/receipts"
echo '{{}}' > "$r/package.json"
echo '{{}}' > "{pi_root}/receipts/$HERDR_FLEET_TARGET_SHA.json"''')
        smoke = script(home / "smoke.sh", f'[ ! -e "{home}/smoke_fail" ]')
        shim = script(home / ".local/bin/herdr-eng", f'''export XDG_STATE_HOME="{home}/state"
export HERDR_ENGINEERING_REPO="{checkout}"
export HERDR_FLEET_HERDR_INSTALL_CMD="{install}" HERDR_FLEET_HERDR_AGENT_CMD=""
export HERDR_FLEET_HERDR_SERVICE_CMD="{ok}" HERDR_FLEET_PARITY_CMD="{ok}"
export HERDR_FLEET_PI_STAGE_CMD="{stage}" HERDR_FLEET_PI_SMOKE_CMD="{smoke}"
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null
exec "{sys.executable}" -I -c "{BOOT}" "$@"''')
        self.shims[host] = shim

    def mode(self, host: str, mode: str) -> None:
        (self.dir / f"{host}.mode").write_text(mode)

    def calls(self, host: str | None = None) -> list[str]:
        path = self.dir / "calls.log"
        lines = path.read_text().splitlines() if path.exists() else []
        return [ln for ln in lines if host is None or ln.startswith(host + " ")]

    def installed(self, host: str) -> str | None:
        marker = self.home(host) / "installed.marker"
        return marker.read_text().strip() if marker.exists() else None

    def checkout(self, host: str) -> Path:
        return self.home(host) / "checkout"

    @property
    def state_dir(self) -> Path:
        return self.runner_home / "state/herdr-engineering/fleet-watch"

    def env(self) -> dict:
        return {**os.environ, "HOME": str(self.runner_home), "XDG_STATE_HOME": str(self.runner_home / "state"),
                "GH_TOKEN": "t", "HERDR_FLEET_WATCH_API": self.gh.url,
                "HERDR_FLEET_WATCH_GIT_BASE": str(self.forge.root), "HERDR_FLEET_WATCH_SSH": str(self.ssh),
                "FAKE_SSH_DIR": str(self.dir), "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}

    def cli(self, *args: str, repo: str = HE, apply: bool = True, timeout: int = 180):
        argv = ["fleet-watch", "run", "--repo", repo, "--lock", str(self.lock), "--local-host", "none", *args]
        if apply:
            argv.append("--apply")
        return subprocess.run([sys.executable, "-I", "-c", BOOT, *argv], capture_output=True, text=True,
                              env=self.env(), timeout=timeout)

    def identify(self, host_id: str) -> None:
        """This machine's host.json (the runner's identity); the control plane URL is a closed port."""
        path = self.runner_home / ".config/herdr-engineering/host.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"control_plane_url": "http://127.0.0.1:1", "host_id": host_id,
                                    "token": "t" * 16}))

    def tamper(self, edit) -> None:
        saved = self.saved()
        edit(saved)
        state.save(saved, self.state_dir)

    def queued(self) -> list[dict]:
        return events.Emitter(self.state_dir, client=None).pending()

    def types(self) -> list[str]:
        return [e["event_type"] for e in self.queued()]

    def finished(self) -> dict[str, list[dict]]:
        out: dict[str, list[dict]] = {}
        for e in self.queued():
            if e["event_type"] == "host.finished":
                out.setdefault(e["payload"]["host"], []).append(e["payload"])
        return out

    def saved(self) -> dict:
        return state.load(self.state_dir)


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    forge = Forge(tmp_path / "forge")
    forge.create(HE)
    forge.create(PI)
    gh = FakeGitHub(forge)
    monkeypatch.setenv("HERDR_FLEET_WATCH_API", gh.url)
    monkeypatch.setenv("HERDR_FLEET_WATCH_GIT_BASE", str(forge.root))
    f = Fleet(tmp_path, forge, gh)
    forge.commit(HE, "two")                         # hosts were cloned at "one": they are behind
    yield f
    gh.close()


def assert_closed(payload) -> None:
    """Spec section 14 on real emitted events: nothing but enums, hex, hosts, ints, bools and UTC timestamps."""
    allowed = {*STATUSES, *REASONS, *HOST_ORDER, *LOG_KINDS, *CHECK_NAMES, "ok", "failed", "skipped",
               "autospec", "pi-engineering", "herdr-engineering", "ai-sync", "claude-config"}

    def walk(value, key=""):
        if isinstance(value, dict):
            for k, v in value.items():
                walk(v, k)
        elif isinstance(value, list):
            for v in value:
                walk(v, key)
        elif isinstance(value, str):
            if key in ("ts", "started_at", "finished_at", "merged_at"):
                assert datetime.fromisoformat(value).utcoffset().total_seconds() == 0
            else:
                assert value in allowed or HEX.fullmatch(value), f"free text in {key}: {value!r}"
    walk(payload)


# --------------------------------------------------------------------------- manual + dry-run

def test_run_without_apply_is_a_dry_run_plan_that_touches_nothing(fleet):
    out = fleet.cli(apply=False)
    assert out.returncode == 0, out.stderr
    assert "plan: rollout" in out.stdout and "dry-run" in out.stderr
    assert fleet.calls() == [] and fleet.queued() == []
    assert fleet.installed("beast") is None and fleet.saved()["rollouts"] == {}


def test_apply_rolls_out_serially_canary_first_and_converges(fleet):
    head = fleet.forge.head(HE)
    out = fleet.cli()
    assert out.returncode == 0, out.stderr
    assert "succeeded" in out.stdout
    order = [c.split()[0] for c in fleet.calls()]
    assert order == ["beast"] * 3 + ["bender"] * 3 + ["macbook-pro"] * 3          # precheck, apply, health each
    assert [c.split("--phase ")[1].split()[0] for c in fleet.calls("beast")] == ["precheck", "apply", "health"]
    for host in HOST_ORDER:
        assert fleet.installed(host) == head
        assert git(fleet.checkout(host), "rev-parse", "HEAD") == head
    assert fleet.saved()["converged"] == {HE: head}
    types = fleet.types()
    assert types[:3] == ["rollout.observed", "rollout.planned", "rollout.started"] and types[-1] == "rollout.succeeded"
    assert types[3:8] == ["host.started", "host.step", "host.step", "host.step", "host.finished"]
    assert fleet.cli().stdout.strip() == "fleet-watch run: idle"                   # converged: nothing to do again


def test_every_event_validates_and_contains_no_free_text(fleet):
    fleet.mode("bender", "ok")
    (fleet.checkout("bender") / "untracked-private-name").write_text("x")          # forces a blocked row too
    fleet.cli()
    queued = fleet.queued()
    assert queued
    for env in queued:
        validate_fleet_rollout(env["entity_id"], env["event_type"], env["payload"])
        assert_closed(env["payload"])
    assert "untracked-private-name" not in json.dumps(queued)
    bender = fleet.finished()["bender"][0]
    assert (bender["status"], bender["reason"], bender["counts"]["dirty_paths"]) == ("blocked", "dirty_checkout", 1)
    assert bender["log"]["host"] == "bender" and bender["log"]["path_id"] == "updater"


# --------------------------------------------------------------------------- halting

def test_first_failure_halts_and_later_hosts_are_skipped_halted(fleet):
    (fleet.checkout("bender") / "dirty").write_text("x")
    out = fleet.cli()
    assert out.returncode == 1 and "halted" in out.stdout
    done = fleet.finished()
    assert done["beast"][0]["status"] == "ok"
    assert (done["bender"][0]["status"], done["bender"][0]["reason"]) == ("blocked", "dirty_checkout")
    assert (done["macbook-pro"][0]["status"], done["macbook-pro"][0]["reason"]) == ("skipped", "halted")
    assert fleet.calls("macbook-pro") == []                                          # never even contacted
    halted = [e for e in fleet.queued() if e["event_type"] == "rollout.halted"][0]["payload"]
    assert halted["halted_at_host"] == "bender" and halted["reason"] == "dirty_checkout"
    assert fleet.saved()["rollouts"][halted["rollout_id"]]["status"] == "halted"
    assert fleet.saved()["converged"] == {}
    assert (fleet.checkout("bender") / "dirty").exists()                             # never cleaned


def test_a_halted_rollout_is_not_rerun_without_resume_and_resume_skips_settled_rows(fleet):
    (fleet.checkout("bender") / "dirty").write_text("x")
    fleet.cli()
    before = fleet.calls()
    again = fleet.cli()
    assert again.returncode == 4 and "--resume" in again.stderr
    assert fleet.calls() == before                                                   # nothing was touched
    (fleet.checkout("bender") / "dirty").unlink()
    resumed = fleet.cli("--resume")
    assert resumed.returncode == 0, resumed.stderr
    new = fleet.calls()[len(before):]
    assert not [c for c in new if c.startswith("beast ")]                            # beast was already done
    assert {c.split()[0] for c in new} == {"bender", "macbook-pro"}
    ev = [e["payload"] for e in fleet.queued() if e["event_type"] == "rollout.resumed"]
    assert ev and ev[0]["reason"] == "operator" and fleet.types()[-1] == "rollout.succeeded"


def test_unreachable_macbook_does_not_halt_and_is_skipped_optional(fleet):
    fleet.mode("macbook-pro", "down")
    out = fleet.cli()
    assert out.returncode == 0, out.stderr
    mac = fleet.finished()["macbook-pro"][0]
    assert (mac["status"], mac["reason"]) == ("skipped", "optional_unreachable")
    assert fleet.types()[-1] == "rollout.succeeded"
    assert fleet.saved()["converged"] == {HE: fleet.forge.head(HE)}


@pytest.mark.parametrize(("down", "ran", "skipped"), [
    ("bender", ["beast"], ["macbook-pro"]),
    ("beast", [], ["bender", "macbook-pro"]),
])
def test_unreachable_beast_or_bender_halts(fleet, down, ran, skipped):
    fleet.mode(down, "down")
    out = fleet.cli()
    assert out.returncode == 1
    done = fleet.finished()
    assert (done[down][0]["status"], done[down][0]["reason"]) == ("unreachable", "ssh_failed")
    for host in skipped:
        assert (done[host][0]["status"], done[host][0]["reason"]) == ("skipped", "halted")
        assert fleet.calls(host) == []
    halted = [e for e in fleet.queued() if e["event_type"] == "rollout.halted"][0]["payload"]
    assert halted["halted_at_host"] == down and halted["reason"] == "ssh_failed"


def test_a_timeout_is_failed_timeout_and_halts(fleet):
    fleet.mode("bender", "hang_apply")
    out = fleet.cli("--apply-timeout", "1")
    assert out.returncode == 1
    bender = fleet.finished()["bender"][0]
    assert (bender["status"], bender["reason"]) == ("failed", "timeout")
    assert fleet.finished()["macbook-pro"][0]["reason"] == "halted"


# --------------------------------------------------------------------------- crash-resume

def test_crash_mid_rollout_resumes_at_the_first_unfinished_host(fleet):
    fleet.mode("bender", "die")                       # the ssh stand-in SIGKILLs the runner while on bender
    crashed = fleet.cli()
    assert crashed.returncode == -9
    saved = fleet.saved()
    [rid] = saved["rollouts"]
    assert saved["rollouts"][rid]["status"] == "running"
    assert saved["rollouts"][rid]["rows"]["beast/herdr-engineering"]["state"] == "done"
    assert fleet.installed("beast") == fleet.forge.head(HE) and fleet.installed("bender") is None
    beast_calls = len(fleet.calls("beast"))
    fleet.mode("bender", "ok")
    resumed = fleet.cli()                             # no flag: a crash resumes by itself
    assert resumed.returncode == 0, resumed.stderr
    assert len(fleet.calls("beast")) == beast_calls   # the finished host was not touched again
    types = fleet.types()
    assert types.count("rollout.resumed") == 1 and types.count("rollout.observed") == 1
    assert "reason" not in [e["payload"] for e in fleet.queued() if e["event_type"] == "rollout.resumed"][0]
    assert types[-1] == "rollout.succeeded"
    seqs = [e["payload"]["seq"] for e in fleet.queued()]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    for host in HOST_ORDER:
        assert fleet.installed(host) == fleet.forge.head(HE)


# --------------------------------------------------------------------------- rollback (D8)

def pi_world(fleet, tmp_path_factory=None) -> tuple[str, str]:
    """pi-engineering with a known 'before' release installed and current on every host."""
    before = fleet.forge.head(PI)
    fleet.lock.write_text(f"repos:\n  pi-engineering: {before}\n")
    for host in HOST_ORDER:
        root = fleet.home(host) / ".local/share/herdr-engineering/pi"
        (root / "releases" / before / "node_modules").mkdir(parents=True)
        (root / "releases" / before / "package.json").write_text("{}")
        (root / "receipts").mkdir()
        (root / "receipts" / f"{before}.json").write_text("{}")
        os.symlink(f"releases/{before}", root / "current")
    return before, fleet.forge.commit(PI, "pi-two")


def current(fleet, host: str) -> str:
    return os.readlink(fleet.home(host) / ".local/share/herdr-engineering/pi/current").split("/")[-1]


def test_failed_health_rolls_back_the_failed_host_only_by_default(fleet):
    before, after = pi_world(fleet)
    (fleet.home("bender") / "smoke_fail").write_text("")
    out = fleet.cli(repo=PI)
    assert out.returncode == 1
    assert current(fleet, "beast") == after and current(fleet, "bender") == before
    assert current(fleet, "macbook-pro") == before
    bender = fleet.finished()["bender"]
    assert [(p["status"], p.get("reason")) for p in bender] == [("failed", "health_failed"), ("rolled_back", None)]
    assert bender[1]["after_sha"] == before
    assert fleet.finished()["macbook-pro"][0]["reason"] == "halted"
    for env in fleet.queued():
        validate_fleet_rollout(env["entity_id"], env["event_type"], env["payload"])


def test_rollback_scope_all_updated_also_flips_earlier_hosts_back(fleet):
    before, _ = pi_world(fleet)
    (fleet.home("bender") / "smoke_fail").write_text("")
    out = fleet.cli("--rollback-scope", "all_updated", repo=PI)
    assert out.returncode == 1
    assert [current(fleet, h) for h in HOST_ORDER] == [before] * 3
    assert [p["status"] for p in fleet.finished()["beast"]] == ["ok", "rolled_back"]


def test_herdr_engineering_has_no_automatic_rollback_so_the_row_stays_failed(fleet):
    (fleet.home("bender") / "install.sh").write_text("#!/bin/sh\nexit 3\n")
    out = fleet.cli()
    assert out.returncode == 1
    assert [(p["status"], p.get("reason")) for p in fleet.finished()["bender"]] == [("failed", "apply_failed")]


# --------------------------------------------------------------------------- crash-resume keeps halting

def test_crash_after_a_failed_row_was_saved_but_before_the_halt_does_not_continue(fleet):
    (fleet.checkout("beast") / "dirty").write_text("x")
    assert fleet.cli().returncode == 1                       # beast blocked(dirty_checkout); bender/mac skipped(halted)
    calls = fleet.calls()

    def crash_state(saved):                                  # the state a crash between the two saves would leave
        [rollout] = saved["rollouts"].values()
        rollout["status"] = "running"
        rollout.pop("halt", None)
        rollout.pop("halt_emitted", None)
        for key, row in rollout["rows"].items():
            if not key.startswith("beast/"):
                row.clear()
                row["state"] = "pending"
    fleet.tamper(crash_state)
    (fleet.checkout("beast") / "dirty").unlink()             # even a fixed beast must not be "retried" by a crash resume
    out = fleet.cli()                                        # no flag: a crash resumes by itself
    assert out.returncode == 1 and "halted" in out.stdout
    assert fleet.calls() == calls                            # no later host was contacted, nothing re-applied
    saved = fleet.saved()
    [rollout] = saved["rollouts"].values()
    assert rollout["status"] == "halted" and saved["converged"] == {}
    assert [r["reason"] for k, r in rollout["rows"].items() if not k.startswith("beast/")] == ["halted"] * 2
    assert fleet.installed("bender") is None and fleet.installed("macbook-pro") is None


def test_the_halt_is_persisted_together_with_the_failing_row(fleet):
    (fleet.checkout("bender") / "dirty").write_text("x")
    fleet.cli()
    [rollout] = fleet.saved()["rollouts"].values()
    assert rollout["status"] == "halted" and rollout["halt"] == {"host": "bender", "reason": "dirty_checkout"}
    assert rollout["halt_emitted"] is True


def test_a_halt_that_was_never_emitted_is_emitted_before_the_operator_resume(fleet):
    (fleet.checkout("bender") / "dirty").write_text("x")
    fleet.cli()
    seen = len(fleet.queued())
    fleet.tamper(lambda saved: next(iter(saved["rollouts"].values())).update(halt_emitted=False))
    (fleet.checkout("bender") / "dirty").unlink()
    assert fleet.cli("--resume").returncode == 0
    new = fleet.types()[seen:]
    assert new[:2] == ["rollout.halted", "rollout.resumed"] and new[-1] == "rollout.succeeded"
    halted = [e for e in fleet.queued()[seen:] if e["event_type"] == "rollout.halted"][0]["payload"]
    assert halted["halted_at_host"] == "bender" and halted["reason"] == "dirty_checkout"


# --------------------------------------------------------------------------- rollback targets the host itself

def test_a_no_op_row_is_not_rolled_back_by_a_later_failure(fleet):
    before, after = pi_world(fleet)
    root = fleet.home("beast") / ".local/share/herdr-engineering/pi"
    stage = (fleet.home("beast") / "stage.sh")
    subprocess.run(["sh", "-c", f'HERDR_FLEET_TARGET_SHA={after} sh {stage} && ln -sfn releases/{after} '
                    f'{root}/current'], check=True)                 # beast is ALREADY on the target
    (fleet.home("bender") / "smoke_fail").write_text("")
    out = fleet.cli("--rollback-scope", "all_updated", repo=PI)
    assert out.returncode == 1
    assert current(fleet, "beast") == after                          # untouched: it never changed in this rollout
    assert [p["status"] for p in fleet.finished()["beast"]] == ["ok"]
    assert current(fleet, "bender") == before


def test_rollback_returns_the_host_to_its_own_release_not_the_plan_baseline(fleet):
    before, after = pi_world(fleet)
    own = "c" * 40                                                   # bender legitimately runs a third release
    root = fleet.home("bender") / ".local/share/herdr-engineering/pi"
    (root / "releases" / own / "node_modules").mkdir(parents=True)
    (root / "releases" / own / "package.json").write_text("{}")
    (root / "receipts" / f"{own}.json").write_text("{}")
    os.replace(root / "current", root / "old-current")
    os.symlink(f"releases/{own}", root / "current")
    (fleet.home("bender") / "smoke_fail").write_text("")
    assert fleet.cli(repo=PI).returncode == 1
    assert current(fleet, "bender") == own
    assert fleet.finished()["bender"][1]["after_sha"] == own


# --------------------------------------------------------------------------- who is "local"

def test_an_explicit_local_host_that_is_not_this_machine_is_refused_before_anything_runs(fleet):
    fleet.identify("bender")
    out = fleet.cli("--local-host", "beast", "--remote-bin", str(fleet.shims["beast"]))
    assert out.returncode == 2 and "not this machine" in out.stderr
    assert fleet.calls() == [] and fleet.queued() == [] and fleet.installed("beast") is None
    assert fleet.saved()["rollouts"] == {}


def test_auto_local_host_follows_host_json_and_the_default_is_auto(fleet):
    fleet.identify("beast")
    out = fleet.cli("--local-host", "auto", "--remote-bin", str(fleet.shims["beast"]))
    assert out.returncode == 0, out.stderr
    assert fleet.calls("beast") == [] and fleet.installed("beast") == fleet.forge.head(HE)
    from herdr_engineering.fleet_watch import cli as watch_cli
    assert watch_cli.local_identity(fleet.runner_home) == "beast"


def test_auto_local_host_that_is_no_fleet_host_uses_ssh_for_everyone(fleet):
    fleet.identify("laptop")
    out = fleet.cli("--local-host", "auto")
    assert out.returncode == 0, out.stderr
    assert len(fleet.calls("beast")) == 3


# --------------------------------------------------------------------------- local host, delivery

def test_the_local_host_is_applied_without_ssh(fleet):
    fleet.identify("beast")
    out = fleet.cli("--local-host", "beast", "--remote-bin", str(fleet.shims["beast"]))
    assert out.returncode == 0, out.stderr
    assert fleet.calls("beast") == []                                                # no ssh for beast
    assert len(fleet.calls("bender")) == 3 and fleet.installed("beast") == fleet.forge.head(HE)


@pytest.fixture
def live(acknowledged_sink):
    repo = ControlPlaneRepo(sink=acknowledged_sink)
    svr = ControlPlaneServer(repo, scheduler_admission=True)
    threading.Thread(target=svr.serve, daemon=True).start()
    svr.wait_until_ready()
    yield repo, f"http://127.0.0.1:{svr.port}"
    svr.shutdown()


def test_events_reach_a_real_control_plane_in_order_through_the_outbox(fleet, live):
    repo, base = live
    report = core.run_once([HE], directory=fleet.state_dir, lock_path=fleet.lock, auth="t")
    plan = report["rollout"]
    saved = state.load(fleet.state_dir)
    emitter = events.Emitter(fleet.state_dir, client=MetadataClient(base))
    engine = run.Runner(fleet.state_dir, emitter,
                        run.SshTransport(ssh_bin=str(fleet.ssh), local_host=None))
    os.environ["FAKE_SSH_DIR"] = str(fleet.dir)
    try:
        assert engine.start(saved, plan) == "succeeded"
    finally:
        os.environ.pop("FAKE_SSH_DIR")
    stream = repo.stream("fleet_rollout", plan["rollout_id"])
    assert [e["payload"]["seq"] for e in stream] == list(range(len(stream)))
    assert stream[0]["event_type"] == "rollout.observed" and stream[-1]["event_type"] == "rollout.succeeded"
    assert emitter.pending() == []
    for e in stream:
        assert_closed({k: v for k, v in e["payload"].items() if not k.startswith("_")})


RID = "0123456789abcdef"
SHA = "a" * 40


def host_json(**over) -> str:
    return json.dumps({"schema_version": 1, "phase": "apply", "rollout_id": RID, "repo": "herdr-engineering",
                       "sha": SHA, "status": "ok", "after_sha": SHA, "no_change": False, **over})


def test_unit_parse_result_rejects_anything_outside_the_closed_vocabulary():
    ok = run.Outcome(0, host_json(no_change=True))
    assert run.parse_result(RID, "beast", HE, ok, sha=SHA, phase="apply")["no_change"] is True
    for stdout in ("not json", "[]", json.dumps({"status": "exploded"}),
                   host_json(status="failed", reason="my password is hunter2"),
                   host_json(checks=[{"name": "free text", "status": "ok"}])):
        got = run.parse_result(RID, "beast", HE, run.Outcome(1, stdout), sha=SHA, phase="apply")
        assert (got["status"], got["reason"]) == ("failed", "apply_failed")
    assert run.parse_result(RID, "beast", HE, run.Outcome(0, host_json(status="failed", reason="timeout")),
                            sha=SHA, phase="apply")["reason"] == "timeout"
    assert run.parse_result(RID, "beast", HE, run.Outcome(None, "", timed_out=True))["reason"] == "timeout"
    assert run.parse_result(RID, "beast", HE, run.Outcome(255, ""))["status"] == "unreachable"
    assert run.parse_result(RID, "beast", HE, run.Outcome(2, host_json()), sha=SHA, phase="apply")["status"] == "failed"


@pytest.mark.parametrize("override", [
    {"rollout_id": "f" * 16}, {"repo": "pi-engineering"}, {"sha": "b" * 40}, {"phase": "health"},
    {"after_sha": "b" * 40}, {"after_sha": None}, {"rollout_id": None},
])
def test_unit_parse_result_cross_checks_the_result_against_the_request(override):
    data = json.loads(host_json())
    data.update(override)
    got = run.parse_result(RID, "beast", HE, run.Outcome(0, json.dumps(data)), sha=SHA, phase="apply")
    assert (got["status"], got["reason"]) == ("failed", "apply_failed")


def test_unit_parse_result_a_skipped_without_a_reason_is_not_a_settled_row():
    bare = host_json(status="skipped", after_sha=None)
    got = run.parse_result(RID, "bender", HE, run.Outcome(0, bare), sha=SHA, phase="apply")
    assert (got["status"], got["reason"]) == ("failed", "apply_failed")
    explained = host_json(status="skipped", after_sha=None, not_applicable=True)
    assert run.parse_result(RID, "bender", HE, run.Outcome(0, explained), sha=SHA, phase="apply")["status"] == "skipped"
    assert run.parse_result(RID, "bender", HE, run.Outcome(
        0, host_json(status="skipped", reason="optional_unreachable", after_sha=None)), sha=SHA, phase="apply"
    )["status"] == "skipped"


def test_unit_transport_refuses_unsafe_arguments():
    t = run.SshTransport()
    with pytest.raises(ValueError):
        t.command(["--repo", "x; rm -rf /"])
    assert t.command(["--phase", "apply"]).startswith("cd / && exec $HOME/.local/bin/herdr-eng fleet host-apply")

"""Step 5: operator commands (`fleet-watch resume|abandon`), the `loop` service command, its watchdog, and the
systemd unit + ansible task that install it (off by default).

No mocks: the Fleet fixture of test_fleet_watch_run (real bare repos, real HTTP "GitHub", a real executable ssh
stand-in, throwaway HOMEs) drives real subprocesses. Nothing here starts or enables a real service.
"""
from __future__ import annotations

import json
import re
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

from herdr_engineering.control_plane.fleet_rollout import validate_fleet_rollout
from herdr_engineering.fleet_watch import plan, state
from tests.test_deploy_units import parse_unit
from tests.test_fleet_watch import git
from tests.test_fleet_watch_run import BOOT, HE, fleet  # noqa: F401  (fixture)

ROOT = Path(__file__).resolve().parents[1]


def op(fleet, *args: str, timeout: int = 180):
    """`herdr-eng fleet-watch <args>` in the fixture's throwaway HOME."""
    argv = ["fleet-watch", *args]
    return subprocess.run([sys.executable, "-I", "-c", BOOT, *argv], capture_output=True, text=True,
                          env=fleet.env(), timeout=timeout)


def common(fleet) -> list[str]:
    return ["--lock", str(fleet.lock), "--local-host", "none"]


def halt(fleet) -> str:
    """A halted rollout: bender's checkout is dirty."""
    (fleet.checkout("bender") / "dirty").write_text("x")
    assert fleet.cli().returncode == 1
    (rid,) = fleet.saved()["rollouts"]
    return rid


# --------------------------------------------------------------------------- planner: abandoned is terminal

def test_plan_treats_an_abandoned_rollout_like_a_finished_one_for_the_same_heads():
    targets = {"a/b": "1" * 40}
    rid = plan.rollout_id(targets)
    assert plan.plan_rollout(targets, {}, {rid: {"status": "abandoned"}}) is None
    assert plan.plan_rollout(targets, {}, {rid: {"status": "halted"}})["rollout_id"] == rid
    assert plan.plan_rollout({"a/b": "2" * 40}, {}, {rid: {"status": "abandoned"}}) is not None   # a newer head


# --------------------------------------------------------------------------- resume

def test_resume_reruns_the_halted_rollout_skipping_settled_rows(fleet):
    rid = halt(fleet)
    (fleet.checkout("bender") / "dirty").unlink()
    before = fleet.calls()
    out = op(fleet, "resume", rid, *common(fleet))
    assert out.returncode == 0, out.stderr
    assert "succeeded" in out.stdout
    new = fleet.calls()[len(before):]
    assert {c.split()[0] for c in new} == {"bender", "macbook-pro"}               # beast was already ok
    resumed = [e["payload"] for e in fleet.queued() if e["event_type"] == "rollout.resumed"]
    assert len(resumed) == 1 and resumed[0]["reason"] == "operator"
    assert fleet.saved()["rollouts"][rid]["status"] == "succeeded"
    assert fleet.saved()["converged"] == {HE: fleet.forge.head(HE)}


def test_resume_without_an_id_takes_the_one_halted_rollout(fleet):
    rid = halt(fleet)
    (fleet.checkout("bender") / "dirty").unlink()
    out = op(fleet, "resume", *common(fleet), "--json")
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == {"outcome": "succeeded", "rollout_id": rid}


def test_resume_that_halts_again_exits_1(fleet):
    rid = halt(fleet)
    out = op(fleet, "resume", rid, *common(fleet))                                  # still dirty
    assert out.returncode == 1 and "halted" in out.stdout


@pytest.mark.parametrize("args", [["0123456789abcdef"], ["not-an-id"], []])
def test_resume_refuses_an_unknown_malformed_or_absent_target_before_touching_anything(fleet, args):
    out = op(fleet, "resume", *args, *common(fleet))
    assert out.returncode == 2 and out.stderr.strip()
    assert fleet.calls() == [] and fleet.queued() == []


def test_resume_refuses_a_rollout_that_is_not_halted(fleet):
    assert fleet.cli().returncode == 0
    (rid,) = fleet.saved()["rollouts"]
    out = op(fleet, "resume", rid, *common(fleet))
    assert out.returncode == 2 and "not halted" in out.stderr


def test_resume_without_an_id_is_ambiguous_with_two_halted_rollouts(fleet):
    rid = halt(fleet)

    def clone(saved):
        saved["rollouts"]["f" * 16] = json.loads(json.dumps(saved["rollouts"][rid]))
    fleet.tamper(clone)
    out = op(fleet, "resume", *common(fleet))
    assert out.returncode == 2 and rid in out.stderr and "f" * 16 in out.stderr


def test_a_held_watcher_lock_makes_resume_exit_3(fleet):
    rid = halt(fleet)
    with state.single_instance(fleet.state_dir):
        out = op(fleet, "resume", rid, *common(fleet))
    assert out.returncode == 3 and "lock" in out.stderr


def test_the_old_resume_flag_still_works_with_a_deprecation_note(fleet):
    halt(fleet)
    (fleet.checkout("bender") / "dirty").unlink()
    out = fleet.cli("--resume")
    assert out.returncode == 0, out.stderr
    assert "deprecated" in out.stderr and "fleet-watch resume" in out.stderr
    assert "rollout.succeeded" in fleet.types()[-2:]


# --------------------------------------------------------------------------- abandon

def test_abandon_marks_the_rollout_abandoned_emits_the_event_and_touches_nothing_else(fleet):
    fleet.lock.write_text("repos:\n  herdr-engineering: " + git(fleet.forge.work[HE], "rev-parse", "HEAD~1") + "\n")
    lock_before = fleet.lock.read_text()
    rid = halt(fleet)
    calls = fleet.calls()
    out = op(fleet, "abandon", rid, *common(fleet))
    assert out.returncode == 0, out.stderr
    assert "abandoned" in out.stdout
    saved = fleet.saved()
    assert saved["rollouts"][rid]["status"] == "abandoned" and saved["converged"] == {}
    assert fleet.lock.read_text() == lock_before and fleet.calls() == calls        # no ssh, lock unchanged
    events = fleet.queued()
    assert events[-1]["event_type"] == "rollout.abandoned" and events[-1]["entity_id"] == rid
    assert events[-1]["payload"]["reason"] == "operator"
    validate_fleet_rollout(rid, "rollout.abandoned", events[-1]["payload"])


def test_after_abandon_the_same_heads_stay_idle_and_a_newer_head_rolls_out(fleet):
    rid = halt(fleet)
    assert fleet.cli().returncode == 4                                              # blocked while halted
    assert op(fleet, "abandon", rid, *common(fleet)).returncode == 0
    (fleet.checkout("bender") / "dirty").unlink()
    again = fleet.cli()
    assert again.returncode == 0 and again.stdout.strip() == "fleet-watch run: idle"
    newer = fleet.forge.commit(HE, "three")
    fresh = fleet.cli()
    assert fresh.returncode == 0, fresh.stderr
    saved = fleet.saved()
    assert saved["converged"] == {HE: newer} and saved["rollouts"][rid]["status"] == "abandoned"
    assert len(saved["rollouts"]) == 2


def test_abandon_emits_a_swallowed_halt_first_so_the_stream_stays_valid(fleet):
    rid = halt(fleet)
    fleet.tamper(lambda s: s["rollouts"][rid].update(halt_emitted=False))
    for_halt = [e for e in fleet.queued() if e["event_type"] == "rollout.halted"]
    assert len(for_halt) == 1
    assert op(fleet, "abandon", rid, *common(fleet)).returncode == 0
    types = fleet.types()
    assert types[-2:] == ["rollout.halted", "rollout.abandoned"]


@pytest.mark.parametrize("args", [["0123456789abcdef"], ["zz"], []])
def test_abandon_refuses_unknown_or_absent_targets(fleet, args):
    out = op(fleet, "abandon", *args, *common(fleet))
    assert out.returncode == 2 and fleet.queued() == []


def test_abandon_refuses_a_succeeded_rollout(fleet):
    assert fleet.cli().returncode == 0
    (rid,) = fleet.saved()["rollouts"]
    out = op(fleet, "abandon", rid, *common(fleet))
    assert out.returncode == 2 and "not halted" in out.stderr
    assert fleet.saved()["rollouts"][rid]["status"] == "succeeded"


def test_operator_commands_emit_no_free_text(fleet):
    from tests.test_fleet_watch_run import assert_closed
    rid = halt(fleet)
    op(fleet, "abandon", rid, *common(fleet))
    for env in fleet.queued():
        validate_fleet_rollout(env["entity_id"], env["event_type"], env["payload"])
        assert_closed(env["payload"])


# --------------------------------------------------------------------------- next_sleep (D1/D2 cadence)

def test_next_sleep_follows_the_poller_cadence_and_backoff():
    import random

    from herdr_engineering.fleet_watch import service
    rng = random.Random(1)
    ordinary = {"repos": {"a/x": {"outcome": "unchanged", "wait_s": 130}, "a/y": {"outcome": "unchanged", "wait_s": 101}}}
    assert service.next_sleep(ordinary, None, rng) == 101                          # the soonest repo
    low = {"repos": {"a/x": {"outcome": "unchanged", "wait_s": 600}}}
    assert service.next_sleep(low, None, rng) == 600                               # remaining < 100: stretched
    backoff = {"repos": {"a/x": {"outcome": "backoff", "retry_in_s": 400}, "a/y": {"outcome": "rate_limited", "wait_s": 900}}}
    assert service.next_sleep(backoff, None, rng) == 400
    assert service.next_sleep({"repos": {}}, None, rng) in range(100, 141)          # nothing known: 120 +- 20
    assert service.next_sleep({"repos": {}}, 60, rng) in range(50, 71)             # explicit interval keeps a jitter
    assert service.next_sleep({"repos": {"a/x": {"outcome": "error", "wait_s": 2}}}, None, rng) >= 5


# --------------------------------------------------------------------------- watchdog (soft warns, hard = no progress)

def test_watchdog_soft_limit_only_warns_and_progress_keeps_the_hard_limit_away(tmp_path):
    from herdr_engineering.fleet_watch import service
    marker = tmp_path / "state.json"
    marker.write_text("0")
    soft, hard = [], []
    dog = service.Watchdog([marker], soft_s=0.4, hard_s=1.2, on_soft=lambda s: soft.append(s),
                           on_hard=lambda s: hard.append(s), tick_s=0.05)
    dog.start()
    try:
        with dog.guard():
            for n in range(12):                       # a 2.4 s "healthy" pass that keeps making progress
                time.sleep(0.2)
                marker.write_text(str(n))
        assert len(soft) == 1 and hard == []           # warned once, never killed
    finally:
        dog.stop()


def test_watchdog_hard_limit_fires_only_after_no_progress_and_not_while_disarmed(tmp_path):
    from herdr_engineering.fleet_watch import service
    marker = tmp_path / "state.json"
    marker.write_text("0")
    hard = []
    dog = service.Watchdog([marker], soft_s=0.2, hard_s=0.6, on_soft=lambda s: None,
                           on_hard=lambda s: hard.append(s), tick_s=0.05)
    dog.start()
    try:
        time.sleep(1.0)
        assert hard == []                             # not armed: an idle sleep is never a stall
        with dog.guard():
            deadline = time.monotonic() + 5
            while not hard and time.monotonic() < deadline:
                time.sleep(0.05)
        assert len(hard) == 1 and hard[0] >= 0.6
    finally:
        dog.stop()


# --------------------------------------------------------------------------- loop command

def loop(fleet, *args: str, timeout: int = 180):
    return op(fleet, "loop", *common(fleet), "--repo", HE, "--interval", "1", *args, timeout=timeout)


def test_loop_without_the_enable_flag_only_plans_and_touches_no_host(fleet):
    out = loop(fleet, "--max-passes", "2")
    assert out.returncode == 0, out.stderr
    assert out.stdout.count("pass ") == 2 and "plan only" in out.stdout
    assert fleet.calls() == [] and fleet.queued() == []
    assert fleet.saved()["rollouts"] == {}                                         # nothing was even registered
    assert fleet.saved()["repos"][HE]["head"] == fleet.forge.head(HE)              # but it did poll (ETag cache kept)


def test_loop_with_enable_rollouts_applies_once_then_idles(fleet):
    out = loop(fleet, "--enable-rollouts", "--max-passes", "2")
    assert out.returncode == 0, out.stderr
    assert fleet.saved()["converged"] == {HE: fleet.forge.head(HE)}
    assert fleet.types().count("rollout.succeeded") == 1
    assert len(fleet.calls()) == 9                                                 # the second pass ran nothing
    assert "succeeded" in out.stdout and "idle" in out.stdout


def test_loop_keeps_a_halted_rollout_halted_and_never_resumes_it_by_itself(fleet):
    (fleet.checkout("bender") / "dirty").write_text("x")
    out = loop(fleet, "--enable-rollouts", "--max-passes", "3")
    assert out.returncode == 0, out.stderr
    assert "halted" in out.stdout and "needs_resume" in out.stdout
    assert fleet.types().count("rollout.halted") == 1 and "rollout.resumed" not in fleet.types()
    assert len(fleet.calls()) == 4                                                 # beast x3, bender's blocked precheck: no retries


def test_loop_picks_up_a_new_merge_while_running(fleet):
    assert loop(fleet, "--enable-rollouts", "--max-passes", "1").returncode == 0
    fleet.forge.commit(HE, "three")
    out = loop(fleet, "--enable-rollouts", "--max-passes", "1")
    assert out.returncode == 0 and len(fleet.saved()["rollouts"]) == 2


def test_two_loops_cannot_run_at_once(fleet):
    with state.single_instance(fleet.state_dir, name="loop.lock"):
        out = loop(fleet, "--max-passes", "1")
    assert out.returncode == 3 and "already running" in out.stderr


def test_loop_survives_a_failing_pass_and_reports_only_the_error_class(fleet):
    fleet.gh.mode = "down"                                                         # GitHub answers 500
    out = loop(fleet, "--enable-rollouts", "--max-passes", "2")
    assert out.returncode == 0 and fleet.calls() == []
    assert "traceback" not in out.stderr.lower()


def test_loop_exits_70_when_a_pass_makes_no_progress_past_the_hard_limit(fleet):
    fleet.mode("beast", "hang")                                                    # the precheck call never answers
    started = time.monotonic()
    out = loop(fleet, "--enable-rollouts", "--max-passes", "1", "--soft-limit", "1", "--hard-limit", "3")
    assert out.returncode == 70, (out.returncode, out.stderr)
    assert time.monotonic() - started < 40
    assert "soft limit" in out.stderr and "no progress" in out.stderr
    (rid,) = fleet.saved()["rollouts"]
    assert fleet.saved()["rollouts"][rid]["status"] == "running"                   # durable: the next start resumes it


def test_a_slow_but_progressing_rollout_is_not_killed_by_the_hard_limit(fleet, tmp_path):
    slow = tmp_path / "slow-ssh"
    slow.write_text(f'#!/bin/sh\nsleep 1.2\nexec {shlex.quote(str(fleet.ssh))} "$@"\n')
    slow.chmod(0o755)
    env_ssh = {"HERDR_FLEET_WATCH_SSH": str(slow)}
    argv = [sys.executable, "-I", "-c", BOOT, "fleet-watch", "loop", *common(fleet), "--repo", HE, "--interval", "1",
            "--enable-rollouts", "--max-passes", "1", "--soft-limit", "2", "--hard-limit", "4"]
    out = subprocess.run(argv, capture_output=True, text=True, env={**fleet.env(), **env_ssh}, timeout=120)
    assert out.returncode == 0, out.stderr                                         # ~11 s total, > the 4 s hard limit
    assert "soft limit" in out.stderr and "no progress" not in out.stderr
    assert fleet.saved()["converged"] == {HE: fleet.forge.head(HE)}


def test_loop_stops_cleanly_on_sigterm_between_passes(fleet):
    argv = [sys.executable, "-I", "-c", BOOT, "fleet-watch", "loop", *common(fleet), "--repo", HE, "--interval", "30"]
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=fleet.env())
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and "head" not in json.dumps(fleet.saved()["repos"]):
            time.sleep(0.2)
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=20) in (0, -signal.SIGTERM)
    finally:
        proc.kill()
        proc.communicate()


# --------------------------------------------------------------------------- systemd unit and ansible task

UNIT = ROOT / "scripts" / "herdr-eng-fleet-watch.service"
TASKS = ROOT / "ansible" / "tasks" / "fleet-watch.yml"


def test_unit_runs_the_installed_build_from_a_neutral_cwd_and_only_plans_by_default():
    sections = parse_unit(UNIT)
    service = dict(sections["Service"])
    argv = shlex.split(service["ExecStart"])
    assert argv[0] == "%h/.local/bin/herdr-eng" and argv[1:3] == ["fleet-watch", "loop"]
    assert "python" not in service["ExecStart"] and "-m" not in argv                 # never a checkout (shadowed(), #585)
    assert "--enable-rollouts" not in argv                                           # applying is a separate opt-in
    assert service["WorkingDirectory"] == "/"
    assert service["Restart"] == "on-failure" and int(service["RestartSec"].rstrip("s")) >= 10
    assert service["UMask"] == "0077" and service["NoNewPrivileges"] == "true"
    assert "Environment" not in service and "EnvironmentFile" not in service
    assert dict(sections["Install"])["WantedBy"] == "default.target"
    assert "RuntimeMaxSec" not in service and "TimeoutStartSec" not in service       # no wall-clock cap on healthy runs


def test_the_unit_limits_match_the_cli_defaults():
    from herdr_engineering.fleet_watch import service
    assert service.SOFT_LIMIT_S < service.HARD_LIMIT_S
    assert service.HARD_LIMIT_S > 1800 + 120                                         # longer than one apply + health call


def block():
    return yaml.safe_load(TASKS.read_text())[0]


def test_tasks_install_the_unit_but_enable_only_when_fleet_watch_enabled_on_beast():
    b = block()
    assert b["when"] == "ansible_facts['system'] == 'Linux'" and b["become"] is False
    assert b["environment"]["XDG_RUNTIME_DIR"] == "/run/user/{{ ansible_user_uid }}"
    wanted = b["vars"]["watch_wanted"]
    assert "fleet_watch_enabled | default(false) | bool" in wanted and "inventory_hostname == 'beast'" in wanted
    service = [t["ansible.builtin.systemd_service"] for t in b["block"] if "ansible.builtin.systemd_service" in t][0]
    assert service["name"] == "herdr-eng-fleet-watch.service" and service["scope"] == "user"
    assert "watch_wanted" in service["enabled"] and "stopped" in service["state"]
    copies = [t["ansible.builtin.copy"] for t in b["block"] if "ansible.builtin.copy" in t]
    assert any(c["dest"].endswith("/systemd/user/herdr-eng-fleet-watch.service") for c in copies)


def test_rollouts_need_their_own_opt_in_through_a_drop_in():
    text = TASKS.read_text()
    assert "fleet_watch_apply | default(false) | bool" in text
    assert "--enable-rollouts" in text and "herdr-eng-fleet-watch.service.d" in text
    assert "state: absent" in text or "'absent'" in text                             # removed again when not wanted


@pytest.mark.parametrize("playbook", ["fleet-sync.yml", "site.yml"])
def test_playbooks_converge_the_watcher_unit_after_the_fabric_agent(playbook):
    tasks = yaml.safe_load((ROOT / "ansible/playbooks" / playbook).read_text())[0]["tasks"]
    includes = [t.get("ansible.builtin.include_tasks") for t in tasks]
    assert "../tasks/fleet-watch.yml" in includes
    assert includes.index("../tasks/fabric-agent.yml") < includes.index("../tasks/fleet-watch.yml")


def test_no_default_turns_the_watcher_on():
    for path in list((ROOT / "ansible").rglob("*.yml")) + list((ROOT / "ansible").rglob("*.yaml")):
        text = path.read_text()
        assert not re.search(r"^\s*fleet_watch_(enabled|apply):\s*true", text, re.M), path



def test_the_final_task_starts_and_restarts_only_when_a_file_changed():
    b = block()["block"]
    copies = [t for t in b if "ansible.builtin.copy" in t]
    assert copies and all(t.get("register") for t in copies)
    svc = [t for t in b if "ansible.builtin.systemd_service" in t][0]
    assert "restarted" not in svc["ansible.builtin.systemd_service"]["state"]
    restart = [t for t in b if "restarted" in str(t.get("ansible.builtin.systemd_service", {}).get("state", ""))]
    assert restart and all("changed" in str(t["when"]) for t in restart)


def test_unit_does_not_restart_on_refusal_or_busy_and_wants_the_network():
    unit = parse_unit(UNIT)
    assert dict(unit["Service"])["RestartPreventExitStatus"] == "2 3"
    assert dict(unit["Unit"])["Wants"] == "network-online.target"

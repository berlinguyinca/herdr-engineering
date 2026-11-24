"""The autopilot stands down for Herdr-managed sessions and only for them.

Rule under test: it skips (`herdr_controller_managed`) when HERDR_PANE_ID is set and non-empty, the controller plugin is
linked (or a controller runs), and the controller is not switched off for that pane. Every other case behaves as before, and
every error while deciding leaves the autopilot working. Real env dicts, real files, a real executable standing in for the
`herdr` binary (a script that prints a plugin list), a real controller lock, the real CLI in a subprocess; nothing here can
reach a real Herdr because HERDR_BIN always names a file under tmp_path.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from herdr_engineering import handoff_autopilot as ha
from herdr_engineering.handoff_controller.run import ControllerLock
from herdr_engineering.handoff_controller.switch import set_enabled
from tests.test_handoff_autopilot import write_transcript

CLI = [sys.executable, "-m", "herdr_engineering.cli"]
CODE = "herdr_controller_managed"
PANE = "w1:p1"
CONTROLLER_ID = "herdr.handoff-controller"


def plugin_list(*plugins: tuple[str, bool]) -> str:
    return json.dumps({"result": {"plugins": [{"plugin_id": i, "plugin_root": f"/x/{i}", "enabled": e} for i, e in plugins]}})


def fake_herdr(directory: Path, stdout: str, *, exit_code: int = 0) -> Path:
    """An executable that answers `herdr plugin list --json` with `stdout`."""
    directory.mkdir(parents=True, exist_ok=True)
    exe = directory / "herdr"
    (directory / "answer.json").write_text(stdout)
    exe.write_text(f"#!/bin/sh\necho called >> '{directory / 'calls'}'\ncat '{directory / 'answer.json'}'\nexit {exit_code}\n")
    exe.chmod(0o755)
    return exe


@pytest.fixture()
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict]:
    home = tmp_path / "home"
    state = tmp_path / "state"
    work = tmp_path / "work"
    for path in (home, state, work):
        path.mkdir(parents=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith("HERDR")}
    env.update({"HOME": str(home), "XDG_STATE_HOME": str(state), "XDG_CONFIG_HOME": str(home / ".config"),
                "XDG_DATA_HOME": str(home / ".local" / "share"), "HERDR_ENGINEERING_STATE": str(tmp_path / "eng-state")})
    for name in [k for k in os.environ if k.startswith("HERDR")]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    locks: list[ControllerLock] = []
    yield {"tmp": tmp_path, "home": home, "work": work, "env": env, "state_dir": tmp_path / "eng-state", "locks": locks}
    for lock in locks:
        lock.release()


def herdr_calls(rig: dict) -> bool:
    """Whether the `herdr` stand-in was ever run."""
    return (rig["tmp"] / "bin" / "calls").exists()


def managed_env(rig: dict, *, linked: bool = True, running: bool = True) -> dict[str, str]:
    """A Herdr pane whose controller plugin is `linked` and whose controller is `running` (holds the real lock)."""
    herdr = fake_herdr(rig["tmp"] / "bin", plugin_list((CONTROLLER_ID, True)) if linked else plugin_list())
    if running:
        lock = ControllerLock(rig["state_dir"])
        assert lock.acquire()
        rig["locks"].append(lock)
    return {**rig["env"], "HERDR_PANE_ID": PANE, "HERDR_BIN": str(herdr)}


def decide(rig: dict, env: dict[str, str], session: str = "s-1") -> ha.Decision:
    return ha.decide(session_id=session, harness="pi", percent=90.0, home=rig["home"], env=env)


def test_a_running_and_enabled_controller_in_a_herdr_pane_takes_over(rig: dict) -> None:
    decision = decide(rig, managed_env(rig))
    assert decision.trigger is False and decision.code == CODE
    assert PANE in decision.reason and "outside Herdr" in decision.reason


def test_a_running_controller_counts_even_when_herdr_cannot_be_asked(rig: dict) -> None:
    env = {**rig["env"], "HERDR_PANE_ID": PANE, "HERDR_BIN": str(rig["tmp"] / "no-such-herdr")}
    managed_env(rig)   # takes the controller lock
    assert decide(rig, env).code == CODE


def test_a_linked_controller_that_is_not_running_leaves_the_autopilot_in_charge(rig: dict) -> None:
    """Right after install, or after a crash, the plugin is linked but nothing runs: neither policy may be silent."""
    decision = decide(rig, managed_env(rig, running=False))
    assert decision.trigger is True and decision.code == "context_is_full"
    assert herdr_calls(rig), "the plugin list was consulted and said linked"


@pytest.mark.parametrize("pane", [None, "", "   "])
def test_without_a_pane_id_the_autopilot_behaves_as_before(rig: dict, pane: str | None) -> None:
    env = managed_env(rig)
    env.pop("HERDR_PANE_ID")
    if pane is not None:
        env["HERDR_PANE_ID"] = pane
    decision = decide(rig, env)
    assert decision.trigger is True and decision.code == "context_is_full"


def test_an_unlinked_controller_leaves_the_autopilot_in_charge(rig: dict) -> None:
    decision = decide(rig, managed_env(rig, linked=False, running=False))
    assert decision.trigger is True and decision.code == "context_is_full"


def test_a_disabled_controller_plugin_leaves_the_autopilot_in_charge(rig: dict) -> None:
    herdr = fake_herdr(rig["tmp"] / "bin", plugin_list((CONTROLLER_ID, False)))   # and no controller runs
    assert decide(rig, {**rig["env"], "HERDR_PANE_ID": PANE, "HERDR_BIN": str(herdr)}).trigger is True


def test_a_controller_switched_off_for_this_pane_only_leaves_the_autopilot_in_charge(rig: dict) -> None:
    env = managed_env(rig)
    set_enabled(PANE, False, rig["state_dir"])
    assert decide(rig, env).code == "context_is_full"
    assert decide(rig, {**env, "HERDR_PANE_ID": "w1:p2"}).code == CODE   # another pane is still managed
    set_enabled(PANE, True, rig["state_dir"])
    assert decide(rig, env).code == CODE


def test_a_controller_switched_off_globally_leaves_the_autopilot_in_charge(rig: dict) -> None:
    assert decide(rig, {**managed_env(rig), "HERDR_HANDOFF_AUTO": "0"}).code == "context_is_full"


@pytest.mark.parametrize("answer,exit_code", [("not json at all", 0), ("", 1), (json.dumps({"result": 3}), 0)])
def test_any_trouble_deciding_fails_towards_the_autopilot(rig: dict, answer: str, exit_code: int) -> None:
    # no controller runs, so Herdr's answer decides, and it is unusable
    herdr = fake_herdr(rig["tmp"] / "bin", answer, exit_code=exit_code)
    assert decide(rig, {**rig["env"], "HERDR_PANE_ID": PANE, "HERDR_BIN": str(herdr)}).trigger is True
    assert decide(rig, {**rig["env"], "HERDR_PANE_ID": PANE, "HERDR_BIN": str(rig["tmp"] / "absent")}).trigger is True


def test_an_unreadable_state_path_fails_towards_the_autopilot(rig: dict) -> None:
    blocked = rig["tmp"] / "blocked"
    blocked.write_text("a file, not a directory")
    env = {**managed_env(rig), "HERDR_ENGINEERING_STATE": str(blocked / "state")}
    assert decide(rig, env).code == "context_is_full"   # the switch file cannot be read: the autopilot stays in charge
    assert ha.herdr_controller_reason({**env, "HERDR_PANE_ID": object()}) is None  # type: ignore[dict-item]


def test_below_the_threshold_herdr_is_not_asked_and_nothing_is_written(rig: dict) -> None:
    env = managed_env(rig)
    low = ha.decide(session_id="s-low", harness="pi", percent=10.0, home=rig["home"], env=env)
    assert low.code == "below_threshold"
    assert not herdr_calls(rig)
    transcript = write_transcript(rig["work"] / "low.jsonl", session_id="s-low", input_tokens=2, cache_write=0, cache_read=1_000)
    payload = json.dumps({"session_id": "s-low", "hook_event_name": "Stop", "transcript_path": str(transcript), "cwd": str(rig["work"])})
    done = cli(rig, env, "handoff", "autopilot", "hook", "--harness", "claude", "--record", stdin=payload)
    assert done.returncode == 0 and json.loads(done.stdout)["code"] == "below_threshold"
    assert not herdr_calls(rig) and not ha.state_path(rig["home"]).exists()


def test_the_other_reasons_still_win_when_the_autopilot_is_off(rig: dict) -> None:
    decision = decide(rig, {**managed_env(rig), "HERDR_HANDOFF_AUTOPILOT": "off"})
    assert decision.code == "disabled"


# ------------------------------------------------------------- CLI: check, status, hook, ledger, doctor


def cli(rig: dict, env: dict[str, str], *args: str, stdin: str = "") -> subprocess.CompletedProcess:
    return subprocess.run(CLI + list(args), input=stdin, capture_output=True, text=True, timeout=120, env=env, cwd=str(rig["work"]))


def test_check_json_and_status_show_the_reason(rig: dict) -> None:
    env = managed_env(rig)
    check = cli(rig, env, "handoff", "autopilot", "check", "--harness", "pi", "--session-id", "s-cli", "--percent", "90", "--json")
    assert check.returncode == 0, check.stderr
    verdict = json.loads(check.stdout)
    assert verdict["code"] == CODE and verdict["trigger"] is False and PANE in verdict["reason"]

    status = json.loads(cli(rig, env, "handoff", "autopilot", "status", "--json").stdout)
    assert status["herdr_controller"] == {"managed": True, "code": CODE, "reason": verdict["reason"]}
    assert "stands down" in status["summary"]

    outside = json.loads(cli(rig, rig["env"], "handoff", "autopilot", "status", "--json").stdout)
    assert outside["herdr_controller"]["managed"] is False and "armed" in outside["summary"]


def test_the_hook_skips_exits_zero_and_leaves_the_reason_in_the_ledger_once(rig: dict) -> None:
    env = managed_env(rig)
    transcript = write_transcript(rig["work"] / "t.jsonl", session_id="s-hook", input_tokens=2, cache_write=3_000, cache_read=150_000)
    payload = json.dumps({"session_id": "s-hook", "hook_event_name": "Stop", "transcript_path": str(transcript), "cwd": str(rig["work"])})
    for _ in range(2):
        done = cli(rig, env, "handoff", "autopilot", "hook", "--harness", "claude", "--record", stdin=payload)
        assert done.returncode == 0, done.stderr
        out = json.loads(done.stdout)
        assert out["code"] == CODE and out["trigger"] is False and "hookSpecificOutput" not in out
    entry = ha.read_ledger(home=rig["home"])["sessions"]["s-hook"]
    assert entry["skipped"]["code"] == CODE and PANE in entry["skipped"]["reason"]
    assert entry.get("count", 0) == 0, "a skip is not a trigger"
    before = ha.state_path(rig["home"]).read_text()
    assert cli(rig, env, "handoff", "autopilot", "hook", "--harness", "claude", "--record", stdin=payload).returncode == 0
    assert ha.state_path(rig["home"]).read_text() == before, "the same skip is not written again"

    # Outside Herdr the same session is told, as before.
    told = json.loads(cli(rig, rig["env"], "handoff", "autopilot", "hook", "--harness", "claude", "--record", stdin=payload).stdout)
    assert told["trigger"] is True and told["code"] == "context_is_full"


@pytest.mark.parametrize("answer,exit_code", [("garbage", 0), ("", 3)])
def test_the_hook_exits_zero_and_still_triggers_when_the_managed_check_fails(rig: dict, answer: str, exit_code: int) -> None:
    herdr = fake_herdr(rig["tmp"] / "bin", answer, exit_code=exit_code)
    env = {**rig["env"], "HERDR_PANE_ID": PANE, "HERDR_BIN": str(herdr)}
    transcript = write_transcript(rig["work"] / "t.jsonl", session_id="s-bad", input_tokens=2, cache_write=3_000, cache_read=150_000)
    payload = json.dumps({"session_id": "s-bad", "hook_event_name": "Stop", "transcript_path": str(transcript), "cwd": str(rig["work"])})
    done = cli(rig, env, "handoff", "autopilot", "hook", "--harness", "claude", stdin=payload)
    assert done.returncode == 0, done.stderr
    assert herdr_calls(rig) and json.loads(done.stdout)["code"] == "context_is_full"


def test_doctor_stays_ok_when_the_autopilot_stands_down(rig: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    env = managed_env(rig)
    for key in ("HERDR_PANE_ID", "HERDR_BIN"):
        monkeypatch.setenv(key, env[key])
    monkeypatch.setenv("HERDR_ENGINEERING_STATE", env["HERDR_ENGINEERING_STATE"])
    check = ha.doctor_check(home=rig["home"])   # nothing installed: outside Herdr this is a warning
    assert check["status"] == "ok" and CODE in json.dumps(check["data"]) and "stands down" in check["detail"]
    monkeypatch.delenv("HERDR_PANE_ID")
    assert ha.doctor_check(home=rig["home"])["status"] == "warn"

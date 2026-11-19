"""Creation that is bounded and non-blocking, one environment for controller and runner, and a light load on Herdr."""
from __future__ import annotations

import os
import shlex
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from herdr_engineering.handoff_controller import run as ctlrun
from herdr_engineering.side_pane import app, state
from herdr_engineering.side_pane import placement as placing
from herdr_engineering.side_pane.herdr import SideHerdr
from herdr_engineering.side_pane.placement import DEFAULT_COMMAND, Outcome, runner_command
from herdr_engineering.side_pane.schedule import Backoff
from tests.handoff_controller_herdr import IsolatedHerdr, isolated_herdr, wait_for
from tests.test_side_pane_ensure import SLEEP, env_of, make, panes_in, root_of, session, side_of

herdr_server = pytest.mark.herdr_server


def executable(path: Path, body: str) -> Path:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def passthrough(tmp: Path, h: IsolatedHerdr, log: Path, *, swallow_split: str | None = None) -> SideHerdr:
    """The real herdr behind a script that logs every call; `swallow_split` makes `pane split` print that instead of its reply
    ("run" runs the real split and hides its reply; "skip" does not split at all)."""
    real = shlex.quote(str(h.cli._binary))
    split = {None: f'exec {real} "$@"', "run": f'{real} "$@" >/dev/null; echo "not json"; exit 0',
             "skip": 'echo "not json"; exit 0'}[swallow_split]
    script = executable(tmp / "herdr-logging", f'echo "$*" >> {shlex.quote(str(log))}\n'
                        f'if [ "$1" = pane ] && [ "$2" = split ]; then {split}; fi\nexec {real} "$@"\n')
    return SideHerdr(str(script), h.env, h.root)


# ------------------------------------------------------------------------------------------------ the runner's environment
def test_the_runner_command_carries_the_state_and_store_paths_when_set(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    exe = executable(bin_dir / "herdr-eng", "exit 0\n")
    plain = runner_command(DEFAULT_COMMAND, {"PATH": str(bin_dir)})
    assert plain == f"exec {shlex.quote(str(exe))} side-pane run"
    env = {"PATH": str(bin_dir), "HERDR_ENGINEERING_STATE": "/a state", "HERDR_HANDOFF_STORE": "/the store", "HOME": "/h"}
    command = runner_command(DEFAULT_COMMAND, env)
    assert command is not None
    words = shlex.split(command)
    assert words[:2] == ["exec", "env"] and {"HERDR_ENGINEERING_STATE=/a state", "HERDR_HANDOFF_STORE=/the store",
                                              "HOME=/h"} <= set(words)
    assert words[-3:] == [str(exe), "side-pane", "run"]
    assert runner_command("exec custom", env) == "exec custom"  # a command given by hand is used as it is


# ------------------------------------------------------------------------------------------------------------ the budget
@herdr_server
def test_an_exhausted_budget_is_unavailable_and_leaves_nothing_behind() -> None:
    with isolated_herdr("sp-budget-out") as h:
        side = side_of(h)
        _, tab = session(h, side)
        before = panes_in(side, tab)
        result = make(h, side, tab, budget_s=0.001, lock_timeout_s=0)
        assert result.outcome is Outcome.UNAVAILABLE
        assert panes_in(side, tab) == before and state.read_marker(root_of(h), tab) is None


@herdr_server
def test_a_budgeted_call_still_creates_the_pane_and_stays_within_the_budget() -> None:
    with isolated_herdr("sp-budget-ok") as h:
        side = side_of(h)
        _, tab = session(h, side)
        started = time.monotonic()
        result = make(h, side, tab, budget_s=5.0, lock_timeout_s=0)
        assert result.outcome is Outcome.CREATED and time.monotonic() - started < 6.5
        assert state.read_marker(root_of(h), tab) is not None


def test_a_held_lock_is_locked_at_once_when_the_caller_will_not_wait(tmp_path: Path) -> None:
    root = state.state_dir({"HERDR_ENGINEERING_STATE": str(tmp_path)})
    with state.tab_lock(root, "w1:t1", 0) as held:
        assert held
        started = time.monotonic()
        result = placing.ensure("w1:t1", None, SideHerdr(binary=str(tmp_path / "none")), {"HERDR_ENGINEERING_STATE": str(tmp_path)},
                                lock_timeout_s=0, budget_s=5.0)
    assert result.outcome is Outcome.LOCKED and time.monotonic() - started < 1.0


@herdr_server
def test_an_unparsable_split_reply_finds_the_new_pane_in_the_layout() -> None:
    with isolated_herdr("sp-split-garbled") as h:
        side = side_of(h)
        first, tab = session(h, side)
        garbled = passthrough(h.root, h, h.root / "calls.log", swallow_split="run")
        result = placing.ensure(tab, None, garbled, env_of(h), command=SLEEP)
        now = panes_in(side, tab)
        assert result.outcome is Outcome.CREATED and len(now) == 2 and result.pane_id in now and result.pane_id != first
        marker = state.read_marker(root_of(h), tab)
        assert marker is not None and marker.pane_id == result.pane_id


@herdr_server
def test_an_unparsable_reply_with_no_new_pane_closes_nothing_and_says_so(capsys: pytest.CaptureFixture[str]) -> None:
    with isolated_herdr("sp-split-nothing") as h:
        side = side_of(h)
        _, tab = session(h, side)
        before = panes_in(side, tab)
        garbled = passthrough(h.root, h, h.root / "calls.log", swallow_split="skip")
        result = placing.ensure(tab, None, garbled, env_of(h), command=SLEEP)
        assert result.outcome is Outcome.UNAVAILABLE and panes_in(side, tab) == before
        assert not any(line.startswith("pane close") for line in (h.root / "calls.log").read_text().splitlines())
        assert "split" in capsys.readouterr().err


# --------------------------------------------------------------------------------------------- one environment end to end
@herdr_server
def test_the_runner_registers_its_marker_in_the_state_dir_the_controller_uses(tmp_path: Path) -> None:
    bin_dir = Path(sys.executable).parent
    if not (bin_dir / "herdr-eng").exists():
        pytest.skip("herdr-eng is not installed next to this interpreter")
    with isolated_herdr("sp-env") as h:
        side = side_of(h)
        _, tab = session(h, side)
        custom = tmp_path / "elsewhere"
        env = {**env_of(h), "HERDR_ENGINEERING_STATE": str(custom), "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
               "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
        first = placing.ensure(tab, None, side, env)
        assert first.outcome is Outcome.CREATED and first.pane_id
        directory = state.state_dir(env)
        assert directory.is_relative_to(custom)

        def runner_registered() -> bool:
            marker = state.read_marker(directory, tab)
            return marker is not None and marker.pane_id == first.pane_id and marker.pid is not None

        assert wait_for(runner_registered, 40.0), "the runner wrote its marker somewhere else"
        again = placing.ensure(tab, None, side, env)
        assert again.outcome is Outcome.EXISTS and first.pane_id in panes_in(side, tab)  # not closed as stale


# ---------------------------------------------------------------------------------------- what the controller does with it
def test_the_controller_hook_has_no_unbounded_fallback_call() -> None:
    import inspect
    source = inspect.getsource(ctlrun.default_ensure_side_pane)
    assert "TypeError" not in source and source.count("module.ensure(") == 1 and "budget_s" in source


def test_the_controller_raises_for_an_outcome_the_retry_logic_should_see(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))  # no herdr: ensure answers UNAVAILABLE
    monkeypatch.setenv("HERDR_ENGINEERING_STATE", str(tmp_path / "state"))
    with pytest.raises(ctlrun.SidePaneUnavailable):
        ctlrun.default_ensure_side_pane("w1:t1")


def test_the_controller_treats_a_held_lock_as_retryable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERDR_ENGINEERING_STATE", str(tmp_path))
    monkeypatch.setenv("PATH", str(tmp_path))  # whatever happens, no real `herdr` is run
    root = state.state_dir(os.environ)
    with state.tab_lock(root, "w1:t1", 0) as held:
        assert held
        started = time.monotonic()
        with pytest.raises(ctlrun.SidePaneUnavailable):
            ctlrun.default_ensure_side_pane("w1:t1")
    assert time.monotonic() - started < 2.0


# ---------------------------------------------------------------------------------------------------------- the load
def test_the_refresh_backs_off_to_five_seconds_after_a_quiet_minute() -> None:
    backoff = Backoff(fast_s=2.0, slow_s=5.0, quiet_s=60.0)
    assert backoff.interval_s == 2.0
    backoff.observe(100.0, changed=True)
    backoff.observe(150.0, changed=False)
    assert backoff.interval_s == 2.0
    backoff.observe(161.0, changed=False)
    assert backoff.interval_s == 5.0
    backoff.observe(170.0, changed=True)
    assert backoff.interval_s == 2.0
    backoff.observe(90.0, changed=False)  # a clock set back counts as a change
    assert backoff.interval_s == 2.0


def test_is_marked_asks_ps_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log = tmp_path / "ps.log"
    real = subprocess.run(["which", "ps"], capture_output=True, text=True, check=True).stdout.strip()
    executable(tmp_path / "ps", f'echo x >> {shlex.quote(str(log))}\nexec {shlex.quote(real)} "$@"\n')
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    from herdr_engineering.handoff_controller.herdr_cli import AgentPane
    root = tmp_path / "state"
    start = state.process_start(os.getpid())
    log.unlink(missing_ok=True)
    state.write_marker(root, state.Marker("w1:t1", "w1:p2", "term-2", "term-1", os.getpid(), time.time(), start))
    assert state.is_marked(root, AgentPane("w1:p2", "w1:t1", None, None, None, None), time.time(), "term-2")
    assert len(log.read_text().splitlines()) == 1


@herdr_server
def test_a_session_in_a_quiet_tab_costs_few_herdr_calls(tmp_path: Path) -> None:
    with isolated_herdr("sp-load") as h:
        side = side_of(h)
        pane, tab = session(h, side)
        log = h.root / "calls.log"
        counting = passthrough(h.root, h, log)
        view = app.PaneApp(tab, None, counting, env_of(h), fetch_ci=None, store_root=tmp_path / "store")
        now = time.time()
        view.refresh(now)
        log.unlink(missing_ok=True)
        for n in range(1, 6):  # five more refreshes inside ten seconds
            view.refresh(now + n * 2.0)
        calls = log.read_text().splitlines() if log.exists() else []
        assert sum(c.startswith("agent list") for c in calls) == 5
        assert not any(c.startswith("pane layout") for c in calls), calls  # one session: focus cannot matter
        assert sum(c.startswith("pane list") for c in calls) <= 1, calls
        assert pane


# ------------------------------------------------------------------------------------ a half-made pane is cleaned up or kept
def failing_run(tmp: Path, h: IsolatedHerdr, *, run_sleeps_s: float = 0.0, close_fails: bool = False) -> SideHerdr:
    """The real herdr behind a script whose `pane run` fails (after `run_sleeps_s`), and whose `pane close` fails when asked."""
    real = shlex.quote(str(h.cli._binary))
    close = "exit 1" if close_fails else f'exec {real} "$@"'
    lines = ['if [ "$1" = pane ] && [ "$2" = run ]; then sleep ' + str(run_sleeps_s) + '; exit 1; fi',
             'if [ "$1" = pane ] && [ "$2" = close ]; then ' + close + '; fi', f'exec {real} "$@"']
    script = executable(tmp / "herdr-failing-run", "\n".join(lines) + "\n")
    return SideHerdr(str(script), h.env, h.root)


@herdr_server
def test_a_split_that_ran_out_of_time_is_closed_with_its_own_allowance_and_the_marker_cleared() -> None:
    with isolated_herdr("sp-cleanup-ok") as h:
        side = side_of(h)
        _, tab = session(h, side)
        before = panes_in(side, tab)
        slow = failing_run(h.root, h, run_sleeps_s=2.5)
        result = make(h, slow, tab, budget_s=2.0, lock_timeout_s=0)  # `pane run` outlives the budget: the close is outside it
        assert result.outcome is Outcome.UNAVAILABLE
        assert wait_for(lambda: panes_in(side, tab) == before, 10.0), panes_in(side, tab)
        assert state.read_marker(root_of(h), tab) is None


@herdr_server
def test_an_unclosable_half_made_pane_keeps_its_marker_and_the_next_ensure_makes_no_second_one() -> None:
    with isolated_herdr("sp-cleanup-kept") as h:
        side = side_of(h)
        _, tab = session(h, side)
        before = panes_in(side, tab)
        stuck = failing_run(h.root, h, close_fails=True)
        result = make(h, stuck, tab, budget_s=5.0, lock_timeout_s=0)
        marker = state.read_marker(root_of(h), tab)
        now = panes_in(side, tab)
        assert result.outcome is Outcome.UNAVAILABLE and len(now) == len(before) + 1
        assert marker is not None and marker.pane_id in now  # kept: the pane is still there
        again = make(h, side, tab, start_grace_s=0.0, budget_s=5.0, lock_timeout_s=0)
        assert again.outcome is Outcome.CREATED
        assert wait_for(lambda: len(panes_in(side, tab)) == len(before) + 1, 10.0), panes_in(side, tab)  # replaced, never doubled
        assert marker.pane_id not in panes_in(side, tab)

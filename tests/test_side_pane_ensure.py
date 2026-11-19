"""One side pane per tab: the placement rules as plain logic, and `ensure` against a real isolated Herdr."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
import tomllib
from pathlib import Path

import pytest

from herdr_engineering.handoff_controller.herdr_cli import AgentPane
from herdr_engineering.side_pane import placement as placing
from herdr_engineering.side_pane import state
from herdr_engineering.side_pane.herdr import (
    Layout,
    Rect,
    SideHerdr,
    parse_layout,
    parse_pane,
    parse_panes,
)
from herdr_engineering.side_pane.placement import (
    MIN_SESSION_WIDTH,
    SIDE_WIDTH,
    Outcome,
    pick_target,
)
from tests.handoff_controller_herdr import IsolatedHerdr, isolated_herdr, wait_for
from tests.side_pane_support import focus_pane, focused
from tests.test_side_pane_app import runner_command

herdr_server = pytest.mark.herdr_server
REPO = Path(__file__).resolve().parents[1]
MANIFEST = REPO / "deploy" / "herdr-session-pane-plugin" / "herdr-plugin.toml"
SLEEP = "exec sleep 600"  # a pane that stays open; the real runner is exercised in test_side_pane_app


# ----------------------------------------------------------------------------------------------- placement: plain logic
def layout(*rects: tuple[str, int, int, int, int], focused: str | None = None) -> Layout:
    return Layout("w1:t1", focused, {pane: Rect(x, y, w, h) for pane, x, y, w, h in rects})


def test_the_target_is_the_rightmost_pane_then_the_topmost() -> None:
    assert placing.pick_target(layout(("a", 0, 0, 60, 40), ("b", 60, 0, 60, 20), ("c", 60, 20, 60, 20))) == "b"
    assert placing.pick_target(layout(("a", 0, 0, 120, 40))) == "a"
    assert placing.pick_target(layout()) is None


def test_the_side_pane_keeps_its_width_and_the_session_keeps_a_usable_one() -> None:
    ratio = placing.split_ratio(120)
    assert ratio is not None and round(120 * (1 - ratio)) in range(SIDE_WIDTH - 1, SIDE_WIDTH + 2)
    assert placing.split_ratio(SIDE_WIDTH + MIN_SESSION_WIDTH) is not None
    assert placing.split_ratio(SIDE_WIDTH + MIN_SESSION_WIDTH - 1) is None  # nothing is created when the session would be squeezed
    assert placing.split_ratio(60) is None and placing.split_ratio(0) is None


def test_a_pane_is_dismissed_only_while_every_session_in_the_tab_was_there_when_it_was_closed() -> None:
    assert placing.is_dismissed(frozenset({"t1", "t2"}), ["t1"])
    assert placing.is_dismissed(frozenset({"t1"}), ["t1"])
    assert not placing.is_dismissed(frozenset({"t1"}), ["t1", "t2"])  # a new managed session appeared
    assert not placing.is_dismissed(frozenset(), ["t1"])
    assert not placing.is_dismissed(frozenset({"t1"}), [])


def test_ensure_reports_a_herdr_that_does_not_answer_and_creates_nothing(tmp_path: Path) -> None:
    herdr = SideHerdr("/nonexistent/herdr", {}, tmp_path)
    result = placing.ensure("w1:t1", None, herdr, {"HOME": str(tmp_path)})
    assert result.outcome is Outcome.UNAVAILABLE and result.pane_id is None
    assert state.read_marker(state.state_dir({"HOME": str(tmp_path)}), "w1:t1") is None


def test_ensure_does_nothing_while_another_process_holds_the_tab_lock(tmp_path: Path) -> None:
    env = {"HOME": str(tmp_path)}
    root = state.state_dir(env)
    with state.tab_lock(root, "w1:t1", 1.0) as held:
        assert held
        result = placing.ensure("w1:t1", None, SideHerdr("/nonexistent/herdr", {}, tmp_path), env, lock_timeout_s=0.2)
    assert result.outcome is Outcome.LOCKED


def test_package_level_ensure_and_is_side_pane_are_exported() -> None:
    import herdr_engineering.side_pane as package
    assert package.ensure is placing.ensure and callable(package.is_side_pane) and callable(package.run)


def test_is_side_pane_is_the_marker_and_not_the_title(tmp_path: Path) -> None:
    env = {"HOME": str(tmp_path)}
    root = state.state_dir(env)
    pane = AgentPane("w1:p2", "w1:t1", None, None, None, None)
    other = AgentPane("w1:p3", "w1:t1", None, None, None, None)
    assert not placing.is_side_pane(pane, env)
    state.write_marker(root, state.Marker("w1:t1", "w1:p2", "term-2", "term-1", None, time.time()))
    assert placing.is_side_pane(pane, env) and not placing.is_side_pane(other, env)
    state.write_marker(root, state.Marker("w1:t1", "w1:p2", "term-2", "term-1", 2**22 + 12345, time.time()))  # a dead runner
    assert not placing.is_side_pane(pane, env)


# --------------------------------------------------------------------------------------------- against a real Herdr
def side_of(h: IsolatedHerdr) -> SideHerdr:
    return SideHerdr(h.cli._binary, h.env, h.root)


def env_of(h: IsolatedHerdr) -> dict[str, str]:
    return {"HOME": str(h.root / "home")}


def root_of(h: IsolatedHerdr) -> Path:
    return state.state_dir(env_of(h))


def listed(side: SideHerdr, pane: str) -> bool:
    return any(a.pane_id == pane and a.name == "claude" for a in side.agents() or [])


def session(h: IsolatedHerdr, side: SideHerdr) -> tuple[str, str]:
    """A session in a workspace of its own: its pane and tab."""
    pane = h.start()
    assert wait_for(lambda: listed(side, pane), 20.0)
    tab = side.tab_of(pane)
    assert tab
    return pane, tab


def panes_in(side: SideHerdr, tab: str) -> list[str]:
    return [r.pane_id for r in side.panes() or [] if r.tab_id == tab]


def make(h: IsolatedHerdr, side: SideHerdr, tab: str, command: str = SLEEP, **extra: object) -> placing.EnsureResult:
    return placing.ensure(tab, None, side, env_of(h), command=command, **extra)  # type: ignore[arg-type]


@herdr_server
def test_creates_one_side_pane_on_the_right_of_the_tab() -> None:
    with isolated_herdr("sp-ensure-one") as h:
        side = side_of(h)
        first, tab = session(h, side)
        before = side.layout(first)
        assert before is not None
        result = make(h, side, tab)
        assert result.outcome is Outcome.CREATED and result.pane_id
        after = side.layout(first)
        assert after is not None and set(after.rects) == {first, result.pane_id}
        assert after.rects[result.pane_id].x > after.rects[first].x  # on the right
        assert abs(after.rects[result.pane_id].width - SIDE_WIDTH) <= 2
        marker = state.read_marker(root_of(h), tab)
        assert marker is not None and marker.pane_id == result.pane_id
        assert marker.session_terminal == next(r.terminal_id for r in side.panes() or [] if r.pane_id == first)
        assert placing.is_side_pane(AgentPane(result.pane_id, tab, None, None, None, None), env_of(h))


@herdr_server
def test_ensure_twice_gives_one_pane() -> None:
    with isolated_herdr("sp-ensure-twice") as h:
        side = side_of(h)
        _, tab = session(h, side)
        first = make(h, side, tab)
        second = make(h, side, tab)
        assert (first.outcome, second.outcome) == (Outcome.CREATED, Outcome.EXISTS) and second.pane_id == first.pane_id
        assert len(panes_in(side, tab)) == 2


@herdr_server
def test_concurrent_ensure_calls_create_a_single_pane() -> None:
    with isolated_herdr("sp-ensure-race") as h:
        side = side_of(h)
        _, tab = session(h, side)
        outcomes: list[Outcome] = []
        threads = [threading.Thread(target=lambda: outcomes.append(make(h, side, tab).outcome)) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        assert outcomes.count(Outcome.CREATED) == 1 and len(panes_in(side, tab)) == 2, outcomes


@herdr_server
def test_two_tabs_get_their_own_pane_with_no_cross_talk() -> None:
    with isolated_herdr("sp-ensure-tabs") as h:
        side = side_of(h)
        _, tab_a = session(h, side)
        _, tab_b = session(h, side)
        assert tab_a != tab_b
        a = make(h, side, tab_a)
        assert len(panes_in(side, tab_a)) == 2 and len(panes_in(side, tab_b)) == 1  # B untouched
        b = make(h, side, tab_b)
        assert a.pane_id != b.pane_id and len(panes_in(side, tab_a)) == 2 and len(panes_in(side, tab_b)) == 2
        marker_a, marker_b = state.read_marker(root_of(h), tab_a), state.read_marker(root_of(h), tab_b)
        assert marker_a is not None and marker_b is not None and marker_a.pane_id == a.pane_id and marker_b.pane_id == b.pane_id


@herdr_server
def test_focus_stays_where_it_was() -> None:
    with isolated_herdr("sp-ensure-focus") as h:
        side = side_of(h)
        first, tab = session(h, side)
        before = side.layout(first)
        assert before is not None and before.focused_pane_id == first
        result = make(h, side, tab)
        assert result.outcome is Outcome.CREATED and result.focus is placing.FocusOutcome.UNCHANGED
        after = side.layout(first)
        assert after is not None and after.focused_pane_id == first and focused(side, first)
        assert not focused(side, result.pane_id or "")


@herdr_server
def test_ensure_for_one_tab_leaves_the_focus_the_user_has_in_another_tab() -> None:
    with isolated_herdr("sp-focus-other-tab") as h:
        side = side_of(h)
        a, tab_a = session(h, side)
        b, tab_b = session(h, side)
        assert tab_a != tab_b and focus_pane(side, b) and not focused(side, a)  # the user is working in tab B
        result = make(h, side, tab_a)
        assert result.outcome is Outcome.CREATED and result.focus is placing.FocusOutcome.UNCHANGED
        assert focused(side, b) and not focused(side, a) and not focused(side, result.pane_id or "")
        assert panes_in(side, tab_b) == [b]  # and tab B was not touched


def split_and_focus(side: SideHerdr, target: str) -> str:
    created = side.split(target, 0.7, env={})
    assert created is not None and focus_pane(side, created.pane_id)  # what an accident would look like
    return created.pane_id


@herdr_server
def test_a_side_pane_that_took_the_focus_gives_it_back_with_one_directional_call() -> None:
    with isolated_herdr("sp-focus-restore") as h:
        side = side_of(h)
        first, _ = session(h, side)
        created = split_and_focus(side, first)
        assert placing.settle_focus(side, created, first) is placing.FocusOutcome.RESTORED
        assert focused(side, first) and not focused(side, created)


@herdr_server
def test_it_does_not_walk_through_other_panes_when_it_cannot_restore_by_id() -> None:
    with isolated_herdr("sp-focus-left") as h:
        side = side_of(h)
        first, _ = session(h, side)
        other, _ = session(h, side)  # the user's focus was in another tab; the side pane's left neighbour is not that pane
        created = split_and_focus(side, first)
        assert placing.settle_focus(side, created, other) is placing.FocusOutcome.LEFT_ON_SIDE_PANE
        assert focused(side, created) and not focused(side, first) and not focused(side, other)  # left exactly where it was
        assert placing.settle_focus(side, created, None) is placing.FocusOutcome.LEFT_ON_SIDE_PANE


@herdr_server
def test_a_new_pane_without_the_focus_is_left_alone() -> None:
    with isolated_herdr("sp-focus-unchanged") as h:
        side = side_of(h)
        first, _ = session(h, side)
        created = side.split(first, 0.7, env={})
        assert created is not None
        assert placing.settle_focus(side, created.pane_id, first) is placing.FocusOutcome.UNCHANGED
        assert placing.settle_focus(side, "w9:p9", first) is placing.FocusOutcome.UNKNOWN  # Herdr cannot say


def test_production_code_has_no_way_to_walk_the_focus_or_switch_tabs() -> None:
    assert not hasattr(SideHerdr, "focus_pane") and not hasattr(SideHerdr, "tab_focus")
    source = (REPO / "herdr_engineering" / "side_pane" / "herdr.py").read_text()
    assert '"tab", "focus"' not in source and "'tab', 'focus'" not in source


@herdr_server
def test_a_tab_too_narrow_for_a_side_pane_gets_none() -> None:
    with isolated_herdr("sp-ensure-narrow") as h:
        side = side_of(h)
        first, tab = session(h, side)
        narrow = side.split(first, 0.5, env={})  # two panes of 60 columns: a side pane would squeeze the session
        assert narrow is not None
        before = panes_in(side, tab)
        result = make(h, side, tab)
        assert result.outcome is Outcome.TOO_NARROW and result.pane_id is None
        assert panes_in(side, tab) == before and state.read_marker(root_of(h), tab) is None


@herdr_server
def test_a_tab_without_a_managed_session_gets_none() -> None:
    with isolated_herdr("sp-ensure-none") as h:
        side = side_of(h)
        first, tab = session(h, side)
        shell = side.split(first, 0.5, env={})
        assert shell is not None and side.close(first)
        assert wait_for(lambda: not listed(side, first), 10.0)
        assert make(h, side, tab).outcome is Outcome.NO_SESSION and panes_in(side, tab) == [shell.pane_id]


def real_runner(h: IsolatedHerdr, side: SideHerdr, tab: str) -> placing.EnsureResult:
    """`ensure` with the real runner as the pane's command, waiting until the runner has registered itself."""
    result = placing.ensure(tab, None, side, env_of(h), command=runner_command())
    assert result.outcome is Outcome.CREATED and result.pane_id
    assert wait_for(lambda: (m := state.read_marker(root_of(h), tab)) is not None and m.pid is not None, 30.0), "runner never started"
    return result


@herdr_server
def test_a_pane_closed_through_herdr_stays_closed_until_a_new_session_appears() -> None:
    with isolated_herdr("sp-ensure-closed") as h:
        side = side_of(h)
        first, tab = session(h, side)
        created = real_runner(h, side, tab)
        assert created.pane_id and side.close(created.pane_id)  # `herdr pane close`, not q
        assert wait_for(lambda: created.pane_id not in panes_in(side, tab), 10.0)
        again = placing.ensure(tab, None, side, env_of(h), command=runner_command())
        assert again.outcome is Outcome.DISMISSED and panes_in(side, tab) == [first]
        assert state.read_marker(root_of(h), tab) is None
        assert placing.ensure(tab, None, side, env_of(h), command=runner_command()).outcome is Outcome.DISMISSED  # and it stays
        second = side.split(first, 0.5, env={})  # a new managed session appears ...
        assert second is not None and side.run_in(second.pane_id, str(h.program))
        assert wait_for(lambda: listed(side, second.pane_id), 20.0)
        assert side.close(first)  # ... and is the only one, so the tab is wide again
        assert wait_for(lambda: not listed(side, first) and first not in panes_in(side, tab), 10.0)
        reopened = placing.ensure(tab, None, side, env_of(h), command=SLEEP)
        assert reopened.outcome is Outcome.CREATED
        assert state.read_dismissed(root_of(h), tab) == frozenset()


@herdr_server
@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGHUP])
def test_a_runner_ended_by_a_signal_counts_as_dismissed(sig: int) -> None:
    with isolated_herdr(f"sp-ensure-signal-{sig}") as h:
        side = side_of(h)
        first, tab = session(h, side)
        created = real_runner(h, side, tab)
        marker = state.read_marker(root_of(h), tab)
        assert marker is not None and marker.pid is not None
        os.kill(marker.pid, sig)
        assert wait_for(lambda: created.pane_id not in panes_in(side, tab), 20.0), "the runner did not exit"
        terminal = next(r.terminal_id for r in side.panes() or [] if r.pane_id == first)
        assert wait_for(lambda: terminal in state.read_dismissed(root_of(h), tab), 5.0)
        assert state.read_marker(root_of(h), tab) is None
        assert make(h, side, tab).outcome is Outcome.DISMISSED


@herdr_server
def test_a_runner_that_was_killed_without_a_chance_to_clean_up_is_still_dismissed() -> None:
    with isolated_herdr("sp-ensure-kill") as h:
        side = side_of(h)
        first, tab = session(h, side)
        created = real_runner(h, side, tab)
        assert wait_for(lambda: state.read_shown(root_of(h), tab) != frozenset(), 10.0)
        marker = state.read_marker(root_of(h), tab)
        assert marker is not None and marker.pid is not None
        os.kill(marker.pid, signal.SIGKILL)
        assert wait_for(lambda: created.pane_id not in panes_in(side, tab), 20.0)
        state.clear_marker(root_of(h), tab)  # nothing was left to clear it
        assert make(h, side, tab).outcome is Outcome.DISMISSED  # the record of what the pane was shown for decides


@herdr_server
def test_a_runner_that_never_starts_is_replaced_and_never_duplicated() -> None:
    with isolated_herdr("sp-ensure-nostart") as h:
        side = side_of(h)
        first, tab = session(h, side)
        one = make(h, side, tab, command="true")  # the pane's command exits at once: no runner ever registers
        assert one.outcome is Outcome.CREATED and one.pane_id
        marker = state.read_marker(root_of(h), tab)
        row = next(r for r in side.panes() or [] if r.pane_id == one.pane_id)
        assert marker is not None and marker.pid is None and marker.terminal_id == row.terminal_id and marker.created > 0
        assert make(h, side, tab, command="true").outcome is Outcome.EXISTS  # still inside the start grace
        two = make(h, side, tab, command="true", start_grace_s=0.0)
        assert two.outcome is Outcome.CREATED and two.pane_id != one.pane_id
        assert wait_for(lambda: one.pane_id not in panes_in(side, tab), 10.0)
        assert sorted(panes_in(side, tab)) == sorted([first, two.pane_id or ""]) and len(panes_in(side, tab)) == 2


@herdr_server
def test_a_marker_that_names_somebody_elses_pane_is_cleaned_and_that_pane_is_not_closed() -> None:
    with isolated_herdr("sp-ensure-reused") as h:
        side = side_of(h)
        first, tab = session(h, side)
        shell = side.split(first, 0.7, env={})  # the user's own shell, which happens to have the marker's pane id
        assert shell is not None
        state.write_marker(root_of(h), state.Marker(tab, shell.pane_id, "term-of-another-herdr", None, None, 1.0))
        result = make(h, side, tab, start_grace_s=0.0)
        assert result.outcome in (Outcome.CREATED, Outcome.TOO_NARROW)
        assert shell.pane_id in panes_in(side, tab)  # the terminal id differs: not ours, not closed


@herdr_server
def test_without_herdr_eng_nothing_is_created() -> None:
    with isolated_herdr("sp-ensure-norunner") as h:
        side = side_of(h)
        first, tab = session(h, side)
        env = {**env_of(h), "PATH": "/nonexistent", "HERDR_HANDOFF_BIN_DIRS": ""}
        result = placing.ensure(tab, None, side, env)  # the default command needs `herdr-eng`
        assert result.outcome is Outcome.RUNNER_MISSING and result.pane_id is None
        assert panes_in(side, tab) == [first] and state.read_marker(root_of(h), tab) is None


@herdr_server
def test_the_default_command_starts_the_herdr_eng_found_by_absolute_path() -> None:
    with isolated_herdr("sp-ensure-abs") as h:
        side = side_of(h)
        _, tab = session(h, side)
        runner = h.root / "tools" / "herdr-eng"
        runner.parent.mkdir()
        runner.write_text(f"#!/bin/sh\necho started-by-herdr-eng \"$@\" > {h.root}/ran.txt\nexec sleep 600\n")
        runner.chmod(0o755)
        env = {**env_of(h), "PATH": "/usr/bin:/bin", "HERDR_HANDOFF_BIN_DIRS": str(runner.parent)}
        result = placing.ensure(tab, None, side, env)
        assert result.outcome is Outcome.CREATED
        assert wait_for(lambda: (h.root / "ran.txt").exists(), 10.0)
        assert (h.root / "ran.txt").read_text().strip() == "started-by-herdr-eng side-pane run"


@herdr_server
def test_ensure_prunes_the_state_of_tabs_herdr_no_longer_lists() -> None:
    with isolated_herdr("sp-ensure-prune") as h:
        side = side_of(h)
        _, tab = session(h, side)
        root = root_of(h)
        state.write_marker(root, state.Marker("w9:t9", "w9:p9", "t", None, None, 1.0))
        state.dismiss(root, "w9:t9", ["x"])
        state.write_shown(root, "w9:t9", ["x"])
        assert make(h, side, tab).outcome is Outcome.CREATED
        assert not [p for p in root.iterdir() if p.name.startswith("w9:t9")]
        assert state.read_marker(root, tab) is not None  # the listed tab keeps its own


def test_nothing_is_pruned_when_herdr_cannot_be_listed(tmp_path: Path) -> None:
    env = {"HOME": str(tmp_path)}
    root = state.state_dir(env)
    state.dismiss(root, "w9:t9", ["x"])
    assert placing.ensure("w1:t1", None, SideHerdr(binary=str(tmp_path / "no-herdr")), env).outcome is Outcome.UNAVAILABLE
    assert state.read_dismissed(root, "w9:t9") == {"x"}


@herdr_server
def test_forcing_ensure_clears_the_dismissal_and_reopens_the_pane() -> None:
    with isolated_herdr("sp-ensure-force") as h:
        side = side_of(h)
        _, tab = session(h, side)
        created = make(h, side, tab)
        assert created.pane_id and side.close(created.pane_id)
        assert wait_for(lambda: created.pane_id not in panes_in(side, tab), 10.0)
        assert make(h, side, tab).outcome is Outcome.DISMISSED
        assert make(h, side, tab, force=True).outcome is Outcome.CREATED
        assert state.read_dismissed(root_of(h), tab) == frozenset()


@herdr_server
def test_a_marker_left_by_an_earlier_herdr_is_cleaned_and_the_pane_is_created() -> None:
    with isolated_herdr("sp-ensure-stale") as h:
        side = side_of(h)
        _, tab = session(h, side)
        state.write_marker(root_of(h), state.Marker(tab, "w9:p9", "term-from-before", "term-session-before", 1, time.time() - 999))
        result = make(h, side, tab)
        assert result.outcome is Outcome.CREATED
        marker = state.read_marker(root_of(h), tab)
        assert marker is not None and marker.pane_id == result.pane_id


@herdr_server
def test_the_manifest_is_valid_for_herdr() -> None:
    with isolated_herdr("sp-ensure-manifest") as h:
        link = subprocess.run(["herdr", "plugin", "link", str(MANIFEST.parent), "--disabled"], env=h.env, cwd=h.root,
                              capture_output=True, text=True, timeout=30, check=False)
        try:
            assert link.returncode == 0, link.stdout + link.stderr
            listed_plugins = subprocess.run(["herdr", "plugin", "list"], env=h.env, cwd=h.root, capture_output=True, text=True,
                                            timeout=30, check=False)
            assert "herdr.session-pane" in listed_plugins.stdout, listed_plugins.stdout
        finally:
            subprocess.run(["herdr", "plugin", "unlink", "herdr.session-pane"], env=h.env, cwd=h.root, capture_output=True,
                           text=True, timeout=30, check=False)


# ------------------------------------------------------------------------------------------------------------ the CLI
def cli(*args: str, env: dict[str, str] | None = None, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    base = {**os.environ, "PYTHONPATH": str(REPO)}
    return subprocess.run([sys.executable, "-m", "herdr_engineering.cli", *args], env=env or base, cwd=cwd, capture_output=True,
                          text=True, timeout=60, check=False)


def test_help_documents_collapse_and_reopen() -> None:
    text = cli("side-pane", "--help").stdout
    assert "q closes the pane" in text and "side-pane ensure --tab TAB_ID" in text
    assert "--tab" in cli("side-pane", "ensure", "--help").stdout


def test_run_without_a_pane_exits_2_with_one_line() -> None:
    env = {k: v for k, v in os.environ.items() if k != "HERDR_PANE_ID"} | {"PYTHONPATH": str(REPO)}
    done = cli("side-pane", "run", env=env)
    assert done.returncode == 2 and len(done.stderr.strip().splitlines()) == 1


@herdr_server
def test_ensure_command_reopens_a_pane_the_user_closed() -> None:
    with isolated_herdr("sp-ensure-cli") as h:
        side = side_of(h)
        first, tab = session(h, side)
        env = {**h.env, "PYTHONPATH": str(REPO), "PATH": f"{Path(sys.executable).parent}:{h.env.get('PATH', '')}"}
        opened = cli("side-pane", "ensure", "--tab", tab, "--command", SLEEP, env=env, cwd=h.root)
        assert opened.returncode == 0 and "created" in opened.stderr, opened.stderr
        marker = state.read_marker(root_of(h), tab)
        assert marker is not None and side.close(marker.pane_id)
        assert wait_for(lambda: marker.pane_id not in panes_in(side, tab), 10.0)
        assert make(h, side, tab).outcome is Outcome.DISMISSED
        again = cli("side-pane", "ensure", "--tab", tab, "--command", SLEEP, env=env, cwd=h.root)
        assert again.returncode == 0 and "created" in again.stderr, again.stderr
        assert len(panes_in(side, tab)) == 2


# ------------------------------------------------------------------------------------------------------- manifest
@herdr_server
def test_the_open_action_receives_the_tab_id_and_opens_the_side_pane() -> None:
    with isolated_herdr("sp-action") as h:
        side = side_of(h)
        first, tab = session(h, side)
        tools = h.root / "tools"
        tools.mkdir()
        wrapper = tools / "herdr-eng"  # the real CLI of this checkout, under the name the manifest uses
        wrapper.write_text(f"#!/bin/sh\nexport PATH={tools}:$PATH PYTHONPATH={REPO} HERDR_HANDOFF_BIN_DIRS={tools}\ncd {h.root}  # the rig's socket path is relative (an absolute one is too long for a socket)\n"
                           f"exec {sys.executable} -m herdr_engineering.cli \"$@\"\n")
        wrapper.chmod(0o755)
        plugin = h.root / "plugin"
        plugin.mkdir()
        (plugin / "herdr-plugin.toml").write_text(MANIFEST.read_text().replace("exec herdr-eng", f"exec {wrapper}"))
        link = subprocess.run(["herdr", "plugin", "link", str(plugin)], env=h.env, cwd=h.root, capture_output=True, text=True,
                              timeout=30, check=False)
        assert link.returncode == 0, link.stdout + link.stderr
        invoked = subprocess.run(["herdr", "plugin", "action", "invoke", "open", "--plugin", "herdr.session-pane"], env=h.env,
                                 cwd=h.root, capture_output=True, text=True, timeout=60, check=False)
        assert invoked.returncode == 0, invoked.stdout + invoked.stderr
        assert wait_for(lambda: len(panes_in(side, tab)) == 2, 30.0), panes_in(side, tab)
        marker = state.read_marker(root_of(h), tab)
        assert marker is not None and marker.pane_id in panes_in(side, tab)


def test_manifest_declares_an_open_action_that_passes_the_tab_id_through_the_environment() -> None:
    data = tomllib.loads(MANIFEST.read_text(encoding="utf-8"))
    (action,) = data["actions"]
    assert action["id"] == "open" and set(action["contexts"]) == {"tab", "pane"}
    assert action["command"][:2] == ["sh", "-c"] and "side-pane ensure --tab \"$HERDR_TAB_ID\"" in action["command"][2]


def test_manifest_declares_one_split_pane_that_runs_the_viewer() -> None:
    data = tomllib.loads(MANIFEST.read_text(encoding="utf-8"))
    assert data["min_herdr_version"] == "0.9.1"
    (pane,) = data["panes"]
    assert pane["id"] == "session-pane" and pane["title"] == "Session" and pane["placement"] == "split"
    assert pane["command"] == ["herdr-eng", "side-pane", "run"] and data["id"] == "herdr.session-pane"


# ------------------------------------------------------------------------------- parsing Herdr replies (recorded output)
FIXTURES = REPO / "tests" / "fixtures" / "side_pane"


def test_recorded_pane_list_parses_to_rows() -> None:
    rows = parse_panes((FIXTURES / "herdr_pane_list.json").read_text())
    assert rows and all(r.pane_id and r.tab_id and r.terminal_id for r in rows)


def test_recorded_pane_get_parses_to_one_row() -> None:
    assert parse_pane((FIXTURES / "herdr_pane_get.json").read_text()) is not None


def test_recorded_layout_has_cells_for_both_panes() -> None:
    layout = parse_layout((FIXTURES / "herdr_pane_layout.json").read_text())
    assert layout is not None and layout.focused_pane_id == "w1:p1"
    assert layout.rects["w1:p1"].width == 72 and layout.rects["w1:p2"].x == 72
    assert pick_target(layout) == "w1:p2"


def test_unreadable_replies_give_none() -> None:
    for text in ("", "not json", "{}", '{"result": 3}', '{"result": {"layout": {"panes": []}}}'):
        assert parse_panes(text) is None and parse_pane(text) is None and parse_layout(text) is None


# ---------------------------------------- without a Herdr server: the real classes against a binary that is not there
def absent(tmp_path: Path) -> SideHerdr:
    return SideHerdr(binary=str(tmp_path / "no-herdr"))


def test_herdr_calls_say_unavailable_instead_of_raising(tmp_path: Path) -> None:
    herdr = absent(tmp_path)
    assert herdr.panes() is None and herdr.pane("w1:p1") is None and herdr.tab_of("w1:p1") is None
    assert herdr.layout("w1:p1") is None and herdr.split("w1:p1", 0.5) is None
    assert not herdr.run_in("w1:p2", "x") and not herdr.close("w1:p2")


def test_ensure_is_unavailable_without_herdr_and_leaves_no_marker(tmp_path: Path) -> None:
    env = {"HERDR_ENGINEERING_STATE": str(tmp_path)}
    done = placing.ensure("w1:t1", None, absent(tmp_path), env)
    assert done.outcome is Outcome.UNAVAILABLE and state.read_marker(state.state_dir(env), "w1:t1") is None


def test_ensure_command_exits_1_and_says_why_when_nothing_opens(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    import argparse

    from herdr_engineering.side_pane import cli as side_cli

    args = argparse.Namespace(tab="w1:t1", pane=None, command=placing.DEFAULT_COMMAND)
    code = side_cli._ensure(args, {"HERDR_ENGINEERING_STATE": str(tmp_path), "PATH": str(tmp_path)})
    assert code == 1 and "w1:t1" in capsys.readouterr().err


def test_register_adds_run_and_ensure() -> None:
    import argparse

    from herdr_engineering.side_pane import cli as side_cli

    top = argparse.ArgumentParser()
    side_cli.register(top.add_subparsers(dest="command"))
    assert top.parse_args(["side-pane", "ensure", "--tab", "w1:t1"]).func is side_cli._ensure
    assert top.parse_args(["side-pane", "run", "--no-ci"]).no_ci is True

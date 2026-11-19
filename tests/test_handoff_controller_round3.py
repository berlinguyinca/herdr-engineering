"""Fix round 3: the ready check inside the failure counting, attempts per chain, the dirty-source flag from the log,
the side pane hooks, and chains of panes that are listed but no longer managed."""
from __future__ import annotations

import inspect
from dataclasses import replace
from pathlib import Path

import pytest

from herdr_engineering.handoff_controller import drain as drainmod
from herdr_engineering.handoff_controller import run as ctlrun
from herdr_engineering.handoff_controller import switch as switchmod
from herdr_engineering.handoff_controller import takeover as takeovermod
from herdr_engineering.handoff_controller.events import Colour
from tests.test_handoff_controller_round2 import CFG, _briefed, _dirty_submodule
from tests.test_handoff_controller_run import World, _git
from tests.test_handoff_controller_wave1 import Wave
from tests.test_handoff_controller_wave2 import _fake


def _look_that(w: Wave, answers: list[object]) -> list[int]:
    """A look seam that raises for an Exception item and answers ready/not ready for a bool; the last item repeats."""
    seen: list[int] = []

    def look(ref: object, adapter: object, herdr: object) -> drainmod.Look:
        seen.append(1)
        item = answers[min(len(seen), len(answers)) - 1]
        if isinstance(item, Exception):
            raise item
        return drainmod.Look("idle" if item else "working", bool(item), False)

    w.ctl.look = look
    return seen


def test_a_look_that_always_fails_ends_the_chain_after_three_ticks(tmp_path: Path) -> None:
    w = Wave(tmp_path)
    _briefed(w, tmp_path)
    _look_that(w, [OSError("herdr read error")])
    for _ in range(2):
        w.tick(w.pane("p1", "idle"))
    assert "handoff.stuck" not in w.names()
    w.tick(w.pane("p1", "idle"))
    stuck = [e for e in w.events() if e.event == "handoff.stuck"]
    assert [(e.reason, e.colour) for e in stuck] == [("internal_error:OSError", Colour.RED)]
    assert "herdr read error" not in stuck[0].reason


def test_a_look_that_fails_twice_and_then_works_ends_nothing_and_the_count_starts_over(tmp_path: Path) -> None:
    w = Wave(tmp_path)
    _briefed(w, tmp_path)
    _look_that(w, [OSError("x"), OSError("x"), False])  # then not ready: the chain waits
    for _ in range(3):
        w.tick(w.pane("p1", "idle"))
    assert "handoff.stuck" not in w.names() and w.ctl.runtime["p1"].finish_failures == 0
    _look_that(w, [OSError("x"), OSError("x"), False])
    for _ in range(3):
        w.tick(w.pane("p1", "idle"))
    assert "handoff.stuck" not in w.names()


def test_the_ready_check_is_a_required_argument() -> None:
    param = inspect.signature(takeovermod.run_takeover).parameters["ready"]
    assert param.default is inspect.Parameter.empty and param.kind is inspect.Parameter.KEYWORD_ONLY


def test_the_dirty_source_flag_survives_a_restart(tmp_path: Path) -> None:
    w = Wave(tmp_path, cfg=replace(CFG, push_default=True))
    calls = _briefed(w, tmp_path)
    remote = tmp_path / "remote.git"
    import subprocess
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    _git(w.repo, "remote", "add", "origin", str(remote))
    _git(w.repo, "push", "-q", "-u", "origin", "feat/x")
    _dirty_submodule(w.repo, tmp_path / "sub")
    _git(w.repo, "push", "-q", "origin", "feat/x")
    (w.repo / "vendor" / "a.txt").write_text("changed again\n")
    _look_that(w, [True, False])  # ready for the gate, not ready right before the call: the checkpoint is done, no call
    w.tick(w.pane("p1", "idle"))
    assert "takeover" not in (calls.read_text().split() if calls.exists() else [])
    w.ctl.runtime.clear()  # a restart: only the log is left
    _look_that(w, [True])
    w.tick(w.pane("p1", "idle"))
    assert w.names()[-1] == "handoff.succeeded" and "--allow-dirty-source" in calls.read_text()


def test_takeover_calls_never_exceed_max_attempts_however_often_ready_flips(tmp_path: Path) -> None:
    w = Wave(tmp_path)
    _briefed(w, tmp_path)
    fake, calls = _fake(tmp_path, takeover_ok=False)  # every call is refused
    w.ctl.handoff = fake
    w.ctl.takeover_kwargs = {"retry_delay_s": 0.0, "compact_ready_timeout_s": 0.1}  # no herdr here: compaction fails fast
    answers: list[object] = [True, True, False] * 40  # ready for a call, ready, then not
    _look_that(w, answers)
    for _ in range(30):
        w.tick(w.pane("p1", "idle"))
    assert calls.read_text().split().count("takeover") <= CFG.max_attempts
    assert calls.read_text().split().count("takeover") >= 2


class _Raises:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.fail = 99

    def __call__(self, tab_id: str) -> None:
        self.calls.append(tab_id)
        if len(self.calls) <= self.fail:
            raise RuntimeError("no")


def test_a_side_pane_call_that_raised_is_retried_up_to_three_times_then_left_until_a_new_pane(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    w = World(tmp_path)
    ask = _Raises()
    w.ctl.ensure_side_pane = ask
    w.session("p1", 10.0)
    w.session("p2", 10.0)
    for _ in range(6):
        w.tick(w.pane("p1", tab="t1"))
    assert ask.calls == ["t1"] * 3
    assert capsys.readouterr().err.count("side pane for t1") == 1
    ask.fail = 0
    w.tick(w.pane("p1", tab="t1"), w.pane("p2", tab="t1"))  # a new pane: asked again
    assert ask.calls == ["t1"] * 4


def test_a_side_pane_call_that_works_after_a_failure_is_not_repeated(tmp_path: Path) -> None:
    w = World(tmp_path)
    ask = _Raises()
    ask.fail = 1
    w.ctl.ensure_side_pane = ask
    w.session("p1", 10.0)
    for _ in range(4):
        w.tick(w.pane("p1", tab="t1"))
    assert ask.calls == ["t1", "t1"]


def test_the_side_pane_hook_runs_once_per_pane_per_tick(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.session("p1", 10.0)
    seen: list[str] = []
    w.ctl.is_side_pane = lambda pane: seen.append(pane.pane_id) is not None and False
    w.tick(w.pane("p1"), w.pane("p2"))
    assert sorted(seen) == ["p1", "p2"]


def test_a_chain_on_a_pane_whose_switch_went_off_ends_stuck_and_is_not_a_failure(tmp_path: Path) -> None:
    w = Wave(tmp_path)
    _briefed(w, tmp_path)
    _look_that(w, [False])
    w.tick(w.pane("p1", "idle"))
    assert "handoff.stuck" not in w.names()
    switchmod.set_enabled("p1", False, w.ctl.state_dir)
    w.tick(w.pane("p1", "idle"))
    w.tick(w.pane("p1", "idle"))
    stuck = [e for e in w.events() if e.event == "handoff.stuck"]
    assert [(e.reason, e.colour) for e in stuck] == [("no_longer_managed", Colour.RED)]
    assert len(ctlrun._failed_chains(w.events())) == 0 and not ctlrun.gave_up(w.events(), CFG)


def test_a_chain_on_a_pane_that_became_a_side_pane_ends_stuck(tmp_path: Path) -> None:
    w = Wave(tmp_path)
    _briefed(w, tmp_path)
    w.ctl.is_side_pane = lambda pane: True
    w.tick(w.pane("p1", "idle"))
    w.tick(w.pane("p1", "idle"))
    assert [e.reason for e in w.events() if e.event == "handoff.stuck"] == ["no_longer_managed"]

"""The side pane's per-tab state files: marker, dismissal and lock, on a real directory."""
from __future__ import annotations

import multiprocessing
import os
import time
from pathlib import Path

from herdr_engineering.handoff_controller.herdr_cli import AgentPane
from herdr_engineering.side_pane import state
from herdr_engineering.side_pane.state import Marker


def marker(tab: str = "w1:t1", pane: str = "w1:p2", pid: int | None = None, created: float = 100.0) -> Marker:
    return Marker(tab, pane, "term_side", "term_session", pid, created)


def test_state_dir_is_under_the_engineering_state_root(tmp_path: Path) -> None:
    assert state.state_dir({"HERDR_ENGINEERING_STATE": str(tmp_path)}) == tmp_path / "side-pane"
    assert state.state_dir({"HOME": str(tmp_path)}) == tmp_path / ".local/share/herdr-engineering/state/side-pane"


def test_marker_round_trips_and_is_cleared(tmp_path: Path) -> None:
    assert state.read_marker(tmp_path, "w1:t1") is None
    state.write_marker(tmp_path, marker())
    assert state.read_marker(tmp_path, "w1:t1") == marker()
    assert state.read_marker(tmp_path, "w1:t2") is None  # one file per tab
    state.clear_marker(tmp_path, "w1:t1", pane_id="w1:p9")  # someone else's pane: kept
    assert state.read_marker(tmp_path, "w1:t1") is not None
    state.clear_marker(tmp_path, "w1:t1", pane_id="w1:p2")
    assert state.read_marker(tmp_path, "w1:t1") is None


def test_a_damaged_marker_reads_as_none(tmp_path: Path) -> None:
    (tmp_path / "w1:t1.marker.json").write_text("{not json")
    assert state.read_marker(tmp_path, "w1:t1") is None
    (tmp_path / "w1:t1.marker.json").write_text('{"tab_id": 1}')
    assert state.read_marker(tmp_path, "w1:t1") is None


def test_a_tab_id_cannot_escape_the_directory(tmp_path: Path) -> None:
    state.write_marker(tmp_path / "d", marker(tab="../../evil"))
    assert [p.parent for p in (tmp_path / "d").iterdir()] == [tmp_path / "d"]
    assert not (tmp_path / "evil.marker.json").exists()


def test_dismissal_accumulates_and_clears(tmp_path: Path) -> None:
    assert state.read_dismissed(tmp_path, "w1:t1") == frozenset()
    state.dismiss(tmp_path, "w1:t1", ["term_a"])
    state.dismiss(tmp_path, "w1:t1", ["term_b", "term_a"])
    assert state.read_dismissed(tmp_path, "w1:t1") == {"term_a", "term_b"}
    assert state.read_dismissed(tmp_path, "w1:t2") == frozenset()
    state.clear_dismissed(tmp_path, "w1:t1")
    assert state.read_dismissed(tmp_path, "w1:t1") == frozenset()


def test_marker_is_a_live_side_pane_while_its_runner_lives(tmp_path: Path) -> None:
    pane = AgentPane("w1:p2", "w1:t1", "claude", "idle", None, None)
    assert not state.is_marked(tmp_path, pane, now=100.0)  # no marker
    state.write_marker(tmp_path, marker(pid=os.getpid()))
    assert state.is_marked(tmp_path, pane, now=100.0)
    assert not state.is_marked(tmp_path, AgentPane("w1:p3", "w1:t1", "claude", "idle", None, None), now=100.0)
    assert not state.is_marked(tmp_path, AgentPane("w1:p2", "w1:t2", "claude", "idle", None, None), now=100.0)


def test_marker_of_a_dead_runner_is_not_a_side_pane(tmp_path: Path) -> None:
    child = multiprocessing.get_context("spawn").Process(target=time.sleep, args=(0,))
    child.start()
    child.join()
    assert child.pid is not None
    state.write_marker(tmp_path, marker(pid=child.pid))
    assert not state.is_marked(tmp_path, AgentPane("w1:p2", "w1:t1", "claude", "idle", None, None), now=100.0)


def test_marker_without_a_pid_counts_only_while_young(tmp_path: Path) -> None:
    pane = AgentPane("w1:p2", "w1:t1", "claude", "idle", None, None)
    state.write_marker(tmp_path, marker(pid=None, created=100.0))  # ensure wrote it, the runner has not started yet
    assert state.is_marked(tmp_path, pane, now=100.0 + state.STARTUP_GRACE_S - 1)
    assert not state.is_marked(tmp_path, pane, now=100.0 + state.STARTUP_GRACE_S + 1)


def _hold(root: str, started: object, release: object) -> None:
    with state.tab_lock(Path(root), "w1:t1", 5.0) as got:
        assert got
        started.set()  # type: ignore[attr-defined]
        release.wait(10)  # type: ignore[attr-defined]


def test_tab_lock_excludes_another_process_and_frees_on_exit(tmp_path: Path) -> None:
    ctx = multiprocessing.get_context("spawn")
    started, release = ctx.Event(), ctx.Event()
    holder = ctx.Process(target=_hold, args=(str(tmp_path), started, release))
    holder.start()
    try:
        assert started.wait(15)
        with state.tab_lock(tmp_path, "w1:t1", 0.3) as got:
            assert not got  # the holder still has it
        with state.tab_lock(tmp_path, "w1:t2", 0.3) as other:
            assert other  # another tab's lock is free
    finally:
        release.set()
        holder.join(10)
    with state.tab_lock(tmp_path, "w1:t1", 2.0) as got:
        assert got

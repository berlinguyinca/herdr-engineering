"""Side pane safety rules that need no Herdr server: the viewer's quit guard, marker validation, dismissal records,
pruning, the bounded event follower, the CI deadline and the gauge row. Real files, real processes, real ptys.

Seams used (and why): `run_viewer(clock=...)` and `CiWorker(clock=..., fetch=...)` take a clock or a callable so the
scheduling rules run in milliseconds; where the fetched DATA matters the test reads a real local HTTP server that serves
recorded Woodpecker JSON (see test_side_pane_ci.py).
"""
from __future__ import annotations

import dataclasses
import io
import os
import pty
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

from herdr_engineering.handoff_controller.events import EventLog
from herdr_engineering.handoff_controller.herdr_cli import AgentPane
from herdr_engineering.pane_viewer import run_viewer
from herdr_engineering.side_pane import app, state
from herdr_engineering.side_pane import placement as placing
from herdr_engineering.side_pane.ci_fetch import Chain, CiWorker
from herdr_engineering.side_pane.ci_tab import CiCard, CiState, build_cards, lines
from herdr_engineering.side_pane.follow import EventFollower
from herdr_engineering.side_pane.herdr import Layout, PaneRow, Rect, SideHerdr
from herdr_engineering.side_pane.model import build_model
from tests.handoff_controller_herdr import wait_for
from tests.test_side_pane_ci import PULLS, REPO, Served, woodpecker  # noqa: F401  (a fixture)
from tests.test_side_pane_model import NOW, ago, event, session_ref, three_session_store

REPO_ROOT = Path(__file__).resolve().parents[1]


# ------------------------------------------------------------------------------------- the viewer's quit guard (A1, A2)
def drive_viewer(chunks: list[tuple[float, float | None, bytes]], **kwargs: object) -> tuple[int, list[str], list[float], float]:
    """Run the real viewer on a real pty. Each chunk is (seconds after start, clock value to set first, bytes typed).
    Returns the exit code, the text `on_text` saw, the clock readings at each quit, and when the viewer returned."""
    master, slave = pty.openpty()
    clock = [0.0]
    texts: list[str] = []
    quits: list[float] = []
    started = time.monotonic()

    def send(now: float | None, data: bytes) -> None:
        if now is not None:
            clock[0] = now
        os.write(master, data)

    timers = [threading.Timer(after, send, args=(now, data)) for after, now, data in chunks]
    for timer in timers:
        timer.start()
    try:
        code = run_viewer(lambda w, h: ["x"], scroll=False, fd_in=slave, out=io.StringIO(), size=lambda: (40, 10), refresh_s=0.05,
                          on_text=lambda text: texts.append(text) or False, clock=lambda: clock[0],
                          on_quit=lambda: quits.append(clock[0]), **kwargs)  # type: ignore[arg-type]
        return code, texts, quits, time.monotonic() - started
    finally:
        for timer in timers:
            timer.cancel()
        os.close(master)
        os.close(slave)


def test_the_viewer_ignores_quit_keys_for_its_first_two_seconds_and_reports_the_one_that_ends_it() -> None:
    code, texts, quits, elapsed = drive_viewer([(0.3, None, b"q"), (0.5, None, b"Q"), (1.0, 5.0, b"q")], quit_ignored_s=2.0)
    assert code == 0 and quits == [5.0]  # the early q and Q did nothing; the one after the guard ended it
    assert elapsed >= 0.9 and texts[:2] == ["q", "Q"]


def test_a_quit_key_inside_a_longer_chunk_is_not_a_quit_when_only_a_lone_key_counts() -> None:
    code, texts, quits, _ = drive_viewer([(0.3, 5.0, b"pasted text with a q in it"), (0.8, None, b"q")], quit_ignored_s=2.0,
                                         lone_quit=True)
    assert code == 0 and quits == [5.0] and texts[0] == "pasted text with a q in it"


def test_the_viewer_without_a_guard_quits_at_once_and_on_eof() -> None:
    code, _, quits, elapsed = drive_viewer([(0.3, None, b"q")])
    assert code == 0 and quits == [0.0] and elapsed < 2.0


def test_a_chunk_with_a_q_in_it_is_not_a_tab_key(tmp_path: Path) -> None:
    pane_app = app.PaneApp("w1:t1", "w1:p2", SideHerdr(binary=str(tmp_path / "no-herdr")), {"HERDR_ENGINEERING_STATE": str(tmp_path)},
                           fetch_ci=None, store_root=tmp_path / "store")
    assert pane_app.on_text("hello q, ctrl-c is \x03 and Q") is False


def test_the_runner_asks_the_viewer_for_the_two_second_guard() -> None:
    assert app.QUIT_IGNORED_S == 2.0


# ---------------------------------------------------------------------------------------- the marker is validated (A5)
def agent(pane: str = "w1:p2", tab: str = "w1:t1") -> AgentPane:
    return AgentPane(pane, tab, "claude", "idle", None, None)


def marker(*, pid: int | None, runner_start: str | None = None, terminal: str = "term_side") -> state.Marker:
    return state.Marker("w1:t1", "w1:p2", terminal, "term_session", pid, 100.0, runner_start)


def test_a_process_has_a_start_time_that_a_reused_pid_would_not_share(tmp_path: Path) -> None:
    mine = state.process_start(os.getpid())
    assert mine and mine == state.process_start(os.getpid())
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert wait_for(lambda: state.process_start(other.pid) is not None, 5.0)
        assert state.process_start(other.pid) != mine or other.pid == os.getpid()
    finally:
        other.kill()
        other.wait()
    assert state.process_start(other.pid) is None  # gone


def test_a_marker_whose_pid_belongs_to_another_live_process_is_not_a_side_pane_and_is_removed(tmp_path: Path) -> None:
    start = state.process_start(os.getpid())
    state.write_marker(tmp_path, marker(pid=os.getpid(), runner_start=start))
    assert state.is_marked(tmp_path, agent(), now=100.0, terminal_id="term_side")
    state.write_marker(tmp_path, marker(pid=os.getpid(), runner_start="Mon Jan  1 00:00:00 1990"))  # the pid was reused
    assert not state.is_marked(tmp_path, agent(), now=100.0, terminal_id="term_side")
    assert state.read_marker(tmp_path, "w1:t1") is None


def test_a_marker_for_another_terminal_is_not_this_pane_and_is_removed(tmp_path: Path) -> None:
    state.write_marker(tmp_path, marker(pid=os.getpid(), runner_start=state.process_start(os.getpid())))
    assert not state.is_marked(tmp_path, agent(), now=100.0, terminal_id="a_terminal_that_reused_the_pane_id")
    assert state.read_marker(tmp_path, "w1:t1") is None


def test_a_listing_without_terminal_ids_still_matches_by_pane_id_and_runner(tmp_path: Path) -> None:
    state.write_marker(tmp_path, marker(pid=os.getpid(), runner_start=state.process_start(os.getpid())))
    assert state.is_marked(tmp_path, agent(), now=100.0)


def test_the_start_grace_is_a_constant_and_a_parameter(tmp_path: Path) -> None:
    assert state.STARTUP_GRACE_S > 0
    young = marker(pid=None)
    assert state.marker_live(young, 100.0 + state.STARTUP_GRACE_S - 1) and not state.marker_live(young, 100.0 + state.STARTUP_GRACE_S + 1)
    assert not state.marker_live(young, 101.0, grace_s=0.0)


# ------------------------------------------------------------------------------------ dismissal records and pruning (A4, A6)
def test_the_sessions_a_pane_was_shown_for_are_kept_per_tab_and_never_emptied(tmp_path: Path) -> None:
    assert state.read_shown(tmp_path, "w1:t1") == frozenset()
    state.write_shown(tmp_path, "w1:t1", ["t_a", "t_b"])
    state.write_shown(tmp_path, "w1:t1", [])  # Herdr did not answer: the last known set stays
    assert state.read_shown(tmp_path, "w1:t1") == {"t_a", "t_b"}
    state.write_shown(tmp_path, "w1:t1", ["t_c"])
    assert state.read_shown(tmp_path, "w1:t1") == {"t_c"}
    assert state.read_shown(tmp_path, "w1:t2") == frozenset()


def test_an_empty_dismissal_is_never_written(tmp_path: Path) -> None:
    state.dismiss(tmp_path, "w1:t1", [])
    assert not any(tmp_path.glob("*dismissed*"))
    state.dismiss(tmp_path, "w1:t1", ["t_a"])
    state.dismiss(tmp_path, "w1:t1", [])
    assert state.read_dismissed(tmp_path, "w1:t1") == {"t_a"}


def test_prune_removes_the_state_of_tabs_herdr_no_longer_lists(tmp_path: Path) -> None:
    for tab in ("w1:t1", "w1:t2", "w3:t9"):
        state.write_marker(tmp_path, state.Marker(tab, "p", "t", None, None, 1.0))
        state.dismiss(tmp_path, tab, ["x"])
        state.write_shown(tmp_path, tab, ["x"])
        with state.tab_lock(tmp_path, tab, 1.0):
            pass
    state.prune(tmp_path, {"w1:t1", "w1:t2"})
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(
        f"w1:{t}.{k}" for t in ("t1", "t2") for k in ("marker.json", "dismissed.json", "shown.json", "lock"))
    state.prune(tmp_path, set())
    assert list(tmp_path.iterdir()) == []


def test_prune_of_a_missing_directory_is_quiet(tmp_path: Path) -> None:
    state.prune(tmp_path / "nothing", {"w1:t1"})


# ------------------------------------------------------------------------------------ deciding from facts only (A3, A4)
def rows(*pairs: tuple[str, str]) -> list[PaneRow]:
    return [PaneRow(pane, "w1:t1", terminal, False) for pane, terminal in pairs]


WIDE = Layout("w1:t1", None, {"w1:p1": Rect(0, 0, 120, 40)})


def decide(**kw: object) -> placing.Decision:
    base: dict[str, object] = dict(rows=rows(("w1:p1", "t_session")), sessions=rows(("w1:p1", "t_session")), marker=None,
                                   dismissed=frozenset(), layout=WIDE, target_pane=None, now=1000.0, force=False)
    return placing.decide(**{**base, **kw})  # type: ignore[arg-type]


def test_a_marker_whose_runner_never_started_closes_its_pane_row_before_a_new_one_is_made() -> None:
    old = state.Marker("w1:t1", "w1:p2", "t_side", "t_session", None, 1.0)  # ensure wrote it long ago, nothing ever ran
    both = rows(("w1:p1", "t_session"), ("w1:p2", "t_side"))
    plan = decide(rows=both, marker=old, now=1000.0)
    assert plan.outcome is None and plan.close_pane == "w1:p2" and plan.stale_marker
    young = dataclasses.replace(old, created=999.0)
    assert decide(rows=both, marker=young, now=1000.0).outcome is placing.Outcome.EXISTS
    assert decide(rows=both, marker=young, now=1000.0, grace_s=0.0).close_pane == "w1:p2"


def test_a_row_that_is_not_the_markers_terminal_is_never_closed() -> None:
    old = state.Marker("w1:t1", "w1:p2", "t_side", "t_session", None, 1.0)
    reused = rows(("w1:p1", "t_session"), ("w1:p2", "t_somebody_elses_pane"))  # the pane id was reused by another terminal
    plan = decide(rows=reused, marker=old)
    assert plan.close_pane is None and plan.stale_marker


def test_a_pane_that_vanished_without_a_marker_is_dismissed_for_the_sessions_it_was_shown_for() -> None:
    plan = decide(shown=frozenset({"t_session"}))
    assert plan.outcome is placing.Outcome.DISMISSED and plan.dismiss == ("t_session",)
    two = rows(("w1:p1", "t_session"), ("w1:p3", "t_new"))
    grown = decide(rows=two, sessions=two, shown=frozenset({"t_session"}))
    assert grown.outcome is None and grown.target  # a new managed session appeared: the pane comes back
    assert decide(shown=frozenset({"t_session"}), force=True).outcome is None
    assert decide(shown=frozenset()).outcome is None  # never shown: the first pane


def test_the_runner_command_is_checked_before_anything_is_created(tmp_path: Path) -> None:
    assert placing.runner_command(placing.DEFAULT_COMMAND, {"PATH": str(tmp_path), "HERDR_HANDOFF_BIN_DIRS": ""}) is None
    script = tmp_path / "bin" / "herdr-eng"
    script.parent.mkdir()
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    found = placing.runner_command(placing.DEFAULT_COMMAND, {"PATH": "/nonexistent", "HERDR_HANDOFF_BIN_DIRS": str(script.parent)})
    assert found == f"exec {script} side-pane run"  # absolute, from the controller's bin dirs
    assert placing.runner_command("exec sleep 600", {"PATH": ""}) == "exec sleep 600"  # a command given by hand is left alone


# ------------------------------------------------------------------------------------------- the CLI says what happened
def test_the_ensure_command_prints_the_focus_outcome_when_it_created_a_pane() -> None:
    from herdr_engineering.side_pane import cli as side_cli

    created = placing.EnsureResult(placing.Outcome.CREATED, "w1:p2", placing.FocusOutcome.LEFT_ON_SIDE_PANE)
    assert side_cli.describe(created, "w1:t1") == "side-pane w1:t1: created w1:p2 (focus: left_on_side_pane)"
    missing = side_cli.describe(placing.EnsureResult(placing.Outcome.RUNNER_MISSING), "w1:t1")
    assert missing.startswith("side-pane w1:t1: runner_missing") and "herdr-eng" in missing


# --------------------------------------------------------------------------------- the event follower is bounded (A7)
def test_the_follower_keeps_only_the_newest_events_up_to_its_cap(tmp_path: Path) -> None:
    log = EventLog(tmp_path)
    for n in range(12):
        log.append(event("handoff.triggered", chain=f"c{n}", pane="p-1", session="s1", at=ago(100 - n)))
    follower = EventFollower(tmp_path, max_events=5)
    events, changed = follower.poll()
    assert changed and [e.chain_id for e in events] == [f"c{n}" for n in range(7, 12)]
    log.append(event("handoff.triggered", chain="c12", pane="p-1", session="s1", at=ago(1)))
    events, changed = follower.poll()
    assert changed and len(events) == 5 and events[-1].chain_id == "c12"


def test_the_default_cap_is_documented_and_large_enough_for_a_long_chain() -> None:
    from herdr_engineering.side_pane import follow

    assert follow.MAX_EVENTS >= 5000 and "MAX_EVENTS" in (follow.__doc__ or "")


# ---------------------------------------------------------------------- the gauge belongs to the focused session (A7)
def test_the_gauge_goes_to_the_row_of_the_focused_session_not_the_live_one(tmp_path: Path) -> None:
    three_session_store(tmp_path)
    older = session_ref("p-2", "s2")  # still open beside its successor
    model = build_model(older, tmp_path, {}, NOW)
    shown = app._with_gauge(model, older, 42.0)
    assert [r.context_pct for r in shown.rows] == [None, 42.0, None]
    live = session_ref("p-3", "s3")
    assert [r.context_pct for r in app._with_gauge(model, live, 7.0).rows] == [None, None, 7.0]
    assert app._with_gauge(model, session_ref("p-9", "s9"), 7.0) == model  # a session not in the chain gets no gauge


# --------------------------------------------------- the CI read has a deadline and keeps the last good data, stale (A7)
class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


GOOD = CiCard("#1", "a pull request", CiState.PASSED, ("lint",), None, "feat/x")


def test_a_ci_read_past_its_deadline_frees_the_worker_and_shows_the_last_good_cards_as_stale() -> None:
    clock = Clock()
    release, third = threading.Event(), threading.Event()
    calls: list[int] = []

    def fetch(cwd: Path, chain: Chain = ()) -> tuple[CiCard, ...]:  # scheduling seam: the first read is good, the second hangs, the third is good
        calls.append(1)
        if len(calls) == 2:
            release.wait(20)
            return (dataclasses.replace(GOOD, title="from the hung read"),)
        if len(calls) == 3:
            third.wait(20)  # the replacement read is slow too, so the abandoned read's notice can be looked at
        return (GOOD,)

    worker = CiWorker(fetch, clock=clock, deadline_s=60.0)
    assert worker.request(Path("/w")) and wait_for(lambda: worker.latest() is not None, 5.0)
    assert worker.latest().cards == (GOOD,)  # type: ignore[union-attr]
    assert worker.request(Path("/w")) and wait_for(lambda: len(calls) == 2, 5.0)
    clock.now += 30
    assert not worker.request(Path("/w"))  # still within the deadline
    clock.now += 31
    assert worker.request(Path("/w"))  # past it: the hung read is given up and a new one starts
    latest = worker.latest()
    assert latest is not None and latest.error is not None and "60" in latest.error
    assert [c.state for c in latest.cards] == [CiState.UNAVAILABLE, CiState.PASSED]
    assert latest.cards[1].stale and not latest.cards[0].stale
    third.set()
    release.set()  # the hung read finally ends: it must not overwrite what the newer read published
    assert wait_for(lambda: len(calls) == 3, 5.0)
    assert wait_for(lambda: worker.latest() is not None and worker.latest().cards == (GOOD,), 5.0)  # type: ignore[union-attr]
    time.sleep(0.2)
    assert worker.latest().cards == (GOOD,)  # type: ignore[union-attr]


def test_a_failed_read_keeps_the_last_good_cards_stale_too() -> None:
    results: list[tuple[CiCard, ...] | Exception] = [(GOOD,), OSError("network down")]

    def fetch(cwd: Path, chain: Chain = ()) -> tuple[CiCard, ...]:
        outcome = results.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    worker = CiWorker(fetch, clock=Clock())
    worker.request(Path("/w"))
    assert wait_for(lambda: worker.latest() is not None and worker.latest().error is None, 5.0)  # type: ignore[union-attr]
    assert wait_for(lambda: worker.request(Path("/w")), 5.0)
    assert wait_for(lambda: worker.latest() is not None and worker.latest().error is not None, 5.0)  # type: ignore[union-attr]
    cards = worker.latest().cards  # type: ignore[union-attr]
    assert [c.state for c in cards] == [CiState.UNAVAILABLE, CiState.PASSED] and cards[1].stale


def test_stale_cards_say_so_and_do_not_raise_the_attention_dot() -> None:
    from herdr_engineering.agent_ops_tui import RenderOpts
    from herdr_engineering.side_pane.ci_tab import ci_attention

    failing = dataclasses.replace(GOOD, state=CiState.FAILED, stale=True)
    text = "\n".join(lines((failing,), 50, RenderOpts(color=False)))
    assert "stale" in text and not ci_attention((failing,))


def test_a_worker_reads_real_pipelines_from_a_local_server_that_serves_the_recorded_json(woodpecker: Served) -> None:  # noqa: F811
    provider, seen = woodpecker
    worker = CiWorker(lambda cwd, chain=(): build_cards(REPO, ["main"], provider, PULLS), clock=time.time)
    worker.request(Path("/w"))
    assert wait_for(lambda: worker.latest() is not None, 10.0)
    latest = worker.latest()
    assert latest is not None and latest.error is None and latest.cards and seen  # the HTTP requests really happened

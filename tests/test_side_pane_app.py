"""The side pane runner: the screen and its keys as plain logic, and the real runner in real panes of an isolated Herdr."""
from __future__ import annotations

import dataclasses
import json
import re
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from herdr_engineering.agent_ops_tui import visible_width
from herdr_engineering.handoff_adapters.claude import ClaudeAdapter
from herdr_engineering.handoff_controller.events import EventLog
from herdr_engineering.side_pane import app, state
from herdr_engineering.side_pane.ci_fetch import CiSchedule, CiWorker, repo_slug
from herdr_engineering.side_pane.ci_tab import CiCard, CiState
from herdr_engineering.side_pane.herdr import SideHerdr
from herdr_engineering.side_pane.model import EMPTY, SidePaneModel, build_model
from herdr_engineering.side_pane.render import Tab
from herdr_engineering.side_pane.schedule import CI_REFRESH_S
from tests.handoff_controller_herdr import IsolatedHerdr, isolated_herdr, wait_for
from tests.test_side_pane_model import (
    NOW,
    ago,
    event,
    session_ref,
    three_session_store,
    write_record,
)

herdr_server = pytest.mark.herdr_server
REPO = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------------------------- the screen: plain logic
def three_session_model(store: Path) -> SidePaneModel:
    three_session_store(store)
    return build_model(session_ref("p-3", "s3"), store, {}, NOW)


def test_keys_switch_tabs(tmp_path: Path) -> None:
    screen = app.Screen()
    assert screen.tab is Tab.SUMMARY
    assert screen.key("2") and screen.tab is Tab.HANDOFFS
    assert screen.key("3") and screen.tab is Tab.CI
    assert screen.key("1") and screen.tab is Tab.SUMMARY
    assert screen.key("\t") and screen.tab is Tab.HANDOFFS
    assert screen.key("\t") and screen.key("\t") and screen.tab is Tab.SUMMARY  # Tab wraps round
    assert not screen.key("x") and not screen.key("z") and not screen.key("[") and screen.tab is Tab.SUMMARY  # no other actions
    assert not screen.key("\x1b[A") and screen.tab is Tab.SUMMARY


def test_keys_pressed_together_are_applied_in_order() -> None:
    screen = app.Screen()
    assert screen.key("23") and screen.tab is Tab.CI


def test_each_tab_renders_the_published_model(tmp_path: Path) -> None:
    screen = app.Screen()
    screen.publish(app.Snapshot(three_session_model(tmp_path), None, ""))
    assert not any("brainstorm the guard now" in line for line in screen.render(60, 30, NOW))  # the summary tab is not the chain
    screen.key("2")
    assert any("brainstorm the guard now" in line for line in screen.render(60, 30, NOW))
    screen.key("3")
    assert any("no CI to show" in line for line in screen.render(60, 30, NOW))


@pytest.mark.parametrize("width", [1, 2, 5, 20, 21, 30])
@pytest.mark.parametrize("height", [1, 2, 3, 6])
def test_narrow_and_tiny_panes_never_crash(tmp_path: Path, width: int, height: int) -> None:
    screen = app.Screen()
    screen.publish(app.Snapshot(three_session_model(tmp_path), None, "herdr is not answering"))
    for key in ("1", "2", "3"):
        screen.key(key)
        lines = screen.render(width, height, NOW)
        assert 1 <= len(lines) <= height
        assert all(visible_width(line) <= max(width, 1) for line in lines)


def test_hostile_text_never_reaches_the_terminal(tmp_path: Path) -> None:
    write_record(tmp_path, 1, pane="p-1", session="s1", goal="\x1b]0;pwned\x07\x1b[2Jgoal\x1b[31m red", child="p-2",
                 child_session="s2", completed=["\x1b[?1049h done\x00"], remaining=["next\x9b31m"])
    model = build_model(session_ref("p-2", "s2"), tmp_path, {}, NOW)
    screen = app.Screen()
    screen.publish(app.Snapshot(model, None, "note \x1b[0m"))
    for key in ("1", "2", "3"):
        screen.key(key)
        text = "\n".join(screen.render(60, 30, NOW))
        assert "\x1b" not in text and "\x9b" not in text and "\x00" not in text  # plain opts: no colour codes either


def test_an_error_in_one_tab_is_one_line_in_that_tab_only(tmp_path: Path) -> None:
    model = three_session_model(tmp_path)
    broken = dataclasses.replace(model, rows=tuple(dataclasses.replace(r, duration_s="not a number") for r in model.rows))  # type: ignore[arg-type]
    screen = app.Screen()
    screen.publish(app.Snapshot(broken, None, ""))
    screen.key("2")
    handoffs = screen.render(40, 10, NOW)
    assert any("error" in line for line in handoffs) and len(handoffs) <= 10
    screen.key("1")
    summary = screen.render(40, 10, NOW)
    assert not any("error" in line for line in summary) and any("Goal" in line for line in summary)


def test_the_tab_label_carries_the_attention_dot(tmp_path: Path) -> None:
    waiting = EventLog(tmp_path)
    waiting.append(event("handoff.triggered", chain="c1", pane="p-1", session="s1", at=ago(5), pct=81.0))
    model = build_model(session_ref("p-1", "s1"), tmp_path, {}, NOW)
    screen = app.Screen()
    screen.publish(app.Snapshot(model, None, ""))
    assert "Handoffs ●" in screen.render(40, 5, NOW)[0]


def test_no_session_note_is_shown() -> None:
    screen = app.Screen()
    screen.publish(app.Snapshot(EMPTY, None, "no session in this tab"))
    assert any("no session in this tab" in line for line in screen.render(40, 8, NOW))


# ------------------------------------------------------------------------------------------------------ arguments
def test_run_without_a_pane_or_tab_exits_2_with_one_line(capsys: pytest.CaptureFixture[str]) -> None:
    assert app.run([], env={}) == 2
    err = capsys.readouterr().err
    assert len(err.strip().splitlines()) == 1 and "HERDR_PANE_ID" in err


def test_run_rejects_unknown_arguments_with_2() -> None:
    assert app.run(["--nope"], env={}) == 2


# ------------------------------------------------------------------------------------------------------ CI worker
def test_repo_slug_reads_ssh_and_https_remotes() -> None:
    assert repo_slug("git@github.com:org/repo.git") == "org/repo"
    assert repo_slug("https://github.com/org/repo") == "org/repo"
    assert repo_slug("ssh://git@host:2222/org/repo.git") == "org/repo"
    assert repo_slug("/local/bare/path.git") is None
    assert repo_slug("") is None


def test_a_slow_ci_read_never_blocks_the_caller() -> None:
    release = threading.Event()
    card = CiCard("#1", "t", CiState.PASSED, (), None)

    def slow(cwd: Path) -> tuple[CiCard, ...]:
        release.wait(10)
        return (card,)

    worker = CiWorker(slow, clock=time.monotonic)
    started = time.monotonic()
    worker.request(Path("/work"))
    worker.request(Path("/work"))  # a second request while one runs starts nothing
    assert time.monotonic() - started < 0.5
    assert worker.latest() is None  # nothing yet, and nothing waited for
    release.set()
    assert wait_for(lambda: worker.latest() is not None, 5.0)
    latest = worker.latest()
    assert latest is not None and latest.cards == (card,) and latest.error is None and latest.cwd == Path("/work")


def test_a_failing_ci_read_becomes_a_typed_unavailable_result() -> None:
    def boom(cwd: Path) -> tuple[CiCard, ...]:
        raise OSError("network down")

    worker = CiWorker(boom, clock=time.monotonic)
    worker.request(Path("/work"))
    assert wait_for(lambda: worker.latest() is not None, 5.0)
    latest = worker.latest()
    assert latest is not None and latest.error == "OSError: network down"
    assert [c.state for c in latest.cards] == [CiState.UNAVAILABLE]


def test_ci_is_refreshed_no_faster_than_the_schedule() -> None:
    calls: list[float] = []
    worker = CiWorker(lambda cwd: calls.append(1.0) or (), clock=time.monotonic)  # type: ignore[func-returns-value]
    scheduler = CiSchedule(worker, interval_s=60.0)
    assert scheduler.maybe_request(Path("/w"), now=0.0)
    assert wait_for(lambda: len(calls) == 1, 5.0)
    assert not scheduler.maybe_request(Path("/w"), now=59.0)
    assert scheduler.maybe_request(Path("/other"), now=59.0)  # another session's directory: read at once
    assert wait_for(lambda: len(calls) == 2, 5.0)
    assert scheduler.maybe_request(Path("/other"), now=120.0)
    assert wait_for(lambda: len(calls) == 3, 5.0)
    assert CI_REFRESH_S >= 60.0


# --------------------------------------------------------------------------------------- the real runner in real panes
def home_of(h: IsolatedHerdr) -> Path:
    return h.root / "home"


def store_of(h: IsolatedHerdr) -> Path:
    return home_of(h) / ".local" / "share" / "herdr-handoff" / "store"


def side_state(h: IsolatedHerdr) -> Path:
    return state.state_dir({"HOME": str(home_of(h))})


def runner_command(*args: str) -> str:
    return "exec " + shlex.join(["env", f"PYTHONPATH={REPO}", sys.executable, "-m", "herdr_engineering.cli", "side-pane", "run",
                                 "--no-ci", *args])


def screen_of(side: SideHerdr, pane: str) -> str:
    done = side._run(["pane", "read", pane, "--source", "visible"], 15.0)
    return done.stdout if done is not None and done.returncode == 0 else ""


def agent_listed(side: SideHerdr, pane: str) -> bool:
    return any(a.pane_id == pane and a.name == "claude" for a in side.agents() or [])


def open_side_pane(side: SideHerdr, target: str, *args: str) -> str:
    row = side.split(target, 0.7, env={})
    assert row is not None
    assert side.run_in(row.pane_id, runner_command(*args))
    return row.pane_id


def second_session(h: IsolatedHerdr, side: SideHerdr, first: str) -> str:
    row = side.split(first, 0.5, env={})
    assert row is not None
    assert side.run_in(row.pane_id, str(h.program))
    assert wait_for(lambda: agent_listed(side, row.pane_id), 20.0)
    return row.pane_id


def start_session(h: IsolatedHerdr, side: SideHerdr) -> str:
    pane = h.start()
    assert wait_for(lambda: agent_listed(side, pane), 20.0)
    return pane


def record_for(h: IsolatedHerdr, n: int, pane: str, goal: str, child: str | None = None) -> None:
    write_record(store_of(h), n, pane=pane, session=f"sess-{n}", goal=goal, created=ago(30), child=child,
                 child_session=f"sess-{n}c" if child else None, completed=[f"done by {goal}"])


@herdr_server
def test_runner_renders_three_tabs_and_switches_on_keys() -> None:
    with isolated_herdr("sp-app-tabs") as h:
        side = SideHerdr(h.cli._binary, h.env, h.root)
        session = start_session(h, side)
        record_for(h, 1, session, "ship the filter chips")
        EventLog(store_of(h)).append(event("handoff.triggered", chain="c1", pane=session, session="sess-1", at=ago(1), pct=81.0))
        pane = open_side_pane(side, session)
        assert wait_for(lambda: "ship the filter chips" in screen_of(side, pane), 30.0), screen_of(side, pane)
        first = screen_of(side, pane)
        assert "Summary" in first and "Handoffs" in first and "CI" in first
        assert side.send_keys(pane, ["2"])
        assert wait_for(lambda: "handoffs" in screen_of(side, pane) and "#1 ship the filter" in screen_of(side, pane), 10.0)
        assert side.send_keys(pane, ["3"])
        assert wait_for(lambda: "no CI to show" in screen_of(side, pane) or "CI off" in screen_of(side, pane), 10.0)
        assert side.send_keys(pane, ["tab"])  # wraps to Summary
        assert wait_for(lambda: "Goal" in screen_of(side, pane), 10.0)
        assert side.send_keys(pane, ["x"])  # no other actions: nothing changes, nothing crashes
        time.sleep(0.5)
        assert "Goal" in screen_of(side, pane)
        marker = state.read_marker(side_state(h), "w1:t1")
        assert marker is not None and marker.pane_id == pane and marker.pid is not None  # the pane registers itself


@herdr_server
def test_q_closes_the_pane_and_records_the_dismissal() -> None:
    with isolated_herdr("sp-app-quit") as h:
        side = SideHerdr(h.cli._binary, h.env, h.root)
        session = start_session(h, side)
        pane = open_side_pane(side, session)
        assert wait_for(lambda: state.read_marker(side_state(h), "w1:t1") is not None
                        and state.read_marker(side_state(h), "w1:t1").pid is not None, 30.0)  # type: ignore[union-attr]
        assert side.send_keys(pane, ["q"])
        assert wait_for(lambda: pane not in [r.pane_id for r in side.panes() or []], 20.0), "the pane did not exit"
        assert state.read_marker(side_state(h), "w1:t1") is None
        terminal = next(r.terminal_id for r in side.panes() or [] if r.pane_id == session)
        assert terminal in state.read_dismissed(side_state(h), "w1:t1")


@herdr_server
def test_each_pane_follows_its_own_tabs_focus_with_no_cross_talk() -> None:
    with isolated_herdr("sp-app-focus") as h:
        side = SideHerdr(h.cli._binary, h.env, h.root)
        a1 = start_session(h, side)
        a2 = second_session(h, side, a1)  # same tab as a1
        b1 = start_session(h, side)  # another workspace, another tab
        assert side.tab_of(a1) == side.tab_of(a2) != side.tab_of(b1)
        record_for(h, 1, a1, "goal of A one")
        record_for(h, 2, a2, "goal of A two")
        record_for(h, 3, b1, "goal of B one")
        pane_a, pane_b = open_side_pane(side, a1), open_side_pane(side, b1)
        assert wait_for(lambda: "goal of B one" in screen_of(side, pane_b), 30.0), screen_of(side, pane_b)
        assert wait_for(lambda: "goal of A" in screen_of(side, pane_a), 30.0)
        side.focus_pane(a2)
        assert wait_for(lambda: "goal of A two" in screen_of(side, pane_a), 15.0), screen_of(side, pane_a)
        assert "goal of B one" in screen_of(side, pane_b) and "goal of A" not in screen_of(side, pane_b)  # no cross-talk
        side.focus_pane(a1)
        assert wait_for(lambda: "goal of A one" in screen_of(side, pane_a), 15.0)
        assert "goal of B one" in screen_of(side, pane_b)


@herdr_server
def test_refresh_reads_the_live_context_gauge_through_the_adapter() -> None:
    with isolated_herdr("sp-app-gauge") as h:
        side = SideHerdr(h.cli._binary, h.env, h.root)
        parent = start_session(h, side)
        live = second_session(h, side, parent)
        cwd = next(a.cwd for a in side.agents() or [] if a.pane_id == live)
        assert cwd is not None
        done = subprocess.run(["herdr", "pane", "report-agent-session", live, "--source", "herdr:claude", "--agent", "claude",
                               "--seq", "1", "--agent-session-id", "sess-live"], env=h.env, cwd=h.root,
                              capture_output=True, text=True, check=False)
        assert done.returncode == 0, done.stderr
        write_record(store_of(h), 1, pane=parent, session="sess-1", goal="parent goal", created=ago(30), child=live,
                     child_session="sess-live")
        claude_home = h.root / "claude-home"
        project = claude_home / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(cwd))
        project.mkdir(parents=True)
        (project / "sess-live.jsonl").write_text(json.dumps({"type": "assistant", "isSidechain": False, "message": {
            "model": "claude-opus-4-8", "usage": {"input_tokens": 150_000, "cache_read_input_tokens": 0,
                                                  "cache_creation_input_tokens": 0}}}) + "\n")
        pane_app = app.PaneApp(side.tab_of(live), None, side, {"HOME": str(home_of(h))}, fetch_ci=None,
                               adapter_for=lambda harness: ClaudeAdapter(home=claude_home, env=h.env))
        assert side.focus_pane(live)
        pane_app.refresh(time.time())
        assert pane_app.snapshot.session is not None and pane_app.snapshot.session.pane_id == live
        pane_app.screen.key("2")
        text = "\n".join(pane_app.screen.render(60, 30, time.time()))
        assert "75%" in text, text  # 150k of the 200k window, read now, not what the event log last carried


@herdr_server
def test_runner_survives_a_missing_store_and_a_gone_pane() -> None:
    with isolated_herdr("sp-app-missing") as h:
        side = SideHerdr(h.cli._binary, h.env, h.root)
        session = start_session(h, side)
        assert not store_of(h).exists()
        pane = open_side_pane(side, session)
        assert wait_for(lambda: "Summary" in screen_of(side, pane), 30.0)  # no store, no event log: still draws
        assert side.send_keys(pane, ["2"])
        assert wait_for(lambda: "no handoffs" in screen_of(side, pane), 10.0)
        assert side.close(session)  # the followed session's pane disappears
        assert wait_for(lambda: pane in [r.pane_id for r in side.panes() or []], 5.0)
        time.sleep(3.0)  # a few refreshes later the runner still answers keys
        assert side.send_keys(pane, ["1"])
        assert wait_for(lambda: "Goal" in screen_of(side, pane) or "no briefing" in screen_of(side, pane), 10.0)
        assert side.panes() is not None and pane in [r.pane_id for r in side.panes() or []]

"""Focus following and the refresh schedule: plain logic over real values."""
from __future__ import annotations

import time
from pathlib import Path

from herdr_engineering.handoff_adapters.base import Harness
from herdr_engineering.handoff_controller.events import EventLog, HandoffEvent
from herdr_engineering.handoff_controller.herdr_cli import AgentPane
from herdr_engineering.side_pane.follow import EventFollower, choose_session
from herdr_engineering.side_pane.schedule import Due
from herdr_engineering.side_pane.style import clean


def agent(pane: str, tab: str = "w1:t1", name: str | None = "claude") -> AgentPane:
    return AgentPane(pane, tab, name, "idle", Path("/work"), None)


A1, A2, B1, SIDE = agent("w1:p1"), agent("w1:p2"), agent("w1:p7", "w1:t2"), agent("w1:p3")


def test_follows_the_focused_agent_of_its_own_tab() -> None:
    assert choose_session("w1:p2", [A1, A2, B1], "w1:t1", None) == A2
    assert choose_session("w1:p1", [A1, A2, B1], "w1:t1", A2) == A1


def test_another_tabs_agent_is_never_chosen() -> None:
    assert choose_session("w1:p7", [A1, A2, B1], "w1:t1", None) == A1  # focus names a pane of tab t2: not ours
    assert choose_session("w1:p7", [A1, A2, B1], "w1:t2", None) == B1
    assert choose_session("w1:p7", [B1], "w1:t1", None) is None


def test_focus_on_a_non_session_keeps_the_previous_session() -> None:
    assert choose_session("w1:p9", [A1, A2], "w1:t1", A2) == A2  # focus is on a shell or on the side pane
    assert choose_session(None, [A1, A2], "w1:t1", A2) == A2


def test_a_previous_session_that_is_gone_falls_back_to_a_listed_one() -> None:
    assert choose_session("w1:p9", [A1], "w1:t1", A2) == A1
    assert choose_session("w1:p9", [], "w1:t1", A2) is None


def test_herdr_not_answering_keeps_what_was_followed() -> None:
    assert choose_session("w1:p9", None, "w1:t1", A2) == A2
    assert choose_session("w1:p9", None, "w1:t1", None) is None


def test_side_panes_and_unknown_agents_are_not_sessions() -> None:
    assert choose_session("w1:p3", [A1, SIDE], "w1:t1", A1, is_side=lambda p: p.pane_id == "w1:p3") == A1
    assert choose_session("w1:p5", [agent("w1:p5", name="vim")], "w1:t1", None) is None


def test_due_fires_first_then_once_per_interval() -> None:
    due = Due(2.0)
    assert due.take(10.0)
    assert not due.take(11.9)
    assert due.take(12.0)
    assert not due.take(12.5)
    due.reset()
    assert due.take(12.5)


def test_due_survives_the_clock_going_backwards() -> None:
    due = Due(2.0)
    assert due.take(100.0)
    assert due.take(50.0)  # a wall clock set back must not stall the refresh until it catches up


def test_clean_removes_escape_sequences_and_controls() -> None:
    assert clean("ok\x1b[31mred\x1b[0m done") == "okred done"
    assert clean("a\x1b]0;title\x07b") == "ab"  # OSC
    assert clean("a\x1b]8;;http://x\x1b\\link\x1b]8;;\x1b\\") == "alink"
    assert clean("line1\nline2\ttab\r") == "line1 line2 tab "
    assert clean("bell\x07nul\x00del\x7fc1\x85x") == "bellnuldelc1x"
    assert clean("8bit\x9b31mcsi") == "8bitcsi"
    assert clean("plain ünïcode 日本") == "plain ünïcode 日本"
    assert clean("tail\x1b") == "tail"


def test_event_follower_reads_only_what_was_appended(tmp_path: Path) -> None:
    log = EventLog(tmp_path)
    follower = EventFollower(tmp_path)
    assert follower.poll() == ([], False)  # no log yet
    log.append(HandoffEvent("handoff.triggered", "2026-11-18T12:00:00Z", "c1", "w1:p1", "s", Harness.CLAUDE, None, None, None,
                            None, None, 81.0))
    events, changed = follower.poll()
    assert changed and len(events) == 1
    assert follower.poll() == (events, False)  # nothing new: same list, no re-read
    offset = follower.offset
    log.append(HandoffEvent("handoff.skipped", "2026-11-18T12:00:05Z", "c2", "w1:p1", "s", Harness.CLAUDE, None, None, None,
                            None, "no_record", None))
    events, changed = follower.poll()
    assert changed and [e.event for e in events] == ["handoff.triggered", "handoff.skipped"]
    assert follower.offset > offset


def test_event_follower_rebuilds_when_the_log_is_replaced(tmp_path: Path) -> None:
    log = EventLog(tmp_path)
    for n in range(3):
        log.append(HandoffEvent("handoff.skipped", f"2026-11-18T12:00:0{n}Z", f"c{n}", "w1:p1", None, Harness.CLAUDE, None,
                                None, None, None, "no_record", None))
    follower = EventFollower(tmp_path)
    assert len(follower.poll()[0]) == 3
    (tmp_path / "events.jsonl").unlink()  # replaced by a shorter file
    log.append(HandoffEvent("handoff.skipped", "2026-11-18T13:00:00Z", "z", "w1:p9", None, Harness.CLAUDE, None, None, None,
                            None, "no_record", None))
    events, changed = follower.poll()
    assert changed and [e.chain_id for e in events] == ["z"]


def test_event_follower_with_a_missing_store_does_not_raise(tmp_path: Path) -> None:
    assert EventFollower(tmp_path / "nowhere").poll() == ([], False)
    time.sleep(0)

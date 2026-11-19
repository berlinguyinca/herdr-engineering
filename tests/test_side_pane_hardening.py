"""Fix round 1: hostile text, bad timestamps and the model's edge cases, against real files and the real renderers."""
from __future__ import annotations

import json
import unicodedata
from dataclasses import replace
from pathlib import Path

import pytest

from herdr_engineering.agent_ops_tui import RESET, RenderOpts, sgr, visible_width
from herdr_engineering.handoff_controller.events import Colour, EventLog
from herdr_engineering.side_pane.ci_tab import CiCard, CiState
from herdr_engineering.side_pane.model import build_model
from herdr_engineering.side_pane.render import Tab, render
from herdr_engineering.side_pane.style import Style, clean, line
from tests.test_side_pane_model import (
    NOW,
    ago,
    event,
    hid,
    session_ref,
    three_session_store,
    write_record,
)

PLAIN = RenderOpts(color=False, unicode=True)
ASCII = RenderOpts(color=False, unicode=False)
HOSTILE = "hi\x1b]0;PWNED\x07\x1b[2J\nline2\r\x9b31m‮evil​\x00\x85end"


def stuck_model(tmp_path: Path, goal: str, reason: str, *, done: str = "d", now: str = "n", nxt: str = "x"):
    write_record(tmp_path, 1, pane="p-1", session="s1", goal=goal, created=ago(20), completed=[done], remaining=[nxt],
                 resume_point=now, next_commands=[])
    log = EventLog(tmp_path)
    log.append(event("handoff.triggered", chain="c1", pane="p-1", session="s1", at=ago(25)))
    log.append(event("handoff.briefing_written", chain="c1", pane="p-1", session="s1", at=ago(20), handoff=1))
    log.append(event("handoff.stuck", chain="c1", pane="p-1", session="s1", at=ago(19), handoff=1, reason=reason))
    return build_model(session_ref("p-1", "s1"), tmp_path, {}, NOW, ci=(
        CiCard(f"#1 {HOSTILE}", HOSTILE, CiState.FAILED, (HOSTILE, "unit"), None, branch=HOSTILE, failed=(HOSTILE,),
               reason=HOSTILE),
        CiCard(HOSTILE, HOSTILE, CiState.MERGED, (), None, branch=HOSTILE)))


def everything(model, width: int, opts: RenderOpts = PLAIN) -> list[str]:
    return [row for tab in Tab for row in render(model, tab, width, 200, NOW, opts)]


# ------------------------------------------------------------------------------------------------ 1. hostile text
@pytest.mark.parametrize("width", [20, 30, 60])
@pytest.mark.parametrize("opts", [PLAIN, ASCII])
def test_hostile_text_never_reaches_the_terminal(tmp_path: Path, width: int, opts: RenderOpts) -> None:
    model = stuck_model(tmp_path, HOSTILE, HOSTILE, done=HOSTILE, now=HOSTILE, nxt=HOSTILE)
    rows = everything(model, width, opts)
    assert rows
    for row in rows:
        assert not [c for c in row if unicodedata.category(c) in ("Cc", "Cf")], repr(row)
        assert "PWNED" not in row and "\n" not in row and visible_width(row) <= width


def test_a_hostile_card_is_still_one_row_per_line(tmp_path: Path) -> None:
    hostile = stuck_model(tmp_path / "a", HOSTILE, HOSTILE)
    benign = replace(hostile, ci=(CiCard("#1", "title", CiState.FAILED, ("wf", "unit"), None, branch="b", failed=("wf",),
                                         reason="wf failed"), CiCard("#2", "m", CiState.MERGED, (), None, branch="m")))
    assert len(render(hostile, Tab.CI, 60, 200, NOW, PLAIN)) == len(render(benign, Tab.CI, 60, 200, NOW, PLAIN))


def test_summary_text_is_cleaned_before_it_is_wrapped(tmp_path: Path) -> None:
    model = stuck_model(tmp_path, "word " * 30 + "\x1b[3" + "1m red\nnext", "r")
    rows = render(model, Tab.SUMMARY, 20, 200, NOW, PLAIN)
    assert not any("\x1b" in r or "\n" in r for r in rows)
    assert all("[31m" not in r for r in rows)  # an escape cut by the wrap does not survive as visible garbage either


def test_clean_is_the_one_step_and_handles_c1_osc_and_format_characters() -> None:
    assert clean("a\x9b31mb") == "ab"
    assert clean("a\x1b]8;;http://x\x1b\\link\x1b]8;;\x1b\\b") == "alinkb"
    assert clean("a\x9d0;t\x9cb") == "ab"
    assert clean("x​y‮z﻿") == "xyz"
    assert clean("tab\there\r\nnew") == "tab here new"
    assert clean("é") == "e"  # combining marks are dropped, as `cell_width` counts none


# --------------------------------------------------------------------------------------- width and emoji edge cases
@pytest.mark.parametrize("width", [0, 1, 10])
def test_tiny_widths_never_overflow_or_raise(tmp_path: Path, width: int) -> None:
    three_session_store(tmp_path)
    model = build_model(session_ref("p-3", "s3"), tmp_path, {}, NOW, ci=(CiCard("#1", "title", CiState.RUNNING, ("a",), None),))
    for tab in Tab:
        rows = render(model, tab, width, 50, NOW, PLAIN)
        assert rows and all(visible_width(r) <= max(width, 1) for r in rows)
    assert line([("abc", None)], 0, PLAIN) == "" and line([("abc", None)], 1, PLAIN) == "…"


@pytest.mark.parametrize("width", [10, 20, 30])
def test_emoji_zwj_and_combining_titles_are_measured_in_cells(tmp_path: Path, width: int) -> None:
    title = "👩‍👩‍👧 fam éé 🇩🇪 ✓ 宽宽宽 🚀🚀🚀🚀"
    model = stuck_model(tmp_path, title, title, done=title, now=title, nxt=title)
    for row in everything(model, width):
        assert visible_width(row) <= width, (width, row)
        assert "‍" not in row and "́" not in row


# ---------------------------------------------------------------------------------------------- 2. bad timestamps
def test_one_event_with_an_unparseable_time_is_skipped_and_counted(tmp_path: Path) -> None:
    three_session_store(tmp_path)
    log = EventLog(tmp_path)
    bad = event("handoff.succeeded", chain="c9", pane="p-2", session="s2", at="not-a-time", handoff=2)
    log.append(bad)
    log.append(event("handoff.triggered", chain="c9", pane="p-3", session="s3", at="", pct=50.0))
    model = build_model(session_ref("p-3", "s3"), tmp_path, {}, NOW)
    clean_model = build_model(session_ref("p-3", "s3"), tmp_path / "nowhere", {}, NOW)
    assert [r.session_number for r in model.rows] == [1, 2, 3] and clean_model.rows == ()
    assert model.unreadable == 2
    assert any(r.endswith("2 unreadable events") for r in render(model, Tab.HANDOFFS, 60, 60, NOW, PLAIN))
    one = build_model(session_ref("p-3", "s3"), tmp_path, {}, NOW, events=[bad])
    assert one.unreadable == 1 and any("1 unreadable event" in r for r in render(one, Tab.HANDOFFS, 60, 60, NOW, PLAIN))


def test_a_bad_line_among_good_ones_in_a_real_log_file(tmp_path: Path) -> None:
    three_session_store(tmp_path)
    path = tmp_path / "events.jsonl"
    lines = path.read_text().splitlines()
    first = json.loads(lines[0])
    path.write_text("\n".join([*lines, json.dumps({**first, "at": "yesterday-ish"}), "{not json"]) + "\n")
    model = build_model(session_ref("p-3", "s3"), tmp_path, {}, NOW)
    assert len(model.rows) == 3 and [r.colour for r in model.rows][:2] == [Colour.GREEN, Colour.GREEN]
    assert model.unreadable == 1


def test_no_note_without_unreadable_events(tmp_path: Path) -> None:
    three_session_store(tmp_path)
    model = build_model(session_ref("p-3", "s3"), tmp_path, {}, NOW)
    assert model.unreadable == 0 and not any("unreadable" in r for r in render(model, Tab.HANDOFFS, 60, 60, NOW, PLAIN))


# ----------------------------------------------------------------------------------- 3. two successors, 5. cycles
def two_successors(store: Path) -> None:
    write_record(store, 1, pane="p-1", session="s1", goal="root", created=ago(300), child="p-2", child_session="s2")
    # record 2 sorts first by file name but was created LAST; record 3 sorts last but was created first
    write_record(store, 2, pred=1, pane="p-2", session="s2", goal="late successor", created=ago(50))
    write_record(store, 3, pred=1, pane="p-3", session="s3", goal="early successor", created=ago(100))


def test_two_successors_choose_the_later_created_not_the_later_file_name(tmp_path: Path) -> None:
    two_successors(tmp_path)
    rows = build_model(session_ref("p-1", "s1"), tmp_path, {}, NOW).rows
    assert [r.title for r in rows][:2] == ["root", "late successor"]
    assert not rows[0].dangling


def test_the_other_successor_is_its_own_root_flagged_with_a_question_mark(tmp_path: Path) -> None:
    two_successors(tmp_path)
    model = build_model(session_ref("p-3", "s3"), tmp_path, {}, NOW)
    assert model.rows[0].title == "early successor" and model.rows[0].dangling and model.rows[0].depth == 0
    assert any(r.startswith("? #1 early successor") for r in render(model, Tab.HANDOFFS, 60, 40, NOW, PLAIN))


def test_equal_creation_times_fall_back_to_the_record_id(tmp_path: Path) -> None:
    write_record(tmp_path, 1, pane="p-1", session="s1", goal="root", created=ago(300))
    write_record(tmp_path, 2, pred=1, pane="p-2", session="s2", goal="two", created=ago(50))
    write_record(tmp_path, 3, pred=1, pane="p-3", session="s3", goal="three", created=ago(50))
    first = build_model(session_ref("p-1", "s1"), tmp_path, {}, NOW).rows
    assert [r.title for r in first][:2] == ["root", "three"] == [r.title for r in build_model(session_ref("p-1", "s1"), tmp_path, {}, NOW).rows][:2]


def test_a_cycle_and_a_self_pointing_record_terminate(tmp_path: Path) -> None:
    write_record(tmp_path, 1, pred=2, pane="p-1", session="s1", goal="a", created=ago(20))
    write_record(tmp_path, 2, pred=1, pane="p-2", session="s2", goal="b", created=ago(10))
    rows = build_model(session_ref("p-2", "s2"), tmp_path, {}, NOW).rows
    assert 1 <= len(rows) <= 3
    write_record(tmp_path / "self", 1, predecessor_id=hid(1), pane="p-1", session="s1", goal="me", created=ago(20))
    own = build_model(session_ref("p-1", "s1"), tmp_path / "self", {}, NOW).rows
    assert 1 <= len(own) <= 2 and own[0].title == "me"


# ------------------------------------------------------------------------- 5. unknown reasons, events, compaction
def test_unknown_stuck_reason_and_future_event_names_are_shown_not_fatal(tmp_path: Path) -> None:
    stuck_model(tmp_path, "goal", "a_reason_from_the_future")
    log = tmp_path / "events.jsonl"
    future = json.loads(log.read_text().splitlines()[0]) | {"event": "handoff.teleported", "at": ago(5)}
    log.write_text(log.read_text() + json.dumps(future) + "\n")  # a name only a newer controller writes
    model = build_model(session_ref("p-1", "s1"), tmp_path, {}, NOW)
    assert model.rows[0].colour is Colour.RED and model.rows[0].reason == "a_reason_from_the_future"
    assert any("stuck: a_reason_from_the_future" in r for r in render(model, Tab.HANDOFFS, 60, 40, NOW, PLAIN))


def test_a_second_takeover_started_for_compaction_is_a_failed_handoff(tmp_path: Path) -> None:
    write_record(tmp_path, 1, pane="p-1", session="s1", goal="goal", created=ago(20))
    log = EventLog(tmp_path)
    for name, reason in (("handoff.triggered", None), ("handoff.briefing_written", None), ("handoff.takeover_started", None),
                         ("handoff.takeover_started", "compacting")):
        log.append(event(name, chain="c1", pane="p-1", session="s1", at=ago(10), handoff=1, reason=reason))
    compacting = build_model(session_ref("p-1", "s1"), tmp_path, {}, NOW)
    assert compacting.chain_attention and compacting.rows[0].live
    log.append(event("handoff.failed_compacted", chain="c1", pane="p-1", session="s1", at=ago(5), handoff=1, reason="takeover refused"))
    done = build_model(session_ref("p-1", "s1"), tmp_path, {}, NOW)
    assert done.rows[0].colour is Colour.ORANGE and done.rows[0].reason == "takeover refused" and done.chain_attention


# --------------------------------------------------------------------------------------- 4. small model/render fixes
def test_a_stuck_session_does_not_carry_the_live_glyph(tmp_path: Path) -> None:
    model = stuck_model(tmp_path, "goal", "on_main_with_changes")
    row = next(r for r in render(model, Tab.HANDOFFS, 60, 40, NOW, PLAIN) if "#1 goal" in r)
    assert row.startswith("● ") and "▶" not in row
    live = three_session_store  # a live, healthy session keeps its glyph
    live(tmp_path / "ok")
    assert any(r.startswith("▶ #3") for r in render(build_model(session_ref("p-3", "s3"), tmp_path / "ok", {}, NOW), Tab.HANDOFFS, 60, 40, NOW, PLAIN))


def test_style_names_are_an_enum() -> None:
    assert {s.value for s in Style} >= {"bold", "gray", "cyan", "white", "green", "yellow", "red", "dim"}
    assert line([("x", Style.RED)], 5, RenderOpts(color=True)) == f"{sgr('red')}x{RESET}"
    assert line([("x", (Style.BOLD, Style.CYAN))], 5, RenderOpts(color=True)) == f"{sgr('bold+cyan')}x{RESET}"


# -------------------------------------------------------------------------------------------- 7. repo scoping
def test_records_of_two_repos_in_one_store_do_not_cross_talk(tmp_path: Path) -> None:
    write_record(tmp_path, 1, succ=2, pane="a-1", session="a1", goal="repo A first", created=ago(100), child="a-2", child_session="a2")
    write_record(tmp_path, 2, pred=1, pane="a-2", session="a2", goal="repo A second", created=ago(50))
    write_record(tmp_path, 11, succ=12, pane="b-1", session="b1", goal="repo B first", created=ago(90), child="b-2", child_session="b2")
    write_record(tmp_path, 12, pred=11, pane="b-2", session="b2", goal="repo B second", created=ago(40))
    log = EventLog(tmp_path)
    for chain, pane, session, handoff in (("ca", "a-1", "a1", 1), ("cb", "b-1", "b1", 11)):
        log.append(event("handoff.triggered", chain=chain, pane=pane, session=session, at=ago(110)))
        log.append(event("handoff.succeeded", chain=chain, pane=pane, session=session, at=ago(60), handoff=handoff))
    a = build_model(session_ref("a-2", "a2"), tmp_path, {}, NOW)
    b = build_model(session_ref("b-2", "b2"), tmp_path, {}, NOW)
    assert [r.title for r in a.rows][:2] == ["repo A first", "repo A second"]
    assert [r.title for r in b.rows][:2] == ["repo B first", "repo B second"]
    assert a.summary is not None and a.summary.goal == "repo A second" and b.summary is not None and b.summary.goal == "repo B second"
    assert {r.handoff_id for r in a.rows}.isdisjoint({r.handoff_id for r in b.rows} - {None})
    assert (a.totals.handoffs, a.totals.sessions) == (1, 2)

"""The three tabs as text: golden files for three widths, colour codes, truncation and the Review Focus cases."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from herdr_engineering.agent_ops_tui import RESET, RenderOpts, sgr, visible_width
from herdr_engineering.side_pane.ci_tab import CiCard, CiState
from herdr_engineering.side_pane.model import SidePaneModel, build_model
from herdr_engineering.side_pane.render import Tab, render, tab_label
from tests.test_side_pane_model import (
    NOW,
    EventLog,
    ago,
    event,
    hid,
    session_ref,
    three_session_store,
    write_record,
)

GOLDEN = Path(__file__).parent / "fixtures" / "side_pane"
PLAIN = RenderOpts(color=False, unicode=True)
COLOUR = RenderOpts(color=True, unicode=True)
ASCII = RenderOpts(color=False, unicode=False)
CARDS = (
    CiCard("#44", "box size + Copy as CSS", CiState.RUNNING, ("woodpecker", "verify"), None, running=("woodpecker",)),
    CiCard("#45", "compare/live-edit controls", CiState.FAILED, ("woodpecker", "unit-gates"), None,
           failed=("unit-gates",), reason="unit-gates failed"),
    CiCard("#46", "never pushed to CI", CiState.NOT_STARTED, (), None),
    CiCard("#47", "CI is down", CiState.UNAVAILABLE, (), None),
    CiCard("#39", "guard", CiState.MERGED, (), None),
    CiCard("#41", "filters", CiState.MERGED, (), None),
)


def rich_model(tmp_path: Path, ci: tuple[CiCard, ...] = CARDS) -> SidePaneModel:
    """Three sessions, the last one live and waiting for two subagents, with a fallback briefing and a context reading."""
    three_session_store(tmp_path)
    write_record(tmp_path, 3, pred=2, pane="p-3", session="s3", goal="Filter chips become single-select toggles",
                 created=ago(5), completed=["toggle groups + tests"], remaining=["push, PR, CI"], next_commands=[],
                 resume_point="full vitest run", fallback=True)
    log = EventLog(tmp_path)
    log.append(event("handoff.triggered", chain="c3", pane="p-3", session="s3", at=ago(6), pct=88.0))
    log.append(event("handoff.waiting", chain="c3", pane="p-3", session="s3", at=ago(2), reason="subagents", live=2))
    return build_model(session_ref("p-3", "s3"), tmp_path, {}, NOW, ci=ci)


def failure_model(tmp_path: Path) -> SidePaneModel:
    write_record(tmp_path, 1, pane="p-1", session="s1", goal="fix the thing", created=ago(30))
    write_record(tmp_path, 2, pred=1, pane="p-2", session="s2", goal="fix it again", created=ago(20))
    log = EventLog(tmp_path)
    for n, (pane, session, name, reason) in enumerate((("p-1", "s1", "handoff.failed_compacted", "takeover refused"),
                                                        ("p-2", "s2", "handoff.stuck", "on_main_with_changes")), start=1):
        log.append(event("handoff.triggered", chain=f"c{n}", pane=pane, session=session, at=ago(40 - 10 * n)))
        log.append(event("handoff.briefing_written", chain=f"c{n}", pane=pane, session=session, at=ago(35 - 10 * n), handoff=n))
        log.append(event(name, chain=f"c{n}", pane=pane, session=session, at=ago(34 - 10 * n), handoff=n, reason=reason))
    return build_model(session_ref("p-2", "s2"), tmp_path, {}, NOW)


def text(lines: list[str]) -> str:
    return "\n".join(lines)


def test_handoffs_tree_glyphs_and_colour_codes(tmp_path: Path) -> None:
    lines = render(failure_model(tmp_path), Tab.HANDOFFS, 60, 40, NOW, COLOUR)
    body = text(lines)
    assert "●" in body and "│" in body and "▶" not in body  # the stuck live session is not drawn as running
    assert "▶" in text(render(rich_model(tmp_path / "live"), Tab.HANDOFFS, 60, 40, NOW, PLAIN))
    orange_line = next(line for line in lines if "failed, continued in compaction: takeover refused" in line)
    red_line = next(line for line in lines if "stuck: on_main_with_changes" in line)
    assert sgr("yellow") in orange_line and sgr("red") in red_line
    assert next(line for line in lines if "#1 fix the thing" in line).startswith(sgr("yellow"))  # orange node
    assert sgr("bold+white") in next(line for line in lines if "#2 fix it again" in line)  # the live one is highlighted
    green = render(rich_model(tmp_path / "g"), Tab.HANDOFFS, 60, 40, NOW, COLOUR)
    assert sgr("green") in next(line for line in green if "#1 brainstorm" in line)


def test_handoffs_content_matches_the_mockup(tmp_path: Path) -> None:
    body = text(render(rich_model(tmp_path), Tab.HANDOFFS, 60, 40, NOW, PLAIN))
    assert "2 handoffs · 3 sessions" in body and "3h 19m total" in body
    assert "#1 brainstorm the guard now" in body and "#2 guard PR and CI" in body
    assert "1h 40m" in body  # the second session's duration
    assert "[██████████" not in body and "88%" in body  # the live session's gauge
    assert "… waiting for 2 subagents" in body
    assert "▶ #3 Filter chips become" in body


def test_live_duration_and_total_grow_with_the_clock(tmp_path: Path) -> None:
    model = rich_model(tmp_path)
    later = text(render(model, Tab.HANDOFFS, 60, 40, NOW + 600, PLAIN))
    assert "3h 29m total" in later and "1h 49m" in later


def test_render_truncates_to_width_30(tmp_path: Path) -> None:
    model = rich_model(tmp_path)
    for tab in Tab:
        for line in render(model, tab, 30, 60, NOW, PLAIN):
            assert visible_width(line) <= 30
    assert any(line.endswith("…") for line in render(model, Tab.HANDOFFS, 30, 60, NOW, PLAIN))


@pytest.mark.parametrize("opts", [PLAIN, COLOUR, ASCII])
def test_every_line_at_most_width(tmp_path: Path, opts: RenderOpts) -> None:
    long_title = "宽" * 40 + " very long branch title that never ends " * 3
    write_record(tmp_path, 1, pane="p-1", session="s1", goal=long_title, completed=[long_title], remaining=[long_title],
                 resume_point=long_title, created=ago(10))
    model = build_model(session_ref("p-1", "s1"), tmp_path, {}, NOW, ci=(
        CiCard("#1", long_title, CiState.FAILED, ("a" * 50, "b" * 50), None, failed=("a" * 50,), reason=long_title),
        CiCard("#2", "x", CiState.MERGED, (), None)))
    for width in (1, 2, 5, 19, 20, 21, 30, 44, 60, 120):
        for tab in Tab:
            for line in render(model, tab, width, 80, NOW, opts):
                assert visible_width(line) <= width, (tab, width, line)


def test_no_wrapped_tree_glyphs(tmp_path: Path) -> None:
    model = rich_model(tmp_path)
    wide = render(model, Tab.HANDOFFS, 80, 80, NOW, PLAIN)
    narrow = render(model, Tab.HANDOFFS, 20, 80, NOW, PLAIN)
    assert len(narrow) == len(wide)  # a narrow pane cuts lines, it never wraps them into more
    assert [line[:1] for line in narrow if line[:1] in "●▶"] == [line[:1] for line in wide if line[:1] in "●▶"]
    assert all(line.startswith(("│", " ")) for line in narrow if "handed off" in line or "waiting" in line or "%" in line)


def test_ascii_mode_has_no_unicode_glyphs(tmp_path: Path) -> None:
    for tab in Tab:
        for line in render(rich_model(tmp_path), tab, 40, 80, NOW, ASCII):
            assert line.isascii(), line


def test_render_never_exceeds_height(tmp_path: Path) -> None:
    lines = render(rich_model(tmp_path), Tab.HANDOFFS, 40, 6, NOW, PLAIN)
    assert len(lines) == 6 and lines[-1].startswith("… more")
    assert render(rich_model(tmp_path / "h"), Tab.HANDOFFS, 40, 0, NOW, PLAIN)  # a zero height is read as one line


def test_tab_label_dot_when_attention(tmp_path: Path) -> None:
    three_session_store(tmp_path)
    calm = build_model(session_ref("p-3", "s3"), tmp_path, {}, NOW)
    assert tab_label(calm) == "Session" and tab_label(calm, Tab.CI) == "CI"
    busy = rich_model(tmp_path / "busy")
    assert tab_label(busy) == "Session ●"
    assert tab_label(busy, Tab.HANDOFFS) == "Handoffs ●" and tab_label(busy, Tab.CI) == "CI ●"
    assert tab_label(busy, Tab.SUMMARY) == "Summary"
    ci_only = build_model(session_ref("p-3", "s3"), tmp_path, {}, NOW, ci=CARDS[:1])
    assert tab_label(ci_only) == "Session ●" and tab_label(ci_only, Tab.CI) == "CI ●" and tab_label(ci_only, Tab.HANDOFFS) == "Handoffs"
    assert "CI ●" in render(busy, Tab.SUMMARY, 60, 5, NOW, PLAIN)[0]
    assert not build_model(session_ref("p-3", "s3"), tmp_path, {}, NOW, ci=CARDS[4:]).attention  # merged only


def test_empty_chain_message(tmp_path: Path) -> None:
    for model in (build_model(None, tmp_path, {}, NOW), build_model(session_ref("p-9", "s9"), tmp_path, {}, NOW)):
        assert render(model, Tab.HANDOFFS, 30, 10, NOW, PLAIN)[2:] == ["no handoffs"]
        assert render(model, Tab.SUMMARY, 30, 10, NOW, PLAIN)[2:] == ["no briefing yet"]
        assert render(model, Tab.CI, 30, 10, NOW, PLAIN)[2:] == ["no CI to show"]


def test_dangling_root_is_flagged_with_a_question_mark(tmp_path: Path) -> None:
    write_record(tmp_path, 2, predecessor_id=hid(99), pane="p-2", session="s2", goal="imported", created=ago(10))
    lines = render(build_model(session_ref("p-2", "s2"), tmp_path, {}, NOW), Tab.HANDOFFS, 40, 10, NOW, PLAIN)
    assert any(line.startswith("? #1 imported") for line in lines)


def test_ci_tab_one_line_for_merged(tmp_path: Path) -> None:
    lines = render(rich_model(tmp_path), Tab.CI, 60, 60, NOW, PLAIN)
    assert [line for line in lines if "#39" in line or "#41" in line] == ["merged: #39 #41"]
    assert "4 open · 1 running · 1 failing" in text(lines)
    assert "unit-gates failed" in text(lines)


def test_ci_not_started_is_grey_and_never_red(tmp_path: Path) -> None:
    model = rich_model(tmp_path)
    assert [c.state for c in model.ci].count(CiState.NOT_STARTED) == 1
    assert [c.state for c in model.ci].count(CiState.UNAVAILABLE) == 1
    lines = render(model, Tab.CI, 60, 60, NOW, COLOUR)
    gray = sgr("gray")
    assert f"  {gray}not started{RESET}" in lines
    assert f"  {gray}CI unavailable{RESET}" in lines
    assert not any(sgr("red") in line for line in lines if "not started" in line or "CI unavailable" in line)


def test_summary_marks_a_fallback_briefing(tmp_path: Path) -> None:
    body = text(render(rich_model(tmp_path), Tab.SUMMARY, 60, 40, NOW, PLAIN))
    for part in ("Goal", "Filter chips become single-select toggles", "Done", "· toggle groups + tests", "Now",
                 "· full vitest run", "Next", "· push, PR, CI", "updated at handoff #3 (fallback briefing)"):
        assert part in body
    plain = text(render(build_model(session_ref("p-3", "s3"), tmp_path / "x", {}, NOW), Tab.SUMMARY, 60, 40, NOW, PLAIN))
    assert "fallback" not in plain


@pytest.mark.parametrize("width", [30, 44, 60])
@pytest.mark.parametrize("tab", list(Tab))
def test_golden_snapshots(tmp_path: Path, tab: Tab, width: int) -> None:
    golden = GOLDEN / f"golden_{tab.value}_{width}.txt"
    got = text(render(rich_model(tmp_path), tab, width, 40, NOW, PLAIN)) + "\n"
    if os.environ.get("UPDATE_GOLDEN") == "1":
        golden.write_text(got, encoding="utf-8")
    assert got == golden.read_text(encoding="utf-8")

"""Chains that have no store record: rendered from the event log alone, with phase labels and reason phrases."""
from __future__ import annotations

from pathlib import Path

import pytest

from herdr_engineering.agent_ops_tui import RenderOpts
from herdr_engineering.handoff_controller.events import Colour, EventLog, Phase, StuckReason
from herdr_engineering.side_pane.handoffs_tab import lines
from herdr_engineering.side_pane.model import build_model
from herdr_engineering.side_pane.reasons import phase_label, phrase
from tests.test_side_pane_model import NOW, ago, event, session_ref


def chain_events(log: EventLog, n: int, minutes: float, *names: str, reason: str | None = None) -> None:
    for step, name in enumerate(names):
        log.append(event(name, chain=f"c{n}", pane="p-1", session="s1", at=ago(minutes - step * 0.1),
                         reason=reason if name == "handoff.stuck" else None))


def test_a_chain_stuck_before_any_record_is_a_red_row_with_attention(tmp_path: Path) -> None:
    log = EventLog(tmp_path)
    chain_events(log, 1, 40, "handoff.triggered", "handoff.drain_started", "handoff.stuck", reason="drain_timeout")
    model = build_model(session_ref("p-1", "s1"), tmp_path, {}, NOW)
    assert len(model.rows) == 1
    row = model.rows[0]
    assert (row.colour, row.reason, row.live) == (Colour.RED, "drain_timeout", True)
    assert model.totals.failures == 1 and model.chain_attention and model.attention


def test_an_open_chain_without_a_record_shows_its_phase(tmp_path: Path) -> None:
    log = EventLog(tmp_path)
    chain_events(log, 1, 5, "handoff.triggered", "handoff.drain_started")
    model = build_model(session_ref("p-1", "s1"), tmp_path, {}, NOW)
    assert [r.label for r in model.rows] == [phase_label_for(Phase.DRAINING)] and model.chain_attention


def phase_label_for(phase: Phase) -> str:
    label = phase_label(phase, attempts=1, max_attempts=3, compacting=False)
    assert label
    return label


def test_failed_chains_in_a_row_are_one_row_each_newest_last_and_capped(tmp_path: Path) -> None:
    log = EventLog(tmp_path)
    for n in range(1, 15):
        chain_events(log, n, 300 - n * 10, "handoff.triggered", "handoff.stuck", reason="no_briefing")
    log.append(event("handoff.skipped", chain="c-skip", pane="p-1", session="s1", at=ago(1), reason="gave_up"))
    model = build_model(session_ref("p-1", "s1"), tmp_path, {}, NOW)
    assert len(model.rows) == 10 and model.hidden == 4
    assert [r.live for r in model.rows] == [False] * 9 + [True]
    assert model.totals.failures == 14
    assert model.totals.sessions == 1  # fourteen failed attempts of one session: one session


def test_a_green_chain_without_a_record_after_a_red_one_clears_the_live_red_and_the_attention(tmp_path: Path) -> None:
    log = EventLog(tmp_path)
    chain_events(log, 1, 60, "handoff.triggered", "handoff.stuck", reason="no_briefing")
    assert build_model(session_ref("p-1", "s1"), tmp_path, {}, NOW).chain_attention
    chain_events(log, 2, 10, "handoff.triggered", "handoff.succeeded")
    model = build_model(session_ref("p-1", "s1"), tmp_path, {}, NOW)
    assert [r.colour for r in model.rows] == [Colour.RED, Colour.GREEN] and model.rows[-1].live
    assert not model.chain_attention and not model.attention
    assert (model.totals.handoffs, model.totals.failures, model.totals.sessions) == (1, 1, 1)


def test_a_green_chain_without_a_record_alone_shows_nothing(tmp_path: Path) -> None:
    chain_events(EventLog(tmp_path), 1, 10, "handoff.triggered", "handoff.succeeded")
    assert build_model(session_ref("p-1", "s1"), tmp_path, {}, NOW).rows == ()


def test_the_live_row_of_a_chain_that_has_a_record_names_a_later_stuck_chain(tmp_path: Path) -> None:
    from tests.test_side_pane_model import three_session_store
    three_session_store(tmp_path)
    log = EventLog(tmp_path)
    log.append(event("handoff.triggered", chain="c9", pane="p-3", session="s3", at=ago(3)))
    log.append(event("handoff.stuck", chain="c9", pane="p-3", session="s3", at=ago(2), reason="blocked"))
    model = build_model(session_ref("p-3", "s3"), tmp_path, {}, NOW)
    assert model.rows[-1].live and model.rows[-1].reason == "blocked" and model.chain_attention and model.totals.failures == 1


@pytest.mark.parametrize("phase", list(Phase))
def test_every_phase_has_a_label(phase: Phase) -> None:
    assert phase_label(phase, attempts=2, max_attempts=3, compacting=False)


def test_takeover_and_compaction_labels() -> None:
    assert "attempt 2 of 3" in phase_label(Phase.TAKEOVER, attempts=2, max_attempts=3, compacting=False)
    assert "compact" in phase_label(Phase.TAKEOVER, attempts=3, max_attempts=3, compacting=True)


@pytest.mark.parametrize("reason", list(StuckReason))
def test_every_stuck_reason_has_a_phrase(reason: StuckReason) -> None:
    text = phrase(reason.value)
    assert text and text != reason.value and "_" not in text


def test_reason_details_and_unknown_reasons() -> None:
    assert phrase("internal_error:ValueError").startswith("internal error")
    assert "(ValueError)" in phrase("internal_error:ValueError")
    assert phrase("on_main_with_changes; foo") == phrase("on_main_with_changes")
    assert phrase("git_state_unknown:detached") == "git state unknown (detached)"
    assert phrase("takeover_refused_and_compaction_failed:not_ready; last takeover result: exit 2") == (
        "takeover refused, compaction failed (not ready)")
    assert phrase("on_main_with_changes:\x1b[31m" + "x" * 80).count("x") <= 24
    odd = phrase("weird\x1b[31m reason " + "x" * 200)
    assert "\x1b" not in odd and len(odd) <= 48


def test_a_stuck_row_renders_the_phrase_not_the_raw_reason(tmp_path: Path) -> None:
    chain_events(EventLog(tmp_path), 1, 40, "handoff.triggered", "handoff.stuck", reason="drain_timeout")
    model = build_model(session_ref("p-1", "s1"), tmp_path, {}, NOW)
    text = "\n".join(lines(model, 70, NOW, RenderOpts()))
    assert phrase("drain_timeout") in text and "drain_timeout" not in text

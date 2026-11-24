import re

import pytest

from herdr_engineering.md_render import visible_width
from herdr_engineering.snapshot_plot import render_snapshot_plot


def snaps(n, **over):
    return [{"n": i + 1, "taken_at": f"2026-01-01T{9 + i // 60:02d}:{i % 60:02d}:00Z", "prompts": 5 + 3 * i,
             "events": 40 + 20 * i, "tool_failures": i // 3, "worked_s": 600 * (i + 1), "fresh": 1000 * (i + 1),
             "cached": 5, "segments": 2 + i // 4, "drift": i == 3, "coverage": "complete" if i % 2 else "partial",
             **over} for i in range(n)]


def plain(lines):
    return [re.sub(r"\x1b\[[0-9;]*m", "", ln) for ln in lines]


def test_no_snapshots_draw_nothing_and_one_gets_a_note():
    assert render_snapshot_plot([], 80) == []
    note = plain(render_snapshot_plot(snaps(1), 80, color=False))
    assert any("snapshot 1" in ln and "run /summary again" in ln for ln in note) and len(note) == 2


@pytest.mark.parametrize("n", [2, 7, 300])
@pytest.mark.parametrize("width", [0, 1, 12, 29, 30, 40, 80, 200])
@pytest.mark.parametrize("unicode", [True, False])
def test_no_line_is_wider_than_the_pane(n, width, unicode):
    for ln in plain(render_snapshot_plot(snaps(n), width, unicode=unicode, color=False)):
        assert visible_width(ln) <= max(width, 1)


def test_rows_show_the_computed_first_and_last_values():
    text = "\n".join(plain(render_snapshot_plot(snaps(8), 100, color=False)))
    assert "8 snapshots" in text and "prompts" in text and "5 → 26" in text
    assert "10m → 1h20m" in text and "1k → 8k" in text
    assert "drift flagged" in text and "◆" in text and "evidence" in text


def test_sparkline_rises_with_a_rising_series_and_ascii_has_no_unicode():
    rows = plain(render_snapshot_plot(snaps(8), 100, color=False))
    prompts = next(r for r in rows if r.startswith("prompts"))
    bars = prompts.split()[1]
    assert bars[0] == "▁" and bars[-1] == "█" and list(bars) == sorted(bars)
    assert all(ord(c) < 128 for c in "\n".join(plain(render_snapshot_plot(snaps(8), 100, unicode=False, color=False))))


def test_an_unrecorded_metric_is_left_out_and_gaps_are_not_zero_bars():
    data = snaps(6, fresh=None)
    data[2]["worked_s"] = None
    text = "\n".join(plain(render_snapshot_plot(data, 100, color=False)))
    assert "tokens fresh" not in text
    worked = next(r for r in text.split("\n") if r.startswith("time worked"))
    assert "·" in worked


def test_narrow_panes_degrade_to_text_rows():
    rows = plain(render_snapshot_plot(snaps(5), 20, color=False))
    assert any(r.startswith("prompts:") for r in rows)


def test_colour_can_be_turned_off():
    assert "\x1b" not in "\n".join(render_snapshot_plot(snaps(4), 80, color=False))
    assert "\x1b" in "\n".join(render_snapshot_plot(snaps(4), 80, color=True))

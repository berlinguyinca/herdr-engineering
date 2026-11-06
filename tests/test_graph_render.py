"""The drift graph drawn with line characters: must fit any width."""
import re
from datetime import UTC, timezone

import pytest

from herdr_engineering.graph_render import render_graph
from herdr_engineering.md_render import visible_width


def seg(label, a, b, start="2026-01-01T10:55:00Z", agents=None):
    return {"label": label, "from_prompt": a, "to_prompt": b, "count": b - a + 1, "start": start, "agents": agents}


def graph(n=3, drift=True, **kw):
    segs, a = [], 1
    for i in range(n):
        segs.append(seg(kw.get("label", f"topic number {i + 1}"), a, a + 4, kw.get("start", "2026-01-01T10:55:00Z")))
        a += 5
    return {"total_prompts": a - 1, "segments": segs,
            "drift": {"at": segs[-1]["from_prompt"], "note": "this is a web page task"} if drift else None}


def render(g, width, **kw):
    return render_graph(g, width, tz=UTC, color=kw.pop("color", False), **kw)


GRAPHS = [graph(2), graph(5), graph(8), graph(3, label="日本語のとても長いラベルです" * 2),
          graph(4, label="x" * 40), graph(3, drift=False)]


@pytest.mark.parametrize("width", [0, 1, 12, 29, 30, 31, 40, 60, 80, 110, 200])
@pytest.mark.parametrize("unicode", [True, False])
def test_every_line_fits_every_width(width, unicode):
    for g in GRAPHS:
        for line in render(g, width, unicode=unicode):
            assert visible_width(line) <= max(width, 0), (width, line)


def test_wide_width_is_one_row_joined_by_arrows():
    lines = render(graph(3), 200)
    top = next(line for line in lines if "┌" in line)
    assert top.count("┌") == 3 and any("▶" in line for line in lines) and not any("▼" in line for line in lines)


def test_narrow_width_is_a_column_joined_by_down_arrows():
    lines = render(graph(3), 32)
    assert sum(line.count("┌") for line in lines) == 3 and not any(line.count("┌") > 1 for line in lines)
    assert any("▼" in line for line in lines) and not any("▶" in line for line in lines)


def test_box_shows_label_start_time_and_prompt_count():
    text = "\n".join(render(graph(2), 200))
    assert "topic number 1" in text and "10:55 · 5 prompts" in text
    g = graph(2)
    g["segments"][0]["start"] = None
    text = "\n".join(render(g, 200))
    assert "5 prompts" in text and text.count("10:55") == 1


def test_times_are_shown_in_the_requested_zone():
    from datetime import timedelta
    text = "\n".join(render_graph(graph(2), 200, tz=timezone(timedelta(hours=2)), color=False))
    assert "12:55 · 5 prompts" in text


@pytest.mark.parametrize("width", [200, 32])
def test_drift_is_worded_as_a_judgement_with_the_note(width):
    text = " ".join("\n".join(render(graph(3), width)).split())        # the sentence may wrap in a narrow pane
    assert "looks like a new task" in text.lower() and "this is a web page task" in text and "prompt 11" in text


def test_row_marker_sits_under_the_drift_segment():
    lines = render(graph(3), 200)
    boxes = next(line for line in lines if "┌" in line)
    third = [i for i, ch in enumerate(boxes) if ch == "┌"][2]
    marker = next(line for line in lines if "▲" in line)
    assert third <= marker.index("▲") < third + 30


def test_no_drift_says_so():
    assert "no drift found" in "\n".join(render(graph(3, drift=False), 200)).lower()


def test_ascii_fallback_has_only_ascii():
    for width in (200, 32, 20):
        for line in render(graph(3, label="ascii label"), width, unicode=False):
            assert line.isascii(), line


def test_color_off_has_no_escape_codes_and_color_on_has_some():
    assert not any("\x1b" in line for line in render(graph(3), 120))
    assert any("\x1b[" in line for line in render(graph(3), 120, color=True))


def test_agent_badge_only_when_known():
    g = graph(2)
    g["segments"][1]["agents"] = 2
    text = "\n".join(render(g, 200))
    assert "2 agents" in text and text.count("agents") == 1


def test_below_the_floor_it_is_a_numbered_list():
    lines = render(graph(3), 25)
    assert lines[0].startswith("1.") and any(line.startswith("3.") for line in lines)
    assert not any("┌" in line for line in lines)


@pytest.mark.parametrize("width", [30, 31, 40, 60, 120])
def test_ascii_fallback_is_ascii_for_a_long_cjk_note_in_every_layout(width):
    g = {"total_prompts": 9, "drift": {"at": 5, "note": "日本語のメモ " * 30 + "…"}, "segments": [
        {"label": "日本語", "from_prompt": 1, "to_prompt": 4, "count": 4, "start": None, "agents": None},
        {"label": "second topic with a long label", "from_prompt": 5, "to_prompt": 9, "count": 5, "start": None, "agents": None}]}
    lines = render_graph(g, width, unicode=False, color=False)
    assert all(line.isascii() for line in lines), [x for x in lines if not x.isascii()]


# --- segment metrics --------------------------------------------------------------------------------------

from herdr_engineering.graph_render import format_duration, format_tokens  # noqa: E402


@pytest.mark.parametrize("seconds,text", [(0, "<1m"), (59, "<1m"), (60, "1m"), (2520, "42m"), (3600, "1h"),
                                          (3900, "1h05m"), (10800, "3h"), (93600, "26h")])
def test_format_duration(seconds, text):
    assert format_duration(seconds) == text


@pytest.mark.parametrize("n,text", [(0, "0"), (999, "999"), (1000, "1k"), (1500, "1.5k"), (180_000, "180k"),
                                    (999_999, "1M"), (3_100_000, "3.1M"), (250_000_000, "250M")])
def test_format_tokens(n, text):
    assert format_tokens(n) == text


def metric_graph(**totals):
    g = graph(2)
    first, second = g["segments"]
    first.update(worked_s=2520, span_s=3 * 3600, fresh=180_000, cached=3_100_000)
    second.update(worked_s=600, span_s=600, fresh=None, cached=None)
    g["drift"].update(worked_s=3300, span_s=7200, fresh=1_400_000, cached=9_000_000)
    g["totals"] = {"worked_s": 3120, "span_s": 4 * 3600, "fresh": 1_580_000, "cached": 12_100_000, "main_only": False, **totals}
    return g


def test_box_shows_time_worked_wall_time_and_fresh_over_cached_tokens():
    text = "\n".join(render(metric_graph(), 200))
    assert "42m worked · 3h wall" in text and "180k fresh · 3.1M cached" in text
    assert "10m worked · 10m wall" in text                            # wall time is always shown, even when equal


def test_a_segment_without_usage_shows_no_token_text_and_never_a_zero():
    g = metric_graph()
    g["segments"][0].update(fresh=None, cached=None)
    text = "\n".join(render(g, 200))
    assert "fresh" not in text.split("Looks like")[0]                 # no segment box mentions tokens
    assert " 0 fresh" not in text and "0 cached" not in text


def test_total_line_and_the_cost_of_the_drift():
    text = "\n".join(render(metric_graph(), 200))
    assert "52m worked · 4h wall" in text and "1.6M fresh" in text and "12M cached" in text
    drift = [line for line in text.split("\n") if "55m worked" in line]
    assert drift and "55m worked · 2h wall" in drift[0] and "1.4M fresh" in drift[0]


def test_main_session_only_is_said_when_children_exist():
    assert "main session only" in "\n".join(render(metric_graph(main_only=True), 200))
    assert "main session only" not in "\n".join(render(metric_graph(), 200))


def test_graph_without_metrics_renders_as_before():
    text = "\n".join(render(graph(2), 200))
    assert "worked" not in text and "fresh" not in text and "Total" not in text


@pytest.mark.parametrize("width", list(range(4, 70)) + [80, 110, 200])
@pytest.mark.parametrize("unicode", [True, False])
def test_metrics_never_overflow_any_width_or_charset(width, unicode):
    for line in render(metric_graph(main_only=True), width, unicode=unicode):
        assert visible_width(line) <= width, (width, line)
    if not unicode:
        assert "\n".join(render(metric_graph(), width, unicode=False)).isascii()


# --- layout router: fill the pane instead of leaving half of it empty -------------------------------

def _labelled(n, drift_index=None):
    segs = [seg(f"topic {i + 1}", i * 5 + 1, i * 5 + 5) for i in range(n)]
    drift = {"at": segs[drift_index]["from_prompt"], "note": "new"} if drift_index is not None else None
    return {"total_prompts": n * 5, "segments": segs, "drift": drift}


def _box_count_per_row(lines):
    return [len(re.findall("┌─+┐", line)) for line in lines if re.search("┌─+┐", line)]


@pytest.mark.parametrize("width", [60, 80, 110, 130])
def test_boxes_wrap_into_rows_that_fill_the_pane(width):
    lines = render(_labelled(8), width)
    per_row = _box_count_per_row(lines)
    assert len(per_row) > 1 and max(per_row) > 1                       # a grid, not one long column
    assert max(visible_width(line) for line in lines) >= width * 0.6   # and it uses the width it has
    column = render(_labelled(8), 32)
    assert len(lines) < len(column)


def test_wrapped_rows_are_balanced_not_left_with_an_orphan():
    per_row = _box_count_per_row(render(_labelled(7), 110))
    assert len(per_row) > 1 and max(per_row) > 1 and max(per_row) - min(per_row) <= 1


def test_every_box_after_the_first_has_exactly_one_incoming_arrow():
    for width in (60, 80, 110, 200):
        text = "\n".join(render(_labelled(8), width))
        assert text.count("▶") + text.count("▼") == 7, width


def test_row_to_row_connector_runs_from_the_end_of_one_row_to_the_start_of_the_next():
    lines = render(_labelled(8), 80)
    assert max(_box_count_per_row(lines)) > 1
    down = next(i for i, line in enumerate(lines) if "▼" in line)
    assert lines[down - 3].lstrip().startswith("└") and "│" in lines[down - 2]    # leaves the end of the row
    elbow = lines[down - 1]
    assert ("┘" in elbow and "┌" in elbow) or ("└" in elbow and "┐" in elbow)
    assert lines[down].index("▼") < 20                                  # lands on the first box of the next row


@pytest.mark.parametrize("drift_index", [1, 2, 3, 4, 5, 7])   # 2 and 5 end a row at width 80
def test_drift_marker_stays_under_its_own_box_in_a_grid(drift_index):
    lines = render(_labelled(8, drift_index), 80)
    label = f"topic {drift_index + 1}"
    row = next(i for i, line in enumerate(lines) if f"│ {label} " in line)      # the label line of that box
    line = lines[row]
    col = line.index(label)
    left = line.rfind("│", 0, col)
    right = line.find("│", col)
    marker = next(m for m in lines[row:] if "▲" in m)
    assert left < marker.index("▲") < right
    assert sum(m.count("▲") for m in lines) == 1


@pytest.mark.parametrize("width", [40, 50, 70, 90, 120])
@pytest.mark.parametrize("unicode", [True, False])
def test_grid_never_overflows_and_ascii_stays_ascii(width, unicode):
    for n in (2, 5, 8, 12):
        for drift_index in (None, 1, n - 1):
            lines = render(_labelled(n, drift_index), width, unicode=unicode)
            assert all(visible_width(line) <= width for line in lines)
            if not unicode:
                assert all(line.isascii() for line in lines)

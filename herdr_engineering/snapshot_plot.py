"""Draw the history of /summary snapshots as sparkline rows that fit a terminal of any width.

A pure function of the stored snapshots (``summary_snapshots.load``), so every number comes from the code that
computed it from the session history. One row per metric, one column per snapshot (the newest columns when there
are more snapshots than room), each row ending ``first -> last``. No line is ever wider than ``width``.
"""
from __future__ import annotations

from datetime import datetime

from .graph_render import format_duration, format_tokens
from .md_render import visible_width

MIN_WIDTH = 30
LABEL = 14
_UNI = dict(bars="▁▂▃▄▅▆▇█", arrow="→", mark="◆", rule="─", none="·")
_ASCII = dict(bars="_.-=+*#@", arrow="->", mark="*", rule="-", none=".")
_METRICS = (("prompts", "prompts", str), ("events", "events", str), ("tool_failures", "failed calls", str),
            ("worked_s", "time worked", format_duration), ("fresh", "tokens fresh", format_tokens),
            ("segments", "topics", str))
_COVERAGE = {"complete": "c", "partial": "p", "limited": "l"}


def _when(ts) -> str:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().strftime("%m-%d %H:%M")
    except (AttributeError, ValueError):
        return "?"


def _spark(values: list, columns: int, g: dict) -> str:
    """One glyph per snapshot (the newest ``columns`` of them); None is a gap, never a zero-height bar."""
    values = values[-columns:]
    known = [v for v in values if v is not None]
    low, high = (min(known), max(known)) if known else (0, 0)
    top = len(g["bars"]) - 1
    return "".join(g["none"] if v is None else g["bars"][0 if high == low else round((v - low) * top / (high - low))]
                   for v in values)


def _style(line: str, code: str, color: bool) -> str:
    return f"\x1b[{code}m{line}\x1b[0m" if color else line


def _fit(text: str, width: int) -> str:
    while text and visible_width(text) > width:
        text = text[:-1]
    return text


def render_snapshot_plot(snapshots: list[dict], width: int, *, unicode: bool = True, color: bool = True) -> list[str]:
    """Plot lines for ``snapshots`` (oldest first), or [] when there are none. Needs two to draw a trend."""
    if not snapshots:
        return []
    g = _UNI if unicode else _ASCII
    count = len(snapshots)
    head = (f"Snapshot history: {count} snapshots, {_when(snapshots[0].get('taken_at'))} {g['arrow']} "
            f"{_when(snapshots[-1].get('taken_at'))}" if count > 1 else
            f"Snapshot history: snapshot 1 ({_when(snapshots[0].get('taken_at'))}); run /summary again to see a trend")
    out = ["", _style(_fit(head, max(width, 1)), "1", color)]
    if count < 2 or width < 1:
        return out
    room = width - LABEL - 2
    wide = width >= MIN_WIDTH and room >= 6
    columns = max(min(count, room - 18), 4) if wide else 0
    for key, label, fmt in _METRICS:
        values = [s.get(key) for s in snapshots]
        known = [v for v in values if v is not None]
        if not known:
            continue
        first, last = fmt(known[0]), fmt(known[-1])
        span = f"{first} {g['arrow']} {last}"
        if wide:
            line = f"{label:<{LABEL}} {_spark(values, columns, g):<{columns}}  {span}"
        else:
            line = f"{label}: {span}"
        out.append(_fit(line, width))
    if wide:
        cover = "".join(_COVERAGE.get(s.get("coverage"), g["none"]) for s in snapshots[-columns:])
        out.append(_fit(f"{'evidence':<{LABEL}} {cover:<{columns}}  c complete, p partial, l limited", width))
        if any(s.get("drift") for s in snapshots):
            marks = "".join(g["mark"] if s.get("drift") else " " for s in snapshots[-columns:])
            out.append(_fit(f"{'drift flagged':<{LABEL}} {marks}", width))
    return out

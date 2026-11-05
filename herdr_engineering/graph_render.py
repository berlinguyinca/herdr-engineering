"""Draw a validated drift graph (``drift_graph.build_graph``) as text that fits a terminal of any width.

One row of boxes joined by arrows when it fits, a column of boxes joined by down arrows when it does not, and a
plain numbered list below ``MIN_WIDTH``. Line art uses box-drawing characters, or ASCII when the output encoding
cannot show them. No line is ever wider than ``width``.
"""
from __future__ import annotations

import re
from datetime import UTC, datetime, tzinfo

from .md_render import visible_width

MIN_WIDTH = 30
MAX_LABEL_LINES = 3
_ARROW = 2                      # columns between boxes in a row: the shaft and the head
_ROW_WRAPS = (18, 12, 8)        # label wrap widths tried, widest first, before falling back to a column
_UNI = dict(tl="┌", tr="┐", bl="└", br="┘", h="─", v="│", right="▶", down="▼", up="▲", mark="◆", dot="·")
_ASCII = dict(tl="+", tr="+", bl="+", br="+", h="-", v="|", right=">", down="v", up="^", mark="*", dot="-")
_STYLES = (("[┌┐└┘─│]+", "38;5;244"), ("[▶▼]", "38;5;75"), ("[▲◆]", "1;38;5;221"))


def _chop(word: str, width: int) -> list[str]:
    pieces, cur = [], ""
    for ch in word:
        if cur and visible_width(cur + ch) > width:
            pieces.append(cur)
            cur = ""
        cur += ch
    return pieces + [cur] if cur else pieces


def _wrap(text: str, width: int) -> list[str]:
    width = max(width, 1)
    lines, cur = [], ""
    for word in text.split():
        for piece in _chop(word, width):
            if cur and visible_width(cur) + 1 + visible_width(piece) <= width:
                cur += " " + piece
            else:
                if cur:
                    lines.append(cur)
                cur = piece
    if cur:
        lines.append(cur)
    return lines or [""]


def _pad(text: str, width: int) -> str:
    return text + " " * max(width - visible_width(text), 0)


def format_duration(seconds: int | float) -> str:
    """``<1m``, ``42m``, ``1h05m``, ``3h``."""
    minutes = int(seconds // 60)
    if minutes < 1:
        return "<1m"
    hours, minutes = divmod(minutes, 60)
    if not hours:
        return f"{minutes}m"
    return f"{hours}h" if not minutes else f"{hours}h{minutes:02d}m"


def format_tokens(n: int | float) -> str:
    """``999``, ``1.5k``, ``180k``, ``3.1M``, ``250M``: two or three significant digits."""
    n = max(int(n), 0)
    if n < 1000:
        return str(n)
    for unit, size in (("k", 1e3), ("M", 1e6)):
        value = n / size
        text = f"{value:.0f}" if value >= 10 else f"{value:.1f}".removesuffix(".0")
        if unit == "M" or float(text) < 1000:
            return text + unit
    return str(n)                                   # unreachable: the M branch always returns


def _worked(seconds, span=None, dot=" - ") -> str | None:
    if seconds is None:
        return None
    text = f"{format_duration(seconds)} worked"
    if span is not None:                            # wall-clock time is always shown beside the time worked
        text += f"{dot}{format_duration(span)} wall"
    return text


def _fresh(fresh, cached, dot=" - ") -> str | None:
    if fresh is None:
        return None
    return f"{format_tokens(fresh)} fresh" + (f"{dot}{format_tokens(cached)} cached" if cached else "")


def _local(start: str | None, tz: tzinfo | None) -> datetime | None:
    try:
        return datetime.fromisoformat(start.replace("Z", "+00:00")).astimezone(tz) if start else None
    except ValueError:
        return None


def _meta(seg: dict, g: dict, tz, multi_day: bool) -> list[str]:
    when = _local(seg.get("start"), tz)
    count = f"{seg['count']} prompt{'' if seg['count'] == 1 else 's'}"
    stamp = when.strftime("%m-%d %H:%M" if multi_day else "%H:%M") if when else ""
    lines = [f"{stamp} {g['dot']} {count}" if stamp else count]
    dot = f" {g['dot']} "
    lines += [x for x in (_worked(seg.get("worked_s"), seg.get("span_s"), dot),
                          _fresh(seg.get("fresh"), seg.get("cached"), dot)) if x]
    if seg.get("agents"):
        lines.append(f"{seg['agents']} agent{'' if seg['agents'] == 1 else 's'}")
    return lines


def _box(lines: list[str], glyphs: dict) -> list[str]:
    inner = max(visible_width(line) for line in lines)
    g = glyphs
    return ([g["tl"] + g["h"] * (inner + 2) + g["tr"]]
            + [g["v"] + " " + _pad(line, inner) + " " + g["v"] for line in lines]
            + [g["bl"] + g["h"] * (inner + 2) + g["br"]])


def _content(seg: dict, wrap: int, g: dict, tz, multi_day: bool) -> list[str]:
    label = _wrap(seg["label"], wrap)
    if len(label) > MAX_LABEL_LINES:
        label = label[:MAX_LABEL_LINES]
        label[-1] = _chop(label[-1], max(wrap - 1, 1))[0].rstrip() + "…"
    return label + _meta(seg, g, tz, multi_day)


def _drift_text(graph: dict, g: dict, width: int) -> list[str]:
    drift = graph.get("drift")
    if not drift:
        return []
    text = f"Looks like a new task from prompt {drift['at']}" + (f": {drift['note']}" if drift.get("note") else "")
    wrapped = _wrap(text, width - 2)
    out = [g["mark"] + " " + wrapped[0]] + ["  " + line for line in wrapped[1:]]
    dot = f" {g['dot']} "
    cost = dot.join(x for x in (_worked(drift.get("worked_s"), drift.get("span_s"), dot), _fresh(drift.get("fresh"), None, dot)) if x)
    if cost:
        out += ["  " + line for line in _wrap(f"From there on: {cost}", width - 2)]
    return out


def _total_text(graph: dict, g: dict, width: int) -> list[str]:
    totals = graph.get("totals") or {}
    dot = f" {g['dot']} "
    parts = [x for x in (_worked(totals.get("worked_s"), totals.get("span_s"), dot),
                         _fresh(totals.get("fresh"), totals.get("cached"), dot)) if x]
    if not parts:
        return []
    if totals.get("main_only") and totals.get("fresh") is not None:
        parts.append("main session only")
    return _wrap("Total: " + dot.join(parts), width)


def render_graph(graph: dict, width: int, *, unicode: bool = True, color: bool = True,
                 tz: tzinfo | None = None) -> list[str]:
    """Lines (each at most ``width`` columns) drawing ``graph``; ``tz`` picks the zone for times (default local)."""
    if width < 4:
        return []
    g = dict(_UNI if unicode else _ASCII)
    segments = graph["segments"]
    text = _Text(unicode)
    days = {(_local(s.get("start"), tz) or datetime.min.replace(tzinfo=UTC)).date()
            for s in segments if s.get("start")}
    multi_day = len(days) > 1
    drift_at = (graph.get("drift") or {}).get("at")
    fix = text.fix

    def seg_lines(seg, wrap):
        return [fix(line) for line in _content({**seg, "label": fix(seg["label"])}, wrap, g, tz, multi_day)]

    out: list[str]
    if width < MIN_WIDTH:
        out = []
        for i, seg in enumerate(segments, 1):
            body = " - ".join([fix(seg["label"]), *_meta(seg, g, tz, multi_day)])
            wrapped = _wrap(body, width - 3)
            out += [f"{i}. " + wrapped[0]] + ["   " + line for line in wrapped[1:]]
        out += [fix(line) for line in _drift_text(graph, g, width)]
    else:
        row = None
        for wrap in _ROW_WRAPS:
            boxes = [_box(seg_lines(seg, wrap), g) for seg in segments]
            if sum(visible_width(b[0]) for b in boxes) + _ARROW * (len(boxes) - 1) <= width:
                row = boxes
                break
        out = _row(row, segments, drift_at, g) if row else _column(segments, drift_at, g, width, seg_lines, graph, fix)
        if row:
            out += [fix(line) for line in _drift_text(graph, g, width)]
    if not graph.get("drift"):
        out.append("No drift found.")
    out += [fix(line) for line in _total_text(graph, g, width)]
    out = [line if visible_width(line) <= width else _chop(line, width)[0] for line in out]
    return [_colour(line) for line in out] if color and unicode else out


class _Text:
    def __init__(self, unicode: bool):
        self.unicode = unicode

    def fix(self, text: str) -> str:
        return text if self.unicode else text.encode("ascii", "replace").decode()


def _row(boxes, segments, drift_at, g):
    height = max(len(b) for b in boxes)
    rows = []
    for r in range(height):
        parts = []
        for i, box in enumerate(boxes):
            w = visible_width(box[0])
            parts.append(box[r] if r < len(box) else " " * w)
            if i < len(boxes) - 1:
                parts.append(g["h"] + g["right"] if r == 1 else "  ")
        rows.append("".join(parts))
    for i, seg in enumerate(segments):
        if seg["from_prompt"] == drift_at and i:
            x = sum(visible_width(b[0]) for b in boxes[:i]) + _ARROW * i
            rows.append(" " * (x + visible_width(boxes[i][0]) // 2) + g["up"])
    return rows


def _column(segments, drift_at, g, width, seg_lines, graph, fix):
    out = []
    for i, seg in enumerate(segments):
        if i:
            out += ["  " + g["v"], "  " + g["down"]]
            if seg["from_prompt"] == drift_at:
                out += [fix(line) for line in _drift_text(graph, g, width)]
        out += _box(seg_lines(seg, width - 4), g)
    return out


def _colour(line: str) -> str:
    for pattern, style in _STYLES:
        line = re.sub(pattern, lambda m, s=style: f"\x1b[{s}m{m.group()}\x1b[0m", line)
    return line

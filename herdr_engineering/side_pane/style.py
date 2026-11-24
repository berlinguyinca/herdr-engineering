"""Text lines for the side pane: coloured segments cut to a width with an ellipsis, never wrapped.

Colour and cell widths come from `agent_ops_tui` (`sgr`, `RESET`, `fit`, `cell_width`), the helpers `pane_viewer`'s
neighbours already use; a line that fits is returned whole, escape codes included, so `pane_viewer` never has to cut it.
"""
from __future__ import annotations

import re
from collections.abc import Sequence
from enum import StrEnum

from ..agent_ops_tui import _ASCII, RESET, RenderOpts, _drop, cell_width, fit, sgr


class Style(StrEnum):
    """The colours and weights a segment can have; the values are the names `agent_ops_tui.sgr` takes."""
    BOLD = "bold"
    DIM = "dim"
    RED = "red"
    GREEN = "green"
    YELLOW = "yellow"
    BLUE = "blue"
    MAGENTA = "magenta"
    CYAN = "cyan"
    GRAY = "gray"
    WHITE = "white"


BOLD_CYAN = (Style.BOLD, Style.CYAN)
BOLD_WHITE = (Style.BOLD, Style.WHITE)
Look = Style | tuple[Style, ...] | None  # one style, several combined, or none
Seg = tuple[str, Look]  # text and how to draw it
_PLAIN = {**_ASCII, ord("│"): "|", ord("▶"): ">", ord("✓"): "+", ord("─"): "-"}
_ELLIPSIS = "…"
_ESCAPES = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?|\x1b\[[0-?]*[ -/]*[@-~]?|\x1b[ -/]*[@-~]?|\x9b[0-?]*[ -/]*[@-~]?|\x9d[^\x07\x9c\x1b]*(?:\x07|\x9c|\x1b\\)?")
_BLANKS = re.compile(r"[\t\n\r\x0b\x0c]+")


def clean(text: str) -> str:
    """`text` without terminal escape sequences or control characters: what a record, title or reason may carry must
    never reach the terminal as a command. Line breaks and tabs become a space; the other controls, the zero-width and
    bidi format characters and the combining marks go, the same set `cell_width` counts as nothing. The one place text is
    made safe: `line` cleans every segment, and the wrapping and printing code calls this before it touches the text."""
    return "".join(ch for ch in _BLANKS.sub(" ", _ESCAPES.sub("", text)) if not _drop(ch))


def paint(text: str, style: Look, opts: RenderOpts) -> str:
    code = sgr("+".join(style) if isinstance(style, tuple) else style) if opts.color and style else ""
    return f"{code}{text}{RESET}" if code else text


def line(segs: Sequence[Seg], width: int, opts: RenderOpts) -> str:
    """The segments as one line of at most `width` columns; what does not fit is replaced by a final ellipsis."""
    if width < 1:
        return ""
    segs = [(clean(t), s) for t, s in segs]
    segs = [(t if opts.unicode else t.translate(_PLAIN), s) for t, s in segs]
    total = sum(cell_width(t) for t, _ in segs)
    budget = width if total <= width else width - 1
    out: list[str] = []
    for text, style in segs:
        keep = fit(text, budget)
        budget -= cell_width(keep)
        if keep:
            out.append(paint(keep, style, opts))
        if keep != text:
            break
    if total > width:
        out.append(_ELLIPSIS if opts.unicode else "~")
    return "".join(out)


def duration(seconds: float | None) -> str:
    if seconds is None:
        return ""
    minutes = int(max(seconds, 0) // 60)
    if minutes < 1:
        return f"{int(max(seconds, 0))}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m" if hours else f"{minutes}m"

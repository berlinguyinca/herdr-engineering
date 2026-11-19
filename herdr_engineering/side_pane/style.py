"""Text lines for the side pane: coloured segments cut to a width with an ellipsis, never wrapped.

Colour and cell widths come from `agent_ops_tui` (`sgr`, `RESET`, `fit`, `cell_width`), the helpers `pane_viewer`'s
neighbours already use; a line that fits is returned whole, escape codes included, so `pane_viewer` never has to cut it.
"""
from __future__ import annotations

from collections.abc import Sequence

from ..agent_ops_tui import _ASCII, RESET, RenderOpts, cell_width, fit, sgr

Seg = tuple[str, str | None]  # text, style names joined with "+" as `agent_ops_tui.sgr` takes them
_PLAIN = {**_ASCII, ord("│"): "|", ord("▶"): ">", ord("✓"): "+", ord("─"): "-"}
_ELLIPSIS = "…"


def paint(text: str, style: str | None, opts: RenderOpts) -> str:
    code = sgr(style) if opts.color and style else ""
    return f"{code}{text}{RESET}" if code else text


def line(segs: Sequence[Seg], width: int, opts: RenderOpts) -> str:
    """The segments as one line of at most `width` columns; what does not fit is replaced by a final ellipsis."""
    if width < 1:
        return ""
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

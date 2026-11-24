"""The Handoffs tab: totals, then the chain as a small vertical tree, live session highlighted."""
from __future__ import annotations

from ..agent_ops_tui import RenderOpts, context_bar, ctx_level_style
from ..handoff_controller.events import Colour
from .model import ChainRow, SidePaneModel
from .reasons import phrase
from .style import BOLD_WHITE, Seg, Style, duration, line

_COLOUR = {Colour.GREEN: Style.GREEN, Colour.ORANGE: Style.YELLOW, Colour.RED: Style.RED}  # yellow is the 256-colour orange
_OUTCOME = {Colour.GREEN: "handed off", Colour.ORANGE: "failed, continued in compaction", Colour.RED: "stuck"}
_GAUGE_CELLS = 10


def _node_glyph(row: ChainRow) -> str:
    return "?" if row.dangling else ("▶" if row.live and row.colour is not Colour.RED else "●")


def _title(row: ChainRow) -> list[Seg]:
    style = BOLD_WHITE if row.live else _COLOUR.get(row.colour) if row.colour else Style.WHITE
    return [(f"{_node_glyph(row)} ", _COLOUR.get(row.colour) if row.colour else Style.CYAN if row.live else Style.GRAY),
            (f"#{row.session_number} {row.title or 'session'}", style)]


def _detail(row: ChainRow, grown_s: float, opts: RenderOpts) -> list[Seg]:
    spent = row.duration_s + grown_s if row.duration_s is not None and row.live else row.duration_s
    segs: list[Seg] = [(duration(spent), Style.GRAY)] if spent is not None else []
    if row.context_pct is not None:
        gauge = context_bar({"used": row.context_pct, "limit": 100}, _GAUGE_CELLS + 6, opts)
        segs += [("  " if segs else "", None), (gauge, Style(ctx_level_style(row.context_pct / 100) or Style.GREEN))]
    return segs


def _extra(row: ChainRow) -> list[Seg]:
    """The status line under a node: what the handoff came to, or what the live session waits for."""
    if row.colour in (Colour.ORANGE, Colour.RED):
        reason = f": {phrase(row.reason)}" if row.reason else ""
        return [(f"{_OUTCOME[row.colour]}{reason}", _COLOUR[row.colour])]
    if row.live and row.label != row.title:
        return [(f"… {row.label}", Style.YELLOW)]
    if row.colour is Colour.GREEN:
        return [("→ handed off", Style.GREEN)]
    return []


def _totals(model: SidePaneModel, grown_s: float) -> list[list[Seg]]:
    t = model.totals
    rows = [[(f"{t.handoffs} handoffs · {t.sessions} sessions", Style.BOLD)]]
    if t.total_s is not None:
        rows.append([(f"{duration(t.total_s + grown_s)} total", Style.BOLD)])
    return rows


def _unreadable(model: SidePaneModel, width: int, opts: RenderOpts) -> list[str]:
    n = model.unreadable
    return [line([(f"{n} unreadable event{'' if n == 1 else 's'}", Style.DIM)], width, opts)] if n else []


def lines(model: SidePaneModel, width: int, now: float, opts: RenderOpts) -> list[str]:
    if not model.rows:
        return [line([("no handoffs", Style.GRAY)], width, opts), *_unreadable(model, width, opts)]
    grown = max(now - model.built_at, 0.0)
    out = [line(segs, width, opts) for segs in _totals(model, grown)]
    if model.hidden:
        out.append(line([(f"… {model.hidden} earlier sessions", Style.GRAY)], width, opts))
    for n, row in enumerate(model.rows):
        out.append(line(_title(row), width, opts))
        trail = "│ " if n < len(model.rows) - 1 else "  "
        for segs in (_detail(row, grown, opts), _extra(row)):
            if segs:
                out.append(line([(trail, Style.GRAY), *segs], width, opts))
    return [*out, *_unreadable(model, width, opts)]

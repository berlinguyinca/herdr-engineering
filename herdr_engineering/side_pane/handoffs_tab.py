"""The Handoffs tab: totals, then the chain as a small vertical tree, live session highlighted."""
from __future__ import annotations

from ..agent_ops_tui import RenderOpts, context_bar, ctx_level_style
from ..handoff_controller.events import Colour
from .model import ChainRow, SidePaneModel
from .style import Seg, duration, line

_COLOUR = {Colour.GREEN: "green", Colour.ORANGE: "yellow", Colour.RED: "red"}  # yellow is the 256-colour orange
_OUTCOME = {Colour.GREEN: "handed off", Colour.ORANGE: "failed, continued in compaction", Colour.RED: "stuck"}
_GAUGE_CELLS = 10


def _node_glyph(row: ChainRow) -> str:
    return "?" if row.dangling else ("▶" if row.live else "●")


def _title(row: ChainRow) -> list[Seg]:
    style = "bold+white" if row.live else _COLOUR.get(row.colour) if row.colour else "white"
    return [(f"{_node_glyph(row)} ", _COLOUR.get(row.colour) if row.colour else "cyan" if row.live else "gray"),
            (f"#{row.session_number} {row.title or 'session'}", style)]


def _detail(row: ChainRow, grown_s: float, opts: RenderOpts) -> list[Seg]:
    spent = row.duration_s + grown_s if row.duration_s is not None and row.live else row.duration_s
    segs: list[Seg] = [(duration(spent), "gray")] if spent is not None else []
    if row.context_pct is not None:
        gauge = context_bar({"used": row.context_pct, "limit": 100}, _GAUGE_CELLS + 6, opts)
        segs += [("  " if segs else "", None), (gauge, ctx_level_style(row.context_pct / 100))]
    return segs


def _extra(row: ChainRow) -> list[Seg]:
    """The status line under a node: what the handoff came to, or what the live session waits for."""
    if row.colour in (Colour.ORANGE, Colour.RED):
        reason = f": {row.reason}" if row.reason else ""
        return [(f"{_OUTCOME[row.colour]}{reason}", _COLOUR[row.colour])]
    if row.live and row.label != row.title:
        return [(f"… {row.label}", "yellow")]
    if row.colour is Colour.GREEN:
        return [("→ handed off", "green")]
    return []


def _totals(model: SidePaneModel, grown_s: float) -> list[list[Seg]]:
    t = model.totals
    rows = [[(f"{t.handoffs} handoffs · {t.sessions} sessions", "bold")]]
    if t.total_s is not None:
        rows.append([(f"{duration(t.total_s + grown_s)} total", "bold")])
    return rows


def lines(model: SidePaneModel, width: int, now: float, opts: RenderOpts) -> list[str]:
    if not model.rows:
        return [line([("no handoffs", "gray")], width, opts)]
    grown = max(now - model.built_at, 0.0)
    out = [line(segs, width, opts) for segs in _totals(model, grown)]
    if model.hidden:
        out.append(line([(f"… {model.hidden} earlier sessions", "gray")], width, opts))
    for n, row in enumerate(model.rows):
        out.append(line(_title(row), width, opts))
        trail = "│ " if n < len(model.rows) - 1 else "  "
        for segs in (_detail(row, grown, opts), _extra(row)):
            if segs:
                out.append(line([(trail, "gray"), *segs], width, opts))
    return out

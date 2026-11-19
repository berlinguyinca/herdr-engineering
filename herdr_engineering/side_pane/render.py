"""The side pane's three tabs as lines of text for a given width: pure, no terminal and no clock of its own."""
from __future__ import annotations

from enum import StrEnum

from ..agent_ops_tui import RenderOpts
from . import ci_tab, handoffs_tab, summary_tab
from .model import SidePaneModel
from .style import line


class Tab(StrEnum):
    SUMMARY = "summary"
    HANDOFFS = "handoffs"
    CI = "ci"


_NAME = {Tab.SUMMARY: "Summary", Tab.HANDOFFS: "Handoffs", Tab.CI: "CI"}
_DOT = " ●"


def _attention(model: SidePaneModel, tab: Tab) -> bool:
    return {Tab.SUMMARY: False, Tab.HANDOFFS: model.chain_attention, Tab.CI: ci_tab.ci_attention(model.ci)}[tab]


def tab_label(model: SidePaneModel, tab: Tab | None = None) -> str:
    """The name of `tab` with a dot when something on it runs or fails; without a tab, the pane's own label, which has
    the dot when anything does."""
    if tab is None:
        return "Session" + (_DOT if model.attention else "")
    return _NAME[tab] + (_DOT if _attention(model, tab) else "")


def _bar(model: SidePaneModel, tab: Tab, width: int, opts: RenderOpts) -> str:
    segs = []
    for each in Tab:
        label = tab_label(model, each)
        segs += [(f"[{label}]" if each is tab else f" {label} ", "bold+cyan" if each is tab else "gray")]
    return line(segs, width, opts)


def _body(model: SidePaneModel, tab: Tab, width: int, now: float, opts: RenderOpts) -> list[str]:
    if tab is Tab.SUMMARY:
        return summary_tab.lines(model, width, opts)
    if tab is Tab.HANDOFFS:
        return handoffs_tab.lines(model, width, now, opts)
    return ci_tab.lines(model.ci, width, opts)


def render(model: SidePaneModel, tab: Tab, width: int, height: int, now: float, opts: RenderOpts | None = None) -> list[str]:
    """The tab bar, a rule and the tab, every line at most `width` columns and the whole at most `height` lines."""
    opts = opts or RenderOpts()
    width, height = max(width, 1), max(height, 1)
    rule = line([("─" * width, "gray")], width, opts)
    lines = [_bar(model, tab, width, opts), rule, *_body(model, tab, width, now, opts)]
    if len(lines) > height:
        lines = [*lines[:max(height - 1, 0)], line([("… more", "gray")], width, opts)]
    return lines[:height]

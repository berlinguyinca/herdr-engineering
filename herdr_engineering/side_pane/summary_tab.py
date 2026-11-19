"""The Summary tab: goal, done, now and next from the chain's latest briefing."""
from __future__ import annotations

import textwrap
from collections.abc import Sequence

from ..agent_ops_tui import RenderOpts
from .model import SidePaneModel
from .style import BOLD_CYAN, Seg, Style, clean, line

_ITEM_LINES = 3  # a longer item is cut with an ellipsis after this many lines


def _wrapped(text: str, width: int, bullet: str) -> list[list[Seg]]:
    room = max(width - len(bullet), 1)
    parts = textwrap.wrap(clean(text), room, break_long_words=True, break_on_hyphens=False) or [""]
    if len(parts) > _ITEM_LINES:
        parts = [*parts[:_ITEM_LINES - 1], parts[_ITEM_LINES - 1] + "…"]
    return [[(bullet if n == 0 else " " * len(bullet), Style.GRAY), (part, None)] for n, part in enumerate(parts)]


def _section(title: str, items: Sequence[str], width: int, opts: RenderOpts, bullet: str) -> list[str]:
    out = [line([(title, BOLD_CYAN)], width, opts)]
    for item in items or ("—",):
        out += [line(segs, width, opts) for segs in _wrapped(item, width, bullet)]
    return out


def lines(model: SidePaneModel, width: int, opts: RenderOpts) -> list[str]:
    summary = model.summary
    if summary is None:
        return [line([("no briefing yet", Style.GRAY)], width, opts)]
    out = _section("Goal", [summary.goal] if summary.goal else [], width, opts, "")
    out += _section("Done", summary.done, width, opts, "· ")
    out += _section("Now", [summary.now] if summary.now else [], width, opts, "· ")
    out += _section("Next", summary.next, width, opts, "· ")
    number = next((r.session_number for r in model.rows if r.handoff_id == summary.handoff_id), None)
    note = (f"updated at handoff #{number}" if number else "latest briefing") + (" (fallback briefing)" if summary.fallback else "")
    return [*out, line([(note, Style.YELLOW if summary.fallback else Style.GRAY)], width, opts)]

"""Test-only helpers for driving a real isolated Herdr. Production code never moves the user's focus; tests have to
set it up, and Herdr only moves pane focus by direction, so this walks the layout."""
from __future__ import annotations

from herdr_engineering.side_pane.herdr import SideHerdr

_QUICK_S = 15.0


def focused(side: SideHerdr, pane_id: str) -> bool:
    row = side.pane(pane_id)
    return row is not None and row.focused


def focus_pane(side: SideHerdr, pane_id: str) -> bool:
    """Focus `pane_id`: its tab first, then the pane by direction from the tab's focused pane."""
    row = side.pane(pane_id)
    layout = side.layout(pane_id) if row else None
    if row is None or layout is None:
        return False
    if side._run(["tab", "focus", row.tab_id], _QUICK_S) is None:
        return False
    for _ in range(len(layout.rects) + 1):
        current = side.layout(pane_id)
        if current is None or current.focused_pane_id == pane_id:
            return current is not None and focused(side, pane_id)
        for direction in ("left", "right", "up", "down"):
            done = side._run(["pane", "focus", "--pane", current.focused_pane_id or pane_id, "--direction", direction], _QUICK_S)
            if done is not None and done.returncode == 0 and focused(side, pane_id):
                return True
    return focused(side, pane_id)

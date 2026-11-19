"""The side pane's calls to Herdr: the controller's `HerdrCli` (agents, send-keys) plus panes, layout, split and close.

`HerdrCli._run` is the one place a `herdr` subprocess is started (timeout, environment, no exception); the methods here
only shape its JSON. No method raises: None or False means Herdr did not answer or refused.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass

from ..handoff_controller.herdr_cli import HerdrCli

_QUICK_S = 15.0


@dataclass(frozen=True)
class PaneRow:
    pane_id: str
    tab_id: str
    terminal_id: str  # not reused after a Herdr restart, unlike the pane id
    focused: bool  # focus of the whole Herdr, not of the pane's tab (see `Layout.focused_pane_id`)


@dataclass(frozen=True)
class Layout:
    """One tab's layout: the focused pane of that tab and the cells every pane has."""
    tab_id: str
    focused_pane_id: str | None
    widths: Mapping[str, int]
    heights: Mapping[str, int]


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _row(raw: object) -> PaneRow | None:
    if not isinstance(raw, dict):
        return None
    pane, tab, terminal = _text(raw.get("pane_id")), _text(raw.get("tab_id")), _text(raw.get("terminal_id"))
    return PaneRow(pane, tab, terminal, raw.get("focused") is True) if pane and tab and terminal else None


class SideHerdr(HerdrCli):
    def _json(self, args: list[str]) -> dict[str, object] | None:
        done = self._run(args, _QUICK_S)
        if done is None or done.returncode != 0:
            return None
        try:
            reply = json.loads(done.stdout)["result"]
        except (ValueError, KeyError, TypeError):
            return None
        return reply if isinstance(reply, dict) else None

    def panes(self) -> list[PaneRow] | None:
        """Every pane Herdr lists, or None when it cannot be asked."""
        reply = self._json(["pane", "list"])
        rows = reply.get("panes") if reply else None
        return [row for row in map(_row, rows) if row] if isinstance(rows, list) else None

    def pane(self, pane_id: str) -> PaneRow | None:
        reply = self._json(["pane", "get", pane_id])
        return _row(reply.get("pane")) if reply else None

    def tab_of(self, pane_id: str) -> str | None:
        row = self.pane(pane_id)
        return row.tab_id if row else None

    def layout(self, pane_id: str) -> Layout | None:
        """The layout of the tab that holds `pane_id`; None when the pane or Herdr is gone."""
        reply = self._json(["pane", "layout", "--pane", pane_id])
        raw = reply.get("layout") if reply else None
        if not isinstance(raw, dict) or not _text(raw.get("tab_id")) or not isinstance(raw.get("panes"), list):
            return None
        widths: dict[str, int] = {}
        heights: dict[str, int] = {}
        for entry in raw["panes"]:
            rect = entry.get("rect") if isinstance(entry, dict) else None
            pane, width, height = (_text(entry.get("pane_id")), _int(rect.get("width")), _int(rect.get("height"))) \
                if isinstance(rect, dict) else (None, None, None)
            if pane and width is not None and height is not None:
                widths[pane], heights[pane] = width, height
        return Layout(str(raw["tab_id"]), _text(raw.get("focused_pane_id")), widths, heights)

    def split(self, target: str, ratio: float, env: Mapping[str, str] | None = None) -> PaneRow | None:
        """A new pane to the right of `target`, without taking focus; `ratio` is the share `target` keeps."""
        args = ["pane", "split", target, "--direction", "right", "--ratio", f"{ratio:.4f}", "--no-focus"]
        for key, value in (env or {}).items():
            args += ["--env", f"{key}={value}"]
        reply = self._json(args)
        return _row(reply.get("pane")) if reply else None

    def run_in(self, pane_id: str, command: str) -> bool:
        """Type `command` into the pane's shell and press enter."""
        done = self._run(["pane", "run", pane_id, command], _QUICK_S)
        return done is not None and done.returncode == 0

    def close(self, pane_id: str) -> bool:
        done = self._run(["pane", "close", pane_id], _QUICK_S)
        return done is not None and done.returncode == 0

    def focus_pane(self, pane_id: str) -> bool:
        """Focus `pane_id`: its tab first, then the pane (Herdr only moves pane focus by direction, so this walks the
        layout from the tab's focused pane)."""
        row = self.pane(pane_id)
        layout = self.layout(pane_id) if row else None
        if row is None or layout is None:
            return False
        if self._run(["tab", "focus", row.tab_id], _QUICK_S) is None:
            return False
        for _ in range(len(layout.widths) + 1):
            current = self.layout(pane_id)
            if current is None or current.focused_pane_id == pane_id:
                return current is not None
            for direction in ("left", "right", "up", "down"):
                done = self._run(["pane", "focus", "--pane", current.focused_pane_id or pane_id, "--direction", direction],
                                 _QUICK_S)
                if done is not None and done.returncode == 0 and self._focused(pane_id):
                    return True
        return self._focused(pane_id)

    def _focused(self, pane_id: str) -> bool:
        layout = self.layout(pane_id)
        return layout is not None and layout.focused_pane_id == pane_id

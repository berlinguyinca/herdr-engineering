"""One side pane per Herdr tab, on the right, without moving focus or squeezing the session.

`ensure` is idempotent and takes the tab's lock, so two callers never both create the pane. A pane the user closed is
not reopened until a new managed session appears in the tab (the dismissal lives in `state`, beside the marker);
`force` (the `side-pane ensure` command) reopens it. A pane is recognised by the marker its runner registers, not by
its title.
"""
from __future__ import annotations

import os
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from ..handoff_controller.herdr_cli import AgentPane, harness_for_agent_name
from . import state
from .herdr import Layout, PaneRow, SideHerdr

SIDE_WIDTH = 40  # columns the side pane gets
MIN_SESSION_WIDTH = 60  # columns the session keeps at least; a narrower tab gets no side pane
LOCK_TIMEOUT_S = 10.0
DEFAULT_COMMAND = "exec herdr-eng side-pane run"


class Outcome(StrEnum):
    CREATED = "created"
    EXISTS = "exists"
    DISMISSED = "dismissed"  # the user closed it and no new session has appeared since
    TOO_NARROW = "too_narrow"
    NO_SESSION = "no_session"  # no managed session in the tab
    UNAVAILABLE = "unavailable"  # Herdr did not answer or refused
    LOCKED = "locked"  # another process is creating the pane


@dataclass(frozen=True)
class EnsureResult:
    outcome: Outcome
    pane_id: str | None = None


def pick_target(layout: Layout) -> str | None:
    """The pane to split: the rightmost one, the topmost of those."""
    best = max(layout.rects.items(), key=lambda kv: (kv[1].x + kv[1].width, -kv[1].y), default=None)
    return best[0] if best else None


def split_ratio(width: int, side_width: int = SIDE_WIDTH, min_session: int = MIN_SESSION_WIDTH) -> float | None:
    """The share of a `width`-column pane that stays with the session, or None when it would keep fewer than `min_session`."""
    if width < side_width + min_session:
        return None
    return (width - side_width) / width


def is_dismissed(dismissed: frozenset[str], session_terminals: Sequence[str]) -> bool:
    """True when the pane was closed while every session now in the tab was already there."""
    return bool(dismissed) and bool(session_terminals) and all(t in dismissed for t in session_terminals)


def is_side_pane(pane: AgentPane, env: Mapping[str, str] | None = None) -> bool:
    """Whether `pane` is a live side pane: the marker of its tab names it and its runner is alive."""
    return state.is_marked(state.state_dir(os.environ if env is None else env), pane, time.time())


@dataclass(frozen=True)
class Decision:
    """What `ensure` does, decided from facts only: `outcome` is None when a pane is to be created."""
    outcome: Outcome | None
    pane_id: str | None = None  # EXISTS: the live side pane
    stale_marker: bool = False  # remove the tab's marker
    dismiss: tuple[str, ...] = ()  # remember the user closed the pane, with these session terminals
    clear_dismissed: bool = False
    target: str | None = None  # CREATED: split this pane ...
    ratio: float | None = None  # ... leaving it this share
    owner: str | None = None  # terminal of the target, recorded in the marker


def decide(*, rows: Sequence[PaneRow], sessions: Sequence[PaneRow], marker: state.Marker | None, dismissed: frozenset[str],
           layout: Layout | None, target_pane: str | None, now: float, force: bool) -> Decision:
    """`rows` are the tab's panes and `sessions` those that run a managed agent. A marker that is not live is stale; when
    its pane is gone and the session it was opened beside is still there, the user closed the pane (dismissal), otherwise
    Herdr restarted. A dismissal holds while every session in the tab was there when it was made; `force` ignores it."""
    stale, remembered = False, ()
    if marker is not None:
        row = next((r for r in rows if r.pane_id == marker.pane_id and r.terminal_id == marker.terminal_id), None)
        if row is not None and state.marker_live(marker, now):
            return Decision(Outcome.EXISTS, row.pane_id)
        stale = True
        if row is None and marker.session_terminal in {s.terminal_id for s in sessions}:
            remembered = tuple(s.terminal_id for s in sessions)
    if not sessions:
        return Decision(Outcome.NO_SESSION, stale_marker=stale)
    terminals = [s.terminal_id for s in sessions]
    if not force and is_dismissed(dismissed | frozenset(remembered), terminals):
        return Decision(Outcome.DISMISSED, stale_marker=stale, dismiss=remembered)
    target = target_pane or (pick_target(layout) if layout else None)
    rect = layout.rects.get(target) if layout and target else None
    keep = dict(stale_marker=stale, dismiss=remembered, clear_dismissed=True)
    if rect is None:
        return Decision(Outcome.UNAVAILABLE, **keep)
    ratio = split_ratio(rect.width)
    if ratio is None:
        return Decision(Outcome.TOO_NARROW, **keep)
    owner = next((r.terminal_id for r in rows if r.pane_id == target), None)
    return Decision(None, target=target, ratio=ratio, owner=owner, **keep)


def ensure(tab_id: str, target_pane: str | None = None, herdr: SideHerdr | None = None,
           env: Mapping[str, str] | None = None, *, command: str = DEFAULT_COMMAND, force: bool = False,
           lock_timeout_s: float = LOCK_TIMEOUT_S) -> EnsureResult:
    """Open the side pane of `tab_id` unless it has one, the user closed it, or the tab is too narrow. Never raises."""
    herdr = herdr or SideHerdr()
    root = state.state_dir(os.environ if env is None else env)
    with state.tab_lock(root, tab_id, lock_timeout_s) as held:
        if not held:
            return EnsureResult(Outcome.LOCKED)
        return _ensure_locked(root, tab_id, target_pane, herdr, command, force)


def _sessions(herdr: SideHerdr, tab_id: str, rows: Sequence[PaneRow]) -> list[PaneRow] | None:
    agents = herdr.agents()
    if agents is None:
        return None
    named = {a.pane_id for a in agents if a.tab_id == tab_id and harness_for_agent_name(a.name) is not None}
    return [r for r in rows if r.pane_id in named]


def _ensure_locked(root: Path, tab_id: str, target_pane: str | None, herdr: SideHerdr, command: str, force: bool) -> EnsureResult:
    now = time.time()
    rows = herdr.panes()
    in_tab = [r for r in rows if r.tab_id == tab_id] if rows is not None else []
    sessions = _sessions(herdr, tab_id, in_tab) if rows is not None else None
    if sessions is None:
        return EnsureResult(Outcome.UNAVAILABLE)
    layout = herdr.layout(target_pane or sessions[0].pane_id) if sessions else None
    plan = decide(rows=in_tab, sessions=sessions, marker=state.read_marker(root, tab_id), dismissed=state.read_dismissed(root, tab_id),
                  layout=layout, target_pane=target_pane, now=now, force=force)
    if plan.stale_marker:
        state.clear_marker(root, tab_id)
    if plan.dismiss:
        state.dismiss(root, tab_id, list(plan.dismiss))
    if plan.clear_dismissed:
        state.clear_dismissed(root, tab_id)
    if plan.outcome is not None or plan.target is None or plan.ratio is None:
        return EnsureResult(plan.outcome or Outcome.UNAVAILABLE, plan.pane_id)
    created = herdr.split(plan.target, plan.ratio, env={})
    if created is None:
        return EnsureResult(Outcome.UNAVAILABLE)
    state.write_marker(root, state.Marker(tab_id, created.pane_id, created.terminal_id, plan.owner, None, now))
    if not herdr.run_in(created.pane_id, command):
        herdr.close(created.pane_id)
        state.clear_marker(root, tab_id)
        return EnsureResult(Outcome.UNAVAILABLE)
    after = herdr.layout(plan.target)
    if layout and layout.focused_pane_id and after is not None and after.focused_pane_id != layout.focused_pane_id:
        herdr.focus_pane(layout.focused_pane_id)  # splitting must not take focus from the session
    return EnsureResult(Outcome.CREATED, created.pane_id)

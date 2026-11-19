"""What the side pane follows: the focused session of its own tab, and the event log read incrementally.

`MAX_EVENTS` bounds the events the follower keeps (the newest ones): a pane left open for weeks must not grow without
limit. 20000 events is far more than the chains of one tab produce in a long working session."""
from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path

from ..handoff_controller.events import EventLog, HandoffEvent
from ..handoff_controller.herdr_cli import AgentPane, harness_for_agent_name

MAX_EVENTS = 20000


def _is_session(pane: AgentPane, tab_id: str, is_side: Callable[[AgentPane], bool]) -> bool:
    return pane.tab_id == tab_id and harness_for_agent_name(pane.name) is not None and not is_side(pane)


def choose_session(focused_pane: str | None, agents: Sequence[AgentPane] | None, tab_id: str, previous: AgentPane | None,
                   is_side: Callable[[AgentPane], bool] = lambda pane: False) -> AgentPane | None:
    """The session of `tab_id` to show. The focused pane of the tab if it is a managed agent; else the one shown before
    while it is still listed (focus is on a shell or on a side pane); else the first listed session of the tab. Agents
    of other tabs are never chosen. `agents` None means Herdr did not answer: keep what was shown."""
    if agents is None:
        return previous
    mine = [a for a in agents if _is_session(a, tab_id, is_side)]
    focused = next((a for a in mine if a.pane_id == focused_pane), None)
    if focused is not None:
        return focused
    if previous is not None and any(a.pane_id == previous.pane_id for a in mine):
        return next(a for a in mine if a.pane_id == previous.pane_id)
    return mine[0] if mine else None


class EventFollower:
    """The controller's event log, read with `EventLog.read_since`: only what was appended since the last poll is parsed."""

    def __init__(self, store_root: Path, max_events: int = MAX_EVENTS) -> None:
        self._max = max_events
        self._log = EventLog(store_root)
        self._events: list[HandoffEvent] = []
        self.offset = 0
        self._generation = 0

    def poll(self) -> tuple[list[HandoffEvent], bool]:
        """All events so far and whether this poll found anything new (or a replaced file)."""
        new, offset = self._log.read_since(self.offset)
        replaced = self._log.generation != self._generation
        if replaced:
            self._events, self._generation = [], self._log.generation
        self._events.extend(new)
        if len(self._events) > self._max:
            del self._events[: len(self._events) - self._max]
        self.offset = offset
        return self._events, bool(new) or replaced

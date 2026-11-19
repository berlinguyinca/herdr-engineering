"""The side pane's model of one session: its handoff chain, totals and latest briefing.

Pure and read-only. Inputs are the handoff controller's event log and the shared handoff store (records written by
herdr-handoff); nothing is written and nothing is asked of Herdr. The chain is joined by `predecessor_id` /
`successor_id` of the records (the same join `HandoffRegistry.chain` makes server-side from its own database, which a
local pane does not have). Colours and reasons come from the event log, as the controller wrote them.

Scope. A chain is scoped by the session and pane ids of its records, never by repository: the session's own records
are found by their source or target session id (pane id when no session id is known), then the chain is walked back
through `predecessor_id` to its root and forward through the records that name each record as predecessor. Records of
other repositories in the same store are never reached, because no predecessor link joins them to this chain. When
two records name the same predecessor, the one created last (by its own `created_at`, then by its id; never by file
name) continues the chain and the other is shown as a root of its own, flagged `?`. Events whose time cannot be read
are skipped and counted in `unreadable`.
"""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from ..handoff_adapters.base import Harness, SessionRef
from ..handoff_controller.events import (
    TERMINAL,
    Colour,
    EventLog,
    HandoffEvent,
    chain_colour,
    epoch,
    open_chain,
    pane_events,
)
from ..handoff_controller.store import shared_store_root
from ..handoff_controller.takeover import record_view
from .ci_tab import CiCard, ci_attention

CHAIN_MAX = 10  # rows shown; earlier sessions are only counted
_TITLE_WORDS = 6


@dataclass(frozen=True)
class ChainRow:
    """One session of the chain, oldest first. `handoff_id` is the record this session wrote when it was drained
    (None for a live session that has not written one); `colour` and `reason` are that handoff's outcome."""
    handoff_id: str | None
    depth: int
    harness: Harness
    colour: Colour | None
    label: str  # the status shown on the node: the title, or "waiting for N subagents"
    reason: str | None
    dangling: bool  # a root whose predecessor record is not in the store
    session_number: int = 1  # 1-based over the whole chain, also when the earliest rows are not shown
    live: bool = False
    title: str = ""  # a few words of the session's goal
    duration_s: float | None = None  # None when its start is not recorded (the root session)
    context_pct: float | None = None  # the latest reading in the event log; only the live row has one


@dataclass(frozen=True)
class Totals:
    handoffs: int  # sessions handed off to a successor
    compactions: int  # handoffs that failed and continued in compaction
    failures: int  # chains stuck
    sessions: int = 0
    total_s: float | None = None  # the sum of the known session durations


@dataclass(frozen=True)
class Summary:
    """The latest briefing of the chain."""
    goal: str
    done: tuple[str, ...]
    now: str
    next: tuple[str, ...]
    fallback: bool  # written by the fallback writer, not by the session
    handoff_id: str


@dataclass(frozen=True)
class SidePaneModel:
    rows: tuple[ChainRow, ...] = ()
    totals: Totals = Totals(0, 0, 0)
    summary: Summary | None = None
    hidden: int = 0  # earlier sessions of the chain that `rows` leaves out
    chain_attention: bool = False
    ci: tuple[CiCard, ...] = ()
    built_at: float = 0.0  # the clock the durations were measured at; a later render adds what has passed
    unreadable: int = 0  # events skipped because their time cannot be read

    @property
    def attention(self) -> bool:
        """Something runs or fails: a chain that is not settled, or a CI card that runs or fails."""
        return self.chain_attention or ci_attention(self.ci)


EMPTY = SidePaneModel()


@dataclass(frozen=True)
class _Record:
    id: str
    predecessor: str | None
    source_pane: str | None
    source_session: str | None
    source_harness: str | None
    target_pane: str | None
    target_session: str | None
    target_harness: str | None
    handed_off: bool  # the record shows a confirmed successor pane
    goal: str
    done: tuple[str, ...]
    now: str
    next: tuple[str, ...]
    fallback: bool
    created: float | None
    updated: float | None


def _text(data: object, key: str) -> str | None:
    value = data.get(key) if isinstance(data, dict) else None
    return value if isinstance(value, str) and value else None


def _texts(data: object, key: str) -> tuple[str, ...]:
    value = data.get(key) if isinstance(data, dict) else None
    return tuple(v for v in value if isinstance(v, str) and v) if isinstance(value, list) else ()


def _time(data: object, key: str) -> float | None:
    stamp = _text(data, key)
    try:
        return epoch(stamp) if stamp else None
    except ValueError:
        return None


def _record(raw: object) -> _Record | None:
    if not isinstance(raw, dict) or not _text(raw, "handoff_id"):
        return None
    source, target, context = raw.get("source"), raw.get("target"), raw.get("context")
    view = record_view(raw)
    provenance = raw.get("provenance")
    return _Record(
        str(raw["handoff_id"]), _text(raw, "predecessor_id"),
        _text(source, "pane_id"), _text(source, "session_id"), _text(source, "harness"),
        _text(target, "pane_id") or view.child_pane, _text(target, "session_id"), _text(target, "harness"),
        view.child_confirmed, _text(context, "goal") or "", _texts(context, "completed"),
        _text(context, "resume_point") or "", _texts(context, "remaining") + _texts(context, "next_commands"),
        isinstance(provenance, dict) and provenance.get("fallback") is True, _time(raw, "created_at"),
        _time(raw, "updated_at"))


def load_records(store_root: Path) -> dict[str, _Record]:
    """Every readable record of the store's outbox; an unreadable file is skipped."""
    found: dict[str, _Record] = {}
    for path in sorted((store_root / "outbox").glob("handoff_*.json")):
        try:
            record = _record(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
        if record is not None:
            found[record.id] = record
    return found


def _is_session(session: SessionRef, pane: str | None, session_id: str | None) -> bool:
    if session.session_id and session_id:
        return session.session_id == session_id
    return pane is not None and pane == session.pane_id


def _chain_records(records: Mapping[str, _Record], session: SessionRef) -> tuple[list[_Record], bool]:
    """The records of the chain the session is in, root first, and whether the root's predecessor is missing."""
    mine = [r for r in records.values()
            if _is_session(session, r.source_pane, r.source_session) or _is_session(session, r.target_pane, r.target_session)]
    if not mine:
        return [], False
    successor: dict[str, _Record] = {}
    for r in sorted(records.values(), key=lambda r: (r.created or 0.0, r.id)):  # the last created wins, not the last file
        if r.predecessor:
            successor[r.predecessor] = r
    current = max(mine, key=lambda r: r.created or 0.0)
    back = [current]
    while current.predecessor in records and successor.get(current.predecessor) is current and records[current.predecessor] not in back:
        current = records[current.predecessor]
        back.append(current)
    chain = back[::-1]
    while chain[-1].id in successor and successor[chain[-1].id] not in chain:
        chain.append(successor[chain[-1].id])
    return chain, chain[0].predecessor is not None and (chain[0].predecessor not in records or successor.get(chain[0].predecessor) is not chain[0])


def _outcome(record: _Record, has_next: bool, events: Sequence[HandoffEvent]) -> tuple[Colour | None, str | None, float | None]:
    """The colour of a handoff, its reason when it did not succeed, and when it ended the session."""
    chains = {e.chain_id for e in events if e.handoff_id == record.id}
    mine = [e for e in events if e.chain_id in chains]
    colour = chain_colour(mine)
    end = next((e for e in reversed(mine) if e.event in TERMINAL), None)
    if colour is None and (has_next or record.handed_off):
        return Colour.GREEN, None, record.updated
    reason = end.reason if end is not None and colour is not Colour.GREEN else None
    return colour, reason, epoch(end.at) if end is not None and colour is Colour.GREEN else record.updated


def _title(goal: str) -> str:
    return " ".join(goal.split()[:_TITLE_WORDS])


def _harness(name: str | None, fallback: Harness) -> Harness:
    try:
        return Harness(name) if name else fallback
    except ValueError:
        return fallback


def _waiting_label(events: Sequence[HandoffEvent]) -> str | None:
    chain = open_chain(events)
    if chain is None or chain.waiting is None:
        return None
    reason, live = chain.waiting
    if reason == "subagents" and live:
        return f"waiting for {live} subagent{'' if live == 1 else 's'}"
    return "waiting for the session to finish"


@dataclass(frozen=True)
class _Node:
    record: _Record | None
    pane: str | None
    session_id: str | None
    harness: Harness
    title: str
    start: float | None
    colour: Colour | None = None
    reason: str | None = None
    end: float | None = None


def _nodes(chain: Sequence[_Record], events: Sequence[HandoffEvent], session: SessionRef) -> list[_Node]:
    nodes: list[_Node] = []
    start: float | None = None
    for j, record in enumerate(chain):
        colour, reason, end = _outcome(record, j + 1 < len(chain), events)
        nodes.append(_Node(record, record.source_pane, record.source_session, _harness(record.source_harness, session.harness),
                           _title(record.goal), start, colour, reason, end if colour is Colour.GREEN else None))
        start = end
    last = chain[-1]
    if nodes[-1].colour is Colour.GREEN:  # the live session is the one the last handoff opened
        nodes.append(_Node(None, last.target_pane, last.target_session, _harness(last.target_harness or last.source_harness,
                                                                                session.harness), _title(last.goal), start))
    return nodes


def _rows(nodes: Sequence[_Node], events: Sequence[HandoffEvent], now: float, dangling: bool) -> tuple[list[ChainRow], bool]:
    live = nodes[-1]
    mine = pane_events(events, live.pane, live.session_id) if live.pane else []
    waiting = _waiting_label(mine)
    gauge = next((e.context_pct for e in reversed(mine) if e.context_pct is not None), None)
    open_now = open_chain(mine) is not None
    rows = []
    for n, node in enumerate(nodes):
        is_live = node is live
        end = now if is_live else node.end
        duration = end - node.start if node.start is not None and end is not None else None
        rows.append(ChainRow(node.record.id if node.record else None, n, node.harness, node.colour,
                             waiting if is_live and waiting else node.title, node.reason, dangling and n == 0,
                             n + 1, is_live, node.title, duration, gauge if is_live else None))
    return rows, open_now or live.colour in (Colour.RED, Colour.ORANGE)


def _summary(record: _Record) -> Summary:
    return Summary(record.goal, record.done, record.now, record.next, record.fallback, record.id)


def _totals(all_nodes: Sequence[_Node], now: float) -> Totals:
    colours = [n.colour for n in all_nodes]
    known = [(now if i == len(all_nodes) - 1 else n.end) - n.start for i, n in enumerate(all_nodes)
             if n.start is not None and (i == len(all_nodes) - 1 or n.end is not None)]
    return Totals(colours.count(Colour.GREEN), colours.count(Colour.ORANGE), colours.count(Colour.RED), len(all_nodes),
                  sum(known) if known else None)


def _readable(events: Sequence[HandoffEvent]) -> tuple[list[HandoffEvent], int]:
    """The events whose time parses, and how many did not."""
    good: list[HandoffEvent] = []
    for event in events:
        try:
            epoch(event.at)
        except (ValueError, OverflowError):
            continue
        good.append(event)
    return good, len(events) - len(good)


def build_model(session: SessionRef | None, store_root: Path | None, env: Mapping[str, str], now: float,
                ci: Sequence[CiCard] = (), events: Sequence[HandoffEvent] | None = None) -> SidePaneModel:
    """The model for `session`; the empty model when there is none. `store_root` defaults to the shared store; `ci` are
    the cards `ci_tab.build_cards` made (fetching them is the caller's job: it is the only part that uses the network).
    `events` are the event log's events when the caller already holds them (a running pane reads the log incrementally);
    without them the whole log is read."""
    if session is None:
        return EMPTY
    root = store_root if store_root is not None else shared_store_root(env)
    events, unreadable = _readable(EventLog(root).read() if events is None else events)
    chain, dangling = _chain_records(load_records(root), session)
    if chain:
        nodes = _nodes(chain, events, session)
    elif open_chain(pane_events(events, session.pane_id, session.session_id)) is not None:
        nodes = [_Node(None, session.pane_id, session.session_id, session.harness, "", None)]  # draining, no record yet
    else:
        return SidePaneModel(ci=tuple(ci), built_at=now, unreadable=unreadable)
    rows, attention = _rows(nodes, events, now, dangling)
    hidden = max(len(rows) - CHAIN_MAX, 0)
    shown = [replace(r, depth=r.depth - hidden) for r in rows[hidden:]]
    return SidePaneModel(tuple(shown), _totals(nodes, now), _summary(chain[-1]) if chain else None, hidden,
                         attention, tuple(ci), now, unreadable)

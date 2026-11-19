"""Handoff controller event log: one JSON object per line in ``<store_root>/events.jsonl``."""
from __future__ import annotations

import json
import os
from collections.abc import Sequence
from dataclasses import MISSING, asdict, dataclass, fields
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from ..handoff_adapters.base import Harness
from .herdr_cli import AgentPane


class Colour(StrEnum):
    GREEN = "green"  # handed off
    ORANGE = "orange"  # failed, continued in compaction
    RED = "red"  # stuck


class StuckReason(StrEnum):
    """Why a chain ended red; the `reason` of `handoff.stuck`."""
    TAKEOVER_REFUSED_AND_COMPACTION_FAILED = "takeover_refused_and_compaction_failed"
    BLOCKED = "blocked"  # the session waits on a dialog
    WRONG_AGENT = "wrong_agent"  # the pane runs another harness than the session's
    GIT_STATE_UNKNOWN = "git_state_unknown"
    ON_MAIN_WITH_CHANGES = "on_main_with_changes"
    NO_BRIEFING = "no_briefing"
    NO_COMPACT_COMMAND = "no_compact_command"  # the harness's adapter has no compact command
    COMPACTION_NOT_READY = "compaction_not_ready"  # never idle with an empty composer, so nothing could be typed
    COMPACTION_NOT_STARTED = "compaction_not_started"  # the compact command was typed and the agent never went working
    COMPACTION_TIMEOUT = "compaction_timeout"  # the agent went working and did not come back in time
    POINTER_TOO_LONG = "pointer_too_long"  # the pointer text exceeds the typed-input limit
    SUCCESSOR_WITHOUT_TAKEOVER = "successor_without_takeover"  # the record names a successor but no launched child
    SOURCE_NOT_CLOSED_SUCCESSOR_LIVE = "source_not_closed_successor_live"  # the successor runs; only the close failed
    LAUNCH_STATE_UNKNOWN = "launch_state_unknown"  # a takeover died mid-launch: a child may or may not exist
    DRAIN_TIMEOUT = "drain_timeout"  # the drain order could not be typed within the drain timeout (never idle with an empty composer)
    DRAIN_PROMPT_UNCERTAIN = "drain_prompt_uncertain"  # typing the order failed in a way that left it unknown whether it landed
    DRAIN_ORDER_TOO_LONG = "drain_order_too_long"  # the drain order exceeds the typed-input limit
    NO_RECORD = "no_record"  # the session's context record stayed unreadable for a whole drain timeout
    RECORD_UNREADABLE = "record_unreadable"  # the handoff record could not be read: nothing is launched or compacted on it
    MERGE_IN_PROGRESS = "merge_in_progress"  # the worktree is mid-merge and a push is allowed: nothing is committed or pushed
    PANE_GONE = "pane_gone"  # the pane closed (or holds another session) for `pane_gone_after_s` while the chain was open
    COMPACTION_STATE_UNKNOWN = "compaction_state_unknown"  # a restart found a compaction that may be running: never typed twice
    INTERNAL_ERROR = "internal_error"  # finishing the chain raised three times in a row; `internal_error:<exception type>`


class IsSidePane(Protocol):
    def __call__(self, pane: AgentPane) -> bool: ...


class EnsureSidePane(Protocol):
    def __call__(self, tab_id: str) -> None: ...


EVENT_NAMES = frozenset({
    "handoff.triggered", "handoff.drain_started", "handoff.drained", "handoff.briefing_written",
    "handoff.briefing_fallback", "handoff.wip_committed", "handoff.takeover_started", "handoff.succeeded",
    "handoff.failed_compacted", "handoff.stuck", "handoff.skipped", "handoff.waiting",
})
TERMINAL = {
    "handoff.succeeded": Colour.GREEN,
    "handoff.failed_compacted": Colour.ORANGE,
    "handoff.stuck": Colour.RED,
}
COMPACTING = "compacting"  # the `reason` of the second `handoff.takeover_started`: the attempts are over, compaction begins
OVER_CEILING = "over_ceiling"  # the `reason` of the triggered / fallback-briefing event of a last-resort chain
_REQUIRED_KEYS = frozenset({"event", "at", "chain_id", "pane_id", "harness"})  # every other key reads as None when absent
_REASON_REQUIRED = frozenset({"handoff.stuck", "handoff.skipped", "handoff.waiting"})


@dataclass(frozen=True)
class HandoffEvent:
    event: str
    at: str
    chain_id: str
    pane_id: str
    session_id: str | None
    harness: Harness
    handoff_id: str | None
    predecessor_id: str | None
    successor_id: str | None
    colour: Colour | None
    reason: str | None
    context_pct: float | None
    successor_pane_id: str | None = None  # the successor's pane; `successor_id` is only ever a handoff id
    successor_session_id: str | None = None  # the successor agent's session id, when Herdr reports one
    subagents_live: int | None = None  # `handoff.waiting`: the subagents the registry counts as running
    commit: str | None = None  # `handoff.wip_committed`: the checkpoint commit

    def __post_init__(self) -> None:
        if self.event not in EVENT_NAMES:
            raise ValueError(f"unknown handoff event name: {self.event!r}")
        if self.colour != TERMINAL.get(self.event):  # terminal events carry their colour, all others none
            raise ValueError(f"{self.event} must have colour {TERMINAL.get(self.event)!r}, got {self.colour!r}")
        if self.event in _REASON_REQUIRED and not self.reason:
            raise ValueError(f"{self.event} requires a reason")

    def to_json(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"))

    @classmethod
    def from_json(cls, line: str) -> HandoffEvent:
        data = json.loads(line)
        known = {f.name for f in fields(cls)}
        data = {k: v for k, v in data.items() if k in known}  # a key a newer version added is not an error
        for f in fields(cls):  # nor is an optional key an older version did not write
            if f.name not in data and f.name not in _REQUIRED_KEYS:
                data[f.name] = None if f.default is MISSING else f.default
        data["harness"] = Harness(data["harness"])
        if data["colour"] is not None:
            data["colour"] = Colour(data["colour"])
        return cls(**data)


class EventLog:
    def __init__(self, store_root: Path) -> None:
        self._path = store_root / "events.jsonl"
        self.generation = 0  # counts how often `read_since` found the file replaced; a follower rebuilds when it changes
        self._inode: int | None = None

    def append(self, e: HandoffEvent) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            data = memoryview((e.to_json() + "\n").encode("utf-8"))
            while data:  # normally one write, so a line is not interleaved with another writer's
                data = data[os.write(fd, data):]
        finally:
            os.close(fd)

    def read(self, chain_id: str | None = None) -> list[HandoffEvent]:
        """Every event, parsed from the whole file: O(size) per call. The controller loop does not use this per tick;
        it follows the log with `read_since`."""
        try:
            lines = self._path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        return [e for e in _parse(lines) if chain_id is None or e.chain_id == chain_id]

    def read_since(self, offset: int) -> tuple[list[HandoffEvent], int]:
        """The poller API: the events appended after byte `offset` (0 the first time) and the offset to continue from.
        Only complete lines are consumed, so an append in progress is read on the next call. The log is append-only; a
        file that is shorter than `offset`, or has another inode than the one last read, was replaced: it is read from
        the start and `generation` goes up, which tells the caller to drop what it kept."""
        try:
            with self._path.open("rb") as f:
                stat = os.fstat(f.fileno())
                replaced = (self._inode is not None and stat.st_ino != self._inode) or offset > stat.st_size
                if replaced:
                    self.generation += 1
                self._inode = stat.st_ino
                start = 0 if replaced else offset
                f.seek(start)
                data = f.read()
        except FileNotFoundError:
            return [], 0
        cut = data.rfind(b"\n")
        if cut < 0:
            return [], start
        return _parse(data[:cut].decode("utf-8", errors="replace").splitlines()), start + cut + 1


def _parse(lines: Sequence[str]) -> list[HandoffEvent]:
    events: list[HandoffEvent] = []
    for line in lines:
        try:
            events.append(HandoffEvent.from_json(line))
        except (ValueError, KeyError, TypeError):
            continue  # mid-append, or a line that fails validation: skip it
    return events


def chain_colour(events: Sequence[HandoffEvent]) -> Colour | None:
    """Colour of the last terminal event; None when the chain has none."""
    for e in reversed(events):
        if e.event in TERMINAL:
            return TERMINAL[e.event]
    return None


def source_is_handed_off(events_for_pane: Sequence[HandoffEvent]) -> bool:
    """True when the session in this pane has a live successor, so it must not be drained or handed off again:
    a `handoff.succeeded`, or a `handoff.stuck` for SOURCE_NOT_CLOSED_SUCCESSOR_LIVE (only the close failed)."""
    return any(e.event == "handoff.succeeded"
               or (e.event == "handoff.stuck" and (e.reason or "").split(":")[0].split(";")[0]
                   == StuckReason.SOURCE_NOT_CLOSED_SUCCESSOR_LIVE.value)
               for e in events_for_pane)


class Phase(StrEnum):
    DRAINING = "draining"  # triggered; the order is being typed, or was typed and the briefing is awaited
    WAITING = "waiting"  # the session still works or has subagents running (`handoff.waiting`)
    BRIEFING = "briefing"  # a briefing exists; the checkpoint and takeover follow
    TAKEOVER = "takeover"
    DONE_GREEN = "done_green"
    DONE_ORANGE = "done_orange"
    DONE_RED = "done_red"


@dataclass(frozen=True)
class Chain:
    """What the event log says about one chain of a pane."""
    chain_id: str
    triggered_at: float
    drain_started_at: str | None  # the order was (or may have been) typed from here on
    handoff_id: str | None  # the briefing, written by the session or by the fallback writer
    takeover_started: bool
    last_at: float = 0.0  # the time of the chain's latest event: progress
    last_resort: bool = False  # its fallback briefing was written because the order could not be typed over the ceiling
    briefing_at: float | None = None  # when its briefing was written or found
    waiting: tuple[str, int | None] | None = None  # the last `handoff.waiting` state (reason, subagents live)
    phase: Phase = Phase.DRAINING
    triggered_wall: float = 0.0  # `triggered_at` as the log has it, before any credit mapping (the store's clock)
    compacting: bool = False  # the takeover attempts are over and compaction began: a restart must not replay them


def epoch(iso: str) -> float:
    return datetime.fromisoformat(iso).timestamp()


def iso(at: float) -> str:
    return datetime.fromtimestamp(at, UTC).isoformat().replace("+00:00", "Z")


def now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def pane_events(events: Sequence[HandoffEvent], pane_id: str, session_id: str | None) -> list[HandoffEvent]:
    """The events of this pane's current session; a reused pane id with another session sees none of the old ones."""
    return [e for e in events if e.pane_id == pane_id and (e.session_id is None or session_id is None or e.session_id == session_id)]


def _by_chain(events: Sequence[HandoffEvent]) -> list[list[HandoffEvent]]:
    by_chain: dict[str, list[HandoffEvent]] = {}
    for e in events:
        if e.event != "handoff.skipped":
            by_chain.setdefault(e.chain_id, []).append(e)
    return [mine for mine in by_chain.values() if any(e.event == "handoff.triggered" for e in mine)]


def _is_open(mine: Sequence[HandoffEvent]) -> bool:
    return not any(e.event in TERMINAL for e in mine)


def _chain_of(mine: Sequence[HandoffEvent]) -> Chain:
    names = {e.event for e in mine}
    started = next((e.at for e in mine if e.event == "handoff.drain_started"), None)
    briefing = next((e.handoff_id for e in mine if e.event in ("handoff.briefing_written", "handoff.briefing_fallback")), None)
    over = any(e.reason == OVER_CEILING for e in mine if e.event == "handoff.briefing_fallback")
    written = next((e.at for e in mine if e.event in ("handoff.briefing_written", "handoff.briefing_fallback")), None)
    waited = next((e for e in reversed(mine) if e.event == "handoff.waiting"), None)
    end = next((e for e in reversed(mine) if e.event in TERMINAL), None)
    if end is not None:
        phase = {Colour.GREEN: Phase.DONE_GREEN, Colour.ORANGE: Phase.DONE_ORANGE, Colour.RED: Phase.DONE_RED}[TERMINAL[end.event]]
    elif "handoff.takeover_started" in names:
        phase = Phase.TAKEOVER
    elif briefing is not None:
        phase = Phase.BRIEFING
    else:
        phase = Phase.WAITING if waited is not None else Phase.DRAINING
    return Chain(mine[0].chain_id, epoch(next(e.at for e in mine if e.event == "handoff.triggered")), started, briefing,
                 "handoff.takeover_started" in names, epoch(mine[-1].at), over, epoch(written) if written else None,
                 (waited.reason or "", waited.subagents_live) if waited else None, phase,
                 epoch(next(e.at for e in mine if e.event == "handoff.triggered")),
                 any(e.event == "handoff.takeover_started" and e.reason == COMPACTING for e in mine))


def open_chain(events: Sequence[HandoffEvent]) -> Chain | None:
    """The last chain that was triggered and has no terminal event yet."""
    return next((_chain_of(mine) for mine in reversed(_by_chain(events)) if _is_open(mine)), None)


def last_chain(events: Sequence[HandoffEvent]) -> Chain | None:
    """The last chain that was triggered, finished or not."""
    chains = _by_chain(events)
    return _chain_of(chains[-1]) if chains else None


def open_chain_events(events: Sequence[HandoffEvent]) -> list[list[HandoffEvent]]:
    """The events of every chain that was triggered and has no terminal event yet."""
    return [mine for mine in _by_chain(events) if _is_open(mine)]

"""Handoff controller event log: one JSON object per line in ``<store_root>/events.jsonl``."""
from __future__ import annotations

import json
import os
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path

from ..handoff_adapters.base import Harness


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
    INTERNAL_ERROR = "internal_error"  # finishing the chain raised three times in a row; `internal_error:<exception type>`


EVENT_NAMES = frozenset({
    "handoff.triggered", "handoff.drain_started", "handoff.drained", "handoff.briefing_written",
    "handoff.briefing_fallback", "handoff.wip_committed", "handoff.takeover_started", "handoff.succeeded",
    "handoff.failed_compacted", "handoff.stuck", "handoff.skipped",
})
_TERMINAL = {
    "handoff.succeeded": Colour.GREEN,
    "handoff.failed_compacted": Colour.ORANGE,
    "handoff.stuck": Colour.RED,
}
_REASON_REQUIRED = frozenset({"handoff.stuck", "handoff.skipped"})


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

    def __post_init__(self) -> None:
        if self.event not in EVENT_NAMES:
            raise ValueError(f"unknown handoff event name: {self.event!r}")
        if self.colour != _TERMINAL.get(self.event):  # terminal events carry their colour, all others none
            raise ValueError(f"{self.event} must have colour {_TERMINAL.get(self.event)!r}, got {self.colour!r}")
        if self.event in _REASON_REQUIRED and not self.reason:
            raise ValueError(f"{self.event} requires a reason")

    def to_json(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"))

    @classmethod
    def from_json(cls, line: str) -> HandoffEvent:
        data = json.loads(line)
        data["harness"] = Harness(data["harness"])
        if data["colour"] is not None:
            data["colour"] = Colour(data["colour"])
        return cls(**data)


class EventLog:
    def __init__(self, store_root: Path) -> None:
        self._path = store_root / "events.jsonl"

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
        """The events appended after byte `offset` (0 the first time) and the offset to continue from. Only complete
        lines are consumed, so an append in progress is read on the next call. The log is append-only; a file shorter
        than `offset` is read from the start, which the caller sees as a new offset below the one it passed."""
        try:
            with self._path.open("rb") as f:
                start = offset if offset <= os.fstat(f.fileno()).st_size else 0
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
        if e.event in _TERMINAL:
            return _TERMINAL[e.event]
    return None


def source_is_handed_off(events_for_pane: Sequence[HandoffEvent]) -> bool:
    """True when the session in this pane has a live successor, so it must not be drained or handed off again:
    a `handoff.succeeded`, or a `handoff.stuck` for SOURCE_NOT_CLOSED_SUCCESSOR_LIVE (only the close failed)."""
    return any(e.event == "handoff.succeeded"
               or (e.event == "handoff.stuck" and (e.reason or "").split(":")[0].split(";")[0]
                   == StuckReason.SOURCE_NOT_CLOSED_SUCCESSOR_LIVE.value)
               for e in events_for_pane)

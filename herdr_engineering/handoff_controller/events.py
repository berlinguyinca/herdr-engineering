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
        try:
            lines = self._path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        events: list[HandoffEvent] = []
        for line in lines:
            try:
                parsed = HandoffEvent.from_json(line)
            except (ValueError, KeyError, TypeError):
                continue  # mid-append, or a line that fails validation: skip it
            if chain_id is None or parsed.chain_id == chain_id:
                events.append(parsed)
        return events


def chain_colour(events: Sequence[HandoffEvent]) -> Colour | None:
    """Colour of the last terminal event; None when the chain has none."""
    for e in reversed(events):
        if e.event in _TERMINAL:
            return _TERMINAL[e.event]
    return None

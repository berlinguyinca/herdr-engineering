"""Compaction as the last resort: the briefing's location is handed to the session after its own compaction.

Both inputs (the harness's compact command and the pointer text) are typed only when Herdr reports the right agent
idle with an empty composer, the same rule as the drain order. Nothing here closes a pane.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..handoff_adapters.base import READY_STATUSES, HarnessAdapter, SessionRef, TypedInput
from .drain import Step, agent_matches, next_step, timeout_outcome
from .events import StuckReason
from .herdr_cli import HerdrCli


@dataclass(frozen=True)
class CompactionResult:
    """`reason` says why not when `ok` is false. Truthy exactly when compaction and the pointer both went through."""

    ok: bool
    reason: StuckReason | None = None

    def __bool__(self) -> bool:
        return self.ok


def pointer_text(path: Path) -> str:
    return f"Your briefing is {path}; read it after compaction"


def not_ready_reason(outcome: str) -> StuckReason:
    """What a wait for a safe moment to type reports when it ran out of time, from `drain.timeout_outcome`."""
    return {"blocked": StuckReason.BLOCKED, "wrong_agent": StuckReason.WRONG_AGENT}.get(
        outcome, StuckReason.COMPACTION_NOT_READY)


def _type(herdr: HerdrCli, pane_id: str, typed: TypedInput, sleep: Callable[[float], None]) -> bool:
    """Type the text, pause, send the submit keys (what `handoff_adapters.send.send_typed` does, over the CLI that
    can address any Herdr server)."""
    if not herdr.send_text(pane_id, typed.text):
        return False
    if typed.submit_keys:
        sleep(typed.submit_delay_s)
        return herdr.send_keys(pane_id, [key.lower() for key in typed.submit_keys])
    return True


def _wait_ready(ref: SessionRef, adapter: HarnessAdapter, herdr: HerdrCli, timeout_s: float, poll_s: float,
                sleep: Callable[[float], None], clock: Callable[[], float]) -> StuckReason | None:
    """None once the pane is idle with an empty composer (checked twice in a row), else why not within `timeout_s`."""
    deadline = clock() + timeout_s
    last_status: str | None = None
    wrong = False
    streak = 0
    while True:
        info = herdr.agent(ref.pane_id)
        last_status = info.status if info else None
        wrong = info is not None and not agent_matches(info.name, ref.harness)
        screen = herdr.read_visible(ref.pane_id) if last_status in READY_STATUSES else ""
        ready = last_status in READY_STATUSES and not wrong and adapter.is_ready_for_input(screen, last_status or "")
        streak = streak + 1 if next_step(last_status, ready) is Step.TYPE else 0
        if streak >= 2:
            return None
        remaining = deadline - clock()
        if remaining <= 0:
            return not_ready_reason(timeout_outcome(last_status, wrong))
        sleep(max(0.0, min(poll_s, remaining)))


def compact_with_pointer(ref: SessionRef, adapter: HarnessAdapter, pointer_path: Path, herdr: HerdrCli, *,
                         ready_timeout_s: float = 60.0, work_start_timeout_s: float = 15.0,
                         work_end_timeout_s: float = 600.0, poll_s: float = 0.5,
                         sleep: Callable[[float], None] = time.sleep,
                         clock: Callable[[], float] = time.monotonic) -> CompactionResult:
    """Type the compact command, wait for the agent to work and come back, then type the pointer to the briefing.

    The wait first needs to see the agent working (polled every `poll_s`): a compaction that finishes between two polls
    is never seen, and ends as COMPACTION_NOT_STARTED. That is deliberate and conservative: the pointer is not typed
    without proof that the compaction ran and finished."""
    try:
        compact = adapter.compact_input()
        pointer = adapter.drain_input(pointer_text(pointer_path))  # raises when the text exceeds the typed-input limit
    except NotImplementedError:
        return CompactionResult(False, StuckReason.NO_COMPACT_COMMAND)
    except ValueError:
        return CompactionResult(False, StuckReason.POINTER_TOO_LONG)
    if (why := _wait_ready(ref, adapter, herdr, ready_timeout_s, poll_s, sleep, clock)) is not None:
        return CompactionResult(False, why)
    if not _type(herdr, ref.pane_id, compact, sleep):
        return CompactionResult(False, StuckReason.COMPACTION_NOT_READY)
    started = clock() + work_start_timeout_s
    while herdr.agent_status(ref.pane_id) != "working":  # a compaction that never shows as working did not start
        if clock() >= started:
            return CompactionResult(False, StuckReason.COMPACTION_NOT_STARTED)
        sleep(poll_s)
    if not herdr.wait_idle(ref.pane_id, work_end_timeout_s):
        return CompactionResult(False, StuckReason.COMPACTION_TIMEOUT)
    if (why := _wait_ready(ref, adapter, herdr, ready_timeout_s, poll_s, sleep, clock)) is not None:
        return CompactionResult(False, why)
    if not _type(herdr, ref.pane_id, pointer, sleep):
        return CompactionResult(False, StuckReason.COMPACTION_NOT_READY)
    return CompactionResult(True)

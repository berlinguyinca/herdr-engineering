"""Typing the drain order into a session, only when that cannot land in something else.

The order is typed once, with `herdr agent prompt`, and only when Herdr reports the agent idle or done and the adapter
reads an empty composer on the visible screen, twice in a row (a user may start typing between the two reads).
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum, auto
from typing import Literal

from ..handoff_adapters.base import READY_STATUSES, HarnessAdapter, SessionRef
from ..handoff_subagent_gate import SubagentGate
from .config import ControllerConfig
from .herdr_cli import HerdrCli, PromptResult


class Step(Enum):
    WAIT = auto()   # the agent is not idle: wait for it
    CHECK = auto()  # idle, but the composer is not empty: look again later
    TYPE = auto()   # idle and the composer is empty


@dataclass(frozen=True)
class DrainResult:
    """`typed`: the order was submitted. `blocked`: the timeout expired while the agent waited on a dialog.
    `timeout`: it expired without a safe moment to type. `too_long`: the order exceeds the typed-input limit; nothing was tried."""

    outcome: Literal["typed", "blocked", "timeout", "too_long"]
    waited_s: float


def next_step(status: str | None, screen_ready: bool) -> Step:
    """The whole decision. A blocked, working, unknown or unreadable agent is never typed into."""
    if status not in READY_STATUSES:
        return Step.WAIT
    return Step.TYPE if screen_ready else Step.CHECK


def prompt_reached_pane(status: str | None, screen: str, order_text: str) -> bool:
    """After a failed `herdr agent prompt`: did the order land anyway? Then it must not be typed a second time."""
    return status == "working" or order_text[:40] in screen


def drain(ref: SessionRef, adapter: HarnessAdapter, gate: SubagentGate, herdr: HerdrCli, cfg: ControllerConfig,
          *, poll_s: float = 2.0) -> DrainResult:
    start = time.monotonic()
    deadline = start + cfg.drain_timeout_s

    def result(outcome: Literal["typed", "blocked", "timeout", "too_long"]) -> DrainResult:
        return DrainResult(outcome, time.monotonic() - start)

    try:
        adapter.drain_input(gate.order_text)  # raises when the order exceeds the typed-input limit
    except ValueError:
        return result("too_long")

    def ready_now() -> tuple[str | None, bool, str]:
        status = herdr.agent_status(ref.pane_id)
        screen = herdr.read_visible(ref.pane_id) if status in READY_STATUSES else ""
        return status, status in READY_STATUSES and adapter.is_ready_for_input(screen, status or ""), screen

    attempted = False  # a prompt that failed may still have reached the pane

    blocked = False
    while (remaining := deadline - time.monotonic()) > 0:
        status, screen_ready, _ = ready_now()
        blocked = status == "blocked"
        step = next_step(status, screen_ready)
        if step is Step.WAIT and status is not None:  # no status at all (herdr unreachable) backs off below instead of spinning
            herdr.wait_idle(ref.pane_id, min(remaining, poll_s))  # returns early when it goes idle; times out while blocked
            continue
        if step is Step.TYPE:
            status, screen_ready, screen = ready_now()  # re-read just before typing: the user may have started typing
            if attempted and prompt_reached_pane(status, screen, gate.order_text):
                return result("typed")
            if next_step(status, screen_ready) is Step.TYPE:
                sent = herdr.prompt(ref.pane_id, gate.order_text)
                if sent is PromptResult.SENT:
                    return result("typed")
                if sent is PromptResult.FAILED:
                    attempted = True
                    status, _, screen = ready_now()
                    if prompt_reached_pane(status, screen, gate.order_text):
                        return result("typed")
        time.sleep(max(0.0, min(poll_s, deadline - time.monotonic())))
    return result("blocked" if blocked else "timeout")

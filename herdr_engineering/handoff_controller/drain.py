"""Typing the drain order into a session, only when that cannot land in something else.

The order is typed once, with `herdr agent prompt`, and only when Herdr reports the agent idle or done and the adapter
reads an empty composer on the visible screen, twice in a row (a user may start typing between the two reads).
"""
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum, auto
from typing import Literal

from ..handoff_adapters.base import READY_STATUSES, Harness, HarnessAdapter, SessionRef
from ..handoff_subagent_gate import SubagentGate
from .config import ControllerConfig
from .herdr_cli import HerdrCli, PromptResult


class Step(Enum):
    WAIT = auto()   # the agent is not idle: wait for it
    CHECK = auto()  # idle, but the composer is not empty: look again later
    TYPE = auto()   # idle and the composer is empty


Outcome = Literal["typed", "blocked", "timeout", "too_long", "wrong_agent"]


@dataclass(frozen=True)
class DrainResult:
    """`typed`: the order was submitted. `blocked`: the timeout expired while the agent waited on a dialog.
    `timeout`: it expired without a safe moment to type. `too_long`: the order exceeds the typed-input limit; nothing was tried.
    `wrong_agent`: the timeout expired while the pane ran another harness than the session's; nothing was typed."""

    outcome: Outcome
    waited_s: float


def next_step(status: str | None, screen_ready: bool) -> Step:
    """The whole decision. A blocked, working, unknown or unreadable agent is never typed into."""
    if status not in READY_STATUSES:
        return Step.WAIT
    return Step.TYPE if screen_ready else Step.CHECK


def prompt_reached_pane(status: str | None, screen: str, order_text: str) -> bool:
    """After a failed `herdr agent prompt`: did the order land anyway? Then it must not be typed a second time."""
    return status == "working" or order_text[:40] in screen


def agent_matches(name: str | None, harness: Harness) -> bool:
    """Herdr names a pane's agent after its harness (`claude`, `codex`, `pi`, `opencode`)."""
    return name == harness.value


def timeout_outcome(last_status: str | None, wrong_agent: bool) -> Outcome:
    """What a drain that ran out of time reports, from the last thing it saw."""
    if last_status == "blocked":
        return "blocked"
    return "wrong_agent" if wrong_agent else "timeout"


def drain(ref: SessionRef, adapter: HarnessAdapter, gate: SubagentGate, herdr: HerdrCli, cfg: ControllerConfig,
          *, poll_s: float = 2.0, sleep: Callable[[float], None] = time.sleep,
          clock: Callable[[], float] = time.monotonic) -> DrainResult:
    start = clock()
    deadline = start + cfg.drain_timeout_s

    def result(outcome: Outcome) -> DrainResult:
        return DrainResult(outcome, clock() - start)

    try:
        order = adapter.drain_input(gate.order_text).text  # raises when the order exceeds the typed-input limit
    except ValueError:
        return result("too_long")

    def look() -> tuple[str | None, bool, bool, str]:
        """(status, ready to type into, a different harness runs in the pane, screen)"""
        info = herdr.agent(ref.pane_id)
        status = info.status if info else None
        screen = herdr.read_visible(ref.pane_id) if status in READY_STATUSES else ""
        wrong = info is not None and not agent_matches(info.name, ref.harness)
        ready = status in READY_STATUSES and not wrong and adapter.is_ready_for_input(screen, status or "")
        return status, ready, wrong, screen

    attempted = False  # a prompt that failed may still have reached the pane
    last_status: str | None = None
    wrong_agent = False
    while (remaining := deadline - clock()) > 0:
        began = clock()
        last_status, screen_ready, wrong_agent, _ = look()
        step = next_step(last_status, screen_ready)
        if step is Step.WAIT and last_status is not None:  # no status at all (herdr unreachable) backs off below instead of spinning
            if not herdr.wait_idle(ref.pane_id, min(remaining, poll_s)):  # returns early when it goes idle; times out while blocked
                sleep(max(0.0, min(poll_s - (clock() - began), deadline - clock())))  # a quick failure (pane gone) must not spin
            continue
        if step is Step.TYPE:
            last_status, screen_ready, wrong_agent, screen = look()  # re-read just before typing: the user may have started typing
            if attempted and prompt_reached_pane(last_status, screen, order):
                return result("typed")
            if next_step(last_status, screen_ready) is Step.TYPE:
                sent = herdr.prompt(ref.pane_id, order)
                if sent is PromptResult.SENT:
                    return result("typed")
                if sent is PromptResult.FAILED:
                    attempted = True
                    last_status, _, wrong_agent, screen = look()
                    if prompt_reached_pane(last_status, screen, order):
                        return result("typed")
        sleep(max(0.0, min(poll_s, deadline - clock())))
    return result(timeout_outcome(last_status, wrong_agent))

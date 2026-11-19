"""Hand the source session's pane to its successor with `herdr-handoff takeover`, retry, then compact as a last resort.

herdr-handoff does the work (launch the same harness in a pane below, wait for proof it is live, ask the source harness
to exit, close the source pane) and records every step in the handoff record, so a rerun for the same handoff id
continues from the record: it reuses the recorded child pane and never launches a second one. The controller only
decides whether to call it again and what the chain's terminal event is. It never closes a pane itself.
"""
from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum, auto
from pathlib import Path

from ..handoff_adapters.base import HarnessAdapter, SessionRef
from ..handoff_subagent_gate import finish
from .compaction import compact_with_pointer
from .config import ControllerConfig
from .events import Colour, EventLog, HandoffEvent, StuckReason
from .handoff_cli import HandoffCli, HandoffCliResult
from .herdr_cli import HerdrCli

_CLOSED_PHASES = frozenset({"source_closed", "already_closed"})


class Action(Enum):
    LAUNCH = auto()  # run `herdr-handoff takeover`: it launches, or continues the recorded move and reuses the child
    DONE = auto()  # the record says the source pane is closed
    STUCK = auto()  # a successor is recorded without a launched child: launching again could make a second one


@dataclass(frozen=True)
class RecordView:
    successor_id: str | None
    phase: str | None
    child_pane: str | None
    child_session_id: str | None


def record_view(record: Mapping[str, object] | None) -> RecordView:
    """What the controller needs of a handoff record; anything missing or malformed reads as absent."""
    record = record or {}
    block = record.get("takeover")
    target = record.get("target")

    def text(source: object, key: str) -> str | None:
        value = source.get(key) if isinstance(source, dict) else None
        return value if isinstance(value, str) and value else None

    return RecordView(text(record, "successor_id"), text(block, "phase"), text(block, "child_pane"),
                      text(target, "session_id"))


def next_action(view: RecordView) -> Action:
    """The idempotency rule, before every attempt. A rerun after `child confirmed, source not yet closed` is LAUNCH on
    purpose: herdr-handoff reuses the child and finishes by closing the source."""
    if view.phase in _CLOSED_PHASES and view.child_pane:
        return Action.DONE
    if view.successor_id and not view.child_pane:
        return Action.STUCK
    return Action.LAUNCH


def successor_of(view: RecordView, child_pane: str | None) -> str | None:
    """The identity the terminal event names: a recorded successor handoff, else the child's session, else its pane."""
    return view.successor_id or view.child_session_id or child_pane or view.child_pane


class Outcome(Enum):
    CLOSED = auto()  # the successor is confirmed and the source pane is closed
    REFUSED = auto()  # herdr-handoff reported a refusal status; the source pane is untouched
    FAILED = auto()  # no usable answer: timed out, could not start, exited with a message and no JSON


@dataclass(frozen=True)
class TakeoverResult:
    outcome: Outcome
    reason: str
    child_pane: str | None = None


def classify(done: HandoffCliResult) -> TakeoverResult:
    """The result of one `herdr-handoff takeover` call: its JSON on stdout, exit 1 for a refusal status."""
    try:
        reply = json.loads(done.stdout)
    except ValueError:
        reply = None
    if not isinstance(reply, dict) or not isinstance(reply.get("status"), str):
        first = (done.stderr.strip().splitlines() or ["no output"])[0][:200]
        return TakeoverResult(Outcome.FAILED, f"herdr-handoff exit {done.returncode}: {first}")
    status = reply["status"]
    pane = reply.get("pane_id") if isinstance(reply.get("pane_id"), str) else None
    if status == "source_closed" and reply.get("source_closed") is True and done.returncode == 0:
        return TakeoverResult(Outcome.CLOSED, "", pane)
    detail = reply.get("reason")
    return TakeoverResult(Outcome.REFUSED, f"{status}: {detail}" if isinstance(detail, str) and detail else status, pane)


def stuck_reason(head: StuckReason, detail: StuckReason | str | None, last_takeover: str | None) -> str:
    """`head: detail` plus the last takeover refusal, so one string says what failed and what came before."""
    parts = [head.value + (f": {detail.value if isinstance(detail, StuckReason) else detail}" if detail else "")]
    if last_takeover:
        parts.append(f"last takeover result: {last_takeover}")
    return "; ".join(parts)


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def run_takeover(ref: SessionRef, adapter: HarnessAdapter, handoff_id: str, cfg: ControllerConfig, log: EventLog,
                 handoff: HandoffCli, herdr: HerdrCli, *, store_root: Path, worktree: Path, push_allowed: bool,
                 env: Mapping[str, str], chain_id: str | None = None, predecessor_id: str | None = None,
                 context_pct: float | None = None, confirm_timeout_s: float = 25.0, call_margin_s: float = 60.0,
                 retry_delay_s: float = 2.0, compact_ready_timeout_s: float = 60.0,
                 compact_work_start_s: float = 15.0, compact_work_end_s: float = 600.0, poll_s: float = 0.5,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic) -> HandoffEvent:
    """Up to `cfg.max_attempts` takeovers (each preceded by a look at the record), then compaction. The returned
    event, `handoff.succeeded`, `handoff.failed_compacted` or `handoff.stuck`, is appended to `log` exactly once, after
    `handoff.takeover_started`. `--allow-dirty-source` is passed only when `push_allowed` is False: there unpushed
    commits are recorded by herdr-handoff and do not stop the close; where pushing is allowed they must be pushed."""
    chain = chain_id or handoff_id

    def event(name: str, *, colour: Colour | None = None, reason: str | None = None,
              successor_id: str | None = None) -> HandoffEvent:
        made = HandoffEvent(name, _now(), chain, ref.pane_id, ref.session_id, ref.harness, handoff_id, predecessor_id,
                            successor_id, colour, reason, context_pct)
        log.append(made)
        return made

    def succeeded(child_pane: str | None) -> HandoffEvent:
        view = record_view(handoff.inspect(store_root, handoff_id))
        made = event("handoff.succeeded", colour=Colour.GREEN, successor_id=successor_of(view, child_pane))
        finish(ref.pane_id, env)
        return made

    def stuck(why: str) -> HandoffEvent:
        return event("handoff.stuck", colour=Colour.RED, reason=why)

    event("handoff.takeover_started")
    exit_plan = adapter.exit_plan()
    timeout_s = confirm_timeout_s + exit_plan.process_end_timeout_s + call_margin_s
    last: str | None = None
    for attempt in range(cfg.max_attempts):
        if attempt:
            sleep(retry_delay_s)
        view = record_view(handoff.inspect(store_root, handoff_id))
        match next_action(view):
            case Action.DONE:
                return succeeded(view.child_pane)
            case Action.STUCK:
                return stuck(stuck_reason(StuckReason.SUCCESSOR_WITHOUT_TAKEOVER, view.successor_id, last))
            case Action.LAUNCH:
                pass
        result = classify(handoff.takeover(store_root, handoff_id, parent_pane=ref.pane_id, worktree=worktree,
                                           exit_plan=exit_plan, allow_dirty=not push_allowed,
                                           confirm_timeout_s=confirm_timeout_s, timeout_s=timeout_s))
        if result.outcome is Outcome.CLOSED:
            return succeeded(result.child_pane)
        last = result.reason
    pointer = handoff.document(store_root, handoff_id)
    if pointer is None:
        return stuck(stuck_reason(StuckReason.NO_BRIEFING, "the briefing document could not be located", last))
    done = compact_with_pointer(ref, adapter, pointer, herdr, ready_timeout_s=compact_ready_timeout_s,
                                work_start_timeout_s=compact_work_start_s, work_end_timeout_s=compact_work_end_s,
                                poll_s=poll_s, sleep=sleep, clock=clock)
    if done:
        return event("handoff.failed_compacted", colour=Colour.ORANGE, reason=last)
    return stuck(stuck_reason(StuckReason.TAKEOVER_REFUSED_AND_COMPACTION_FAILED, done.reason, last))

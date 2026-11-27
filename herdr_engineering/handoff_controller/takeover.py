"""Hand the source session's pane to its successor with `herdr-handoff takeover`, retry, then compact as a last resort.

herdr-handoff does the work (launch the same harness in a pane below, wait for proof it is live, ask the source harness
to exit, close the source pane) and records every step in the handoff record, so a rerun for the same handoff id
continues from the record: it reuses the recorded child pane and never launches a second one. The controller only
decides whether to call it again and what the chain's terminal event is. It never closes a pane itself.

Two limits to know when reading an event:
- A takeover killed after the child was launched but before it was confirmed counts as unconfirmed, so the last-resort
  compaction may run while a slow child is still starting. This shows as `successor_pane_id` on the ORANGE event.
- An agent that turns `blocked` right after a failed prompt is still reported as typed: the screen shows a dialog, not
  whether the text went in.
"""
from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path

from ..handoff_adapters.base import HarnessAdapter, SessionRef
from ..handoff_subagent_gate import finish
from .compaction import compact_with_pointer
from .config import ControllerConfig
from .events import ATTEMPT, COMPACTING, Colour, EventLog, HandoffEvent, StuckReason, now_iso
from .handoff_cli import HandoffCli, HandoffCliResult
from .herdr_cli import HerdrCli

_CLOSED_PHASES = frozenset({"source_closed", "already_closed"})
_CONFIRMED_PHASES = frozenset({"confirmed", "source_exit_failed"})  # the child is proven live; the source stays open


class Action(Enum):
    LAUNCH = auto()  # run `herdr-handoff takeover`: it launches, or continues the recorded move and reuses the child
    DONE = auto()  # the record says the source pane is closed
    STUCK = auto()  # a successor is recorded without a launched child: launching again could make a second one
    LAUNCH_UNKNOWN = auto()  # a takeover died between placing the pane and recording it: a child may exist
    SUCCESSOR_LIVE = auto()  # after the attempts: the child is confirmed live, only the close failed
    COMPACT = auto()  # after the attempts: no confirmed child, so compaction is the last resort


@dataclass(frozen=True)
class RecordView:
    successor_id: str | None
    phase: str | None
    child_pane: str | None
    child_session_id: str | None
    child_confirmed: bool = False  # launched and confirmed by herdr-handoff, whether or not the source closed


def record_view(record: Mapping[str, object] | None) -> RecordView:
    """What the controller needs of a handoff record; anything missing or malformed reads as absent."""
    record = record or {}
    block = record.get("takeover")
    target = record.get("target")

    def text(source: object, key: str) -> str | None:
        value = source.get(key) if isinstance(source, dict) else None
        return value if isinstance(value, str) and value else None

    phase, child = text(block, "phase"), text(block, "child_pane")
    return RecordView(text(record, "successor_id"), phase, child, text(target, "session_id"),
                      bool(child) and (phase in _CONFIRMED_PHASES or phase in _CLOSED_PHASES))


def next_action(view: RecordView) -> Action:
    """The idempotency rule, before every attempt. A rerun after `child confirmed, source not yet closed` is LAUNCH on
    purpose: herdr-handoff reuses the child and finishes by closing the source."""
    if view.phase in _CLOSED_PHASES and view.child_pane:
        return Action.DONE
    if view.phase == "launching" and not view.child_pane:  # killed after placing the pane, before recording it
        return Action.LAUNCH_UNKNOWN
    if view.successor_id and not view.child_pane:
        return Action.STUCK
    return Action.LAUNCH


def next_after_attempts(view: RecordView) -> Action:
    """What follows when no attempt closed the source: the record decides, never the last refusal. A confirmed child
    means the handoff happened and compacting the source would make two sessions work on one task."""
    base = next_action(view)
    if base is not Action.LAUNCH:
        return base
    return Action.SUCCESSOR_LIVE if view.child_confirmed else Action.COMPACT


class Outcome(Enum):
    CLOSED = auto()  # the successor is confirmed and the source pane is closed
    REFUSED = auto()  # herdr-handoff reported a refusal status; the source pane is untouched
    FAILED = auto()  # no usable answer: timed out, could not start, exited with a message and no JSON


@dataclass(frozen=True)
class TakeoverResult:
    outcome: Outcome
    reason: str
    child_pane: str | None = None
    status: str | None = None  # herdr-handoff's status word, when it answered with one


def retry_can_help(result: TakeoverResult) -> bool:
    """`source_dirty` repeats until the work is committed or pushed, which nothing here does: do not ask again."""
    return result.status != "source_dirty"


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
        return TakeoverResult(Outcome.CLOSED, "", pane, status)
    detail = reply.get("reason")
    return TakeoverResult(Outcome.REFUSED, f"{status}: {detail}" if isinstance(detail, str) and detail else status, pane,
                          status)


def stuck_reason(head: StuckReason, detail: StuckReason | str | None, last_takeover: str | None) -> str:
    """`head: detail` plus the last takeover refusal, so one string says what failed and what came before."""
    parts = [head.value + (f": {detail.value if isinstance(detail, StuckReason) else detail}" if detail else "")]
    if last_takeover:
        parts.append(f"last takeover result: {last_takeover}")
    return "; ".join(parts)


def run_takeover(ref: SessionRef, adapter: HarnessAdapter, handoff_id: str, cfg: ControllerConfig, log: EventLog,
                 handoff: HandoffCli, herdr: HerdrCli, *, store_root: Path, worktree: Path, push_allowed: bool,
                 env: Mapping[str, str], ready: Callable[[], bool], attempts_made: int = 0, chain_id: str | None = None, predecessor_id: str | None = None,
                 context_pct: float | None = None, started: bool = False, confirm_timeout_s: float = 25.0, call_margin_s: float = 60.0,
                 retry_delay_s: float = 2.0, compact_ready_timeout_s: float = 60.0,
                 compact_work_start_s: float = 15.0, compact_work_end_s: float = 600.0, poll_s: float = 0.5,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic) -> HandoffEvent | None:
    """Up to `cfg.max_attempts` takeovers (each preceded by a look at the record), then compaction. The returned
    event, `handoff.succeeded`, `handoff.failed_compacted` or `handoff.stuck`, is appended to `log` exactly once, after
    `handoff.takeover_started`. `--allow-dirty-source` is passed only when `push_allowed` is False: there unpushed
    commits are recorded by herdr-handoff and do not stop the close; where pushing is allowed they must be pushed.
    The controller also passes False after a refused push (`push_failed`) or `NOTHING_STAGEABLE`.
    `started`: the chain's `handoff.takeover_started` is already in the log.
    `ready` is asked right before every call to herdr-handoff, after the look at the record; when it says no, nothing is
    called and None is returned with no terminal event: the chain goes on waiting, and the next call starts over.
    `attempts_made`: the calls the log already counts for this chain; only the rest of `cfg.max_attempts` is made."""
    chain = chain_id or handoff_id

    def event(name: str, *, colour: Colour | None = None, reason: str | None = None,
              view: RecordView | None = None, pane: str | None = None) -> HandoffEvent:
        """`successor_id` is the record's successor handoff id; the child's pane and session ride in their own fields."""
        view = view or RecordView(None, None, None, None)
        made = HandoffEvent(name, now_iso(), chain, ref.pane_id, ref.session_id, ref.harness, handoff_id, predecessor_id,
                            view.successor_id, colour, reason, context_pct, view.child_pane or pane,
                            view.child_session_id)
        log.append(made)
        if colour is Colour.GREEN:  # only a closed source may drop its subagents' record; red and orange leave them running
            try:
                finish(ref.pane_id, env)
            except Exception:  # noqa: BLE001 - cleanup must not change the outcome
                pass
        return made

    def succeeded(view: RecordView, pane: str | None) -> HandoffEvent:
        return event("handoff.succeeded", colour=Colour.GREEN, view=view, pane=pane)

    def stuck(why: str, view: RecordView | None = None, pane: str | None = None) -> HandoffEvent:
        return event("handoff.stuck", colour=Colour.RED, reason=why, view=view, pane=pane)

    if not started:
        event("handoff.takeover_started")
    exit_plan = adapter.exit_plan()
    timeout_s = confirm_timeout_s + exit_plan.process_end_timeout_s + call_margin_s
    last: str | None = None
    last_pane: str | None = None
    for attempt in range(max(cfg.max_attempts - attempts_made, 0)):
        if attempt:
            sleep(retry_delay_s)
        record = handoff.inspect(store_root, handoff_id)
        if record is None:  # an unreadable record says nothing: never launch, never compact on it
            return stuck(StuckReason.RECORD_UNREADABLE.value)
        view = record_view(record)
        if next_action(view) is not Action.LAUNCH:
            break
        if not ready():
            return None
        event("handoff.takeover_started", reason=ATTEMPT)  # counted in the log: a later call never exceeds `max_attempts`
        result = classify(handoff.takeover(store_root, handoff_id, parent_pane=ref.pane_id, worktree=worktree,
                                           exit_plan=exit_plan, allow_dirty=not push_allowed,
                                           confirm_timeout_s=confirm_timeout_s, timeout_s=timeout_s))
        if result.outcome is Outcome.CLOSED:
            return succeeded(record_view(handoff.inspect(store_root, handoff_id)), result.child_pane)
        last, last_pane = result.reason, result.child_pane or last_pane
        if not retry_can_help(result):
            break
    record = handoff.inspect(store_root, handoff_id)
    if record is None:
        return stuck(stuck_reason(StuckReason.RECORD_UNREADABLE, None, last), pane=last_pane)
    view = record_view(record)
    pane = view.child_pane or last_pane
    match next_after_attempts(view):
        case Action.DONE:
            return succeeded(view, pane)
        case Action.STUCK:
            return stuck(stuck_reason(StuckReason.SUCCESSOR_WITHOUT_TAKEOVER, view.successor_id, last), view, pane)
        case Action.LAUNCH_UNKNOWN:
            return stuck(stuck_reason(StuckReason.LAUNCH_STATE_UNKNOWN, "a takeover ended before recording its pane",
                                      last), view, pane)
        case Action.SUCCESSOR_LIVE:
            return stuck(stuck_reason(StuckReason.SOURCE_NOT_CLOSED_SUCCESSOR_LIVE, pane, last), view, pane)
        case _:
            pass
    pointer = handoff.document(store_root, handoff_id)
    if pointer is None:
        return stuck(stuck_reason(StuckReason.NO_BRIEFING, "the briefing document could not be located", last), view,
                     pane)
    event("handoff.takeover_started", reason=COMPACTING, view=view, pane=pane)  # the log remembers: a restart ends stuck, never retypes
    done = compact_with_pointer(ref, adapter, pointer, herdr, ready_timeout_s=compact_ready_timeout_s,
                                work_start_timeout_s=compact_work_start_s, work_end_timeout_s=compact_work_end_s,
                                poll_s=poll_s, sleep=sleep, clock=clock)
    if done:
        return event("handoff.failed_compacted", colour=Colour.ORANGE, reason=last, view=view, pane=pane)
    return stuck(stuck_reason(StuckReason.TAKEOVER_REFUSED_AND_COMPACTION_FAILED, done.reason, last), view, pane)

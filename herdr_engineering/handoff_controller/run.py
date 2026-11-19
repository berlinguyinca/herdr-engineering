"""The controller loop: every tick looks at each managed agent session, drains one at the context limit, and carries its
chain (drain, briefing, checkpoint commit, takeover) one step further.

The loop is single-threaded over all panes, so a tick never waits long for one session: the drain order is tried for a
few seconds per tick, and the briefing is looked for once per tick (`wait_for_briefing` with timeout 0). Everything a
chain needs to continue after a restart is in `events.jsonl` and the store; the in-memory `PaneRuntime` only holds what
cannot be told from them (the order was typed in this process, a cool-down). What each tick does is decided by the pure
functions `open_chain`, `may_start` and `next_step`.

A session that is still working is never acted on. After the order is typed the briefing timeout counts only the time
the session is idle (or done) with no live subagent; until then the chain waits (`handoff.waiting`, once per change),
bounded by `drain_timeout_s` from the trigger. The takeover is entered only into a ready pane (`_finish_when_ready`);
past `briefing_timeout_s` of waiting for that, the chain takes the last-resort path, which types only into a ready pane.
"""
from __future__ import annotations

import fcntl
import importlib
import os
import shutil
import signal
import sys
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import Enum, auto
from pathlib import Path

from ..handoff_adapters import adapter_for
from ..handoff_adapters.base import READY_STATUSES, Harness, HarnessAdapter, SessionRef
from ..handoff_subagent_gate import prepare_drain
from ..handoff_subagent_registry import live_entries
from .briefing import HarnessRunner, headless_runner, wait_for_briefing, write_fallback_briefing
from .config import ControllerConfig, config_from_env
from .drain import DrainResult, Look, drain, look
from .events import Colour, EventLog, HandoffEvent, StuckReason, source_is_handed_off
from .handoff_cli import HandoffCli
from .herdr_cli import AgentPane, HerdrCli, harness_for_agent_name
from .proc import run_group, use_tool
from .store import shared_store_root
from .switch import is_enabled
from .takeover import run_takeover, stuck_reason
from .trigger import Decision, decide
from .wip import WipReason, WipResult, wip_commit

Locator = Callable[[HarnessAdapter, AgentPane], SessionRef | None]
_TERMINAL = frozenset({"handoff.succeeded", "handoff.failed_compacted", "handoff.stuck"})
_DRAIN_STUCK: dict[str, StuckReason] = {
    "blocked": StuckReason.BLOCKED, "wrong_agent": StuckReason.WRONG_AGENT, "timeout": StuckReason.DRAIN_TIMEOUT,
    "too_long": StuckReason.DRAIN_ORDER_TOO_LONG, "prompt_uncertain": StuckReason.DRAIN_PROMPT_UNCERTAIN,
}
OVER_CEILING = "over_ceiling"  # the `reason` of the triggered / fallback-briefing event of a last-resort chain
_MAX_CHAINS = 3  # non-green chains per session before the controller gives up on it
_MAX_FINISH_FAILURES = 3
_FALLBACK_WORK_MAX_AGE_S = 3600.0
_LOCATE_RETRY_S = 30.0  # a session that could not be located is looked for again after this long (Codex's lookup forks ps/lsof)


# --- pure decisions -------------------------------------------------------------------------------------------------

def _epoch(iso: str) -> float:
    return datetime.fromisoformat(iso).timestamp()


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class Chain:
    """What the event log says about the chain in progress for a pane."""
    chain_id: str
    triggered_at: float
    drain_started_at: str | None  # the order was (or may have been) typed from here on
    handoff_id: str | None  # the briefing, written by the session or by the fallback writer
    takeover_started: bool
    last_at: float = 0.0  # the time of the chain's latest event: progress
    last_resort: bool = False  # its fallback briefing was written because the order could not be typed over the ceiling
    briefing_at: float | None = None  # when its briefing was written or found
    waiting: tuple[str, int | None] | None = None  # the last `handoff.waiting` state (reason, subagents live)


def pane_events(events: Sequence[HandoffEvent], pane_id: str, session_id: str | None) -> list[HandoffEvent]:
    """The events of this pane's current session; a reused pane id with another session sees none of the old ones."""
    return [e for e in events if e.pane_id == pane_id and (e.session_id is None or session_id is None or e.session_id == session_id)]


def open_chain(events: Sequence[HandoffEvent]) -> Chain | None:
    """The last chain that was triggered and has no terminal event yet."""
    by_chain: dict[str, list[HandoffEvent]] = {}
    for e in events:
        if e.event != "handoff.skipped":
            by_chain.setdefault(e.chain_id, []).append(e)
    for chain_id, mine in reversed(list(by_chain.items())):
        names = {e.event for e in mine}
        if "handoff.triggered" not in names or names & _TERMINAL:
            continue
        started = next((e.at for e in mine if e.event == "handoff.drain_started"), None)
        briefing = next((e.handoff_id for e in mine if e.event in ("handoff.briefing_written", "handoff.briefing_fallback")), None)
        over = any(e.reason == OVER_CEILING for e in mine if e.event == "handoff.briefing_fallback")
        written = next((e.at for e in mine if e.event in ("handoff.briefing_written", "handoff.briefing_fallback")), None)
        waited = next((e for e in reversed(mine) if e.event == "handoff.waiting"), None)
        return Chain(chain_id, _epoch(next(e.at for e in mine if e.event == "handoff.triggered")), started, briefing,
                     "handoff.takeover_started" in names, _epoch(mine[-1].at), over,
                     _epoch(written) if written else None,
                     (waited.reason or "", waited.subagents_live) if waited else None)
    return None


def _failed_chains(events: Sequence[HandoffEvent]) -> list[HandoffEvent]:
    """The orange and red terminal events since the switch was last seen off (turning it off and on starts over)."""
    start = max((i + 1 for i, e in enumerate(events) if e.event == "handoff.skipped" and e.reason == "switch_off"), default=0)
    return [e for e in events[start:] if e.event in _TERMINAL and e.event != "handoff.succeeded"]


def gave_up(events: Sequence[HandoffEvent], cfg: ControllerConfig) -> bool:
    """True after `_MAX_CHAINS` chains of this session ended orange or red: it is left alone until its session id
    changes (its events are then not these) or the switch is seen off and on again."""
    return len(_failed_chains(events)) >= _MAX_CHAINS


def may_start(events: Sequence[HandoffEvent], now: float, cfg: ControllerConfig) -> bool:
    """A new chain may start unless the session already has a successor, the controller gave up on it, or its last
    chain ended (orange or red) less than `drain_timeout_s` times 1, 2, 4 ... (doubling per failed chain) ago."""
    if source_is_handed_off(events):
        return False
    failed = _failed_chains(events)
    if not failed:
        return True
    return len(failed) < _MAX_CHAINS and now - _epoch(failed[-1].at) >= cfg.drain_timeout_s * 2 ** (len(failed) - 1)


class Step(Enum):
    NONE = auto()
    DRAIN = auto()  # start the chain, or try to type the order again
    CHECK_BRIEFING = auto()  # the order is typed (or a cool-down runs): look for the briefing once
    FALLBACK = auto()  # the briefing wait is over: look once more, then write one
    FINISH = auto()  # a briefing exists: checkpoint commit, takeover
    DRAIN_STUCK = auto()  # the order could not be typed within the drain timeout


@dataclass
class PaneRuntime:
    session_id: str | None
    chain_id: str | None = None  # the chain this runtime belongs to; another chain starts it afresh
    typed: bool = False  # the order was typed, or may have been: never type it again
    wait_since: float = 0.0  # when the briefing wait began
    cooldown_until: float = 0.0  # after an uncertain prompt: no new order before this
    quiet_since: float | None = None  # since when the session is idle with no live subagent, after the order was typed
    wait_state: tuple[str, int | None] | None = None  # the `handoff.waiting` state last said
    last_outcome: str | None = None  # the last drain outcome that was not `typed`
    finish_failures: int = 0  # consecutive times finishing the chain raised

    def reset(self, chain_id: str | None) -> None:
        self.chain_id, self.typed, self.wait_since, self.cooldown_until = chain_id, False, 0.0, 0.0
        self.quiet_since, self.wait_state = None, None
        self.last_outcome, self.finish_failures = None, 0


def next_step(chain: Chain | None, rt: PaneRuntime, decision: Decision, now: float, cfg: ControllerConfig,
              startable: bool) -> Step:
    if chain is None:
        return Step.DRAIN if startable and decision is not Decision.NONE else Step.NONE
    if chain.handoff_id is not None or chain.takeover_started:
        return Step.FINISH
    if rt.typed:
        floor = rt.wait_since + cfg.briefing_timeout_s  # the session always gets this long to write its briefing
        quiet_done = rt.quiet_since is not None and now >= rt.quiet_since + cfg.briefing_timeout_s
        bound = max(chain.triggered_at + cfg.drain_timeout_s, floor)  # a session that keeps working is waited for this long
        full = decision is Decision.OVER_CEILING and now >= floor  # a full context cannot wait for the quiet time
        return Step.FALLBACK if quiet_done or full or now >= bound else Step.CHECK_BRIEFING
    if now - chain.triggered_at >= cfg.drain_timeout_s:
        return Step.DRAIN_STUCK
    if now < rt.cooldown_until:
        return Step.CHECK_BRIEFING
    return Step.DRAIN


def push_allowed(cfg: ControllerConfig, worktree: Path, common_dir: Path | None) -> bool:
    """True when the worktree, or the main checkout of its repository (parent of `common_dir`), is under one of
    `cfg.push_allowed_repos`; any other worktree follows `cfg.push_default`."""
    roots = [Path(r).expanduser().resolve() for r in cfg.push_allowed_repos]
    where = [worktree.resolve(), *([common_dir.resolve().parent] if common_dir else [])]
    return any(r == w or r in w.parents for r in roots for w in where) or cfg.push_default


def drain_not_typed_reason(outcome: str) -> StuckReason:
    return _DRAIN_STUCK.get(outcome, StuckReason.DRAIN_TIMEOUT)


# --- the controller -------------------------------------------------------------------------------------------------

def state_dir_for(env: Mapping[str, str]) -> Path:
    from ..handoff_subagent_registry import _state_dir
    return _state_dir(env).parent


def default_locate(adapter: HarnessAdapter, pane: AgentPane) -> SessionRef | None:
    """The production way to find a session's record: the adapter's own lookup."""
    return adapter.locate(pane.pane_id, pane.cwd) if pane.cwd else None


@dataclass
class Controller:
    """Everything a tick needs. `locate` is the seam for tests whose Herdr cannot report a session id."""
    cfg: ControllerConfig
    store_root: Path
    state_dir: Path
    env: Mapping[str, str]
    herdr: HerdrCli
    handoff: HandoffCli
    log: EventLog
    locate: Locator = default_locate
    look: Callable[[SessionRef, HarnessAdapter, HerdrCli], Look] = look  # one reading of a pane; the seam of tests without a Herdr
    clock: Callable[[], float] = time.time
    sleep: Callable[[float], None] = time.sleep
    harness_runner: HarnessRunner = headless_runner
    drain_attempt_s: float = 6.0  # one tick tries the drain order for this long at most
    drain_poll_s: float = 2.0
    fallback_timeout_s: float = 300.0  # the fallback writer blocks the tick for up to this long
    gh: tuple[str, ...] = ("gh",)  # the command the fallback writer lists pull requests with
    is_side_pane: Callable[[AgentPane], bool] = lambda pane: False  # the side pane module (plan 4) marks its own panes
    takeover_kwargs: Mapping[str, float] = field(default_factory=dict)
    runtime: dict[str, PaneRuntime] = field(default_factory=dict)
    _refs: dict[str, tuple[tuple[str | None, str | None], SessionRef | None, float]] = field(default_factory=dict)
    adapters: dict[Harness, HarnessAdapter] = field(default_factory=dict)  # prefilled by tests that need another home
    _tabs: set[str] = field(default_factory=set)
    log_offset: int = 0  # how far into events.jsonl `_events` reaches
    _events: list[HandoffEvent] = field(default_factory=list)

    def adapter(self, harness: Harness) -> HarnessAdapter:
        if harness not in self.adapters:
            self.adapters[harness] = adapter_for(harness)
        return self.adapters[harness]

    def ref_for(self, pane: AgentPane, harness: Harness, now: float) -> SessionRef | None:
        """The session's record, found once per (pane, agent, session) and kept; a failed lookup is retried later."""
        key = (pane.name, pane.session)
        cached = self._refs.get(pane.pane_id)
        if cached and cached[0] == key and (cached[1] is not None or now < cached[2]):
            return cached[1]
        ref = self.locate(self.adapter(harness), pane)
        self._refs[pane.pane_id] = (key, ref, now + _LOCATE_RETRY_S)
        return ref

    def sync_events(self) -> list[HandoffEvent]:
        """All events, read incrementally: only what was appended since the last tick is parsed."""
        new, offset = self.log.read_since(self.log_offset)
        if offset < self.log_offset:  # the file was replaced by a shorter one: start over
            self._events = []
        self._events.extend(new)
        self.log_offset = offset
        return self._events

    def manages(self, pane: AgentPane) -> bool:
        """An agent of a known harness that is neither the pane this controller runs in nor a side pane."""
        return (harness_for_agent_name(pane.name) is not None and pane.pane_id != self.env.get("HERDR_PANE_ID")
                and not self.is_side_pane(pane))

    def forget_gone(self, present: set[str]) -> None:
        for pane_id in [p for p in self._refs if p not in present]:
            del self._refs[pane_id]
        for pane_id in [p for p in self.runtime if p not in present]:
            del self.runtime[pane_id]


def _warn(message: str) -> None:
    print(f"herdr-eng handoff controller: {message}", file=sys.stderr, flush=True)


def _emit(ctl: Controller, ref: SessionRef, name: str, chain_id: str, *, reason: str | None = None,
          colour: Colour | None = None, pct: float | None = None, handoff_id: str | None = None,
          subagents_live: int | None = None) -> None:
    ctl.log.append(HandoffEvent(name, _iso(ctl.clock()), chain_id, ref.pane_id, ref.session_id, ref.harness,
                                handoff_id, None, None, colour, reason, pct, subagents_live=subagents_live))


def _skip_once(ctl: Controller, events: Sequence[HandoffEvent], pane: AgentPane, harness: Harness, reason: str,
               session_id: str | None = None) -> None:
    """`handoff.skipped` once per pane and reason in a row; the log is the memory, so a restart does not repeat it."""
    last = next((e for e in reversed(events) if e.event == "handoff.skipped"), None)
    if last is not None and last.reason == reason:
        return
    ctl.log.append(HandoffEvent("handoff.skipped", _iso(ctl.clock()), f"skipped:{pane.pane_id}", pane.pane_id,
                                session_id, harness, None, None, None, None, reason, None))


def _ensure_side_pane(ctl: Controller, tab_id: str | None) -> None:
    """The side pane (plan 4) is created on the first managed session of a tab, when that module exists."""
    if tab_id is None or tab_id in ctl._tabs:
        return
    ctl._tabs.add(tab_id)
    try:
        module = importlib.import_module("herdr_engineering.side_pane")
    except ImportError:
        return
    try:
        module.ensure(tab_id)
    except Exception as exc:  # noqa: BLE001 - a side pane problem must not stop the handoffs
        _warn(f"side pane for {tab_id}: {exc}")


def _restore(session_id: str | None, chain: Chain | None) -> PaneRuntime:
    """A runtime from the log alone: an order that was started may have been typed, so it is waited for, not retyped."""
    rt = PaneRuntime(session_id, chain.chain_id if chain else None)
    if chain is not None and chain.drain_started_at is not None:
        rt.typed, rt.wait_since = True, _epoch(chain.drain_started_at)
    if chain is not None:
        rt.wait_state = chain.waiting  # what was already said is not said again
    return rt


def _common_dir(worktree: Path) -> Path | None:
    done = run_group(["git", "-C", str(worktree), "rev-parse", "--path-format=absolute", "--git-common-dir"], 30.0)
    return Path(done.stdout.strip()) if done.returncode == 0 and done.stdout.strip() else None


def _has_changes(worktree: Path) -> bool | None:
    """Tracked changes or unpushed commits; None when git cannot say."""
    status = run_group(["git", "-C", str(worktree), "status", "--porcelain", "--untracked-files=no"], 60.0)
    unpushed = run_group(["git", "-C", str(worktree), "rev-list", "--abbrev-commit", "HEAD", "--not", "--remotes"], 60.0)
    if status.returncode != 0 or unpushed.returncode != 0:
        return None
    return bool(status.stdout.strip() or unpushed.stdout.strip())


def _stuck(ctl: Controller, ref: SessionRef, chain_id: str, why: str, pct: float | None) -> None:
    _emit(ctl, ref, "handoff.stuck", chain_id, reason=why, colour=Colour.RED, pct=pct)


def _checkpoint(ctl: Controller, ref: SessionRef, chain_id: str, worktree: Path, allowed: bool, pct: float | None) -> bool:
    """Commit the worktree's tracked changes; False when the chain ended stuck instead."""
    result: WipResult = wip_commit(worktree, push_allowed=allowed)
    if result.error:
        _stuck(ctl, ref, chain_id, stuck_reason(StuckReason.GIT_STATE_UNKNOWN, result.reason.value if result.reason else None,
                                                result.error), pct)
        return False
    if result.reason in (WipReason.ON_MAIN, WipReason.DETACHED):
        changes = _has_changes(worktree)
        if changes is None:
            _stuck(ctl, ref, chain_id, StuckReason.GIT_STATE_UNKNOWN.value, pct)
            return False
        if changes:
            _stuck(ctl, ref, chain_id, stuck_reason(StuckReason.ON_MAIN_WITH_CHANGES, result.reason.value, None), pct)
            return False
    if result.committed:
        _emit(ctl, ref, "handoff.wip_committed", chain_id, reason=f"sha={result.sha}", pct=pct)
    return True


def _finish(ctl: Controller, ref: SessionRef, chain: Chain, pct: float | None, *, last_resort: bool = False) -> None:
    handoff_id = chain.handoff_id
    if handoff_id is None:  # a takeover was started but the briefing event is missing: the newest record of this pane
        handoff_id = wait_for_briefing(ctl.store_root, _iso(chain.triggered_at), ref.pane_id, 0.0)
        if handoff_id is None:
            _stuck(ctl, ref, chain.chain_id, StuckReason.NO_BRIEFING.value, pct)
            return
    worktree = ref.cwd
    allowed = push_allowed(ctl.cfg, worktree, _common_dir(worktree) if ctl.cfg.push_allowed_repos else None)
    if not chain.takeover_started and not _checkpoint(ctl, ref, chain.chain_id, worktree, allowed, pct):
        return
    cfg = replace(ctl.cfg, max_attempts=0) if last_resort else ctl.cfg  # no attempts: straight to compaction
    run_takeover(ref, ctl.adapter(ref.harness), handoff_id, cfg, ctl.log, ctl.handoff, ctl.herdr,
                 store_root=ctl.store_root, worktree=worktree, push_allowed=allowed, env=ctl.env,
                 chain_id=chain.chain_id, context_pct=pct, sleep=ctl.sleep, **ctl.takeover_kwargs)


def _subagents(ctl: Controller, ref: SessionRef) -> list[dict[str, object]]:
    """The subagents the registry says still run in the pane; only Claude Code emits the hooks, so only it has any."""
    if ref.harness is not Harness.CLAUDE:
        return []
    try:
        return live_entries(ctl.env, ref.pane_id)
    except OSError:
        return []


def _wait(ctl: Controller, ref: SessionRef, chain: Chain, rt: PaneRuntime, reason: str, live: int, pct: float | None) -> None:
    """`handoff.waiting` when the wait state differs from the one last said, never otherwise."""
    if rt.wait_state != (reason, live):
        rt.wait_state = (reason, live)
        _emit(ctl, ref, "handoff.waiting", chain.chain_id, reason=reason, pct=pct, subagents_live=live)


def _finish_when_ready(ctl: Controller, ref: SessionRef, chain: Chain, rt: PaneRuntime, pct: float | None,
                       now: float) -> None:
    """Enter the takeover only when the source is idle (or done) with an empty composer, the right agent and no live
    subagent. Until `briefing_timeout_s` after the briefing it waits; after that the chain takes the last-resort path,
    whose compaction is typed only into a ready pane. A takeover already started, or a last-resort chain, is not held."""
    if not chain.takeover_started and not chain.last_resort:
        live = _subagents(ctl, ref)
        if live or not ctl.look(ref, ctl.adapter(ref.harness), ctl.herdr).ready:
            if now < (chain.briefing_at if chain.briefing_at is not None else chain.last_at) + ctl.cfg.briefing_timeout_s:
                _wait(ctl, ref, chain, rt, "subagents" if live else "working", len(live), pct)
                return
            chain = replace(chain, last_resort=True)
        rt.wait_state = None
    _finish_guarded(ctl, ref, chain, rt, pct)


def _write_fallback(ctl: Controller, ref: SessionRef, chain_id: str, pct: float | None, *,
                    last_resort: bool = False) -> str | None:
    """The fallback briefing (it may block the tick for `fallback_timeout_s`); None, with the stuck event, when none.
    The subagents still running are listed in it."""
    found = write_fallback_briefing(ref, ref.cwd, ctl.store_root, ctl.harness_runner, handoff=ctl.handoff,
                                    gh=ctl.gh, timeout_s=ctl.fallback_timeout_s, subagents=_subagents(ctl, ref))
    if found is None:
        _stuck(ctl, ref, chain_id, StuckReason.NO_BRIEFING.value, pct)
        return None
    _emit(ctl, ref, "handoff.briefing_fallback", chain_id, handoff_id=found, pct=pct,
          reason=OVER_CEILING if last_resort else None)  # a restart rebuilds the last resort from this
    return found


def _finish_guarded(ctl: Controller, ref: SessionRef, chain: Chain, rt: PaneRuntime, pct: float | None) -> None:
    """`_finish`, except that a third failure in a row ends the chain stuck (the exception type only: its message
    could carry anything); the earlier ones are raised for the loop to report."""
    try:
        _finish(ctl, ref, chain, pct, last_resort=chain.last_resort)
    except Exception as exc:
        rt.finish_failures += 1
        if rt.finish_failures < _MAX_FINISH_FAILURES:
            raise
        _stuck(ctl, ref, chain.chain_id, f"{StuckReason.INTERNAL_ERROR.value}:{type(exc).__name__}", pct)
    else:
        rt.finish_failures = 0


def _try_drain(ctl: Controller, pane: AgentPane, ref: SessionRef, chain_id: str, started: bool, rt: PaneRuntime,
               pct: float | None) -> str:
    """One bounded attempt to type the order; the outcome word. `handoff.drain_started` is appended right before the
    first real attempt, so a restart never retypes an order that may have gone out."""
    if pane.status not in READY_STATUSES:  # working or blocked: nothing to type into yet, and nothing to wait for here
        return "blocked" if pane.status == "blocked" else "timeout"
    if not started:
        _emit(ctl, ref, "handoff.drain_started", chain_id, pct=pct)
    gate = prepare_drain(ref.pane_id, ref.harness, ctl.env)
    attempt = replace(ctl.cfg, drain_timeout_s=ctl.drain_attempt_s)
    result: DrainResult = drain(ref, ctl.adapter(ref.harness), gate, ctl.herdr, attempt, poll_s=ctl.drain_poll_s,
                                sleep=ctl.sleep)
    now = ctl.clock()
    if result.outcome == "typed":
        rt.typed, rt.wait_since = True, now
    elif result.outcome == "prompt_uncertain":
        rt.cooldown_until = now + ctl.cfg.briefing_timeout_s
    return result.outcome


def _check_briefing(ctl: Controller, ref: SessionRef, chain: Chain, rt: PaneRuntime, pct: float | None) -> str | None:
    """The briefing's id when it exists now (the drain is over: events appended), else None."""
    if chain.drain_started_at is None:
        return None
    found = wait_for_briefing(ctl.store_root, chain.drain_started_at, ref.pane_id, 0.0)
    if found is None:
        return None
    _emit(ctl, ref, "handoff.drained", chain.chain_id, pct=pct)
    _emit(ctl, ref, "handoff.briefing_written", chain.chain_id, handoff_id=found, pct=pct)
    rt.typed = True
    return found


def _advance(ctl: Controller, pane: AgentPane, ref: SessionRef, rt: PaneRuntime, decision: Decision,
             pct: float | None, events: Sequence[HandoffEvent], now: float) -> None:
    chain = open_chain(events)
    if chain is None and gave_up(events, ctl.cfg):
        _skip_once(ctl, events, pane, ref.harness, "gave_up", ref.session_id)
        return
    if chain is not None and rt.typed and chain.handoff_id is None and not chain.takeover_started:
        _track_quiet(ctl, pane, ref, rt, now)
    step = next_step(chain, rt, decision, now, ctl.cfg, may_start(events, now, ctl.cfg))
    if step is Step.NONE:
        return
    if chain is None:
        chain_id, over = uuid.uuid4().hex, decision is Decision.OVER_CEILING
        _emit(ctl, ref, "handoff.triggered", chain_id, pct=pct, reason=OVER_CEILING if over else None)
        chain = Chain(chain_id, now, None, None, False, now)
        rt.reset(chain_id)  # nothing of an earlier chain carries over
    match step:
        case Step.DRAIN:
            outcome = _try_drain(ctl, pane, ref, chain.chain_id, chain.drain_started_at is not None, rt, pct)
            if outcome == "typed":
                return
            rt.last_outcome = outcome
            if decision is Decision.OVER_CEILING:  # a full context cannot wait for the order that cannot be typed
                found = _write_fallback(ctl, ref, chain.chain_id, pct, last_resort=True)
                if found is not None:
                    _finish_guarded(ctl, ref, replace(chain, handoff_id=found, last_resort=True), rt, pct)
        case Step.CHECK_BRIEFING:
            if (written := _check_briefing(ctl, ref, chain, rt, pct)) is not None:
                _finish_when_ready(ctl, ref, replace(chain, handoff_id=written, briefing_at=now), rt, pct, now)
            elif rt.typed:
                _say_wait(ctl, ref, chain, rt, pct)
        case Step.FALLBACK:
            if (written := _check_briefing(ctl, ref, chain, rt, pct)) is not None:
                _finish_when_ready(ctl, ref, replace(chain, handoff_id=written, briefing_at=now), rt, pct, now)
                return
            _emit(ctl, ref, "handoff.drained", chain.chain_id, pct=pct)
            found = _write_fallback(ctl, ref, chain.chain_id, pct)
            if found is not None:
                _finish_when_ready(ctl, ref, replace(chain, handoff_id=found, briefing_at=ctl.clock()), rt, pct, ctl.clock())
        case Step.FINISH:
            _finish_when_ready(ctl, ref, chain, rt, pct, now)
        case Step.DRAIN_STUCK:
            _stuck(ctl, ref, chain.chain_id, _drain_stuck_reason(pane, rt).value, pct)


def _track_quiet(ctl: Controller, pane: AgentPane, ref: SessionRef, rt: PaneRuntime, now: float) -> None:
    """After the order was typed the briefing timeout counts only the time the session is idle (or done) with no live
    subagent; anything else starts the count over."""
    rt.quiet_since = (rt.quiet_since if rt.quiet_since is not None else now) \
        if pane.status in READY_STATUSES and not _subagents(ctl, ref) else None


def _say_wait(ctl: Controller, ref: SessionRef, chain: Chain, rt: PaneRuntime, pct: float | None) -> None:
    """While the session works or subagents run: say so once per change (the quiet state says nothing)."""
    live = _subagents(ctl, ref)
    if rt.quiet_since is None:
        _wait(ctl, ref, chain, rt, "subagents" if live else "working", len(live), pct)
    else:
        rt.wait_state = None


def _drain_stuck_reason(pane: AgentPane, rt: PaneRuntime) -> StuckReason:
    """Why the order was never typed. What the pane's status says now (blocked on a dialog, or just busy) is read at the
    moment of timing out, so it holds after a restart; only the outcomes that need a failed attempt are remembered."""
    if rt.last_outcome in ("wrong_agent", "too_long", "prompt_uncertain"):
        return drain_not_typed_reason(rt.last_outcome)
    return StuckReason.BLOCKED if pane.status == "blocked" else StuckReason.DRAIN_TIMEOUT


def _tick_pane(ctl: Controller, pane: AgentPane, events: Sequence[HandoffEvent], now: float) -> None:
    if not ctl.manages(pane):
        return
    harness = harness_for_agent_name(pane.name)
    assert harness is not None
    _ensure_side_pane(ctl, pane.tab_id)
    mine = [e for e in events if e.pane_id == pane.pane_id]
    if not is_enabled(pane.pane_id, ctl.env, ctl.state_dir):
        _skip_once(ctl, mine, pane, harness, "switch_off")
        return
    ref = ctl.ref_for(pane, harness, now)
    if ref is None:
        _skip_once(ctl, mine, pane, harness, "no_session")
        return
    mine = pane_events(events, pane.pane_id, ref.session_id)
    if source_is_handed_off(mine):
        return
    reading = ctl.adapter(harness).read_context(ref)  # None: no usage yet (a missing window never gets here)
    chain = open_chain(mine)
    if reading is None and chain is None:
        _skip_once(ctl, mine, pane, harness, "no_record", ref.session_id)
        return
    rt = ctl.runtime.get(pane.pane_id)
    if rt is None or rt.session_id != ref.session_id or rt.chain_id != (chain.chain_id if chain else None):
        rt = ctl.runtime[pane.pane_id] = _restore(ref.session_id, chain)
    if reading is None:  # mid-chain: the record was rotated away; go on with the session found earlier
        assert chain is not None
        if now - chain.last_at >= ctl.cfg.drain_timeout_s:
            _stuck(ctl, ref, chain.chain_id, StuckReason.NO_RECORD.value, None)
            return
        _advance(ctl, pane, ref, rt, Decision.NONE, None, mine, now)
        return
    _advance(ctl, pane, ref, rt, decide(reading, ctl.cfg), reading.percent, mine, now)


def run_once(ctl: Controller, panes: Sequence[AgentPane] | None = None) -> None:
    """One tick over every agent Herdr reports (`panes` replaces that listing, for tests that run without Herdr).
    One pane's failure is reported on stderr and never stops the others."""
    panes = ctl.herdr.agents() if panes is None else panes
    if panes is None:
        return
    ctl.forget_gone({p.pane_id for p in panes})
    ctl._tabs &= {p.tab_id for p in panes if p.tab_id and ctl.manages(p)}  # a tab without managed panes is forgotten
    events = ctl.sync_events()
    for pane in panes:
        try:
            _tick_pane(ctl, pane, events, ctl.clock())
        except Exception as exc:  # noqa: BLE001 - see the docstring
            _warn(f"{pane.pane_id}: {type(exc).__name__}: {exc}")


# --- host: lock, loop, status ---------------------------------------------------------------------------------------

def lock_path(state_dir: Path) -> Path:
    return state_dir / "handoff-controller" / "controller.lock"


class ControllerLock:
    """An exclusive `flock` on the lock file, which holds the owner's pid. The kernel drops it when the owner dies."""

    def __init__(self, state_dir: Path) -> None:
        self._path = lock_path(state_dir)
        self._fd: int | None = None

    def acquire(self) -> bool:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        self._fd = fd
        return True

    def release(self) -> None:
        if self._fd is not None:
            os.close(self._fd)  # closing drops the flock
            self._fd = None


def lock_held(state_dir: Path) -> bool:
    probe = ControllerLock(state_dir)
    if probe.acquire():
        probe.release()
        return False
    return True


class MissingBinary(Exception):
    """A program the controller cannot run without is not installed."""


@dataclass(frozen=True)
class Binaries:
    herdr: Path
    handoff: Path
    git: Path
    gh: Path | None  # optional: without it the fallback briefing has no pull request list


def default_bin_dirs(env: Mapping[str, str]) -> list[Path]:
    """Where user programs usually live when a plugin's PATH is short: `HERDR_HANDOFF_BIN_DIRS` (`:`-separated, empty
    for none) when set, else `~/.local/bin`, `/opt/homebrew/bin`, `/usr/local/bin`."""
    configured = env.get("HERDR_HANDOFF_BIN_DIRS")
    if configured is not None:
        return [Path(part) for part in configured.split(":") if part]
    home = env.get("HOME") or str(Path.home())
    return [Path(home) / ".local" / "bin", Path("/opt/homebrew/bin"), Path("/usr/local/bin")]


def resolve_binaries(env: Mapping[str, str], extra_dirs: Sequence[Path] | None = None) -> Binaries:
    """Absolute paths of `herdr`, `herdr-handoff`, `git` (required) and `gh`: the process PATH first, then the usual
    user bin dirs. A missing required program raises MissingBinary naming all of them."""
    dirs = default_bin_dirs(env) if extra_dirs is None else list(extra_dirs)
    path = os.pathsep.join([env.get("PATH", os.defpath), *map(str, dirs)])

    def find(name: str) -> Path | None:
        found = shutil.which(name, path=path)
        return Path(os.path.abspath(found)) if found else None

    herdr, handoff, git = find("herdr"), find("herdr-handoff"), find("git")
    missing = [n for n, found in (("herdr", herdr), ("herdr-handoff", handoff), ("git", git)) if found is None]
    if missing or herdr is None or handoff is None or git is None:
        raise MissingBinary(f"required program not found on PATH or in {', '.join(map(str, dirs)) or 'no extra dirs'}: "
                            + ", ".join(missing))
    return Binaries(herdr, handoff, git, find("gh"))


def sweep_fallback_work(store_root: Path, now: float, max_age_s: float = _FALLBACK_WORK_MAX_AGE_S) -> int:
    """Remove `<store>/fallback-work/*` directories a killed controller left behind; how many were removed."""
    removed = 0
    try:
        entries = list((store_root / "fallback-work").iterdir())
    except OSError:
        return 0
    for entry in entries:
        try:
            if now - entry.stat().st_mtime >= max_age_s:
                shutil.rmtree(entry, ignore_errors=True)
                removed += 1
        except OSError:
            continue
    return removed


def build_controller(env: Mapping[str, str], *, herdr: HerdrCli | None = None, cfg: ControllerConfig | None = None,
                     bin_dirs: Sequence[Path] | None = None, require_binaries: bool = True) -> Controller:
    """The production controller: the shared store, and `herdr`, `herdr-handoff`, `git` and `gh` as resolved at start
    (MissingBinary when a required one is missing; `require_binaries=False` is for read-only commands)."""
    store = shared_store_root(env)
    base = Controller(cfg or config_from_env(env), store, state_dir_for(env), env, herdr or HerdrCli(), HandoffCli(),
                      EventLog(store))
    if not require_binaries:
        return base
    found = resolve_binaries(env, bin_dirs)
    use_tool("git", str(found.git))
    return replace(base, herdr=herdr or HerdrCli(str(found.herdr)), handoff=HandoffCli((str(found.handoff),)),
                   gh=(str(found.gh),) if found.gh else base.gh)


def serve(cfg: ControllerConfig | None = None, *, interval_s: float = 5.0, once: bool = False,
          env: Mapping[str, str] | None = None, controller: Controller | None = None,
          bin_dirs: Sequence[Path] | None = None) -> int:
    """Run ticks every `interval_s` until SIGTERM or SIGINT. A second controller exits 0 with a message; a missing
    required program is one line on stderr and exit code 2 (also with `once`)."""
    env = os.environ if env is None else env
    try:
        ctl = controller or build_controller(env, cfg=cfg, bin_dirs=bin_dirs)
    except MissingBinary as exc:
        print(f"herdr-eng handoff controller: {exc}", file=sys.stderr)
        return 2
    lock = ControllerLock(ctl.state_dir)
    if not lock.acquire():
        print(f"another handoff controller is running (lock {lock_path(ctl.state_dir)}); exiting")
        return 0
    stop = threading.Event()
    previous = {}
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGTERM, signal.SIGINT):
            previous[sig] = signal.signal(sig, lambda *_: stop.set())
    try:
        sweep_fallback_work(ctl.store_root, time.time())
        while not stop.is_set():
            run_once(ctl)
            if once:
                break
            stop.wait(interval_s)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        lock.release()
    return 0


def status_lines(state_dir: Path, log: EventLog) -> list[str]:
    """Whether the controller runs, then the last event of every chain still in progress."""
    lines = ["controller: " + ("running" if lock_held(state_dir) else "not running")]
    by_chain: dict[str, list[HandoffEvent]] = {}
    for e in log.read():
        if e.event != "handoff.skipped":
            by_chain.setdefault(e.chain_id, []).append(e)
    for chain_id, mine in by_chain.items():
        if not any(e.event in _TERMINAL for e in mine):
            last = mine[-1]
            lines.append(f"{chain_id} pane {last.pane_id} {last.harness.value}: {last.event} at {last.at}"
                         + (f" ({last.reason})" if last.reason else ""))
    return lines

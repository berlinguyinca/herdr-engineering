"""One side pane per Herdr tab, on the right, without moving focus or squeezing the session.

`ensure` is idempotent and takes the tab's lock, so two callers never both create the pane. A pane the user closed is
not reopened until a new managed session appears in the tab (the dismissal lives in `state`, beside the marker);
`force` (the `side-pane ensure` command) reopens it. A pane is recognised by the marker its runner registers, not by
its title.

Focus: the pane is split with `--no-focus`. Herdr has no focus-by-id, so afterwards `settle_focus` acts only when a fresh
read shows the NEW pane holds the focus, and then makes at most one directional call, only when the new pane's left
neighbour is the pane that had the focus. It never switches tabs and never walks through other panes; when it cannot
restore the focus it says so with a `FocusOutcome`.
"""
from __future__ import annotations

import os
import shlex
import shutil
import sys
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from ..handoff_controller.herdr_cli import AgentPane, harness_for_agent_name
from . import state
from .herdr import Layout, PaneRow, SideHerdr

SIDE_WIDTH = 40  # columns the side pane gets
MIN_SESSION_WIDTH = 60  # columns the session keeps at least; a narrower tab gets no side pane
LOCK_TIMEOUT_S = 10.0
CLOSE_WAIT_S = 5.0
DEFAULT_COMMAND = "exec herdr-eng side-pane run"
CLEANUP_RESERVE_S = 1.0  # of a call's budget, kept for recording or removing what the work left
CARRIED_ENV = ("HERDR_ENGINEERING_STATE", "HERDR_HANDOFF_STORE", "HOME")  # what decides the state dir and the store of the runner


class Outcome(StrEnum):
    CREATED = "created"
    EXISTS = "exists"
    DISMISSED = "dismissed"  # the user closed it and no new session has appeared since
    TOO_NARROW = "too_narrow"
    NO_SESSION = "no_session"  # no managed session in the tab
    UNAVAILABLE = "unavailable"  # Herdr did not answer or refused
    LOCKED = "locked"  # another process is creating the pane
    RUNNER_MISSING = "runner_missing"  # `herdr-eng` cannot be found, so no pane was created


class FocusOutcome(StrEnum):
    """What happened to the focus when a pane was created."""
    UNCHANGED = "unchanged"  # the new pane did not take the focus
    RESTORED = "restored"  # it took the focus and one directional call gave it back
    LEFT_ON_SIDE_PANE = "left_on_side_pane"  # it took the focus and Herdr cannot give it back by id: left as it is
    UNKNOWN = "unknown"  # Herdr did not answer, or the call did not give the focus back


@dataclass(frozen=True)
class EnsureResult:
    outcome: Outcome
    pane_id: str | None = None
    focus: FocusOutcome | None = None  # set when a pane was created


def pick_target(layout: Layout) -> str | None:
    """The pane to split: the rightmost one, the topmost of those."""
    best = max(layout.rects.items(), key=lambda kv: (kv[1].x + kv[1].width, -kv[1].y), default=None)
    return best[0] if best else None


def split_ratio(width: int, side_width: int = SIDE_WIDTH, min_session: int = MIN_SESSION_WIDTH) -> float | None:
    """The share of a `width`-column pane that stays with the session, or None when it would keep fewer than `min_session`."""
    if width < side_width + min_session:
        return None
    return (width - side_width) / width


def is_dismissed(dismissed: frozenset[str], session_terminals: Sequence[str]) -> bool:
    """True when the pane was closed while every session now in the tab was already there."""
    return bool(dismissed) and bool(session_terminals) and all(t in dismissed for t in session_terminals)


def is_side_pane(pane: AgentPane, env: Mapping[str, str] | None = None) -> bool:
    """Whether `pane` is a live side pane: the marker of its tab names it and its runner is alive."""
    return state.is_marked(state.state_dir(os.environ if env is None else env), pane, time.time())


@dataclass(frozen=True)
class Decision:
    """What `ensure` does, decided from facts only: `outcome` is None when a pane is to be created."""
    outcome: Outcome | None
    pane_id: str | None = None  # EXISTS: the live side pane
    stale_marker: bool = False  # remove the tab's marker
    dismiss: tuple[str, ...] = ()  # remember the user closed the pane, with these session terminals
    clear_dismissed: bool = False
    target: str | None = None  # CREATED: split this pane ...
    ratio: float | None = None  # ... leaving it this share
    owner: str | None = None  # terminal of the target, recorded in the marker
    close_pane: str | None = None  # the marker's own pane row, which no live runner holds: close it first


def decide(*, rows: Sequence[PaneRow], sessions: Sequence[PaneRow], marker: state.Marker | None, dismissed: frozenset[str],
           layout: Layout | None, target_pane: str | None, now: float, force: bool,
           shown: frozenset[str] = frozenset(), grace_s: float = state.STARTUP_GRACE_S) -> Decision:
    """`rows` are the tab's panes and `sessions` those that run a managed agent. A marker that is not live is stale.
    Its pane row, when it is still there and the marker proves `ensure` made it (same terminal id), is closed before a new
    one is made. When there is no pane of ours but there was one for these sessions (`shown`), or its runner started and
    then died, the user ended it (dismissal); otherwise Herdr restarted. A dismissal holds while every session in the tab
    was there when it was made; `force` ignores it."""
    stale, remembered, close = False, (), None
    row = None
    if marker is not None:
        row = next((r for r in rows if r.pane_id == marker.pane_id and r.terminal_id == marker.terminal_id), None)
        if row is not None and state.marker_live(marker, now, grace_s):
            return Decision(Outcome.EXISTS, row.pane_id)
        stale = True
        close = row.pane_id if row is not None else None
    if (row is None or marker is None or marker.pid is not None) and shown:
        remembered = tuple(sorted(shown))
    if not sessions:
        return Decision(Outcome.NO_SESSION, stale_marker=stale, close_pane=close)
    terminals = [s.terminal_id for s in sessions]
    if not force and is_dismissed(dismissed | frozenset(remembered), terminals):
        return Decision(Outcome.DISMISSED, stale_marker=stale, dismiss=remembered, close_pane=close)
    if close is not None:  # the layout still holds the pane that is about to go: decide again once it is closed
        return Decision(None, stale_marker=stale, dismiss=remembered, clear_dismissed=True, close_pane=close)
    target = target_pane or (pick_target(layout) if layout else None)
    rect = layout.rects.get(target) if layout and target else None
    keep = dict(stale_marker=stale, dismiss=remembered, clear_dismissed=True, close_pane=close)
    if rect is None:
        return Decision(Outcome.UNAVAILABLE, **keep)
    ratio = split_ratio(rect.width)
    if ratio is None:
        return Decision(Outcome.TOO_NARROW, **keep)
    owner = next((r.terminal_id for r in rows if r.pane_id == target), None)
    return Decision(None, target=target, ratio=ratio, owner=owner, **keep)


def _bin_dirs(env: Mapping[str, str]) -> list[Path]:
    """Where user programs usually live (the controller's rule, restated so this package does not import the controller loop)."""
    configured = env.get("HERDR_HANDOFF_BIN_DIRS")
    if configured is not None:
        return [Path(part) for part in configured.split(":") if part]
    home = env.get("HOME") or str(Path.home())
    return [Path(home) / ".local" / "bin", Path("/opt/homebrew/bin"), Path("/usr/local/bin")]


def runner_command(command: str, env: Mapping[str, str]) -> str | None:
    """The command to type into the new pane. The default one is made absolute (the pane's shell may have a short PATH):
    `herdr-eng` is looked for on PATH, then in the controller's usual bin dirs; None when it is nowhere. The variables that
    decide the runner's state dir and store are passed explicitly (the pane's shell has Herdr's environment, not ours), so
    the marker lands where `ensure` and the controller look. A command given by hand is used as it is."""
    if command != DEFAULT_COMMAND:
        return command
    search = os.pathsep.join([env["PATH"] if "PATH" in env else os.environ.get("PATH", ""), *map(str, _bin_dirs(env))])
    found = shutil.which("herdr-eng", path=search)
    if not found:
        return None
    carried = [f"{name}={env[name]}" for name in CARRIED_ENV if env.get(name)]
    return f"exec {shlex.join(['env', *carried]) + ' ' if carried else ''}{shlex.quote(found)} side-pane run"


def settle_focus(herdr: SideHerdr, created: str, previous: str | None) -> FocusOutcome:
    """After a split with `--no-focus`: act only if the new pane holds the focus. Herdr gives the focus back by direction
    only, so with exactly one call, and only when the new pane's left neighbour is `previous` (the pane that had it).
    Nothing walks through other panes and no tab is switched; otherwise the outcome says where the focus was left."""
    row = herdr.pane(created)
    if row is None:
        return FocusOutcome.UNKNOWN
    if not row.focused:
        return FocusOutcome.UNCHANGED
    if previous is None or herdr.neighbor(created, "left") != previous:
        return FocusOutcome.LEFT_ON_SIDE_PANE
    landed = herdr.focus_toward(created, "left")
    back = herdr.pane(previous)
    return FocusOutcome.RESTORED if landed == previous and back is not None and back.focused else FocusOutcome.UNKNOWN


def _note(text: str) -> None:
    print(f"herdr-eng side-pane: {text}", file=sys.stderr)


@contextmanager
def _within(herdr: SideHerdr, budget_s: float | None) -> Iterator[None]:
    """Give every Herdr call in the block what is left of `budget_s` (None: no limit), keeping `CLEANUP_RESERVE_S` for cleanup."""
    if budget_s is None:
        yield
        return
    end = time.monotonic() + max(budget_s, 0.0)
    herdr.hard_deadline = end
    herdr.deadline = end - min(CLEANUP_RESERVE_S, budget_s / 4)
    try:
        yield
    finally:
        herdr.deadline = herdr.hard_deadline = None


def ensure(tab_id: str, target_pane: str | None = None, herdr: SideHerdr | None = None,
           env: Mapping[str, str] | None = None, *, command: str = DEFAULT_COMMAND, force: bool = False,
           lock_timeout_s: float = LOCK_TIMEOUT_S, start_grace_s: float = state.STARTUP_GRACE_S,
           budget_s: float | None = None) -> EnsureResult:
    """Open the side pane of `tab_id` unless it has one, the user closed it, or the tab is too narrow. Never raises.
    `lock_timeout_s=0` never waits for another process that is creating the pane (LOCKED). With `budget_s` the whole call
    stays within that many seconds: every Herdr call gets what is left, and an exhausted budget is UNAVAILABLE."""
    herdr = herdr or SideHerdr()
    env = os.environ if env is None else env
    root = state.state_dir(env)
    with _within(herdr, budget_s), state.tab_lock(root, tab_id, lock_timeout_s) as held:
        if not held:
            return EnsureResult(Outcome.LOCKED)
        return _ensure_locked(root, tab_id, target_pane, herdr, command, force, env, start_grace_s)


def _sessions(herdr: SideHerdr, tab_id: str, rows: Sequence[PaneRow]) -> list[PaneRow] | None:
    agents = herdr.agents()
    if agents is None:
        return None
    named = {a.pane_id for a in agents if a.tab_id == tab_id and harness_for_agent_name(a.name) is not None}
    return [r for r in rows if r.pane_id in named]


def _new_pane(herdr: SideHerdr, tab_id: str, before: Sequence[PaneRow] | None) -> PaneRow | None:
    """The one pane that appeared in `tab_id` since `before` (a split whose reply could not be read), else None."""
    now = herdr.panes()
    if before is None or now is None:
        return None
    known = {r.pane_id for r in before}
    new = [r for r in now if r.tab_id == tab_id and r.pane_id not in known]
    return new[0] if len(new) == 1 else None


def _wait_gone(herdr: SideHerdr, pane_id: str, anchor: str, timeout_s: float = CLOSE_WAIT_S) -> None:
    """Closing is asynchronous in Herdr: wait until the pane is in neither the pane list nor the layout of `anchor`'s tab,
    so the next layout read is the new one."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        rows, layout = herdr.panes(), herdr.layout(anchor)
        if rows is None or layout is None or (all(r.pane_id != pane_id for r in rows) and pane_id not in layout.rects):
            return
        time.sleep(0.05)


def _ensure_locked(root: Path, tab_id: str, target_pane: str | None, herdr: SideHerdr, command: str, force: bool,
                   env: Mapping[str, str], grace_s: float) -> EnsureResult:
    now = time.time()
    rows = herdr.panes()
    in_tab = [r for r in rows if r.tab_id == tab_id] if rows is not None else []
    sessions = _sessions(herdr, tab_id, in_tab) if rows is not None else None
    if sessions is None or rows is None:
        return EnsureResult(Outcome.UNAVAILABLE)
    if rows:
        state.prune(root, {r.tab_id for r in rows} | {tab_id})  # tabs Herdr no longer lists
    layout = herdr.layout(target_pane or sessions[0].pane_id) if sessions else None
    plan = decide(rows=in_tab, sessions=sessions, marker=state.read_marker(root, tab_id), dismissed=state.read_dismissed(root, tab_id),
                  layout=layout, target_pane=target_pane, now=now, force=force, shown=state.read_shown(root, tab_id),
                  grace_s=grace_s)
    runner = runner_command(command, env) if plan.outcome is None else command
    if runner is None:
        return EnsureResult(Outcome.RUNNER_MISSING)  # nothing is created, closed or recorded
    if plan.stale_marker:
        state.clear_marker(root, tab_id)  # first: the closed runner must not read its own marker and record a dismissal
    if plan.dismiss:
        state.dismiss(root, tab_id, list(plan.dismiss))
    if plan.close_pane:
        if not herdr.close(plan.close_pane):
            return EnsureResult(Outcome.UNAVAILABLE)
        _wait_gone(herdr, plan.close_pane, sessions[0].pane_id if sessions else plan.close_pane)
        if plan.outcome is None:  # a pane that never started was in the way: look again, and make exactly one
            return _ensure_locked(root, tab_id, target_pane, herdr, command, True, env, grace_s)
    if plan.clear_dismissed:
        state.clear_dismissed(root, tab_id)
    if plan.outcome is not None or plan.target is None or plan.ratio is None:
        return EnsureResult(plan.outcome or Outcome.UNAVAILABLE, plan.pane_id)
    before = herdr.panes()
    previous = next((r.pane_id for r in before or [] if r.focused), None)
    created = herdr.split(plan.target, plan.ratio, env={})
    herdr.release_reserve()  # a split that happened must be recorded, whatever time is left
    if created is None:
        created = _new_pane(herdr, tab_id, before)
        if created is None:
            _note(f"split in {tab_id} gave no readable reply and no new pane was found; nothing was closed or recorded")
            return EnsureResult(Outcome.UNAVAILABLE)
    state.write_marker(root, state.Marker(tab_id, created.pane_id, created.terminal_id, plan.owner, None, time.time()))
    state.write_shown(root, tab_id, [s.terminal_id for s in sessions])
    if not herdr.run_in(created.pane_id, runner):
        herdr.close(created.pane_id)
        state.clear_marker(root, tab_id)
        return EnsureResult(Outcome.UNAVAILABLE)
    return EnsureResult(Outcome.CREATED, created.pane_id, settle_focus(herdr, created.pane_id, previous))

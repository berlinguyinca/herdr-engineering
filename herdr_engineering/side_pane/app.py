"""`herdr-eng side-pane run`: a read-only viewer of the focused session of its own Herdr tab.

Three tabs (Summary, Handoffs, CI); keys 1/2/3 or Tab switch, q quits. However the pane ends (q, a signal, a crash, the
user closing it in Herdr) it counts as closed by the user for the sessions in the tab, unless `side-pane ensure`
replaced it. Everything slow (Herdr, the event log, CI) happens on background threads that publish an immutable
`Snapshot`; the render path only reads the latest one, so a slow Herdr, Woodpecker or GitHub never freezes the keys.

Data sources: the controller's event log and shared store; the Herdr CLI; pull requests from `gh pr list` run with the
user's own `gh` login, for the focused session's directory and branch only (a chain spanning repositories shows the
current repository's PRs); pipelines from Woodpecker's public reads. No Woodpecker token is ever used.
"""
from __future__ import annotations

import argparse
import os
import re
import signal
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path

from ..agent_ops_tui import RenderOpts
from ..handoff_adapters import adapter_for as default_adapter_for
from ..handoff_adapters.base import Harness, HarnessAdapter, SessionRef
from ..handoff_controller.herdr_cli import AgentPane, harness_for_agent_name
from ..handoff_controller.store import shared_store_root
from ..pane_viewer import run_viewer
from . import state
from .ci_fetch import CiSchedule, CiWorker, fetch_cards
from .ci_tab import CiCard
from .follow import EventFollower, choose_session
from .herdr import PaneRow, SideHerdr
from .model import EMPTY, SidePaneModel, build_model
from .render import Tab, render
from .schedule import CI_REFRESH_S, EVENTS_REFRESH_S, Due
from .style import Style, clean, line

QUIT_IGNORED_S = 2.0  # q and ctrl-c do nothing this long after the viewer starts: a pane that got the focus by accident survives a stray key
REDRAW_S = 1.0  # the viewer redraws this often so durations and "ago" keep moving between refreshes
_KEYS = {"1": Tab.SUMMARY, "2": Tab.HANDOFFS, "3": Tab.CI}
_ESCAPES = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1bO.|\x1b")
_NO_PANE = "side-pane run: HERDR_PANE_ID is not set; run it in a Herdr pane, or pass --tab TAB_ID or --pane PANE_ID"
_OUT = "herdr is not answering"


@dataclass(frozen=True)
class Snapshot:
    model: SidePaneModel
    session: SessionRef | None
    note: str


class Screen:
    """The tab shown and the latest snapshot. Pure: `key` and `render` do no I/O, so they are plain unit tests."""

    def __init__(self, opts: RenderOpts | None = None) -> None:
        self.opts = opts or RenderOpts()
        self.tab = Tab.SUMMARY
        self.snapshot = Snapshot(EMPTY, None, "reading Herdr...")

    def publish(self, snapshot: Snapshot) -> None:
        self.snapshot = snapshot

    def key(self, text: str) -> bool:
        """Apply typed keys in order (1/2/3, Tab); anything else, escape sequences included, does nothing. True if one applied."""
        handled = False
        order = list(Tab)
        for char in _ESCAPES.sub("", text):
            if char in _KEYS:
                self.tab, handled = _KEYS[char], True
            elif char == "\t":
                self.tab, handled = order[(order.index(self.tab) + 1) % len(order)], True
        return handled

    def render(self, width: int, height: int, now: float) -> list[str]:
        width, height = max(width, 1), max(height, 1)
        snap = self.snapshot
        note = clean(snap.note)
        room = height - 1 if note and height > 2 else height
        try:
            lines = render(snap.model, self.tab, width, room, now, self.opts)
        except Exception as error:  # one tab's build failing is one line in that tab; the others keep working
            bar = render(EMPTY, self.tab, width, 1, now, self.opts)
            lines = [*bar, line([(f"error: {type(error).__name__}: {error}", Style.RED)], width, self.opts)][:room]
        if room < height:
            lines = [*lines, line([(note, Style.YELLOW)], width, self.opts)]
        return lines[:height]


def _with_gauge(model: SidePaneModel, ref: SessionRef, percent: float) -> SidePaneModel:
    """The reading belongs to the row of the focused session `ref`, not to whichever row is live."""
    found = [i for i, row in enumerate(model.rows) if row.pane == ref.pane_id and row.session_id == ref.session_id]
    if not found:
        return model
    rows = list(model.rows)
    rows[found[-1]] = replace(rows[found[-1]], context_pct=percent)
    return replace(model, rows=tuple(rows))


class PaneApp:
    """Follows the focused session of one tab and publishes what the screen shows. Nothing here raises out of `refresh`."""

    def __init__(self, tab_id: str | None, pane_id: str | None, herdr: SideHerdr, env: Mapping[str, str], *,
                 fetch_ci: Callable[[Path], tuple[CiCard, ...]] | None = fetch_cards,
                 adapter_for: Callable[[Harness], HarnessAdapter] = default_adapter_for,
                 store_root: Path | None = None, ci_interval_s: float = CI_REFRESH_S) -> None:
        self.tab_id, self.pane_id = tab_id, pane_id
        self.screen = Screen()
        self.quit_requested = False  # the viewer quit on a q, ctrl-c or Q (see `on_quit`)
        self._shown: frozenset[str] = frozenset()
        self._herdr, self._adapter_for = herdr, adapter_for
        self._store = store_root if store_root is not None else shared_store_root(env)
        self._state = state.state_dir(env)
        self._follower = EventFollower(self._store)
        self._events_due = Due(EVENTS_REFRESH_S)
        self._ci = CiWorker(fetch_ci) if fetch_ci is not None else None
        self._ci_schedule = CiSchedule(self._ci, ci_interval_s) if self._ci is not None else None
        self._previous: AgentPane | None = None
        self._registered = False
        self._built: tuple[object, SidePaneModel] | None = None

    @property
    def snapshot(self) -> Snapshot:
        return self.screen.snapshot

    def tick(self, mono: float, wall: float) -> bool:
        """Refresh when the events schedule says so; True when it did."""
        if not self._events_due.take(mono):
            return False
        self.refresh(wall)
        return True

    def refresh(self, now: float) -> None:
        try:
            snapshot = self._build(now)
        except Exception as error:  # keep what was shown and say what failed
            snapshot = replace(self.screen.snapshot, note=f"error: {type(error).__name__}")
        self.screen.publish(snapshot)

    def _resolve_tab(self) -> str | None:
        if self.tab_id is None and self.pane_id is not None:
            self.tab_id = self._herdr.tab_of(self.pane_id)
        return self.tab_id

    def _focused_pane(self, tab: str, rows: list[PaneRow]) -> str | None:
        anchor = self.pane_id
        if anchor is None:
            anchor = next((r.pane_id for r in rows if r.tab_id == tab), None)
        layout = self._herdr.layout(anchor) if anchor else None
        return layout.focused_pane_id if layout else None

    def _register(self, tab: str, now: float) -> None:
        """Write this pane's marker (with the pid) so `is_side_pane` recognises it; `ensure` may have written one already."""
        row = self._herdr.pane(self.pane_id) if self.pane_id else None
        if row is None:
            return
        before = state.read_marker(self._state, tab)
        kept = before if before is not None and before.pane_id == row.pane_id and before.terminal_id == row.terminal_id else None
        state.write_marker(self._state, state.Marker(tab, row.pane_id, row.terminal_id, kept.session_terminal if kept else None,
                                                     os.getpid(), kept.created if kept else now, state.process_start(os.getpid())))
        self._registered = True

    def _session_ref(self, pane: AgentPane) -> SessionRef | None:
        harness = harness_for_agent_name(pane.name)
        if harness is None:
            return None
        cwd = pane.cwd or Path(".")
        try:
            found = self._adapter_for(harness).locate_session(pane.session, cwd, pane.pane_id)
        except Exception:  # an adapter that cannot read its records still leaves the pane's own facts
            found = None
        return found or SessionRef(harness, pane.pane_id, cwd, pane.session, None)

    def _gauge(self, ref: SessionRef) -> float | None:
        try:
            reading = self._adapter_for(ref.harness).read_context(ref)
        except Exception:
            return None
        return reading.percent if reading is not None else None

    def _ci_cards(self, ref: SessionRef) -> tuple[CiCard, ...]:
        if self._ci is None or self._ci_schedule is None:
            return ()
        self._ci_schedule.maybe_request(ref.cwd, time.monotonic())
        result = self._ci.latest()
        return result.cards if result is not None and result.cwd == ref.cwd else ()

    def _outbox_stamp(self) -> int | None:
        try:
            return (self._store / "outbox").stat().st_mtime_ns
        except OSError:
            return None

    def _build(self, now: float) -> Snapshot:
        before = self.screen.snapshot
        tab = self._resolve_tab()
        if tab is None:
            return replace(before, note=_OUT)
        if not self._registered:
            self._register(tab, now)
        agents = self._herdr.agents()
        rows = self._herdr.panes() or []
        terminals = {r.pane_id: r.terminal_id for r in rows}
        self._remember_shown(tab, agents, terminals)
        chosen = choose_session(self._focused_pane(tab, rows), agents, tab, self._previous,
                                lambda pane: state.is_marked(self._state, pane, now, terminals.get(pane.pane_id)))
        self._previous = chosen
        if chosen is None:
            self._built = None
            return Snapshot(EMPTY, None, _OUT if agents is None else "no session in this tab")
        ref = self._session_ref(chosen)
        if ref is None:
            return Snapshot(EMPTY, None, "no session in this tab")
        events, _ = self._follower.poll()
        cards = self._ci_cards(ref)
        key = (ref.pane_id, ref.session_id, self._follower.offset, self._outbox_stamp(), cards)
        if self._built is None or self._built[0] != key:
            self._built = (key, build_model(ref, self._store, {}, now, cards, events))
        model = self._built[1]
        percent = self._gauge(ref)
        return Snapshot(_with_gauge(model, ref, percent) if percent is not None else model, ref, _OUT if agents is None else "")

    def _managed_terminals(self, tab: str, agents: list[AgentPane] | None, terminals: Mapping[str, str]) -> frozenset[str]:
        """Terminal ids of the managed sessions of `tab` (this pane excluded)."""
        return frozenset(terminals[a.pane_id] for a in agents or [] if a.tab_id == tab and a.pane_id != self.pane_id
                         and a.pane_id in terminals and harness_for_agent_name(a.name) is not None)

    def _remember_shown(self, tab: str, agents: list[AgentPane] | None, terminals: Mapping[str, str]) -> None:
        """Keep the record of the sessions this pane is shown for, so a pane that vanishes without a trace still counts as
        dismissed for them."""
        shown = self._managed_terminals(tab, agents, terminals)
        if shown and shown != self._shown:
            self._shown = shown
            state.write_shown(self._state, tab, sorted(shown))

    def on_text(self, text: str) -> bool:
        """The viewer's key hook: the tab keys. Quitting is the viewer's decision (`on_quit`), not any chunk with a q in it."""
        return self.screen.key(text)

    def on_quit(self) -> None:
        """Called by the viewer as it exits on a quit key."""
        self.quit_requested = True

    def close(self) -> None:
        """However the pane ends: while the marker is still this pane's (`ensure` clears it before replacing a pane), the
        user ended it, so remember the sessions in the tab and `ensure` does not reopen the pane for them. Nothing is
        written when no session is known (Herdr not answering): the earlier record stays."""
        tab = self.tab_id
        if tab is None or not self.pane_id:
            return
        marker = state.read_marker(self._state, tab)
        if marker is None or marker.pane_id != self.pane_id:
            return
        rows = self._herdr.panes()
        agents = self._herdr.agents()
        known = self._managed_terminals(tab, agents, {r.pane_id: r.terminal_id for r in rows or []}) if agents is not None else frozenset()
        state.dismiss(self._state, tab, sorted(known or self._shown))
        state.clear_marker(self._state, tab, self.pane_id)


def add_run_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--tab", default=None, help="follow this Herdr tab (default: the tab of $HERDR_PANE_ID)")
    parser.add_argument("--pane", default=None, help="the pane this viewer runs in (default: $HERDR_PANE_ID)")
    parser.add_argument("--no-ci", action="store_true", help="do not read CI (the CI tab stays empty)")
    parser.add_argument("--snapshot", action="store_true",
                        help="render one frame to stdout and exit 0, without Herdr, CI or a terminal (needs --tab or --pane)")
    parser.add_argument("--tab-view", choices=[t.value for t in Tab], default="summary",
                        help="with --snapshot: the tab to render (default summary)")
    parser.add_argument("--width", type=int, default=60, help="with --snapshot: the frame width in columns (default 60)")
    parser.add_argument("--session-pane", default=None, metavar="PANE_ID",
                        help="with --snapshot: show the session of this pane id, read from the store (needs --session-id)")
    parser.add_argument("--session-id", default=None, help="with --snapshot: the session id of --session-pane")


def _snapshot(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    """One frame of the chosen tab, read from the store only: a smoke test and a way to see the pane without a terminal."""
    ref = (SessionRef(Harness.CLAUDE, args.session_pane, Path.cwd(), args.session_id, None)
           if args.session_pane and args.session_id else None)
    now = time.time()
    screen = Screen(RenderOpts(color=False))
    screen.tab = Tab(args.tab_view)
    screen.publish(Snapshot(build_model(ref, shared_store_root(env), env, now), ref, "" if ref else "no session in this tab"))
    print("\n".join(screen.render(max(args.width, 1), 200, now)))
    return 0


def run_with(args: argparse.Namespace, env: Mapping[str, str] | None = None) -> int:
    env = dict(os.environ) if env is None else dict(env)
    pane = args.pane or env.get("HERDR_PANE_ID") or None
    if not pane and not args.tab:
        print(_NO_PANE, file=sys.stderr)
        return 2
    if args.snapshot:
        return _snapshot(args, env)
    pane_app = PaneApp(args.tab, pane, SideHerdr(env=env), env, fetch_ci=None if args.no_ci else fetch_cards)
    stop = threading.Event()

    def poll() -> None:
        while not stop.is_set():
            pane_app.tick(time.monotonic(), time.time())
            stop.wait(0.25)

    threading.Thread(target=poll, daemon=True, name="side-pane-poll").start()
    previous: dict[int, object] = {}
    for sig in (signal.SIGTERM, signal.SIGHUP):  # closing the pane still records the dismissal and removes the marker
        try:
            previous[sig] = signal.signal(sig, lambda *_: sys.exit(0))
        except ValueError:  # not the main thread
            pass
    try:
        return run_viewer(lambda w, h: pane_app.screen.render(w, h, time.time()), scroll=False, refresh_s=REDRAW_S,
                          on_text=pane_app.on_text, quit_ignored_s=QUIT_IGNORED_S, lone_quit=True, on_quit=pane_app.on_quit)
    finally:
        stop.set()
        pane_app.close()
        for sig, handler in previous.items():
            signal.signal(sig, handler)  # type: ignore[arg-type]


def run(argv: list[str], env: Mapping[str, str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="side-pane run")
    add_run_arguments(parser)
    try:
        args = parser.parse_args(argv)
    except SystemExit as exit_:
        return int(exit_.code) if isinstance(exit_.code, int) else 2
    return run_with(args, env)

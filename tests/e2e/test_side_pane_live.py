"""The session side pane against a real Herdr and a real Claude session, after a real automatic handoff.

Opt in with ``HERDR_HANDOFF_LIVE_E2E=1 pytest tests/e2e/test_side_pane_live.py -m herdr_live -v``. Everything is skipped,
with the reason, unless ALL of these hold: ``herdr`` answers, ``herdr-handoff --version`` is 0.5.0 or newer, the
``claude`` CLI is installed, and ``HERDR_HANDOFF_LIVE_E2E=1``.

What it touches, and what it costs. Each test opens its own NEW Herdr tab (never splits or types into an existing pane)
with its working directory in a throwaway git repository under ``.herdr-test/side-pane-live/`` (a real repository with a
real bare remote). It starts a real interactive Claude session there and submits two short prompts, then lets the
handoff controller (this process, one ``run_once`` per tick, restricted to that one pane) type the drain order and hand
the session to a successor: this SPENDS MODEL TOKENS, twice over (a session and its successor per test). Afterwards
it opens the side pane in that tab with ``side_pane.placement.ensure`` (the controller does not place it yet) running
this checkout's viewer against the throwaway store and state directories, and reads it with ``herdr pane read``. The
store is ``.herdr-test/side-pane-live/<test>/store`` and the state directory ``.../state``, so the user's real handoff
store, dismissals and markers are untouched. Every tab, and with it every pane the test opened, is closed in
``finally``; a tab that will not close fails the test. Claude asks to trust a folder it has not seen: the test never
confirms that dialog, it skips; open Claude once in the repository it names (or trust ``.herdr-test``) and run again.

The limit is lowered instead of the transcript being faked, exactly as in ``test_handoff_controller_live.py``: the
session and the controller configuration are real, ``drain_limit_pct`` is one point under what the session measures.

Two chains are run. The green one is on a feature branch and ends ``handoff.succeeded``. The red one is on ``main`` with
a tracked file changed, which the controller refuses to commit (``on_main_with_changes``), so the chain ends
``handoff.stuck`` with that reason and the session stays as it was.
"""
import os
import shutil
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from herdr_engineering.handoff_adapters import adapter_for
from herdr_engineering.handoff_adapters.base import Harness
from herdr_engineering.handoff_adapters.screen import strip_ansi
from herdr_engineering.handoff_controller.config import ControllerConfig
from herdr_engineering.handoff_controller.events import Colour, HandoffEvent, chain_colour
from herdr_engineering.handoff_controller.herdr_cli import HerdrCli
from herdr_engineering.handoff_controller.run import build_controller, default_locate, run_once
from herdr_engineering.side_pane.placement import Outcome, ensure
from tests.e2e.test_handoff_controller_live import (
    _CHAIN_TIMEOUT_S,
    _FIRST_PROMPT,
    _MIN_HANDOFF,
    _STARTUP_TIMEOUT_S,
    _TICK_S,
    Tab,
    _agent_panes,
    _git,
    _handoff_version,
    _json,
    _open_tab,
    _pane_ids_of,
    _ready,
    _run,
    _skip_unless_live,
    _start_harness,
    _submit,
    _wait,
    tabs,  # noqa: F401  (a fixture: it closes every tab the test opened)
)

pytestmark = pytest.mark.herdr_live

_LIVE_DIR = Path(__file__).resolve().parents[2] / ".herdr-test" / "side-pane-live"
_REPO = Path(__file__).resolve().parents[2]
_FEATURE_PROMPT = "Append the word live on a new line of README.md, then reply with the single word done."
_DOT = " ●"  # the attention dot the side pane puts after a tab label


@pytest.fixture(scope="module", autouse=True)
def _cleanup_directory() -> Iterator[None]:
    yield
    shutil.rmtree(_LIVE_DIR, ignore_errors=True)


def _make_repo(root: Path, branch: str | None) -> Path:
    """A real repository with a real bare remote; on `branch` with an upstream, or left on main."""
    remote, work = root / "remote.git", root / "work"
    _run("git", "init", "--bare", "-b", "main", str(remote), timeout=60)
    work.mkdir()
    _run("git", "init", "-b", "main", str(work), timeout=60)
    _git(work, "config", "user.name", "Live Test")
    _git(work, "config", "user.email", "live-test@example.invalid")
    (work / "README.md").write_text("throwaway repository for the side pane live test\n", encoding="utf-8")
    _git(work, "add", "README.md")
    _git(work, "commit", "-m", "initial")
    _git(work, "remote", "add", "origin", str(remote))
    _git(work, "push", "-u", "origin", "main")
    if branch:
        _git(work, "checkout", "-b", branch)
        _git(work, "push", "-u", "origin", branch)
    return work


def _preconditions() -> None:
    _skip_unless_live()
    version = _handoff_version()
    if version is None or version < _MIN_HANDOFF:
        pytest.skip(f"herdr-handoff >= {'.'.join(map(str, _MIN_HANDOFF))} is required, found {version}")
    if not shutil.which("claude"):
        pytest.skip("the claude CLI is not installed")


def _run_chain(name: str, branch: str | None, second_prompt: str, tabs: list[Tab]) -> tuple[Tab, Path, Path, list[HandoffEvent]]:  # noqa: F811
    """A real session driven to the end of one chain by the controller. Returns the tab, store, state and the events."""
    root = _LIVE_DIR / name
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True)
    work = _make_repo(root, branch)
    store, state = root / "store", root / "state"
    store.mkdir()
    state.mkdir()
    shared = {"HERDR_HANDOFF_STORE": str(store), "HERDR_ENGINEERING_STATE": str(state)}
    env = {**os.environ, **shared, "HERDR_HANDOFF_AUTO": "1"}
    tab = _open_tab(work, name, shared)
    tabs.append(tab)
    herdr = HerdrCli()
    source = tab.pane_id
    _start_harness(Harness.CLAUDE, source)
    assert _wait(lambda: _ready(herdr, source, Harness.CLAUDE), _STARTUP_TIMEOUT_S, 1.0), \
        f"claude never showed an input-ready prompt:\n{strip_ansi(herdr.read_visible(source))[-1500:]}"
    _submit(herdr, source, _FIRST_PROMPT)
    pane = next(iter(_agent_panes(herdr, source)), None)
    assert pane is not None, "Herdr does not list the Claude pane as an agent"
    adapter = adapter_for(Harness.CLAUDE)
    ref = default_locate(adapter, pane)
    assert ref is not None, "the controller's lookup cannot find the Claude session record"
    reading = adapter.read_context(ref)
    assert reading is not None, "the session has no usage reading after one turn"
    cfg = ControllerConfig(drain_limit_pct=max(0.1, reading.percent - 1.0), push_allowed_repos=(str(work),))
    _submit(herdr, source, second_prompt)
    ctl = build_controller(env, cfg=cfg)
    events: list[HandoffEvent] = []

    def tick() -> bool:
        run_once(ctl, _agent_panes(ctl.herdr, source))  # only this pane: no other session is ever looked at
        events[:] = ctl.log.read()
        return chain_colour(events) is not None

    assert _wait(tick, _CHAIN_TIMEOUT_S, _TICK_S), f"no chain ended in {_CHAIN_TIMEOUT_S:.0f}s; events: {[e.event for e in events]}"
    return tab, store, state, events


def _open_side_pane(tab: Tab, store: Path, state: Path) -> str:
    """Place the side pane like the controller will, running this checkout's viewer on the throwaway store and state."""
    runner = (f"exec env PYTHONPATH={_REPO} HERDR_HANDOFF_STORE={store} HERDR_ENGINEERING_STATE={state} "
              f"{sys.executable} -m herdr_engineering.cli side-pane run")
    env = {**os.environ, "HERDR_ENGINEERING_STATE": str(state)}
    result = ensure(tab.tab_id, env=env, command=runner)
    assert result.outcome is Outcome.CREATED and result.pane_id, f"the side pane was not opened: {result}"
    return result.pane_id


def _read(pane_id: str) -> str:
    return strip_ansi(_run("herdr", "pane", "read", pane_id, "--source", "visible", timeout=30).stdout)


def _show(pane_id: str, key: str) -> None:
    _run("herdr", "pane", "send-keys", pane_id, key, timeout=30)


def _wait_for_text(pane_id: str, text: str, timeout_s: float = 60.0) -> str:
    seen = ""

    def has() -> bool:
        nonlocal seen
        seen = _read(pane_id)
        return text in seen

    assert _wait(has, timeout_s, 1.0), f"{text!r} never showed in the side pane {pane_id}:\n{seen}"
    return seen


def _focused_pane(tab_id: str) -> str | None:
    panes = _json(_run("herdr", "pane", "list", timeout=60).stdout).get("result", {}).get("panes", [])
    return next((p["pane_id"] for p in panes if isinstance(p, dict) and p.get("tab_id") == tab_id and p.get("focused") is True), None)


def _assert_one_side_pane_on_the_right(tab: Tab, side: str, session_panes: list[str]) -> None:
    panes = _pane_ids_of(tab.tab_id)
    assert panes.count(side) == 1, panes
    assert len(panes) == len(session_panes) + 1, f"one side pane expected beside the sessions {session_panes}, found {panes}"
    for pane in session_panes:
        neighbour = _json(_run("herdr", "pane", "neighbor", "--pane", pane, "--direction", "right", timeout=30).stdout)
        assert side in str(neighbour), f"the side pane is not to the right of {pane}: {neighbour}"


def test_after_a_green_handoff_the_side_pane_shows_the_chain_and_the_successor_live(tabs: list[Tab]) -> None:  # noqa: F811
    _preconditions()
    tab, store, state, events = _run_chain("green", "feat/side-pane-live", _FEATURE_PROMPT, tabs)
    assert chain_colour(events) == Colour.GREEN, [e.event for e in events]
    successor = events[-1].successor_pane_id
    assert successor, f"the succeeded event names no successor pane: {events[-1]}"
    before = _focused_pane(tab.tab_id)

    side = _open_side_pane(tab, store, state)
    _show(side, "2")
    frame = _wait_for_text(side, "handed off")
    assert "▶" in frame, f"no live row in the Handoffs tab:\n{frame}"  # the successor is the live row
    assert "#2" in frame, f"the successor is not the second session:\n{frame}"
    assert _DOT not in frame.splitlines()[0], f"an attention dot after a green chain:\n{frame}"
    _assert_one_side_pane_on_the_right(tab, side, [successor])
    assert _focused_pane(tab.tab_id) == before, "opening the side pane moved the focus"


def test_after_a_stuck_chain_the_side_pane_shows_a_red_row_and_its_reason(tabs: list[Tab]) -> None:  # noqa: F811
    _preconditions()
    tab, store, state, events = _run_chain("red", None, _FEATURE_PROMPT, tabs)  # on main, README.md changed
    assert chain_colour(events) == Colour.RED and events[-1].event == "handoff.stuck", [e.event for e in events]
    assert events[-1].reason == "on_main_with_changes", events[-1]
    session_panes = _pane_ids_of(tab.tab_id)
    assert session_panes, "the stuck session's pane is gone"

    side = _open_side_pane(tab, store, state)
    _show(side, "2")
    frame = _wait_for_text(side, "stuck")
    assert "on_main_with_changes" in frame, f"the reason is not shown:\n{frame}"
    assert f"Handoffs{_DOT}" in frame.splitlines()[0], f"no attention dot after a red chain:\n{frame}"
    _assert_one_side_pane_on_the_right(tab, side, [p for p in session_panes if p != side])

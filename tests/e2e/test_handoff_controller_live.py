"""The handoff controller against a real Herdr and a real Claude session, plus its agent-name mapping for every harness.

Opt in with ``HERDR_HANDOFF_LIVE_E2E=1 pytest tests/e2e/test_handoff_controller_live.py -m herdr_live -v``. Everything is
skipped, with the reason, unless ALL of these hold: ``herdr`` answers, ``herdr-handoff --version`` is 0.5.0 or newer,
the ``claude`` CLI is installed, and ``HERDR_HANDOFF_LIVE_E2E=1``. (The mapping test needs only Herdr, the variable and
the harness it probes.)

What it touches, and what it costs. It opens its own NEW Herdr tab (never splits or types into an existing pane) with
its working directory in a throwaway git repository under ``.herdr-test/controller-live/`` (a real repository with a
real bare remote, on a feature branch). It starts a real interactive Claude session there, submits two short prompts,
and lets the controller type the drain order into that session: this SPENDS MODEL TOKENS, and the session itself writes
a briefing, commits and runs ``herdr-handoff``. It then hands the session to a successor Claude in a pane below it,
which is also a real session. The controller runs in this process, one ``run_once`` per tick, restricted to the one pane
the test opened (``run_once(panes=...)``), so no other session in the user's Herdr is ever looked at, drained or closed.
The shared store is ``.herdr-test/controller-live/store`` and the switch state directory is
``.herdr-test/controller-live/state`` (both passed to the pane too), so the user's real handoff store is untouched.
The tab, and with it the source pane and the successor pane, is closed in ``finally``; a tab that will not close fails
the test. Claude Code asks to trust a folder it has not seen: the test never confirms that dialog, it skips; open Claude
once in ``.herdr-test/controller-live/work`` (or trust ``.herdr-test`` as a whole) and run again. Claude also loads the
instructions of the checkout that contains ``.herdr-test``.

Why the limit is lowered instead of the transcript being faked: a handoff is only believable when the context reading,
the drain order, the briefing and the takeover are all real. Filling a real session to 80% costs far more tokens than
the test is worth, and a fabricated transcript would test the reader, not the session. So the session is real and the
``ControllerConfig`` is real, but ``drain_limit_pct`` is set one point below what the session measures after its first
turn (the ceiling stays 12 points above it). Everything the controller decides is then decided on a genuine reading.

The mapping test starts each installed harness in a pane of a throwaway tab, WITHOUT sending a prompt, and asserts that
the controller's Herdr-agent-name to Harness table recognises it. It exists because the names Herdr uses for Codex and
OpenCode were assumed, not measured (see ``herdr_cli._AGENT_NAMES``).
"""
import json
import os
import re
import shutil
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from herdr_engineering.handoff_adapters import adapter_for
from herdr_engineering.handoff_adapters.base import Harness
from herdr_engineering.handoff_adapters.screen import strip_ansi
from herdr_engineering.handoff_adapters.send import send_typed
from herdr_engineering.handoff_controller.config import ControllerConfig
from herdr_engineering.handoff_controller.events import Colour, HandoffEvent, chain_colour
from herdr_engineering.handoff_controller.herdr_cli import (
    AgentPane,
    HerdrCli,
    harness_for_agent_name,
)
from herdr_engineering.handoff_controller.run import build_controller, default_locate, run_once
from herdr_engineering.herdr_pane_client import HerdrPaneClient

pytestmark = pytest.mark.herdr_live

_LIVE_DIR = Path(__file__).resolve().parents[2] / ".herdr-test" / "controller-live"
_MIN_HANDOFF = (0, 5, 0)
_STARTUP_TIMEOUT_S = 90.0
_TURN_TIMEOUT_S = 180.0
_CHAIN_TIMEOUT_S = 25 * 60.0
_TICK_S = 5.0
_TRUST_DIALOGS = ("Trust this folder", "Quick safety check", "Do you trust")
_DRAIN_ORDER_START = "Context is nearly full; hand off now."
_FIRST_PROMPT = "Reply with the single word ok."
_SECOND_PROMPT = "Create a file notes.txt containing the word live, then reply with the single word done."


def _run(*args: str, timeout: float = 120.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(args), capture_output=True, text=True, timeout=timeout)


def _json(text: str) -> dict:
    try:
        parsed = json.loads(text)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _wait(until: Callable[[], bool], timeout_s: float, step_s: float = 0.5) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if until():
            return True
        time.sleep(step_s)
    return until()


def _handoff_version() -> tuple[int, int, int] | None:
    try:
        out = _run("herdr-handoff", "--version", timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", out)
    return (int(match[1]), int(match[2]), int(match[3])) if match else None


def _skip_unless_live() -> None:
    if os.environ.get("HERDR_HANDOFF_LIVE_E2E") != "1":
        pytest.skip("HERDR_HANDOFF_LIVE_E2E=1 is not set: this test types into a real session and spends model tokens")
    if not shutil.which("herdr"):
        pytest.skip("herdr is not installed")
    try:
        reachable = _run("herdr", "tab", "list", timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        reachable = False
    if not reachable:
        pytest.skip("herdr does not answer (no running Herdr socket)")


@pytest.fixture(scope="module")
def live() -> Path:
    """The throwaway directory of this file, empty, removed afterwards."""
    _skip_unless_live()
    shutil.rmtree(_LIVE_DIR, ignore_errors=True)
    _LIVE_DIR.mkdir(parents=True)
    return _LIVE_DIR


@pytest.fixture(scope="module", autouse=True)
def _cleanup_directory() -> Iterator[None]:
    yield
    shutil.rmtree(_LIVE_DIR, ignore_errors=True)


def _git(cwd: Path, *args: str) -> None:
    done = _run("git", "-C", str(cwd), *args, timeout=60)
    assert done.returncode == 0, f"git {' '.join(args)} failed: {done.stderr.strip()}"


def _make_repo(root: Path) -> Path:
    """A real repository with a real bare remote, on a feature branch with an upstream."""
    remote, work = root / "remote.git", root / "work"
    _run("git", "init", "--bare", "-b", "main", str(remote), timeout=60)
    work.mkdir()
    _run("git", "init", "-b", "main", str(work), timeout=60)
    _git(work, "config", "user.name", "Live Test")
    _git(work, "config", "user.email", "live-test@example.invalid")
    (work / "README.md").write_text("throwaway repository for the handoff controller live test\n", encoding="utf-8")
    _git(work, "add", "README.md")
    _git(work, "commit", "-m", "initial")
    _git(work, "remote", "add", "origin", str(remote))
    _git(work, "push", "-u", "origin", "main")
    _git(work, "checkout", "-b", "feat/controller-live")
    _git(work, "push", "-u", "origin", "feat/controller-live")
    return work


@dataclass
class Tab:
    tab_id: str
    pane_id: str


def _pane_ids_of(tab_id: str) -> list[str]:
    panes = _json(_run("herdr", "pane", "list", timeout=60).stdout).get("result", {}).get("panes", [])
    return [p["pane_id"] for p in panes if isinstance(p, dict) and p.get("tab_id") == tab_id and "pane_id" in p]


def _open_tab(cwd: Path, label: str, env: dict[str, str]) -> Tab:
    args = ["herdr", "tab", "create", "--cwd", str(cwd), "--label", label, "--no-focus"]
    for key, value in env.items():
        args += ["--env", f"{key}={value}"]
    reply = _run(*args).stdout
    match = re.search(r'"tab_id"\s*:\s*"([^"]+)"', reply)
    if not match:
        pytest.fail(f"herdr tab create returned no tab id: {reply[:300]!r}", pytrace=False)
    tab_id = match.group(1)
    if not _wait(lambda: bool(_pane_ids_of(tab_id)), 15.0):
        _run("herdr", "tab", "close", tab_id)
        pytest.fail(f"the new tab {tab_id} has no pane", pytrace=False)
    return Tab(tab_id, _pane_ids_of(tab_id)[0])


@pytest.fixture
def tabs() -> Iterator[list[Tab]]:
    """Tabs this test opened; every one is closed afterwards, even when the test failed."""
    opened: list[Tab] = []
    try:
        yield opened
    finally:
        problems: list[str] = []
        for tab in opened:
            try:
                _run("herdr", "tab", "close", tab.tab_id, timeout=60)
                still = _run("herdr", "tab", "list", timeout=60).stdout
            except (OSError, subprocess.SubprocessError) as error:
                problems.append(f"{tab.tab_id}: could not close or confirm: {error}")
                continue
            if f'"tab_id":"{tab.tab_id}"' in still:
                problems.append(f"{tab.tab_id}: still listed after tab close")
        if problems:
            pytest.fail("tab(s) left open: " + "; ".join(problems), pytrace=False)


def _start_harness(harness: Harness, pane_id: str) -> None:
    started = _run("herdr", "agent", "start", "probe", "--kind", harness.value, "--pane", pane_id, "--timeout", "60000")
    assert started.returncode == 0 and '"error"' not in started.stdout, \
        f"herdr agent start failed (exit {started.returncode}): {(started.stdout + started.stderr).strip()[:300]}"


def _agent_panes(herdr: HerdrCli, pane_id: str) -> list[AgentPane]:
    return [p for p in (herdr.agents() or []) if p.pane_id == pane_id]


def _ready(herdr: HerdrCli, pane_id: str, harness: Harness) -> bool:
    agent = herdr.agent(pane_id)
    if agent is None or agent.status is None:
        return False
    screen = herdr.read_visible(pane_id)
    if any(text in strip_ansi(screen) for text in _TRUST_DIALOGS):
        pytest.skip(f"{harness.value} asks to trust this folder; open it once in {_LIVE_DIR / 'work'} "
                    "(or trust .herdr-test) and run again")
    return adapter_for(harness).is_ready_for_input(screen, agent.status)


def _submit(herdr: HerdrCli, pane_id: str, text: str) -> None:
    send_typed(HerdrPaneClient(), pane_id, adapter_for(Harness.CLAUDE).drain_input(text))
    assert _wait(lambda: (herdr.agent_status(pane_id) or "") == "working", 60.0, 0.25), "the prompt never started a turn"
    assert herdr.wait_idle(pane_id, _TURN_TIMEOUT_S), f"the turn did not finish in {_TURN_TIMEOUT_S:.0f}s"


class _Sampler(threading.Thread):
    """Watches the source pane while the controller works: the pane below it, and the processes running in it.

    The takeover closes the source pane, so where the child sits and which processes the source had can only be read
    before that. It polls Herdr read-only, every half second, until stopped.
    """

    def __init__(self, source: str) -> None:
        super().__init__(daemon=True)
        self.source = source
        self.below: set[str] = set()
        self.pids: set[int] = set()
        self._stop = threading.Event()

    def run(self) -> None:
        while not self._stop.is_set():
            below = _run("herdr", "pane", "neighbor", "--pane", self.source, "--direction", "down", timeout=30).stdout
            self.below |= set(re.findall(r'"pane_id"\s*:\s*"([^"]+)"', below)) - {self.source}
            info = _run("herdr", "pane", "process-info", "--pane", self.source, timeout=30).stdout
            self.pids |= {int(pid) for pid in re.findall(r'"pid"\s*:\s*(\d+)', info)}
            self._stop.wait(0.5)

    def finish(self) -> None:
        self._stop.set()
        self.join(timeout=30)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def test_a_real_session_at_its_limit_is_handed_to_a_successor_below_it(live: Path, tabs: list[Tab]) -> None:
    version = _handoff_version()
    if version is None or version < _MIN_HANDOFF:
        pytest.skip(f"herdr-handoff >= {'.'.join(map(str, _MIN_HANDOFF))} is required, found {version}")
    if not shutil.which("claude"):
        pytest.skip("the claude CLI is not installed")
    work = _make_repo(live)
    store, state = live / "store", live / "state"
    store.mkdir()
    state.mkdir()
    env = {**os.environ, "HERDR_HANDOFF_STORE": str(store), "HERDR_ENGINEERING_STATE": str(state),
           "HERDR_HANDOFF_AUTO": "1"}
    tab = _open_tab(work, "controller-live", {"HERDR_HANDOFF_STORE": str(store), "HERDR_ENGINEERING_STATE": str(state)})
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
    # One point under what the real session measures: it crosses the limit, and stays inside the 12-point ceiling.
    cfg = ControllerConfig(drain_limit_pct=max(0.1, reading.percent - 1.0), push_allowed_repos=(str(work),))
    _submit(herdr, source, _SECOND_PROMPT)

    ctl = build_controller(env, cfg=cfg)
    sampler = _Sampler(source)
    sampler.start()
    events: list[HandoffEvent] = []
    try:
        def tick() -> bool:
            run_once(ctl, _agent_panes(ctl.herdr, source))  # only this pane: no other session is ever looked at
            events[:] = ctl.log.read()
            return chain_colour(events) is not None

        assert _wait(tick, _CHAIN_TIMEOUT_S, _TICK_S), \
            f"no chain ended in {_CHAIN_TIMEOUT_S:.0f}s; events: {[e.event for e in events]}"
    finally:
        sampler.finish()

    names = [e.event for e in events]
    last = events[-1]
    assert last.event == "handoff.succeeded", f"chain ended {last.event} ({last.reason}); events: {names}"
    assert chain_colour(events) == Colour.GREEN
    assert {"handoff.triggered", "handoff.drain_started", "handoff.briefing_written", "handoff.takeover_started"} <= set(names), names
    assert "handoff.briefing_fallback" not in names, "the session did not write its own briefing; the fallback did"

    # the order was typed into the session
    assert ref.record_path is not None and _DRAIN_ORDER_START in ref.record_path.read_text(encoding="utf-8", errors="replace"), \
        "the drain order is not in the session transcript"
    # a briefing record exists for this pane
    briefing = next(e for e in events if e.event == "handoff.briefing_written")
    record = _json((store / "outbox" / f"{briefing.handoff_id}.json").read_text(encoding="utf-8"))
    assert record, f"no briefing record {briefing.handoff_id} in {store / 'outbox'}"
    # the child pane sits below the source, in the same tab
    child = last.successor_pane_id
    assert child and child != source, f"the succeeded event names no successor pane: {last}"
    assert child in sampler.below, f"the pane below {source} was never {child}; saw {sorted(sampler.below)}"
    assert child in _pane_ids_of(tab.tab_id), "the successor pane is not in the source's tab"
    # the source process ended and its pane closed
    assert source not in _pane_ids_of(tab.tab_id), f"the source pane {source} is still open"
    assert not [pid for pid in sampler.pids if _alive(pid)], f"processes of the source pane still run: {sorted(sampler.pids)}"


_HARNESSES = list(Harness)


@pytest.mark.parametrize("harness", _HARNESSES)
def test_the_agent_name_herdr_reports_maps_to_the_harness(harness: Harness, live: Path, tabs: list[Tab]) -> None:
    """Measures the names Herdr gives each harness; no prompt is sent, so no tokens are spent."""
    if not shutil.which(harness.value):
        pytest.skip(f"{harness.value} is not installed")
    probe = live / "probe"
    probe.mkdir(exist_ok=True)
    tab = _open_tab(probe, f"map-{harness.value}", {})
    tabs.append(tab)
    _start_harness(harness, tab.pane_id)
    herdr = HerdrCli()
    names: list[str | None] = []

    def named() -> bool:
        info = herdr.agent(tab.pane_id)
        names.append(info.name if info else None)
        return names[-1] is not None

    assert _wait(named, 60.0, 1.0), f"Herdr never reported an agent name for {harness.value} in {tab.pane_id}"
    print(f"MEASURED herdr agent name for {harness.value}: {names[-1]!r}")
    assert harness_for_agent_name(names[-1]) is harness, \
        f"Herdr calls {harness.value} {names[-1]!r}, which the controller maps to {harness_for_agent_name(names[-1])}"

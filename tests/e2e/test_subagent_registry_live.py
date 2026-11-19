"""A real interactive Claude background agent is seen by the subagent registry hooks, measured in a real Herdr pane.

Opt in with ``pytest tests/e2e/test_subagent_registry_live.py -m herdr_live -v``. Skipped unless a Herdr socket and
``HERDR_PANE_ID`` are present, ``claude`` and ``herdr-eng`` are installed, and the real ``~/.claude/settings.json``
already carries the managed ``SubagentStart`` and ``SubagentStop`` hooks (marker ``herdr-handoff-auto``; read only:
this test never installs them). ``claude -p`` is not enough, hooks fire for interactive subagents, so it splits a
pane off the one it runs in (``--no-focus``), starts interactive Claude there, submits ONE prompt asking for one
background agent that sleeps about 20 seconds, and polls ``herdr-eng handoff subagents --pane <id> --status --json``
until ``live == 1`` and then until ``live == 0``. Every pane it opened is closed afterwards, even on failure. The
pane's working directory is ``$HERDR_LIVE_CWD`` or the home directory; point it at a folder Claude already trusts.
"""
import json
import os
import shutil
import subprocess
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from herdr_engineering.handoff_adapters import adapter_for
from herdr_engineering.handoff_adapters.base import Harness
from herdr_engineering.handoff_adapters.screen import strip_ansi
from herdr_engineering.handoff_adapters.send import send_typed
from herdr_engineering.herdr_pane_client import HerdrError, HerdrPaneClient

pytestmark = pytest.mark.herdr_live

_HOOK_MARKER = "herdr-handoff-auto"
_HOOK_EVENTS = ("SubagentStart", "SubagentStop")
_STARTUP_TIMEOUT_S = 90.0
_APPEAR_TIMEOUT_S = 120.0  # the model has to decide to start the agent
_FINISH_TIMEOUT_S = 90.0  # the agent sleeps about 20 seconds
_TRUST_DIALOGS = ("Trust this folder", "Quick safety check", "Do you trust")
_PROMPT = ("Run exactly one background agent (the Agent tool with run_in_background) whose only job is to run "
           "`sleep 20` in the shell and then reply done. Do nothing else and start no other agent.")


def _herdr(*args: str) -> dict:
    out = subprocess.run(["herdr", *args], capture_output=True, text=True, timeout=120).stdout
    return json.loads(out) if out.strip().startswith("{") else {}


def _wait(until: Callable[[], bool], timeout_s: float, step_s: float = 0.5) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if until():
            return True
        time.sleep(step_s)
    return until()


def _hooks_installed() -> bool:
    """Read only: both managed events carry the marker in the real settings.json."""
    try:
        hooks = json.loads((Path.home() / ".claude" / "settings.json").read_text(encoding="utf-8")).get("hooks", {})
    except (OSError, ValueError, AttributeError):
        return False
    return all(_HOOK_MARKER in json.dumps(hooks.get(event, [])) for event in _HOOK_EVENTS)


@pytest.fixture(scope="module")
def client() -> HerdrPaneClient:
    if not shutil.which("herdr") or not os.environ.get("HERDR_PANE_ID"):
        pytest.skip("not running inside a Herdr pane")
    herdr = HerdrPaneClient()
    try:
        herdr.list_panes()
    except HerdrError as error:
        pytest.skip(f"no herdr socket: {error}")
    if not shutil.which("claude"):
        pytest.skip("claude is not installed")
    if not shutil.which("herdr-eng"):
        pytest.skip("herdr-eng is not installed")
    if not _hooks_installed():
        pytest.skip(f"the managed SubagentStart/SubagentStop hooks ({_HOOK_MARKER}) are not installed in "
                    "~/.claude/settings.json; install them first (docs/operations/handoff-auto-triggers.md)")
    return herdr


def _close(client: HerdrPaneClient, pane_id: str) -> str | None:
    """Close a pane this test opened; the problem as text when it is still listed afterwards."""
    try:
        closed = subprocess.run(["herdr", "pane", "close", pane_id], capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return f"{pane_id}: herdr pane close timed out"
    try:
        listed = {pane.get("pane_id") for pane in client.list_panes()}
    except HerdrError as error:
        return f"{pane_id}: could not confirm the close: {error}"
    if pane_id not in listed:
        return None
    return f"{pane_id}: still listed after pane close (exit {closed.returncode}): {(closed.stderr or closed.stdout).strip()[:200]}"


@pytest.fixture
def opened(client: HerdrPaneClient) -> Iterator[list[str]]:
    """Ids of the panes this test opened; every one is closed afterwards, even when the test failed."""
    panes: list[str] = []
    try:
        yield panes
    finally:
        problems = [problem for pane_id in panes if (problem := _close(client, pane_id))]
        if problems:
            pytest.fail("pane(s) left open: " + "; ".join(problems), pytrace=False)


def _status(pane_id: str) -> dict:
    run = subprocess.run(["herdr-eng", "handoff", "subagents", "--pane", pane_id, "--status", "--json"],
                         capture_output=True, text=True, timeout=60)
    try:
        return json.loads(run.stdout)
    except ValueError:
        return {"error": f"exit {run.returncode}: {(run.stdout + run.stderr).strip()[:200]}"}


def test_a_real_background_agent_is_registered_then_released(client: HerdrPaneClient, opened: list[str]) -> None:
    cwd = os.environ.get("HERDR_LIVE_CWD") or str(Path.home())
    split = _herdr("pane", "split", os.environ["HERDR_PANE_ID"], "--direction", "down", "--no-focus", "--cwd", cwd)
    pane_id = split["result"]["pane"]["pane_id"]
    opened.append(pane_id)
    adapter = adapter_for(Harness.CLAUDE)

    def start() -> bool:  # the pane's shell may still be starting
        run = subprocess.run(["herdr", "agent", "start", "probe", "--kind", "claude", "--pane", pane_id,
                              "--timeout", "60000"], capture_output=True, text=True, timeout=120)
        return run.returncode == 0 and '"error"' not in run.stdout

    _wait(start, 30.0, step_s=1.0)

    def screen() -> str:
        return strip_ansi(client.read_visible(pane_id))

    def ready() -> bool:
        if any(text in screen() for text in _TRUST_DIALOGS):
            pytest.skip(f"claude asks to trust {cwd}; set HERDR_LIVE_CWD to a folder it already trusts")
        agent = _herdr("agent", "get", pane_id).get("result", {}).get("agent", {}).get("agent_status", "unknown")
        return adapter.is_ready_for_input(client.read_visible(pane_id), agent)

    assert _wait(ready, _STARTUP_TIMEOUT_S, step_s=1.0), f"claude never showed an input-ready prompt:\n{screen()[-1500:]}"
    send_typed(client, pane_id, adapter.drain_input(_PROMPT))

    seen: list[dict] = []

    def live_is(count: int) -> Callable[[], bool]:
        def check() -> bool:
            seen.append(_status(pane_id))
            return seen[-1].get("live") == count
        return check

    assert _wait(live_is(1), _APPEAR_TIMEOUT_S), f"no live subagent registered; last status {seen[-1:]}:\n{screen()[-1500:]}"
    assert _wait(live_is(0), _FINISH_TIMEOUT_S), f"the subagent was never released; last status {seen[-1:]}:\n{screen()[-1500:]}"
    assert seen[-1] == {"known": True, "live": 0}, f"released but not known; last status {seen[-1]}"

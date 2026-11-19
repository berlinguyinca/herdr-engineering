"""Idle detection, exit and compact commands of each handoff adapter, measured in real Herdr panes.

Opt in with ``pytest tests/e2e/test_handoff_adapters_live.py -m herdr_live -v``. It splits a pane off the pane it runs
in (``HERDR_PANE_ID``, ``--no-focus``), starts the installed harness there, types without submitting, and closes every
pane it opened (a pane that will not close fails the test). A harness that is not installed, or that stops at a trust or login dialog, is skipped. The pane's
working directory is ``$HERDR_LIVE_CWD`` or the home directory; Claude Code and Codex ask to trust a folder they have
not seen, so point it at one they already trust to run those two. The after-a-turn test submits exactly one trivial
prompt per harness, in the pane the test opened; every other test types without submitting.
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
from herdr_engineering.handoff_adapters.base import Harness, HarnessAdapter
from herdr_engineering.handoff_adapters.herdr_session import herdr_agent_session
from herdr_engineering.handoff_adapters.screen import strip_ansi
from herdr_engineering.handoff_adapters.send import send_typed
from herdr_engineering.herdr_pane_client import HerdrError, HerdrPaneClient

pytestmark = pytest.mark.herdr_live

_SHELLS = frozenset({"zsh", "bash", "sh", "fish", "dash"})
_STARTUP_TIMEOUT_S = 90.0
# a harness that is waiting on the user before its prompt: not something a test may confirm
_TRUST_DIALOGS = ("Trust this folder", "Quick safety check", "Do you trust")
# a dialog that leaves the user's settings alone when dismissed with Escape
_DISMISSIBLE_DIALOGS = ("enter continue · esc skip", "Skip  Confirm")  # Codex and OpenCode update prompts


def _herdr(*args: str) -> dict:
    out = subprocess.run(["herdr", *args], capture_output=True, text=True, timeout=120).stdout
    return json.loads(out) if out.strip().startswith("{") else {}


def _wait(until: Callable[[], bool], timeout_s: float, step_s: float = 0.25) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if until():
            return True
        time.sleep(step_s)
    return until()


@pytest.fixture(scope="module")
def client() -> HerdrPaneClient:
    if not shutil.which("herdr") or not os.environ.get("HERDR_PANE_ID"):
        pytest.skip("not running inside a Herdr pane")
    herdr = HerdrPaneClient()
    try:
        herdr.list_panes()
    except HerdrError as error:
        pytest.skip(f"no herdr socket: {error}")
    return herdr


def _close(client: HerdrPaneClient, pane_id: str) -> str | None:
    """Close a pane this test opened; the problem as text when herdr fails to close it or the pane is still listed."""
    closed = subprocess.run(["herdr", "pane", "close", pane_id], capture_output=True, text=True, timeout=120)
    try:
        listed = {pane.get("pane_id") for pane in client.list_panes()}
    except HerdrError as error:
        return f"{pane_id}: could not confirm the close: {error}"
    if pane_id not in listed:
        return None  # closed, or already gone
    return f"{pane_id}: still listed after pane close (exit {closed.returncode}): {(closed.stderr or closed.stdout).strip()[:200]}"


@pytest.fixture
def opened(client: HerdrPaneClient) -> Iterator[list[str]]:
    """Ids of the panes this test opened; every one is closed afterwards, even when the test failed.

    A pane that will not close fails the test after the others were closed: it must not stay open unnoticed.
    """
    panes: list[str] = []
    try:
        yield panes
    finally:
        problems = [problem for pane_id in panes if (problem := _close(client, pane_id))]
        if problems:
            pytest.fail("pane(s) left open: " + "; ".join(problems), pytrace=False)


def _screen(client: HerdrPaneClient, pane_id: str) -> str:
    return client.read_visible(pane_id)


def _status(pane_id: str) -> str:
    return _herdr("agent", "get", pane_id).get("result", {}).get("agent", {}).get("agent_status", "unknown")


def _start(harness: Harness, client: HerdrPaneClient, opened: list[str]) -> tuple[str, HarnessAdapter]:
    """A pane running the harness with an empty, input-ready prompt; skips when that cannot be reached."""
    if not shutil.which(harness.value):
        pytest.skip(f"{harness.value} is not installed")
    cwd = os.environ.get("HERDR_LIVE_CWD") or str(Path.home())
    split = _herdr("pane", "split", os.environ["HERDR_PANE_ID"], "--direction", "down", "--no-focus", "--cwd", cwd)
    pane_id = split["result"]["pane"]["pane_id"]
    opened.append(pane_id)
    adapter = adapter_for(harness)

    def start() -> bool:  # the pane's shell may still be starting
        reply = _herdr("agent", "start", "probe", "--kind", harness.value, "--pane", pane_id, "--timeout", "60000")
        return reply.get("error", {}).get("code") != "agent_pane_busy"

    _wait(start, 30.0, step_s=1.0)

    def ready() -> bool:
        plain = strip_ansi(_screen(client, pane_id))
        if any(text in plain for text in _TRUST_DIALOGS):
            pytest.skip(f"{harness.value} asks to trust {cwd}; set HERDR_LIVE_CWD to a folder it already trusts")
        if any(text in plain for text in _DISMISSIBLE_DIALOGS):
            client.send_keys(pane_id, ["esc"])
            time.sleep(1.0)
        return adapter.is_ready_for_input(_screen(client, pane_id), _status(pane_id))

    if not _wait(ready, _STARTUP_TIMEOUT_S, step_s=1.0):
        pytest.fail(f"{harness.value} never showed an input-ready prompt:\n{strip_ansi(_screen(client, pane_id))[-1500:]}")
    return pane_id, adapter


def _foreground_names(pane_id: str) -> set[str]:
    info = _herdr("pane", "process-info", "--pane", pane_id)
    processes = info.get("result", {}).get("process_info", {}).get("foreground_processes", [])
    return {process.get("name", "") for process in processes}


@pytest.mark.parametrize("harness", list(Harness))
def test_prompt_detection_on_a_real_pane(harness: Harness, client: HerdrPaneClient, opened: list[str]) -> None:
    pane_id, adapter = _start(harness, client, opened)
    assert adapter.is_ready_for_input(_screen(client, pane_id), "idle")  # _start proved this with the live status
    assert not adapter.is_ready_for_input(_screen(client, pane_id), "working")
    client.send_text(pane_id, "typed but never submitted")
    assert _wait(lambda: "typed but never" in strip_ansi(_screen(client, pane_id)), 10.0)
    assert not adapter.is_ready_for_input(_screen(client, pane_id), "idle")


@pytest.mark.parametrize("harness", list(Harness))
def test_exit_plan_ends_the_process(harness: Harness, client: HerdrPaneClient, opened: list[str]) -> None:
    pane_id, adapter = _start(harness, client, opened)
    plan = adapter.exit_plan()
    assert _foreground_names(pane_id) - _SHELLS  # the harness is running
    started = time.monotonic()
    send_typed(client, pane_id, plan)
    ended = _wait(lambda: _foreground_names(pane_id) <= _SHELLS, plan.process_end_timeout_s, step_s=0.1)
    elapsed = time.monotonic() - started
    print(f"MEASURED {harness.value} exit: text={plan.text!r} keys={plan.keys} ended={ended} after {elapsed:.2f}s")
    assert ended, f"{harness.value} still running {plan.process_end_timeout_s}s after {plan.text!r} {plan.keys}"


@pytest.mark.parametrize("harness", [Harness.CLAUDE, Harness.CODEX, Harness.PI])
def test_compact_command_is_offered_by_the_slash_menu(harness: Harness, client: HerdrPaneClient, opened: list[str]) -> None:
    """The command is typed, not submitted: the menu that opens lists it (Pi drops the slash) as well as the typed row.

    A session-less OpenCode pane does not list /compact; it is checked after a turn, below.
    """
    pane_id, adapter = _start(harness, client, opened)
    assert _lists_command(client, pane_id, adapter.compact_input().text)


def _lists_command(client: HerdrPaneClient, pane_id: str, command: str) -> bool:
    client.send_text(pane_id, command)

    def rows_naming_it() -> int:
        return sum(command.lstrip("/") in row for row in strip_ansi(_screen(client, pane_id)).splitlines())

    return _wait(lambda: rows_naming_it() >= 2, 10.0)


@pytest.mark.parametrize("harness, kind", [
    (Harness.CLAUDE, "id"), (Harness.PI, "path"), (Harness.CODEX, None), (Harness.OPENCODE, None),
])
def test_agent_session_kind_reported_by_herdr_before_a_prompt(
        harness: Harness, kind: str | None, client: HerdrPaneClient, opened: list[str]) -> None:
    """What Herdr reports before any prompt; test_one_turn_then_idle_detection_and_session_kind covers after one."""
    pane_id, _ = _start(harness, client, opened)
    session = herdr_agent_session(pane_id, None)
    assert (session[0] if session else None) == kind


_TURN_PROMPT = "Reply with the single word ok."
# what `herdr agent get` reports as agent_session after one turn; Codex reports none, in a fresh pane and after a turn
_SESSION_KIND_AFTER_A_TURN = {Harness.CLAUDE: "id", Harness.CODEX: None, Harness.PI: "path", Harness.OPENCODE: "id"}


@pytest.mark.parametrize("harness", list(Harness))
def test_one_turn_then_idle_detection_and_session_kind(harness: Harness, client: HerdrPaneClient, opened: list[str]) -> None:
    """The only test that submits a prompt: one trivial turn, then the screen a handoff would really face."""
    pane_id, adapter = _start(harness, client, opened)
    send_typed(client, pane_id, adapter.drain_input(_TURN_PROMPT))
    assert _wait(lambda: "single word ok" in strip_ansi(_screen(client, pane_id)), 30.0), "the prompt was not taken"
    _wait(lambda: _status(pane_id) == "working", 10.0)  # a fast turn may already be over
    assert _wait(lambda: adapter.is_ready_for_input(_screen(client, pane_id), _status(pane_id)), 180.0), \
        f"{harness.value} never went input-ready after the turn:\n{strip_ansi(_screen(client, pane_id))[-1500:]}"
    time.sleep(2.0)  # footer rows settle
    assert adapter.is_ready_for_input(_screen(client, pane_id), "idle")
    session = herdr_agent_session(pane_id, None)
    print(f"MEASURED {harness.value} agent_session after a turn: kind={session[0] if session else None}")
    assert (session[0] if session else None) == _SESSION_KIND_AFTER_A_TURN[harness]
    client.send_text(pane_id, "typed but never submitted")
    assert _wait(lambda: "typed but never" in strip_ansi(_screen(client, pane_id)), 10.0)
    assert not adapter.is_ready_for_input(_screen(client, pane_id), "idle")
    client.send_keys(pane_id, ["ctrl+u"])
    assert _wait(lambda: "typed but never" not in strip_ansi(_screen(client, pane_id)), 10.0)
    assert _lists_command(client, pane_id, adapter.compact_input().text)  # typed, not submitted: no compaction is run

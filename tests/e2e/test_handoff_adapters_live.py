"""Idle detection, exit and compact commands of each handoff adapter, measured in real Herdr panes.

Opt in with ``pytest tests/e2e/test_handoff_adapters_live.py -m herdr_live -v``. It splits a pane off the pane it runs
in (``HERDR_PANE_ID``, ``--no-focus``), starts the installed harness there, types without submitting, and closes every
pane it opened. A harness that is not installed, or that stops at a trust or login dialog, is skipped. The pane's
working directory is ``$HERDR_LIVE_CWD`` or the home directory; Claude Code and Codex ask to trust a folder they have
not seen, so point it at one they already trust to run those two.
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
from herdr_engineering.handoff_adapters.base import TYPE_TO_SUBMIT_DELAY_S, Harness, HarnessAdapter
from herdr_engineering.handoff_adapters.herdr_session import herdr_agent_session
from herdr_engineering.handoff_adapters.screen import strip_ansi
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


@pytest.fixture
def opened(client: HerdrPaneClient) -> Iterator[list[str]]:
    """Ids of the panes this test opened; every one is closed afterwards, even when the test failed."""
    panes: list[str] = []
    try:
        yield panes
    finally:
        for pane_id in panes:
            _herdr("pane", "close", pane_id)


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


def _send(client: HerdrPaneClient, pane_id: str, text: str | None, keys: tuple[str, ...]) -> None:
    if text:
        client.send_text(pane_id, text)
        time.sleep(TYPE_TO_SUBMIT_DELAY_S)  # Codex ignores an Enter sent in the same instant as the text
    if keys:
        client.send_keys(pane_id, [key.lower() for key in keys])


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
    _send(client, pane_id, plan.text, plan.keys)
    ended = _wait(lambda: _foreground_names(pane_id) <= _SHELLS, plan.process_end_timeout_s, step_s=0.1)
    elapsed = time.monotonic() - started
    print(f"MEASURED {harness.value} exit: text={plan.text!r} keys={plan.keys} ended={ended} after {elapsed:.2f}s")
    assert ended, f"{harness.value} still running {plan.process_end_timeout_s}s after {plan.text!r} {plan.keys}"


@pytest.mark.parametrize("harness", [
    Harness.CLAUDE, Harness.CODEX, Harness.PI,
    pytest.param(Harness.OPENCODE, marks=pytest.mark.xfail(
        strict=True, reason="a session-less OpenCode pane does not offer /compact in its slash menu")),
])
def test_compact_command_is_offered_by_the_slash_menu(harness: Harness, client: HerdrPaneClient, opened: list[str]) -> None:
    """The command is typed, not submitted: the menu that opens lists it (Pi drops the slash) as well as the typed row."""
    pane_id, adapter = _start(harness, client, opened)
    command = adapter.compact_input().text
    client.send_text(pane_id, command)

    def rows_naming_it() -> int:
        return sum(command.lstrip("/") in row for row in strip_ansi(_screen(client, pane_id)).splitlines())

    assert _wait(lambda: rows_naming_it() >= 2, 10.0)


@pytest.mark.parametrize("harness, kind", [
    (Harness.CLAUDE, "id"), (Harness.PI, "path"), (Harness.CODEX, None), (Harness.OPENCODE, None),
])
def test_agent_session_kind_reported_by_herdr_before_a_prompt(
        harness: Harness, kind: str | None, client: HerdrPaneClient, opened: list[str]) -> None:
    """Codex and OpenCode report no agent_session until a prompt is submitted, which this test never does."""
    pane_id, _ = _start(harness, client, opened)
    session = herdr_agent_session(pane_id, None)
    assert (session[0] if session else None) == kind

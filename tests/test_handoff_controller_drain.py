"""Drain typing: the pure decision, and the real thing against a real isolated Herdr server and a composer-shaped program."""
from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from herdr_engineering.handoff_adapters import adapter_for
from herdr_engineering.handoff_adapters.base import Harness, SessionRef
from herdr_engineering.handoff_adapters.drain import drain_order
from herdr_engineering.handoff_controller.config import ControllerConfig
from herdr_engineering.handoff_controller.drain import Step, drain, next_step
from herdr_engineering.handoff_controller.herdr_cli import HerdrCli, PromptResult
from herdr_engineering.handoff_subagent_gate import SubagentGate
from tests.handoff_controller_herdr import IsolatedHerdr, isolated_herdr, wait_for

ADAPTER = adapter_for(Harness.CLAUDE)
GATE = SubagentGate(drain_order(False, Harness.CODEX), known=False, live=0)
POLL_S = 0.1


@pytest.fixture(scope="module")
def _server() -> Iterator[IsolatedHerdr]:
    with isolated_herdr("handoff_controller_drain") as herdr:
        yield herdr


@pytest.fixture
def server(_server: IsolatedHerdr) -> IsolatedHerdr:
    _server.submitted.unlink(missing_ok=True)
    return _server


def ref(pane: str) -> SessionRef:
    return SessionRef(Harness.CLAUDE, pane, Path.cwd(), None, None)


def pane_in_state(server: IsolatedHerdr, status: str, *, extra_env: str = "") -> str:
    pane = server.start(extra_env=extra_env)
    assert wait_for(lambda: server.cli.agent_status(pane) == status, 15.0), f"never became {status}"
    return pane


def submitted(server: IsolatedHerdr) -> list[str]:
    return server.submitted.read_text().splitlines() if server.submitted.exists() else []


# --- the decision, no Herdr needed -------------------------------------------------------------------------------

@pytest.mark.parametrize("status", ["working", "blocked", "unknown", None])
@pytest.mark.parametrize("screen_ready", [True, False])
def test_never_types_unless_the_agent_is_idle_or_done(status: str | None, screen_ready: bool) -> None:
    assert next_step(status, screen_ready) is Step.WAIT


@pytest.mark.parametrize("status", ["idle", "done"])
def test_idle_agent_with_an_empty_composer_is_typed_into(status: str) -> None:
    assert next_step(status, True) is Step.TYPE


@pytest.mark.parametrize("status", ["idle", "done"])
def test_idle_agent_with_text_in_the_composer_is_rechecked(status: str) -> None:
    assert next_step(status, False) is Step.CHECK


def test_a_missing_herdr_binary_makes_every_method_return_its_failure_value(tmp_path: Path) -> None:
    herdr = HerdrCli(str(tmp_path / "no-such-herdr"), {})
    assert herdr.agent_status("w1:p1") is None
    assert herdr.wait_idle("w1:p1", 0.1) is False
    assert herdr.read_visible("w1:p1") == ""
    assert herdr.prompt("w1:p1", "x") is PromptResult.FAILED


def test_too_long_order_is_not_attempted(tmp_path: Path) -> None:
    long_gate = SubagentGate("x" * 901, known=False, live=0)
    done = drain(ref("w1:p1"), ADAPTER, long_gate, HerdrCli(str(tmp_path / "none"), {}),
                 ControllerConfig(drain_timeout_s=0.2), poll_s=POLL_S)
    assert done.outcome == "too_long"


def test_an_unreachable_herdr_times_out_without_spinning_or_raising(tmp_path: Path) -> None:
    done = drain(ref("w1:p1"), ADAPTER, GATE, HerdrCli(str(tmp_path / "none"), {}),
                 ControllerConfig(drain_timeout_s=0.3), poll_s=POLL_S)
    assert done.outcome == "timeout"


# --- the real thing -----------------------------------------------------------------------------------------------

def test_types_order_when_idle_and_empty(server: IsolatedHerdr) -> None:
    pane = pane_in_state(server, "idle")
    done = drain(ref(pane), ADAPTER, GATE, server.cli, ControllerConfig(drain_timeout_s=10.0), poll_s=POLL_S)
    assert done.outcome == "typed"
    assert wait_for(lambda: len(submitted(server)) == 1, 5.0)


def test_does_not_type_when_input_has_text(server: IsolatedHerdr) -> None:
    pane = pane_in_state(server, "idle", extra_env="FAKE_TUI_PREFILL=half-written")
    assert not ADAPTER.is_ready_for_input(server.cli.read_visible(pane), "idle")  # the premise: text is in the composer
    done = drain(ref(pane), ADAPTER, GATE, server.cli, ControllerConfig(drain_timeout_s=0.5), poll_s=POLL_S)
    assert done.outcome == "timeout"
    time.sleep(0.3)
    assert submitted(server) == []
    assert "half-written" in server.cli.read_visible(pane)  # the user's draft is untouched


def test_does_not_type_when_blocked(server: IsolatedHerdr) -> None:
    pane = pane_in_state(server, "blocked", extra_env="FAKE_TUI_BLOCKED=1")
    done = drain(ref(pane), ADAPTER, GATE, server.cli, ControllerConfig(drain_timeout_s=0.5), poll_s=POLL_S)
    assert done.outcome == "blocked"
    assert submitted(server) == []


def test_times_out_after_cfg_drain_timeout_s(server: IsolatedHerdr) -> None:
    pane = pane_in_state(server, "idle", extra_env="FAKE_TUI_PREFILL=x")
    done = drain(ref(pane), ADAPTER, GATE, server.cli, ControllerConfig(drain_timeout_s=0.6), poll_s=POLL_S)
    assert done.outcome == "timeout"
    assert 0.6 <= done.waited_s < 3.0


def test_order_is_the_gate_text(server: IsolatedHerdr) -> None:
    pane = pane_in_state(server, "idle")
    assert drain(ref(pane), ADAPTER, GATE, server.cli, ControllerConfig(drain_timeout_s=10.0), poll_s=POLL_S).outcome == "typed"
    assert wait_for(lambda: submitted(server) == [GATE.order_text], 5.0), submitted(server)
    assert GATE.order_text[:40] in server.cli.read_visible(pane)

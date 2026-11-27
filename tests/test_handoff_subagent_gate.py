"""The subagent gate: real registry files under .herdr-test/, no doubles."""
from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

import pytest

from herdr_engineering import handoff_subagent_registry as reg
from herdr_engineering.handoff_adapters.base import Harness
from herdr_engineering.handoff_adapters.drain import drain_order
from herdr_engineering.handoff_subagent_gate import SubagentGate, finish, prepare_drain

TEST_ROOT = Path(__file__).resolve().parents[1] / ".herdr-test" / "subagent-gate"


@pytest.fixture
def env():
    TEST_ROOT.mkdir(parents=True, exist_ok=True)  # one directory per test: pytest-xdist workers share this file's tests
    root = Path(tempfile.mkdtemp(dir=TEST_ROOT))
    yield {"HERDR_ENGINEERING_STATE": str(root), "HERDR_PANE_ID": "%7"}
    shutil.rmtree(root, ignore_errors=True)


def _start(env: dict[str, str], agent_id: str) -> None:
    reg.record("start", json.dumps({"agent_id": agent_id, "agent_type": "Explore"}), env)


def test_prepare_drain_known_zero_uses_known_order(env) -> None:
    _start(env, "a1")
    reg.record("stop", json.dumps({"agent_id": "a1"}), env)
    gate = prepare_drain("%7", Harness.CLAUDE, env)
    assert gate == SubagentGate(order_text=drain_order(True, Harness.CLAUDE), known=True, live=0)


def test_prepare_drain_known_with_live_agents_reports_the_count(env) -> None:
    _start(env, "a1")
    _start(env, "a2")
    gate = prepare_drain("%7", Harness.CLAUDE, env)
    assert (gate.known, gate.live) == (True, 2) and gate.order_text == drain_order(True, Harness.CLAUDE)


def test_prepare_drain_unseen_pane_uses_unknown_order(env) -> None:
    gate = prepare_drain("%7", Harness.CLAUDE, env)
    assert gate == SubagentGate(order_text=drain_order(False, Harness.CLAUDE), known=False, live=0)


def test_non_claude_harness_always_unknown(env) -> None:
    _start(env, "a1")  # registry data exists for the pane, but only Claude emits the hooks
    for harness in (Harness.CODEX, Harness.PI, Harness.OPENCODE):
        gate = prepare_drain("%7", harness, env)
        assert gate == SubagentGate(order_text=drain_order(False, harness), known=False, live=0)


def test_finish_purges(env) -> None:
    _start(env, "a1")
    finish("%7", env)
    assert reg.status("%7", env) == reg.SubagentStatus(known=False, live=0)


def test_finish_on_a_pane_without_a_directory_returns_quietly(env) -> None:
    assert finish("%never-seen", env) is None

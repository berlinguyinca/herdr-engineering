"""Contract tests for NativeHerdrAdapter (spec 0030) using a mock runner."""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import json

import pytest

from herdr_engineering.errors import UnreachableError
from herdr_engineering.herdr_adapter import NativeHerdrAdapter


class FakeRunner:
    """Deterministic fake herdr CLI for contract tests."""
    def __init__(self):
        self.calls = []
    def run(self, args, timeout=30):
        self.calls.append(args)
        cmd = " ".join(args)
        if cmd.startswith("--version"):
            return "herdr 0.9.1\n"
        if cmd.startswith("api schema"):
            return json.dumps({"protocol": 22, "schema_version": 1})
        if cmd.startswith("machine list"):
            return json.dumps([
                {"id": "m1", "label": "fry", "target": "fry", "selected": True},
                {"id": "m2", "label": "beast", "target": "beast"},
            ])
        if cmd.startswith("plugin list"):
            return json.dumps({"result": {"plugins": [{"id": "x"}]}})
        if cmd.startswith("api snapshot"):
            return json.dumps({"result": {"snapshot": {
                "agents": [{"agent": "pi", "agent_status": "working", "cwd": "/r"}],
                "workspaces": [{"workspace_id": "w1", "label": "ws", "number": 1}],
            }}})
        if cmd.startswith("integration status"):
            return "pi: not installed"
        if cmd.startswith("worktree list"):
            return "[]"
        if cmd.startswith("--machine"):
            return "forwarded output"
        return ""


class FailingRunner(FakeRunner):
    def run(self, args, timeout=30):
        raise FileNotFoundError("herdr")


def test_capabilities_negotiation():
    a = NativeHerdrAdapter(runner=FakeRunner())
    caps = a.capabilities(refresh=True)
    assert caps.version == "herdr 0.9.1"
    assert caps.machines is True
    assert caps.snapshot is True
    assert caps.plugins is True
    assert caps.integration_status is True


def test_list_and_resolve_machines():
    a = NativeHerdrAdapter(runner=FakeRunner())
    machines = a.list_machines()
    assert len(machines) == 2
    assert machines[0].label == "fry"
    assert a.resolve_machine("beast").id == "m2"
    assert a.resolve_machine("missing") is None


def test_list_agents_and_workspaces():
    a = NativeHerdrAdapter(runner=FakeRunner())
    agents = a.list_agents()
    assert agents[0].agent == "pi" and agents[0].status == "working"
    ws = a.list_workspaces()
    assert ws[0].workspace_id == "w1" and ws[0].label == "ws"


def test_machine_forwarding():
    a = NativeHerdrAdapter(runner=FakeRunner())
    out = a.machine_targeted("fry", ["status"])
    assert out == "forwarded output"


def test_unreachable_structured_error():
    a = NativeHerdrAdapter(runner=FailingRunner())
    with pytest.raises(UnreachableError):
        a.list_machines()

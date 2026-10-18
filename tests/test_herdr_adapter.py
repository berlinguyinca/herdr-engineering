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


class NativeRunner:
    def __init__(self):
        self.calls = []

    def run(self, args, timeout=30):
        self.calls.append(args)
        if args[-2:] == ["api", "snapshot"]:
            return json.dumps({"ok": True, "result": {"snapshot": {"agents": [{
                "agent": "pi", "agent_status": "working", "pane_id": "w1:p1",
                "terminal_id": "terminal-123", "revision": 7,
                "interactive_ready": False, "launch_pending": True,
                "state_change_seq": 0, "agent_session": {
                    "source": "integration", "agent": "pi", "kind": "path",
                    "value": "/tmp/conversation.jsonl"},
            }]}}})
        return json.dumps({"ok": True, "result": {"submitted": True}})


def test_discovery_preserves_native_identity_and_routes_named_machine_server():
    runner = NativeRunner()
    agent = NativeHerdrAdapter(runner=runner).list_agents(machine="beast", session="engineering")[0]
    assert runner.calls == [["--machine", "beast", "--session", "engineering", "api", "snapshot"]]
    assert agent.agent_session["value"] == "/tmp/conversation.jsonl"
    assert agent.terminal_id == "terminal-123"
    assert agent.revision == 7
    assert agent.interactive_ready is False
    assert agent.launch_pending is True
    assert agent.state_change_seq == 0
    assert agent.to_dict()["machine"] == "beast"
    assert agent.to_dict()["session"] == "engineering"


@pytest.mark.parametrize("target", ["w1:p1", "terminal-123", "name with spaces"])
def test_prompt_uses_explicit_target_and_literal_text(target):
    runner = NativeRunner()
    text = "--wait $(touch /tmp/unsafe); `whoami`\nsecond line"
    result = NativeHerdrAdapter(runner=runner).send_input(target, text, machine="beast", session="eng")
    assert runner.calls == [["--machine", "beast", "--session", "eng", "agent", "prompt", "--", target, text]]
    assert result["status"] == "delivered"
    assert result["mission_progress_verified"] is False


def test_prompt_refuses_missing_target_and_unsuccessful_native_receipt():
    from herdr_engineering.errors import InvalidError
    runner = NativeRunner()
    adapter = NativeHerdrAdapter(runner=runner)
    with pytest.raises(InvalidError):
        adapter.send_input("", "hello")
    assert not runner.calls
    runner.run = lambda *args, **kwargs: '{"ok": false, "error": {"code": "agent_blocked"}}'
    with pytest.raises(InvalidError):
        adapter.send_input("terminal-123", "hello")


def test_discovery_does_not_treat_failed_snapshot_as_empty_fleet():
    from herdr_engineering.errors import InvalidError
    runner = NativeRunner()
    runner.run = lambda *args, **kwargs: '{"ok": false, "error": {"code": "unavailable"}}'
    with pytest.raises(InvalidError):
        NativeHerdrAdapter(runner=runner).list_agents()


def test_discovery_rejects_snapshot_without_inventory():
    from herdr_engineering.errors import InvalidError
    runner = NativeRunner()
    runner.run = lambda *args, **kwargs: '{"ok": true, "result": {"snapshot": {}}}'
    with pytest.raises(InvalidError):
        NativeHerdrAdapter(runner=runner).list_agents()

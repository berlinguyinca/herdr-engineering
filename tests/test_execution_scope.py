from dataclasses import replace
from types import SimpleNamespace

import pytest

from herdr_engineering.execution_scope import (
    ScopeError,
    ScopeReceipt,
    ScopeStartUncertain,
    SystemdExecutionScope,
)
from herdr_engineering.recovery import ExecutionLease


def lease():
    return ExecutionLease("agent", "host", 1, "private-token", True)


def test_scope_stop_checks_cgroup_empty_before_acknowledging(tmp_path):
    sandbox = tmp_path / "sandbox.sh"
    sandbox.touch()
    group = tmp_path / "user.slice" / "test"
    group.mkdir(parents=True)
    events = group / "cgroup.events"
    events.write_text("populated 1\n")
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="ControlGroup=/user.slice/test\nInvocationID=" + "a" * 32 + "\nKillMode=control-group")
    scope = SystemdExecutionScope(sandbox, run=run, cgroup_root=tmp_path)
    receipt = ScopeReceipt("agent", "host", 1, scope.unit_for(lease()), "/user.slice/test", "/work", "a" * 32)
    with pytest.raises(ScopeError, match="populated"):
        scope.stop(lease(), receipt)
    events.write_text("populated 0\n")
    assert scope.stop(lease(), receipt)["stop_verified"] is True
    with pytest.raises(ScopeError, match="identity"):
        scope.stop(replace(lease(), generation=2), receipt)
    assert len(calls) == 4
    with pytest.raises(ScopeError, match="not bound"):
        scope.stop(lease(), replace(receipt, control_group="/user.slice/nonexistent"))
    with pytest.raises(ScopeError, match="identity"):
        scope.stop(replace(lease(), token="different"), receipt)


def test_scope_accounting_does_not_fabricate_missing_measurements(tmp_path):
    sandbox = tmp_path / "sandbox.sh"
    sandbox.touch()
    def run(*args, **kwargs):
        return SimpleNamespace(returncode=0, stdout="ControlGroup=/user.slice/missing\nInvocationID=" + "a" * 32 + "\nKillMode=control-group")
    scope = SystemdExecutionScope(sandbox, cgroup_root=tmp_path, run=run)
    receipt = ScopeReceipt("agent", "host", 1, scope.unit_for(lease()), "/user.slice/missing", "/work", "a" * 32)
    metrics = scope.accounting(receipt)
    assert metrics["cpu_seconds"] is None and metrics["memory_bytes"] is None
    assert metrics["io"] is None and metrics["source_errors"]


def test_start_identity_failure_attempts_cleanup_and_returns_unit_for_reconciliation(tmp_path, monkeypatch):
    from pathlib import Path
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    workspace = tmp_path / "herdr-agents/work/task"
    workspace.mkdir(parents=True)
    sandbox = tmp_path / "sandbox.sh"
    sandbox.touch()
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="invalid identity")
    scope = SystemdExecutionScope(sandbox, run=run)
    with pytest.raises(ScopeStartUncertain) as error:
        scope.start(lease(), workspace=workspace, argv=["true"])
    assert error.value.unit == scope.unit_for(lease())
    assert error.value.cleanup_attempted is True
    assert calls[-1] == ["systemctl", "--user", "stop", scope.unit_for(lease())]

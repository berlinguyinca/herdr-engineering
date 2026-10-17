"""Tests for the one-command fleet onboarding (herdr_engineering/fleet.py)."""
from pathlib import Path
from unittest.mock import patch

from herdr_engineering import fleet


def _fake_repo(tmp_path):
    inv = tmp_path / "ansible" / "inventory" / "hosts.yml"
    inv.parent.mkdir(parents=True)
    inv.write_text(
        "all:\n  children:\n    herdr_fleet:\n      children:\n"
        "        linux:\n          hosts:\n            fry: {}\n"
        "        macos:\n          hosts:\n            mac: {}\n"
    )
    (tmp_path / "ansible" / "playbooks").mkdir(parents=True)
    (tmp_path / "ansible" / "tasks").mkdir(parents=True)
    return tmp_path


@patch("herdr_engineering.fleet._run")
def test_fleet_join_attaches_provisions_verifies(mock_run, tmp_path, monkeypatch):
    repo = _fake_repo(tmp_path)
    monkeypatch.setenv("HERDR_ENGINEERING_REPO", str(repo))

    # _run returns (rc, stdout, stderr) for ansible-playbook invocations.
    mock_run.return_value = (0, "ok", "")

    class FakeAdapter:
        def __init__(self, herdr_bin="herdr"):
            pass
        def add_machine(self, **kw):
            self.called = kw
        def devfabric_list(self):
            return [{"machine_id": "newbox", "target": "newbox"}]

    with patch("herdr_engineering.fleet.NativeHerdrAdapter", FakeAdapter):
        rep = fleet.fleet_join(ssh_target="newbox.tail.ts.net", label="newbox",
                               ansible_host="newbox.tail.ts.net", user="alice")

    assert rep["steps"]["herdr_attach"]["ok"] is True
    assert rep["steps"]["inventory"]["added"] is True
    assert rep["registered"] is True
    assert rep["ok"] is True
    # playbook invoked with the host limit
    join_cmd = mock_run.call_args[0][0]
    assert any("fleet-sync.yml" in str(a) for a in join_cmd)
    assert "-l" in join_cmd and join_cmd[join_cmd.index("-l") + 1] == "newbox"


@patch("herdr_engineering.fleet._run")
def test_fleet_join_attach_failure_aborts(mock_run, tmp_path, monkeypatch):
    repo = _fake_repo(tmp_path)
    monkeypatch.setenv("HERDR_ENGINEERING_REPO", str(repo))

    class FakeAdapter:
        def __init__(self, herdr_bin="herdr"):
            pass
        def add_machine(self, **kw):
            raise RuntimeError("boom")

    with patch("herdr_engineering.fleet.NativeHerdrAdapter", FakeAdapter):
        rep = fleet.fleet_join(ssh_target="x", label="x")
    assert rep["ok"] is False
    assert "herdr attach failed" in rep["errors"][0]
    # provisioning should not run if attach failed
    mock_run.assert_not_called()


@patch("herdr_engineering.fleet._run")
def test_fleet_join_idempotent_inventory(mock_run, tmp_path, monkeypatch):
    repo = _fake_repo(tmp_path)
    monkeypatch.setenv("HERDR_ENGINEERING_REPO", str(repo))
    mock_run.return_value = (0, "ok", "")

    class FakeAdapter:
        def __init__(self, herdr_bin="herdr"):
            pass
        def add_machine(self, **kw):
            pass
        def devfabric_list(self):
            return []

    with patch("herdr_engineering.fleet.NativeHerdrAdapter", FakeAdapter):
        r1 = fleet.fleet_join(ssh_target="newbox", label="newbox")
        r2 = fleet.fleet_join(ssh_target="newbox", label="newbox")
    assert r1["steps"]["inventory"]["added"] is True
    assert r2["steps"]["inventory"]["added"] is False  # already present


def test_repo_root_uses_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HERDR_ENGINEERING_REPO", str(tmp_path))
    assert fleet.repo_root() == tmp_path

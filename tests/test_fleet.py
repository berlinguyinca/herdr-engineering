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
    (tmp_path / ".git").mkdir()
    (tmp_path / "pyproject.toml").write_text("[project]\nname='herdr-engineering'\n")
    return tmp_path


def test_devfabric_list_unwraps_leases():
    from herdr_engineering.herdr_adapter import NativeHerdrAdapter
    ad = NativeHerdrAdapter()
    ad._run_json = lambda *a, **k: {"leases": [{"target": {"machine_id": "beast"}}]}
    assert ad.devfabric_list() == [{"target": {"machine_id": "beast"}}]
    ad._run_json = lambda *a, **k: [{"id": "x"}]
    assert ad.devfabric_list() == [{"id": "x"}]
    ad._run_json = lambda *a, **k: {"leases": []}
    assert ad.devfabric_list() == []


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


@patch("herdr_engineering.fleet._run")
@patch("herdr_engineering.fleet.fleet_status")
def test_fleet_update_pulls_converges_reports(mock_status, mock_run, tmp_path, monkeypatch):
    repo = _fake_repo(tmp_path)
    monkeypatch.setenv("HERDR_ENGINEERING_REPO", str(repo))
    mock_status.return_value = {"rc": 0, "hosts": []}
    mock_run.return_value = (0, "ok", "")

    rep = fleet.fleet_update(host="beast")

    # controller pull + local reinstall + fleet-sync playbook all invoked
    cmds = [c[0][0] for c in mock_run.call_args_list]
    assert any("git" in c and "pull" in c for c in cmds)
    assert any("pip" in c and "install" in c for c in cmds)
    sync_cmd = next(c for c in cmds if any("fleet-sync.yml" in str(a) for a in c))
    assert "-l" in sync_cmd and sync_cmd[sync_cmd.index("-l") + 1] == "beast"
    assert rep["ok"] is True
    assert rep["status"] == {"rc": 0, "hosts": []}


@patch("herdr_engineering.fleet._run")
def test_fleet_update_skips_local_when_flagged(mock_run, tmp_path, monkeypatch):
    repo = _fake_repo(tmp_path)
    monkeypatch.setenv("HERDR_ENGINEERING_REPO", str(repo))
    mock_run.return_value = (0, "ok", "")

    fleet.fleet_update(host=None, pull_controller=False, reinstall_local=False)
    cmds = [c[0][0] for c in mock_run.call_args_list]
    # only the fleet-sync playbook should run
    assert not any("git" in c for c in cmds)
    assert not any("pip" in c for c in cmds)
    assert any(any("fleet-sync.yml" in str(a) for a in c) for c in cmds)


def test_repo_root_uses_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HERDR_ENGINEERING_REPO", str(tmp_path))
    assert fleet.repo_root() == tmp_path

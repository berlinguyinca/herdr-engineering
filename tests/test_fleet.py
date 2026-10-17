"""Tests for the one-command fleet onboarding (herdr_engineering/fleet.py)."""
from unittest.mock import patch

from herdr_engineering import fleet

REVISION = "a" * 40
STATUS = {"rc": 0, "hosts": [{"host": "beast", "revision": REVISION}]}


def _success(cmd, **kwargs):
    if "--porcelain" in cmd:
        return 0, "", ""
    if "--show-current" in cmd:
        return 0, "main", ""
    if "rev-list" in cmd:
        return 0, "0", ""
    if "rev-parse" in cmd:
        return 0, REVISION, ""
    return 0, "ok", ""


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
    mock_status.return_value = STATUS
    mock_run.return_value = (0, "ok", "")

    mock_run.side_effect = _success

    rep = fleet.fleet_update(host="beast")

    # controller pull + local reinstall + fleet-sync playbook all invoked
    cmds = [c[0][0] for c in mock_run.call_args_list]
    assert any("git" in c and "pull" in c for c in cmds)
    assert any("pip" in c and "install" in c for c in cmds)
    sync_cmd = next(c for c in cmds if any("fleet-sync.yml" in str(a) for a in c))
    assert "-l" in sync_cmd and sync_cmd[sync_cmd.index("-l") + 1] == "beast"
    assert rep["ok"] is True
    assert rep["status"] == STATUS


@patch("herdr_engineering.fleet._run")
def test_fleet_update_skips_local_when_flagged(mock_run, tmp_path, monkeypatch):
    repo = _fake_repo(tmp_path)
    monkeypatch.setenv("HERDR_ENGINEERING_REPO", str(repo))
    mock_run.return_value = (0, "ok", "")

    fleet.fleet_update(host=None, pull_controller=False, reinstall_local=False)
    cmds = [c[0][0] for c in mock_run.call_args_list]
    # Local mutations are skipped; read-only revision verification still runs.
    assert not any("pull" in c or "fetch" in c for c in cmds)
    assert not any("pip" in c for c in cmds)
    assert any(any("fleet-sync.yml" in str(a) for a in c) for c in cmds)


def test_repo_root_uses_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HERDR_ENGINEERING_REPO", str(tmp_path))
    assert fleet.repo_root() == tmp_path


def test_update_aborts_on_dirty_controller(tmp_path, monkeypatch):
    repo = _fake_repo(tmp_path)
    monkeypatch.setenv('HERDR_ENGINEERING_REPO', str(repo))
    calls = []
    def run(cmd, **kwargs):
        calls.append(cmd)
        if 'status' in cmd:
            return 0, ' M tracked.py\n', ''
        return 0, '', ''
    monkeypatch.setattr(fleet, '_run', run)
    result = fleet.fleet_update()
    assert not result['ok']
    assert not any('pip' in cmd or 'ansible-playbook' in cmd for cmd in calls)


def test_update_aborts_on_install_failure(tmp_path, monkeypatch):
    repo = _fake_repo(tmp_path)
    monkeypatch.setenv('HERDR_ENGINEERING_REPO', str(repo))
    calls = []
    def run(cmd, **kwargs):
        calls.append(cmd)
        return (1, '', 'installation failed') if 'pip' in cmd else (0, '', '')
    monkeypatch.setattr(fleet, '_run', run)
    result = fleet.fleet_update(pull_controller=False)
    assert not result['ok']
    assert not any('ansible-playbook' in cmd for cmd in calls)


def test_update_refreshes_enabled_local_cleanup(tmp_path, monkeypatch):
    repo = _fake_repo(tmp_path)
    (repo / 'scripts').mkdir()
    (repo / 'scripts/build-cleanup.sh').write_text('#!/bin/sh\n')
    monkeypatch.setenv('HERDR_ENGINEERING_REPO', str(repo))
    from herdr_engineering import config
    monkeypatch.setattr(config, 'load_config', lambda: {'build_cleanup': {'enabled': True, 'roots': ['/projects']}})
    calls = []
    def run(cmd, **kwargs):
        calls.append(cmd)
        return _success(cmd, **kwargs)
    monkeypatch.setattr(fleet, '_run', run)
    monkeypatch.setattr(fleet, 'fleet_status', lambda **kwargs: STATUS)
    result = fleet.fleet_update(pull_controller=False, reinstall_local=False)
    assert result['ok']
    assert any(str(repo / 'scripts/build-cleanup.sh') in cmd for cmd in calls)


def test_update_status_failure_is_not_success(tmp_path, monkeypatch):
    repo = _fake_repo(tmp_path)
    monkeypatch.setenv('HERDR_ENGINEERING_REPO', str(repo))
    monkeypatch.setattr(fleet, '_run', lambda *args, **kwargs: (0, '', ''))
    monkeypatch.setattr(fleet, 'fleet_status', lambda **kwargs: {'rc': 4, 'hosts': []})
    result = fleet.fleet_update(pull_controller=False, reinstall_local=False)
    assert not result['ok']


def test_update_refreshes_installed_timer(tmp_path, monkeypatch):
    repo = _fake_repo(tmp_path)
    (repo / 'scripts').mkdir()
    (repo / 'scripts/fleet-auto-update.sh').write_text('#!/bin/sh\n')
    monkeypatch.setenv('HERDR_ENGINEERING_REPO', str(repo))
    calls = []
    def run(cmd, **kwargs):
        calls.append(cmd)
        return _success(cmd, **kwargs)
    monkeypatch.setattr(fleet, '_run', run)
    monkeypatch.setattr(fleet, 'fleet_status', lambda **kwargs: STATUS)
    result = fleet.fleet_update(pull_controller=False, reinstall_local=False)
    assert result['ok']
    assert any(str(repo / 'scripts/fleet-auto-update.sh') in cmd for cmd in calls)


def test_update_rejects_unpublished_commits(tmp_path, monkeypatch):
    repo = _fake_repo(tmp_path)
    monkeypatch.setenv('HERDR_ENGINEERING_REPO', str(repo))
    calls = []
    def run(cmd, **kwargs):
        calls.append(cmd)
        if '--show-current' in cmd:
            return 0, 'main', ''
        if 'rev-list' in cmd:
            return 0, '1', ''
        return 0, '', ''
    monkeypatch.setattr(fleet, '_run', run)
    result = fleet.fleet_update()
    assert not result['ok']
    assert not any('pip' in cmd or 'ansible-playbook' in cmd for cmd in calls)


def test_update_rejects_reported_revision_mismatch(tmp_path, monkeypatch):
    repo = _fake_repo(tmp_path)
    monkeypatch.setenv('HERDR_ENGINEERING_REPO', str(repo))
    monkeypatch.setattr(fleet, '_run', lambda *args, **kwargs: (0, 'a' * 40, ''))
    monkeypatch.setattr(fleet, 'fleet_status', lambda **kwargs: {
        'rc': 0, 'hosts': [{'host': 'example', 'revision': 'b' * 40}]})
    result = fleet.fleet_update(pull_controller=False, reinstall_local=False)
    assert not result['ok']
    assert result['steps']['revision_parity']['mismatched_hosts'] == ['example']


def test_update_empty_host_report_is_failure(tmp_path, monkeypatch):
    repo = _fake_repo(tmp_path)
    monkeypatch.setenv('HERDR_ENGINEERING_REPO', str(repo))
    monkeypatch.setattr(fleet, '_run', lambda *args, **kwargs: (0, 'a' * 40, ''))
    monkeypatch.setattr(fleet, 'fleet_status', lambda **kwargs: {'rc': 0, 'hosts': []})
    assert not fleet.fleet_update(pull_controller=False, reinstall_local=False)['ok']


def test_update_stops_disabled_local_cleanup(tmp_path, monkeypatch):
    repo = _fake_repo(tmp_path)
    monkeypatch.setenv('HERDR_ENGINEERING_REPO', str(repo))
    from herdr_engineering import config
    monkeypatch.setattr(config, 'load_config', lambda: {'build_cleanup': {'enabled': False, 'roots': []}})
    calls = []
    def run(cmd, **kwargs):
        calls.append(cmd)
        return (0, 'loaded', '') if 'show' in cmd else (0, 'a' * 40, '')
    monkeypatch.setattr(fleet, '_run', run)
    monkeypatch.setattr(fleet, 'fleet_status', lambda **kwargs: {'rc': 0, 'hosts': []})
    fleet.fleet_update(pull_controller=False, reinstall_local=False)
    assert ['systemctl', '--user', 'disable', '--now', 'herdr-eng-build-cleanup.service'] in calls

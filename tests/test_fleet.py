"""Tests for the one-command fleet onboarding (herdr_engineering/fleet.py)."""
import hashlib
from unittest.mock import patch

from herdr_engineering import fleet

REVISION = "a" * 40
GATE_CHECKSUM = hashlib.md5(b"test gate").hexdigest()
STATUS = {"rc": 0, "hosts": [
    {"host": host, "revision": REVISION, "herdr_eng": "herdr-eng 0.1.0",
     "build_gate": GATE_CHECKSUM, "agent": "active"} for host in ["fry", "mac"]]}


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
    (tmp_path / "deploy/pi-build-gate").mkdir(parents=True)
    (tmp_path / "deploy/pi-build-gate/build-gate.ts").write_text("test gate")
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
    mock_status.return_value = {**STATUS, "hosts": STATUS["hosts"][:1]}
    mock_run.return_value = (0, "ok", "")

    mock_run.side_effect = _success

    rep = fleet.fleet_update(host="fry")

    # controller pull + local reinstall + fleet-sync playbook all invoked
    cmds = [c[0][0] for c in mock_run.call_args_list]
    assert any("git" in c and "merge" in c for c in cmds)
    assert any("pip" in c and "install" in c for c in cmds)
    sync_cmd = next(c for c in cmds if any("fleet-sync.yml" in str(a) for a in c))
    assert "-l" in sync_cmd and sync_cmd[sync_cmd.index("-l") + 1] == "fry"
    assert rep["ok"] is True
    assert rep["status"] == mock_status.return_value


@patch("herdr_engineering.fleet._run")
def test_fleet_update_skips_local_when_flagged(mock_run, tmp_path, monkeypatch):
    repo = _fake_repo(tmp_path)
    monkeypatch.setenv("HERDR_ENGINEERING_REPO", str(repo))
    mock_run.side_effect = _success

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
    monkeypatch.setattr(fleet, '_run', _success)
    monkeypatch.setattr(fleet, 'fleet_status', lambda **kwargs: {
        'rc': 0, 'hosts': [{'host': 'example', 'revision': 'b' * 40}]})
    result = fleet.fleet_update(pull_controller=False, reinstall_local=False)
    assert not result['ok']
    assert result['steps']['revision_parity']['mismatched_hosts'] == ['example']


def test_update_empty_host_report_is_failure(tmp_path, monkeypatch):
    repo = _fake_repo(tmp_path)
    monkeypatch.setenv('HERDR_ENGINEERING_REPO', str(repo))
    monkeypatch.setattr(fleet, '_run', _success)
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
        return (0, 'loaded', '') if 'show' in cmd else _success(cmd, **kwargs)
    monkeypatch.setattr(fleet, '_run', run)
    monkeypatch.setattr(fleet, 'fleet_status', lambda **kwargs: {'rc': 0, 'hosts': []})
    fleet.fleet_update(pull_controller=False, reinstall_local=False)
    assert ['systemctl', '--user', 'disable', '--now', 'herdr-eng-build-cleanup.service'] in calls


def test_update_missing_inventory_host_is_failure(tmp_path, monkeypatch):
    repo = _fake_repo(tmp_path)
    monkeypatch.setenv('HERDR_ENGINEERING_REPO', str(repo))
    monkeypatch.setattr(fleet, '_run', _success)
    monkeypatch.setattr(fleet, 'fleet_status', lambda **kwargs: {'rc': 0, 'hosts': [
        {'host': 'fry', 'revision': REVISION, 'herdr_eng': 'herdr-eng 0.1.0',
         'build_gate': GATE_CHECKSUM, 'agent': 'active'}]})
    result = fleet.fleet_update(pull_controller=False, reinstall_local=False)
    assert not result['ok']
    assert result['steps']['revision_parity']['missing_hosts'] == ['mac']


def test_update_missing_components_is_failure(tmp_path, monkeypatch):
    repo = _fake_repo(tmp_path)
    monkeypatch.setenv('HERDR_ENGINEERING_REPO', str(repo))
    monkeypatch.setattr(fleet, '_run', _success)
    monkeypatch.setattr(fleet, 'fleet_status', lambda **kwargs: {'rc': 0, 'hosts': [
        {'host': name, 'revision': REVISION} for name in ['fry', 'mac']]})
    result = fleet.fleet_update(pull_controller=False, reinstall_local=False)
    assert not result['ok']
    assert result['steps']['revision_parity']['invalid_hosts'] == ['fry', 'mac']


def test_update_pins_selected_revision_before_deployment(tmp_path, monkeypatch):
    import json
    repo = _fake_repo(tmp_path)
    monkeypatch.setenv('HERDR_ENGINEERING_REPO', str(repo))
    calls = []
    def run(cmd, **kwargs):
        calls.append(cmd)
        return _success(cmd, **kwargs)
    monkeypatch.setattr(fleet, '_run', run)
    monkeypatch.setattr(fleet, 'fleet_status', lambda **kwargs: STATUS)
    fleet.fleet_update(host='fry', pull_controller=False, reinstall_local=False)
    cmd = next(c for c in calls if 'ansible-playbook' in c)
    variables = json.loads(cmd[cmd.index('-e') + 1])
    assert variables['herdr_engineering_revision'] == REVISION
    assert calls.index(next(c for c in calls if 'rev-parse' in c)) < calls.index(cmd)


def test_stage_pi_returns_verified_source_root(tmp_path, monkeypatch):
    import json
    repo = _fake_repo(tmp_path)
    monkeypatch.setenv('HERDR_ENGINEERING_REPO', str(repo))
    calls = []
    monkeypatch.setattr(fleet, '_run', lambda cmd, **kw: (calls.append(cmd) or (0, '', '')))
    source = '/home/operator/.local/share/herdr-engineering/pi/releases/' + REVISION
    monkeypatch.setattr(fleet, 'fleet_status', lambda **kw: {'rc': 0, 'hosts': [
        {'host': 'fry', 'pi_revision': REVISION, 'pi_path': source}]})
    result = fleet.stage_pi_runtime(REVISION, host='fry')
    assert result['ok']
    assert result['sourceRoot'] == source
    assert json.loads(calls[0][calls[0].index('-e') + 1]) == {'pi_engineering_revision': REVISION}
    assert not any('pull' in cmd or 'pip' in cmd or 'restart' in cmd for cmd in calls)


def test_stage_pi_offline_or_failed_install_is_not_success(tmp_path, monkeypatch):
    repo = _fake_repo(tmp_path)
    monkeypatch.setenv('HERDR_ENGINEERING_REPO', str(repo))
    monkeypatch.setattr(fleet, '_run', lambda cmd, **kw: (4, '', 'host unreachable'))
    monkeypatch.setattr(fleet, 'fleet_status', lambda **kw: {'rc': 4, 'hosts': []})
    result = fleet.stage_pi_runtime(REVISION, host='fry')
    assert not result['ok']
    assert result['sourceRoot'] is None
    assert result['parity']['missing_hosts'] == ['fry']


def test_stage_pi_rejects_host_patterns_and_moving_refs(tmp_path, monkeypatch):
    repo = _fake_repo(tmp_path)
    monkeypatch.setenv('HERDR_ENGINEERING_REPO', str(repo))
    monkeypatch.setattr(fleet, '_run', lambda *a, **kw: (_ for _ in ()).throw(AssertionError('must not run')))
    assert not fleet.stage_pi_runtime('main', host='fry')['ok']
    assert not fleet.stage_pi_runtime(REVISION, host='all')['ok']


def test_update_no_pull_still_refuses_dirty_selected_source(tmp_path, monkeypatch):
    repo = _fake_repo(tmp_path)
    monkeypatch.setenv('HERDR_ENGINEERING_REPO', str(repo))
    calls = []
    def run(cmd, **kw):
        calls.append(cmd)
        return (0, ' M source.py', '') if 'status' in cmd else _success(cmd, **kw)
    monkeypatch.setattr(fleet, '_run', run)
    monkeypatch.setattr(fleet, 'fleet_status', lambda **kw: STATUS)
    result = fleet.fleet_update(pull_controller=False, reinstall_local=False)
    assert not result['ok']
    assert not any('ansible-playbook' in cmd for cmd in calls)


def test_fleet_status_retains_full_gate_and_pi_attestation(tmp_path, monkeypatch):
    repo = _fake_repo(tmp_path)
    monkeypatch.setenv('HERDR_ENGINEERING_REPO', str(repo))
    output = ('"msg": "fry | herdr-eng=herdr-eng 0.1.0 | build-gate=' + 'b' * 32
              + ' | agent=active | revision=' + REVISION + ' | cleanup=inactive | pi-revision='
              + REVISION + ' | pi-path=/home/user/release"')
    monkeypatch.setattr(fleet, '_run', lambda *a, **kw: (0, output, ''))
    result = fleet.fleet_status()
    assert result['hosts'][0]['build_gate'] == 'b' * 32
    assert result['hosts'][0]['pi_revision'] == REVISION
    assert result['hosts'][0]['pi_path'] == '/home/user/release'


def test_update_wrong_build_gate_fails_parity(tmp_path, monkeypatch):
    repo = _fake_repo(tmp_path)
    monkeypatch.setenv('HERDR_ENGINEERING_REPO', str(repo))
    monkeypatch.setattr(fleet, '_run', _success)
    monkeypatch.setattr(fleet, 'fleet_status', lambda **kw: {'rc': 0, 'hosts': [
        {**entry, 'build_gate': '0' * 32} for entry in STATUS['hosts']]})
    result = fleet.fleet_update(pull_controller=False, reinstall_local=False)
    assert not result['ok']
    assert result['steps']['revision_parity']['invalid_hosts'] == ['fry', 'mac']


def test_update_selected_pi_revision_is_required_on_each_host(tmp_path, monkeypatch):
    import json
    repo = _fake_repo(tmp_path)
    monkeypatch.setenv('HERDR_ENGINEERING_REPO', str(repo))
    calls = []
    def run(cmd, **kw):
        calls.append(cmd)
        return _success(cmd, **kw)
    monkeypatch.setattr(fleet, '_run', run)
    monkeypatch.setattr(fleet, 'fleet_status', lambda **kw: STATUS)
    result = fleet.fleet_update(pull_controller=False, reinstall_local=False, pi_revision=REVISION)
    assert not result['ok']
    cmd = next(c for c in calls if 'ansible-playbook' in c)
    assert json.loads(cmd[cmd.index('-e') + 1])['pi_engineering_revision'] == REVISION


def _optional_mac(repo, *, inline=False):
    inv = repo / "ansible" / "inventory"
    if inline:
        (inv / "hosts.yml").write_text(
            "all:\n  children:\n    herdr_fleet:\n      children:\n"
            "        linux:\n          hosts:\n            fry: {}\n"
            "        macos:\n          hosts:\n            mac:\n              herdr_fleet_optional: true\n")
    else:
        (inv / "host_vars").mkdir()
        (inv / "host_vars" / "mac.yml").write_text("herdr_fleet_optional: true\n")


def _fleet_with_mac(monkeypatch, repo, *, mac_up, status_hosts):
    monkeypatch.setenv('HERDR_ENGINEERING_REPO', str(repo))
    calls, limits = [], []
    def run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[0] == 'ansible':
            return (0, 'SUCCESS', '') if mac_up else (4, '', 'unreachable')
        return _success(cmd, **kwargs)
    monkeypatch.setattr(fleet, '_run', run)
    def status(**kwargs):
        limits.append(kwargs.get('host'))
        return {'rc': 0, 'hosts': [h for h in STATUS['hosts'] if h['host'] in status_hosts]}
    monkeypatch.setattr(fleet, 'fleet_status', status)
    return calls, limits


def test_absent_optional_host_does_not_fail_the_update(tmp_path, monkeypatch):
    for inline in (False, True):
        repo = _fake_repo(tmp_path / str(inline))
        _optional_mac(repo, inline=inline)
        calls, limits = _fleet_with_mac(monkeypatch, repo, mac_up=False, status_hosts={'fry'})
        result = fleet.fleet_update(pull_controller=False, reinstall_local=False)
        assert result['ok'], result
        assert result['absent_optional'] == ['mac']
        assert result['steps']['revision_parity']['expected_hosts'] == ['fry']
        sync = next(c for c in calls if c[0] == 'ansible-playbook' and any('fleet-sync.yml' in part for part in c))
        assert sync[sync.index('-l') + 1] == 'fry'
        assert limits == ['fry']


def test_present_optional_host_is_updated_and_required(tmp_path, monkeypatch):
    repo = _fake_repo(tmp_path)
    _optional_mac(repo)
    calls, limits = _fleet_with_mac(monkeypatch, repo, mac_up=True, status_hosts={'fry', 'mac'})
    result = fleet.fleet_update(pull_controller=False, reinstall_local=False)
    assert result['ok'] and result['absent_optional'] == []
    assert not any('-l' in c for c in calls if c[0] == 'ansible-playbook')
    assert limits == [None]
    # ...and if it answered the ping but is then missing from the status, that is a failure.
    calls, limits = _fleet_with_mac(monkeypatch, repo, mac_up=True, status_hosts={'fry'})
    result = fleet.fleet_update(pull_controller=False, reinstall_local=False)
    assert not result['ok'] and result['steps']['revision_parity']['missing_hosts'] == ['mac']


def test_absent_host_that_is_not_optional_still_fails(tmp_path, monkeypatch):
    repo = _fake_repo(tmp_path)
    calls, _ = _fleet_with_mac(monkeypatch, repo, mac_up=False, status_hosts={'fry'})
    result = fleet.fleet_update(pull_controller=False, reinstall_local=False)
    assert not result['ok'] and result['steps']['revision_parity']['missing_hosts'] == ['mac']
    assert not any(c[0] == 'ansible' for c in calls)


def test_optional_absence_does_not_hide_another_hosts_failure(tmp_path, monkeypatch):
    repo = _fake_repo(tmp_path)
    _optional_mac(repo)
    _fleet_with_mac(monkeypatch, repo, mac_up=False, status_hosts=set())
    result = fleet.fleet_update(pull_controller=False, reinstall_local=False)
    assert not result['ok'] and result['steps']['revision_parity']['missing_hosts'] == ['fry']


def test_naming_an_optional_host_explicitly_is_not_tolerated(tmp_path, monkeypatch):
    repo = _fake_repo(tmp_path)
    _optional_mac(repo)
    calls, _ = _fleet_with_mac(monkeypatch, repo, mac_up=False, status_hosts=set())
    result = fleet.fleet_update(host='mac', pull_controller=False, reinstall_local=False)
    assert not result['ok'] and result['absent_optional'] == []
    assert not any(c[0] == 'ansible' for c in calls)


def test_fleet_status_reports_summary_skill_state_without_gating(tmp_path, monkeypatch):
    import hashlib
    repo = _fake_repo(tmp_path)
    (repo / 'skills' / 'summary').mkdir(parents=True)
    (repo / 'skills' / 'summary' / 'SKILL.md').write_text('canonical')
    want = hashlib.sha256(b'canonical').hexdigest()
    monkeypatch.setenv('HERDR_ENGINEERING_REPO', str(repo))
    base = ('"msg": "fry | herdr-eng=herdr-eng 0.1.0 | build-gate=' + 'b' * 32
            + ' | agent=active | revision=' + REVISION + ' | cleanup=inactive | pi-revision='
            + REVISION + ' | pi-path=/p | autospec-revision=x | autospec-cli=y')
    for tail, expected in ((f' | summary={want}"', 'current'), (' | summary=' + 'c' * 64 + '"', 'differs'),
                           (' | summary=MISSING"', 'MISSING'), ('"', 'unknown')):
        monkeypatch.setattr(fleet, '_run', lambda *a, _o=base + tail, **kw: (0, _o, ''))
        host = fleet.fleet_status()['hosts'][0]
        assert host['summary'] == expected
        assert host['build_gate'] == 'b' * 32 and host['pi_path'] == '/p'

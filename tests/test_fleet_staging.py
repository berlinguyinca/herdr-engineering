"""Managed releases preserve work and require real installation provenance."""
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest


def module():
    path = Path(__file__).resolve().parents[1] / 'ansible/files/stage-release.py'
    spec = importlib.util.spec_from_file_location('stage_release', path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


@pytest.fixture
def repository(tmp_path):
    repo = tmp_path / 'upstream'
    repo.mkdir()
    subprocess.run(['git', 'init', '-b', 'main', str(repo)], check=True, capture_output=True)
    for key, value in [('user.name', 'Test'), ('user.email', 'test@example.test')]:
        subprocess.run(['git', '-C', str(repo), 'config', key, value], check=True)
    (repo / 'package.json').write_text(json.dumps({'name': 'pi-engineering-runtime'}))
    (repo / 'package-lock.json').write_text('{}')
    (repo / '.gitignore').write_text('node_modules/\n')
    subprocess.run(['git', '-C', str(repo), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(repo), 'commit', '-m', 'initial'], check=True, capture_output=True)
    sha = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True).strip()
    return repo, sha


def install_ok(cmd, **kwargs):
    if cmd[0] == 'npm':
        (Path(kwargs['cwd']) / 'node_modules').mkdir(exist_ok=True)
        return ''
    return subprocess.check_output(cmd, text=True, stderr=subprocess.STDOUT, **kwargs).strip()


def test_staging_pins_revision_preserves_settings_and_developer_work(repository, tmp_path):
    stage = module()
    repo, sha = repository
    home = tmp_path / 'home'
    settings = home / '.pi/agent/settings.json'
    settings.parent.mkdir(parents=True)
    developer = home / 'IdeaProjects/pi-engineering'
    developer.mkdir(parents=True)
    (developer / 'package.json').write_text('{"name":"pi-engineering-runtime"}')
    (developer / 'notes').write_text('uncommitted work')
    settings.write_text(json.dumps({'theme': 'dark', 'packages': [
        'npm:unrelated', {'source': str(developer), 'extensions': ['!hidden.ts']}]}))
    (repo / 'new-file').write_text('upstream moved')
    subprocess.run(['git', '-C', str(repo), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(repo), 'commit', '-m', 'next'], check=True, capture_output=True)
    result = stage.stage_pi(home, sha, repository=str(repo), runner=install_ok)
    assert result['revision'] == sha
    release = Path(result['sourceRoot'])
    assert not (release / 'new-file').exists()
    assert (developer / 'notes').read_text() == 'uncommitted work'
    assert json.loads(settings.read_text()) == {'theme': 'dark', 'packages': [
        'npm:unrelated', {'source': str(release), 'extensions': ['!hidden.ts']}]}
    assert stage.describe_pi(home, runner=install_ok) == result
    assert stage.stage_pi(home, sha, repository=str(repo), runner=install_ok) == result


def test_install_failure_does_not_register_or_attest(repository, tmp_path):
    stage = module()
    repo, sha = repository
    home = tmp_path / 'home'
    def fail(cmd, **kwargs):
        if cmd[0] == 'npm':
            raise RuntimeError('dependency install failed')
        return install_ok(cmd, **kwargs)
    with pytest.raises(RuntimeError, match='dependency install failed'):
        stage.stage_pi(home, sha, repository=str(repo), runner=fail)
    assert not (home / '.pi/agent/settings.json').exists()
    assert stage.describe_pi(home, runner=install_ok) is None


def test_dirty_managed_release_is_preserved_and_refused(repository, tmp_path):
    stage = module()
    repo, sha = repository
    home = tmp_path / 'home'
    result = stage.stage_pi(home, sha, repository=str(repo), runner=install_ok)
    changed = Path(result['sourceRoot']) / 'package.json'
    changed.write_text('local work')
    with pytest.raises(RuntimeError, match='dirty'):
        stage.stage_pi(home, sha, repository=str(repo), runner=install_ok)
    assert changed.read_text() == 'local work'
    assert stage.describe_pi(home, runner=install_ok) is None


def test_invalid_settings_are_not_overwritten(repository, tmp_path):
    stage = module()
    repo, sha = repository
    home = tmp_path / 'home'
    settings = home / '.pi/agent/settings.json'
    settings.parent.mkdir(parents=True)
    settings.write_text('{not json')
    with pytest.raises(ValueError):
        stage.stage_pi(home, sha, repository=str(repo), runner=install_ok)
    assert settings.read_text() == '{not json'


def test_moving_ref_rejected_without_commands(tmp_path):
    stage = module()
    with pytest.raises(ValueError, match='commit'):
        stage.stage_pi(tmp_path, 'main', runner=lambda *a, **kw: pytest.fail('must not run'))


def test_pi_settings_lock_is_respected(repository, tmp_path):
    stage = module()
    repo, sha = repository
    home = tmp_path / 'home'
    settings = home / '.pi/agent/settings.json'
    settings.parent.mkdir(parents=True)
    settings.write_text('{"theme":"dark"}')
    settings.with_name('settings.json.lock').mkdir()
    with pytest.raises(RuntimeError, match='settings.*locked'):
        stage.stage_pi(home, sha, repository=str(repo), runner=install_ok)
    assert settings.read_text() == '{"theme":"dark"}'
    assert stage.describe_pi(home, runner=install_ok) is None


def test_pi_concurrent_settings_edit_is_preserved(repository, tmp_path):
    stage = module()
    repo, sha = repository
    home = tmp_path / 'home'
    settings = home / '.pi/agent/settings.json'
    settings.parent.mkdir(parents=True)
    settings.write_text('{"theme":"dark"}')
    def editing(cmd, **kwargs):
        if cmd[0] == 'npm':
            settings.write_text('{"theme":"light"}')
        return install_ok(cmd, **kwargs)
    with pytest.raises(RuntimeError, match='settings changed'):
        stage.stage_pi(home, sha, repository=str(repo), runner=editing)
    assert settings.read_text() == '{"theme":"light"}'
    assert stage.describe_pi(home, runner=install_ok) is None


def test_herdr_install_uses_managed_exact_source(repository, tmp_path):
    stage = module()
    repo, sha = repository
    home = tmp_path / 'home'
    developer = home / 'herdr-engineering-repo'
    developer.mkdir(parents=True)
    (developer / 'work').write_text('preserve me')
    calls = []
    def installer(cmd, **kwargs):
        calls.append(cmd)
        if '-c' in cmd:
            return json.dumps({'url': (home / '.local/share/herdr-engineering/herdr/releases' / sha).as_uri(), 'dir_info': {}})
        if '-m' in cmd and ('venv' in cmd or 'pip' in cmd):
            return ''
        return install_ok(cmd, **kwargs)
    result = stage.stage_herdr(home, sha, repository=str(repo), runner=installer)
    assert result['revision'] == sha
    assert result['sourceRoot'].endswith('/herdr/releases/' + sha)
    assert (developer / 'work').read_text() == 'preserve me'
    install = next(c for c in calls if 'pip' in c)
    assert install[-1] == result['sourceRoot']
    assert stage.describe(home, 'herdr', runner=installer) == result


def test_staging_lock_refuses_concurrent_install(repository, tmp_path):
    import fcntl
    stage = module()
    repo, sha = repository
    home = tmp_path / 'home'
    root = home / '.local/share/herdr-engineering/pi'
    root.mkdir(parents=True)
    with (root / 'stage.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            stage.stage_pi(home, sha, repository=str(repo), runner=install_ok)
    assert stage.describe_pi(home, runner=install_ok) is None


def test_herdr_attestation_refuses_replaced_installed_distribution(repository, tmp_path):
    stage = module()
    repo, sha = repository
    home = tmp_path / 'home'
    installed_origin = {}
    def installer(cmd, **kwargs):
        if '-m' in cmd and 'pip' in cmd:
            installed_origin.update({'url': Path(cmd[-1]).as_uri(), 'dir_info': {}})
            return ''
        if '-m' in cmd and 'venv' in cmd:
            return ''
        if '-c' in cmd:
            return json.dumps(installed_origin)
        return install_ok(cmd, **kwargs)
    result = stage.stage_herdr(home, sha, repository=str(repo), runner=installer)
    assert stage.describe(home, 'herdr', runner=installer) == result
    installed_origin.update({'url': 'https://github.com/berlinguyinca/herdr-engineering.git',
                             'vcs_info': {'vcs': 'git', 'commit_id': 'f' * 40}})
    assert stage.describe(home, 'herdr', runner=installer) is None


def test_macos_companion_receives_pinned_release_path():
    import yaml
    root = Path(__file__).resolve().parents[1]
    tasks = yaml.safe_load((root / 'ansible/tasks/fabric-agent.yml').read_text())[0]['block']
    command = next(task['ansible.builtin.command'] for task in tasks
                   if task.get('register') == 'fabric_agent_result')
    # This is a deployment configuration contract. Rendering belongs to Ansible;
    # the Python test suite must run with only the declared project dependencies.
    argv = command['argv']
    assert isinstance(argv, str)
    expression = ' '.join(argv.split())
    assert "herdr_engineering_checkout ~ '/scripts/'" in expression
    assert "('fabric-agent-macos.sh' if ansible_facts['system'] == 'Darwin' else 'fabric-agent.sh')" in expression
    assert expression.endswith(
        "+ (['--repo', herdr_engineering_checkout] if ansible_facts['system'] == 'Darwin' else []) }}")
    checkout = next(task['ansible.builtin.set_fact']['herdr_engineering_checkout']
                    for task in tasks
                    if 'herdr_engineering_checkout' in task.get('ansible.builtin.set_fact', {}))
    assert checkout == "{{ fleet_home }}/.local/share/herdr-engineering/herdr/releases/{{ herdr_engineering_revision }}"


def test_distribution_probe_reads_actual_target_interpreter_metadata(tmp_path):
    import sys
    stage = module()
    home = tmp_path / 'home'
    venv = home / '.venvs/herdr-engineering'
    subprocess.run([sys.executable, '-m', 'venv', '--without-pip', str(venv)], check=True)
    python = venv / 'bin/python'
    site = Path(subprocess.check_output([str(python), '-I', '-c',
        "import sysconfig; print(sysconfig.get_path('purelib'))"], text=True).strip())
    distribution = site / 'herdr_engineering-0.1.0.dist-info'
    distribution.mkdir()
    (distribution / 'METADATA').write_text('Metadata-Version: 2.1\nName: herdr-engineering\nVersion: 0.1.0\n')
    release = home / '.local/share/herdr-engineering/herdr/releases' / ('a' * 40)
    metadata = distribution / 'direct_url.json'
    metadata.write_text(json.dumps({'url': release.as_uri(), 'dir_info': {}}))
    stage.verify_herdr_distribution(home, release, stage.run)
    metadata.write_text(json.dumps({'url': (tmp_path / 'different-source').as_uri(), 'dir_info': {}}))
    with pytest.raises(RuntimeError, match='distribution does not match'):
        stage.verify_herdr_distribution(home, release, stage.run)
    metadata.unlink()
    with pytest.raises(RuntimeError, match='distribution does not match'):
        stage.verify_herdr_distribution(home, release, stage.run)


def test_already_staged_release_is_reused_without_change(repository, tmp_path):
    stage = module()
    repo, sha = repository
    home = tmp_path / 'home'
    (home / '.pi/agent').mkdir(parents=True)
    calls = []

    def runner(cmd, **kwargs):
        calls.append(cmd[0])
        return install_ok(cmd, **kwargs)

    first, changed = stage.stage_or_reuse(home, sha, 'pi', str(repo), runner)
    assert changed is True
    calls.clear()
    again, changed = stage.stage_or_reuse(home, sha, 'pi', str(repo), runner)
    assert changed is False and again == first
    assert 'npm' not in calls  # nothing reinstalled

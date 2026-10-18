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

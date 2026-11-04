"""Exercise the preparation boundary with real Git and a captured Docker context."""
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


def git(source, *args):
    return subprocess.check_output(['git', '-C', str(source), *args], text=True).strip()


@pytest.fixture
def setup_image(tmp_path):
    repo = tmp_path / 'repo'
    control = repo / 'deploy/control-plane'
    control.mkdir(parents=True)
    for name in ('prepare-memory-client.sh', 'Dockerfile.memory-client'):
        original = ROOT / 'deploy/control-plane' / name
        if original.exists():
            shutil.copy(original, control / name)
    (repo / 'lock').mkdir()
    source = tmp_path / 'source'
    source.mkdir()
    git(source, 'init', '-q')
    git(source, 'config', 'user.email', 'test@example.invalid')
    git(source, 'config', 'user.name', 'Test')
    (source / 'src').mkdir()
    (source / 'src/client.py').write_text('LOCKED = True\n')
    (source / 'pyproject.toml').write_text('[project]\nname="test-client"\nversion="0.1.6"\n')
    (source / 'README.md').write_text('locked readme\n')
    for name in ('.env', 'credentials.json', 'tests/test_secret.py', 'models/weights'):
        path = source / name
        path.parent.mkdir(exist_ok=True)
        path.write_text('DO NOT EXPORT')
    git(source, 'add', '.')
    git(source, 'commit', '-qm', 'locked')
    pin = git(source, 'rev-parse', 'HEAD')
    lock = repo / 'lock/upstreams.yaml'
    lock.write_text(f'dependencies:\n  herdr-memory:\n    source: berlinguyinca/herdr-memory\n    revision: {pin}\n')
    binary = tmp_path / 'bin'
    binary.mkdir()
    docker = binary / 'docker'
    docker.write_text('#!/usr/bin/env python3\nimport json, os, pathlib, shutil, sys\n'
                      'pathlib.Path(os.environ["CAPTURE_ARGS"]).write_text(json.dumps(sys.argv[1:]))\n'
                      'shutil.copytree(sys.argv[-1], os.environ["CAPTURE_CONTEXT"])\n'
                      'print("docker build log")\n'
                      'sys.exit(int(os.environ.get("DOCKER_FAILURE", "0")))\n')
    docker.chmod(0o755)
    env = dict(os.environ, PATH=f'{binary}:{os.environ["PATH"]}',
               CAPTURE_ARGS=str(tmp_path / 'args.json'), CAPTURE_CONTEXT=str(tmp_path / 'context'))
    return repo, source, pin, env


def run_prepare(setup_image, *args):
    repo, source, _, env = setup_image
    return subprocess.run(['bash', str(repo / 'deploy/control-plane/prepare-memory-client.sh'),
                           '--source', str(source), *args], env=env, text=True, capture_output=True)


def test_exports_locked_revision_without_changing_dirty_source(setup_image):
    _, source, pin, env = setup_image
    (source / 'src/client.py').write_text('NEW HEAD\n')
    git(source, 'commit', '-qam', 'newer')
    (source / 'src/client.py').write_text('DIRTY\n')
    before = (git(source, 'rev-parse', 'HEAD'), git(source, 'status', '--porcelain'))
    result = run_prepare(setup_image)
    assert result.returncode == 0, result.stderr
    assert result.stdout == f'herdr-memory-client:{pin}\n'
    assert 'docker build log' in result.stderr
    context = Path(env['CAPTURE_CONTEXT'])
    assert {str(p.relative_to(context)) for p in context.rglob('*') if p.is_file()} == {
        'src/client.py', 'pyproject.toml', 'README.md', 'Dockerfile',
    }
    assert (context / 'src/client.py').read_text() == 'LOCKED = True\n'
    args = json.loads(Path(env['CAPTURE_ARGS']).read_text())
    assert args[:3] == ['build', '--tag', f'herdr-memory-client:{pin}']
    assert not Path(args[-1]).exists()  # temporary build context removed
    assert before == (git(source, 'rev-parse', 'HEAD'), git(source, 'status', '--porcelain'))
    assert (source / 'src/client.py').read_text() == 'DIRTY\n'


@pytest.mark.parametrize('revision', ['cc20ad4', 'master', 'a' * 40, ''])
def test_invalid_or_missing_pin_never_builds(setup_image, revision):
    repo, _, _, env = setup_image
    (repo / 'lock/upstreams.yaml').write_text(
        f'dependencies:\n  herdr-memory:\n    source: berlinguyinca/herdr-memory\n    revision: "{revision}"\n')
    result = run_prepare(setup_image)
    assert result.returncode != 0
    assert result.stdout == ''
    assert not Path(env['CAPTURE_ARGS']).exists()


def test_wrong_upstream_never_builds(setup_image):
    repo, _, _, env = setup_image
    lock = repo / 'lock/upstreams.yaml'
    lock.write_text(lock.read_text().replace('berlinguyinca/herdr-memory', 'other/repo'))
    assert run_prepare(setup_image).returncode != 0
    assert not Path(env['CAPTURE_ARGS']).exists()


@pytest.mark.parametrize('link', ['src/leak', 'README.md'])
def test_archive_symlinks_are_rejected_before_build(setup_image, link):
    repo, source, pin, env = setup_image
    path = source / link
    path.unlink(missing_ok=True)
    path.symlink_to('/etc/passwd')
    git(source, 'add', '.')
    git(source, 'commit', '-qm', 'symlink')
    lock = repo / 'lock/upstreams.yaml'
    lock.write_text(lock.read_text().replace(pin, git(source, 'rev-parse', 'HEAD')))
    result = run_prepare(setup_image)
    assert result.returncode != 0
    assert result.stdout == ''
    assert not Path(env['CAPTURE_ARGS']).exists()


def test_build_failure_has_no_success_tag(setup_image):
    setup_image[3]['DOCKER_FAILURE'] = '1'
    result = run_prepare(setup_image)
    assert result.returncode != 0
    assert result.stdout == ''


def test_missing_required_file_never_builds(setup_image):
    repo, source, pin, env = setup_image
    git(source, 'rm', 'README.md')
    git(source, 'commit', '-qm', 'missing readme')
    lock = repo / 'lock/upstreams.yaml'
    lock.write_text(lock.read_text().replace(pin, git(source, 'rev-parse', 'HEAD')))
    result = run_prepare(setup_image)
    assert result.returncode != 0
    assert result.stdout == ''
    assert not Path(env['CAPTURE_ARGS']).exists()


def test_unknown_arguments_never_build(setup_image):
    result = run_prepare(setup_image, '--install')
    assert result.returncode != 0
    assert result.stdout == ''
    assert not Path(setup_image[3]['CAPTURE_ARGS']).exists()


def test_existing_default_cache_needs_no_checkout_or_network(setup_image, tmp_path):
    repo, source, pin, env = setup_image
    cache = tmp_path / 'cache'
    target = cache / 'herdr-engineering/herdr-memory'
    target.parent.mkdir(parents=True)
    shutil.copytree(source, target)
    env['XDG_CACHE_HOME'] = str(cache)
    result = subprocess.run(['bash', str(repo / 'deploy/control-plane/prepare-memory-client.sh')],
                            env=env, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout == f'herdr-memory-client:{pin}\n'


def managed_cache_runner(setup_image, tmp_path):
    repo, source, _, env = setup_image
    env['XDG_CACHE_HOME'] = str(tmp_path / 'cache')
    env['REAL_GIT'] = shutil.which('git')
    env['TEST_GIT_SOURCE'] = str(source)
    env['GIT_EVENTS'] = str(tmp_path / 'git-events.jsonl')
    wrapper = Path(env['CAPTURE_ARGS']).parent / 'bin/git'
    wrapper.write_text('''#!/usr/bin/env python3
import json, os, pathlib, stat, subprocess, sys
args = sys.argv[1:]
with open(os.environ['GIT_EVENTS'], 'a') as events:
    event = {'args': args}
    if args[0] == 'clone':
        target = pathlib.Path(args[-1])
        event['mode'] = stat.S_IMODE(target.stat().st_mode) if target.exists() else None
        event['uid'] = target.stat().st_uid if target.exists() else None
    events.write(json.dumps(event) + '\\n')
if args[0] == 'clone':
    assert args == ['clone', '--mirror', 'git@github.com:berlinguyinca/herdr-memory.git', args[-1]]
    if os.environ.get('FAIL_CLONE_MARKER'):
        marker = pathlib.Path(os.environ['FAIL_CLONE_MARKER'])
        if not marker.exists():
            marker.write_text('failed once')
            (pathlib.Path(args[-1]) / 'partial-object').write_text('incomplete')
            sys.exit(1)
    args[-2] = os.environ['TEST_GIT_SOURCE']
    if os.environ.get('CLONE_BARRIER'):
        import time
        barrier = pathlib.Path(os.environ['CLONE_BARRIER'])
        (barrier / str(os.getpid())).write_text('ready')
        deadline = time.monotonic() + 15
        while len(list(barrier.iterdir())) < 2:
            if time.monotonic() > deadline:
                sys.exit(97)
            time.sleep(0.01)
sys.exit(subprocess.call([os.environ['REAL_GIT'], *args]))
''')
    wrapper.chmod(0o755)
    target = Path(env['XDG_CACHE_HOME']) / 'herdr-engineering/herdr-memory'

    def run():
        return subprocess.run(['bash', str(repo / 'deploy/control-plane/prepare-memory-client.sh')],
                              env=env, text=True, capture_output=True)

    return target, run


def test_new_managed_cache_is_private_before_clone(setup_image, tmp_path):
    target, run = managed_cache_runner(setup_image, tmp_path)
    result = run()
    assert result.returncode == 0, result.stderr
    assert target.stat().st_mode & 0o777 == 0o700
    assert target.stat().st_uid == os.getuid()
    events = [json.loads(line) for line in Path(setup_image[3]['GIT_EVENTS']).read_text().splitlines()]
    assert events[0]['mode'] == 0o700
    assert events[0]['uid'] == os.getuid()


@pytest.mark.parametrize('dangling', [False, True])
def test_managed_cache_symlink_rejected_before_any_git(setup_image, tmp_path, dangling):
    target, run = managed_cache_runner(setup_image, tmp_path)
    target.parent.mkdir(parents=True)
    destination = tmp_path / 'missing' if dangling else setup_image[1]
    before = setup_image[1].stat().st_mode
    target.symlink_to(destination, target_is_directory=True)
    result = run()
    assert result.returncode != 0
    assert result.stdout == ''
    assert target.is_symlink()
    assert setup_image[1].stat().st_mode == before
    assert not Path(setup_image[3]['GIT_EVENTS']).exists()


def test_existing_managed_cache_is_made_private(setup_image, tmp_path):
    target, run = managed_cache_runner(setup_image, tmp_path)
    target.parent.mkdir(parents=True)
    shutil.copytree(setup_image[1], target)
    target.chmod(0o755)
    result = run()
    assert result.returncode == 0, result.stderr
    assert target.stat().st_mode & 0o777 == 0o700


def test_explicit_source_permissions_are_preserved(setup_image):
    source = setup_image[1]
    source.chmod(0o755)
    assert run_prepare(setup_image).returncode == 0
    assert source.stat().st_mode & 0o777 == 0o755


@pytest.mark.skipif(os.geteuid() != 0, reason='changing fixture ownership requires root')
def test_foreign_owned_managed_cache_is_rejected(setup_image, tmp_path):
    target, run = managed_cache_runner(setup_image, tmp_path)
    target.mkdir(parents=True)
    os.chown(target, 65534, -1)
    result = run()
    assert result.returncode != 0
    assert result.stdout == ''
    assert not Path(setup_image[3]['GIT_EVENTS']).exists()


def test_updated_lock_selects_new_client_revision(setup_image):
    repo, source, old_pin, env = setup_image
    project = source / 'pyproject.toml'
    project.write_text(project.read_text().replace('0.1.6', '0.1.7'))
    git(source, 'commit', '-qam', 'next client release')
    new_pin = git(source, 'rev-parse', 'HEAD')
    lock = repo / 'lock/upstreams.yaml'
    lock.write_text(lock.read_text().replace(old_pin, new_pin))
    result = run_prepare(setup_image)
    assert result.returncode == 0, result.stderr
    assert result.stdout == f'herdr-memory-client:{new_pin}\n'
    assert 'version="0.1.7"' in (Path(env['CAPTURE_CONTEXT']) / 'pyproject.toml').read_text()


@pytest.mark.parametrize('explicit_source', [False, True])
def test_dry_run_only_prints_tag_without_git_docker_or_cache(setup_image, tmp_path, explicit_source):
    repo, _, pin, env = setup_image
    target, _ = managed_cache_runner(setup_image, tmp_path)
    args = ['--dry-run']
    if explicit_source:
        args += ['--source', str(tmp_path / 'nonexistent-source')]
    result = subprocess.run(['bash', str(repo / 'deploy/control-plane/prepare-memory-client.sh'), *args],
                            env=env, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout == f'herdr-memory-client:{pin}\n'
    assert result.stderr == ''
    assert not target.parent.parent.exists()
    assert not Path(env['GIT_EVENTS']).exists()
    assert not Path(env['CAPTURE_ARGS']).exists()
    assert not Path(env['CAPTURE_CONTEXT']).exists()
    assert not (tmp_path / 'nonexistent-source').exists()


@pytest.mark.parametrize(('source', 'revision'), [
    ('other/repo', 'a' * 40),
    ('berlinguyinca/herdr-memory', 'cc20ad4'),
    ('berlinguyinca/herdr-memory', 'master'),
    ('berlinguyinca/herdr-memory', ''),
])
def test_dry_run_rejects_invalid_lock_without_side_effects(setup_image, tmp_path, source, revision):
    repo, _, _, env = setup_image
    target, _ = managed_cache_runner(setup_image, tmp_path)
    (repo / 'lock/upstreams.yaml').write_text(
        f'dependencies:\n  herdr-memory:\n    source: {source}\n    revision: "{revision}"\n')
    result = subprocess.run(['bash', str(repo / 'deploy/control-plane/prepare-memory-client.sh'), '--dry-run'],
                            env=env, text=True, capture_output=True)
    assert result.returncode != 0
    assert result.stdout == ''
    assert ('upstream must be' if source != 'berlinguyinca/herdr-memory'
            else 'full immutable') in result.stderr
    assert not target.parent.parent.exists()
    assert not Path(env['GIT_EVENTS']).exists()
    assert not Path(env['CAPTURE_ARGS']).exists()


def test_failed_clone_is_cleaned_and_retry_succeeds(setup_image, tmp_path):
    target, run = managed_cache_runner(setup_image, tmp_path)
    setup_image[3]['FAIL_CLONE_MARKER'] = str(tmp_path / 'clone-failed-once')
    first = run()
    assert first.returncode != 0
    assert first.stdout == ''
    assert not target.exists()
    assert list(target.parent.iterdir()) == []
    second = run()
    assert second.returncode == 0, second.stderr
    assert second.stdout == f'herdr-memory-client:{setup_image[2]}\n'
    assert git(target, 'rev-parse', 'HEAD') == setup_image[2]
    assert set(target.parent.iterdir()) == {target, target.parent / '.herdr-memory-cache.lock'}


def test_two_helpers_publish_one_cache_with_persistent_private_lock(setup_image, tmp_path):
    repo, _, pin, env = setup_image
    target, _ = managed_cache_runner(setup_image, tmp_path)
    barrier = tmp_path / 'barrier'
    barrier.mkdir()
    env['CLONE_BARRIER'] = str(barrier)
    command = ['bash', str(repo / 'deploy/control-plane/prepare-memory-client.sh')]
    processes = []
    try:
        for index in range(2):
            isolated = dict(env, CAPTURE_ARGS=str(tmp_path / f'args-{index}.json'),
                            CAPTURE_CONTEXT=str(tmp_path / f'context-{index}'))
            processes.append(subprocess.Popen(command, env=isolated, text=True,
                                              stdout=subprocess.PIPE, stderr=subprocess.PIPE))
        for process in processes:
            stdout, stderr = process.communicate(timeout=25)
            assert process.returncode == 0, stderr
            assert stdout == f'herdr-memory-client:{pin}\n'
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.communicate()
    assert len(list(barrier.iterdir())) == 2  # both started cloning before publication
    assert git(target, 'rev-parse', 'HEAD') == pin
    lock = target.parent / '.herdr-memory-cache.lock'
    assert lock.stat().st_mode & 0o777 == 0o600
    assert lock.stat().st_uid == os.geteuid()
    before = lock.stat().st_ino
    assert set(target.parent.iterdir()) == {target, lock}
    env.pop('CLONE_BARRIER')
    result = subprocess.run(command, env=env, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert lock.stat().st_ino == before


def test_managed_build_works_without_ctypes(setup_image, tmp_path):
    _, run = managed_cache_runner(setup_image, tmp_path)
    modules = tmp_path / 'portable-python'
    modules.mkdir()
    (modules / 'ctypes.py').write_text('raise ImportError("libc syscall bridge unavailable")\n')
    setup_image[3]['PYTHONPATH'] = str(modules)
    result = run()
    assert result.returncode == 0, result.stderr

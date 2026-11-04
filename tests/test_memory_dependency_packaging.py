"""Offline host bootstrap against canonical locks and temporary committed pin repos."""
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PREPARE = ROOT / 'deploy/control-plane/prepare-memory-dependency.sh'


def run(*args, **kwargs):
    return subprocess.run(args, check=True, capture_output=True, text=True, **kwargs)


def offline_python(venv):
    run(sys.executable, '-m', 'venv', '--without-pip', '--system-site-packages', str(venv))
    python = venv / 'bin/offline-python'
    python.write_text('#!/bin/sh\nshift 3\nexec ' + shlex.quote(str(venv / 'bin/python'))
                      + ' -m pip install --no-index --no-deps --no-build-isolation "$@"\n')
    python.chmod(0o700)
    return python


@pytest.fixture
def pins(tmp_path):
    repo = tmp_path / 'source'
    repo.mkdir()
    run('git', 'init', '-q', str(repo))
    (repo / 'src/fixture_memory').mkdir(parents=True)
    (repo / 'src/fixture_memory/__init__.py').write_text('def main(): print("fixture CLI")\n')
    (repo / 'skill/herdr-memory').mkdir(parents=True)
    (repo / 'skill/herdr-memory/SKILL.md').write_text('first pinned skill')
    (repo / 'pyproject.toml').write_text('''[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"
[project]
name = "fixture-herdr-memory"
version = "0.0.1"
[project.scripts]
herdr-memory = "fixture_memory:main"
[tool.setuptools.packages.find]
where = ["src"]
''')

    def commit():
        run('git', '-C', str(repo), 'add', '.')
        run('git', '-C', str(repo), '-c', 'user.name=Fixture', '-c',
            'user.email=fixture@example.invalid', 'commit', '-qm', 'temporary test pin')
        return run('git', '-C', str(repo), 'rev-parse', 'HEAD').stdout.strip()

    first = commit()
    (repo / 'skill/herdr-memory/SKILL.md').write_text('second pinned skill')
    second = commit()
    venv = tmp_path / 'host'
    python = offline_python(venv)
    user = tmp_path / 'user'
    lock = tmp_path / 'memory.lock.json'
    env = dict(os.environ, HERDR_MEMORY_SOURCE_REPO=str(repo),
               HERDR_MEMORY_LOCK_FILE=str(lock), HERDR_MEMORY_USER_ROOT=str(user),
               PIP_DISABLE_PIP_VERSION_CHECK='1', PYTHONDONTWRITEBYTECODE='1')
    env['HERDR_MEMORY_SKILL_DIR'] = str(user / '.agents/skills/herdr-memory')
    env['HERDR_MEMORY_BIN_DIR'] = str(user / '.local/bin')

    def install(revision, *, python_path=None):
        run('git', '-C', str(repo), 'checkout', '-q', '--detach', revision)
        lock.write_text(json.dumps({'repository': 'git@github.com:berlinguyinca/herdr-memory.git',
                                    'revision': revision}))
        return subprocess.run(['bash', str(PREPARE), '--host-install', str(python_path or python)],
                              env=env, capture_output=True, text=True, timeout=60)

    return repo, user, venv, first, second, install


def test_owned_links_upgrade_and_skill_survives_source_removal(pins):
    repo, user, venv, first, second, install = pins
    result = install(first)
    assert result.returncode == 0, result.stderr
    skill = user / '.agents/skills/herdr-memory'
    binary = user / '.local/bin/herdr-memory'
    assert binary.is_symlink()
    assert binary.resolve() == venv / 'bin/herdr-memory'
    assert (skill / 'SKILL.md').read_text() == 'first pinned skill'
    old_target = os.readlink(skill)
    manifests = list((user / '.local/state').rglob('*manifest.json'))
    assert len(manifests) == 1
    manifest = manifests[0]
    assert manifest.stat().st_mode & 0o777 == 0o600
    assert manifest.parent.stat().st_mode & 0o777 == 0o700
    before = manifest.read_bytes()
    assert install(first).returncode == 0
    assert manifest.read_bytes() == before
    assert install(second).returncode == 0
    assert os.readlink(skill) != old_target
    assert (skill / 'SKILL.md').read_text() == 'second pinned skill'
    repo.rename(repo.with_name('removed-source'))
    assert (skill / 'SKILL.md').read_text() == 'second pinned skill'


@pytest.mark.parametrize('kind', ['file', 'directory', 'symlink', 'dangling'])
def test_unknown_binary_and_skill_are_preserved(pins, kind):
    _, user, _, first, _, install = pins
    paths = [user / '.local/bin/herdr-memory', user / '.agents/skills/herdr-memory']
    for path in paths:
        path.parent.mkdir(parents=True)
        if kind == 'file':
            path.write_text('user owned')
        elif kind == 'directory':
            path.mkdir()
            (path / 'mine').write_text('user owned')
        else:
            target = path.parent / 'other'
            if kind == 'symlink':
                target.write_text('user owned')
            path.symlink_to(target)
    result = install(first)
    assert result.returncode == 0, result.stderr
    for path in paths:
        assert str(path) in result.stderr
        if kind in ('symlink', 'dangling'):
            assert os.readlink(path) == str(path.parent / 'other')
        elif kind == 'directory':
            assert (path / 'mine').read_text() == 'user owned'
        else:
            assert path.read_text() == 'user owned'


def test_modified_owned_links_are_preserved_on_upgrade(pins):
    _, user, _, first, second, install = pins
    assert install(first).returncode == 0
    for path in (user / '.local/bin/herdr-memory', user / '.agents/skills/herdr-memory'):
        path.unlink()
        path.symlink_to(path.parent / 'user-replacement')
    result = install(second)
    assert result.returncode == 0, result.stderr
    for path in (user / '.local/bin/herdr-memory', user / '.agents/skills/herdr-memory'):
        assert os.readlink(path) == str(path.parent / 'user-replacement')
        assert str(path) in result.stderr


def test_owned_cli_link_updates_to_new_host_environment(pins):
    repo, user, _, first, second, install = pins
    assert install(first).returncode == 0
    next_python = offline_python(repo.parent / 'next-host')
    result = install(second, python_path=next_python)
    assert result.returncode == 0, result.stderr
    binary = user / '.local/bin/herdr-memory'
    assert os.readlink(binary) == str(next_python.parent / 'herdr-memory')
    assert run(str(binary)).stdout.strip() == 'fixture CLI'


def test_only_archived_skill_content_is_installed(pins):
    repo, user, _, first, _, install = pins
    (repo / 'skill/herdr-memory/untracked-private-note').write_text('must not ship')
    result = install(first)
    assert result.returncode == 0, result.stderr
    skill = user / '.agents/skills/herdr-memory'
    assert not (skill / 'untracked-private-note').exists()
    assert (skill / 'SKILL.md').read_text() == 'first pinned skill'

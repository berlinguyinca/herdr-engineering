#!/usr/bin/env python3
"""Stage exact fleet releases without touching developer checkouts or Pi processes.

Managed source directories are write-once. A failed install has no installation
receipt and can be retried; an existing modified directory is never reset.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import platform
import re
import subprocess
import tempfile
from pathlib import Path

REPOSITORIES = {
    'pi': 'https://github.com/berlinguyinca/pi-engineering.git',
    'herdr': 'https://github.com/berlinguyinca/herdr-engineering.git',
}


def run(cmd, *, cwd=None):
    result = subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True, timeout=900)
    return result.stdout.strip()


def atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.release-')
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(data, stream, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def validate_revision(revision):
    if not re.fullmatch(r'(?:[0-9a-f]{40}|[0-9a-f]{64})', revision):
        raise ValueError('A full immutable commit ID is required')
    if platform.system() not in ('Linux', 'Darwin'):
        raise RuntimeError('Release staging supports Linux and macOS only')


def verify_checkout(path, revision, runner):
    if runner(['git', '-C', str(path), 'rev-parse', 'HEAD']) != revision:
        raise RuntimeError('Managed release has the wrong commit; refusing overwrite')
    if runner(['git', '-C', str(path), 'status', '--porcelain', '--untracked-files=all']):
        raise RuntimeError('Managed release is dirty; preserving local work')


def checkout(root, revision, repository, runner):
    release = root / 'releases' / revision
    if not release.exists():
        release.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.TemporaryDirectory(dir=release.parent, prefix='.staging-') as temporary:
            source = Path(temporary) / 'source'
            runner(['git', 'init', str(source)])
            runner(['git', '-C', str(source), 'remote', 'add', 'origin', repository])
            runner(['git', '-C', str(source), 'fetch', '--depth', '1', 'origin', revision])
            runner(['git', '-C', str(source), 'checkout', '--detach', 'FETCH_HEAD'])
            verify_checkout(source, revision, runner)
            source.rename(release)
    verify_checkout(release, revision, runner)
    return release


def is_pi_package(entry, home):
    source = entry if isinstance(entry, str) else entry.get('source') if isinstance(entry, dict) else None
    if not isinstance(source, str):
        raise ValueError('Invalid Pi package entry; refusing to rewrite settings')
    if re.fullmatch(r'(?:git:)?(?:https://github.com/|github.com/|git@github.com:)berlinguyinca/pi-engineering(?:\.git)?(?:@[^\s]+)?', source):
        return True
    if source.startswith('npm:pi-engineering-runtime'):
        return source == 'npm:pi-engineering-runtime' or source.startswith('npm:pi-engineering-runtime@')
    if source.startswith(('~/', '/', './', '../')):
        if source.startswith('~/'):
            directory = home / source[2:]
        elif source.startswith('/'):
            directory = Path(source)
        else:
            directory = home / '.pi/agent' / source
        try:
            return json.loads((directory / 'package.json').read_text()).get('name') == 'pi-engineering-runtime'
        except (OSError, ValueError):
            return False
    return False


def registration(home, release):
    settings = home / '.pi/agent/settings.json'
    before = settings.read_bytes() if settings.exists() else None
    value = json.loads(before) if before is not None else {}
    if not isinstance(value, dict) or not isinstance(value.get('packages', []), list):
        raise ValueError('Invalid Pi settings/packages; refusing to overwrite')
    packages = []
    replaced = False
    for entry in value.get('packages', []):
        if is_pi_package(entry, home):
            if replaced:
                raise ValueError('Multiple Pi Engineering registrations require reconciliation')
            packages.append({**entry, 'source': str(release)} if isinstance(entry, dict) else str(release))
            replaced = True
        else:
            packages.append(entry)
    if not replaced:
        packages.append(str(release))
    value['packages'] = packages
    # Avoid overwriting settings that changed while staging. Updaters themselves
    # are serialized by the release lock; unrelated Pi settings remain intact.
    return settings, before, value


def _stage(home, revision, kind, repository, runner):
    validate_revision(revision)
    home = home.expanduser().resolve()
    root = home / '.local/share/herdr-engineering' / kind
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root / 'stage.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        release = root / 'releases' / revision
        pending_settings = registration(home, release) if kind == 'pi' else None
        release = checkout(root, revision, repository, runner)
        receipt = root / 'receipts' / (revision + '.json')
        if kind == 'pi':
            package = json.loads((release / 'package.json').read_text())
            if package.get('name') != 'pi-engineering-runtime':
                raise ValueError('Selected source is not Pi Engineering')
            if not receipt.exists() or not (release / 'node_modules').is_dir():
                runner(['npm', 'ci', '--include=dev', '--no-audit', '--no-fund'], cwd=str(release))
        else:
            venv = home / '.venvs/herdr-engineering'
            if not (venv / 'bin/python').exists():
                runner(['python3', '-m', 'venv', str(venv)])
            runner([str(venv / 'bin/python'), '-m', 'pip', 'install', '--quiet', '--upgrade', str(release)])
        verify_checkout(release, revision, runner)
        result = {'revision': revision, 'sourceRoot': str(release)}
        atomic_json(receipt, result)
        if pending_settings:
            settings, before, value = pending_settings
            settings.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            # Pi's FileSettingsStorage uses proper-lockfile with realpath:false:
            # the atomic settings.json.lock mkdir is its writer exclusion protocol.
            settings_lock = settings.with_name(settings.name + '.lock')
            try:
                settings_lock.mkdir(mode=0o700)
            except FileExistsError as exc:
                raise RuntimeError('Pi settings are locked; retry safely') from exc
            try:
                if (settings.read_bytes() if settings.exists() else None) != before:
                    raise RuntimeError('Pi settings changed during staging; retry safely')
                atomic_json(settings, value)
            finally:
                settings_lock.rmdir()
        atomic_json(root / 'installed.json', result)
        return result


def stage_pi(home, revision, *, repository=REPOSITORIES['pi'], runner=run):
    return _stage(home, revision, 'pi', repository, runner)


def stage_herdr(home, revision, *, repository=REPOSITORIES['herdr'], runner=run):
    return _stage(home, revision, 'herdr', repository, runner)


def describe(home, kind, runner=run):
    home = home.expanduser().resolve()
    root = home / '.local/share/herdr-engineering' / kind
    try:
        result = json.loads((root / 'installed.json').read_text())
        revision = result['revision']
        validate_revision(revision)
        release = root / 'releases' / revision
        if result['sourceRoot'] != str(release):
            return None
        verify_checkout(release, revision, runner)
        if json.loads((root / 'receipts' / (revision + '.json')).read_text()) != result:
            return None
        if kind == 'pi':
            packages = json.loads((home / '.pi/agent/settings.json').read_text()).get('packages', [])
            sources = [entry if isinstance(entry, str) else entry.get('source') for entry in packages]
            if str(release) not in sources or not (release / 'node_modules').is_dir():
                return None
        return result
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError):
        return None


def describe_pi(home, *, runner=run):
    return describe(home, 'pi', runner)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--kind', choices=['pi', 'herdr'], required=True)
    parser.add_argument('--revision')
    parser.add_argument('--home', type=Path, default=Path.home())
    parser.add_argument('--describe', action='store_true')
    args = parser.parse_args()
    if args.describe:
        result = describe(args.home, args.kind)
        print(json.dumps(result or {}))
        return 0 if result else 1
    if not args.revision:
        parser.error('--revision is required for staging')
    result = _stage(args.home, args.revision, args.kind, REPOSITORIES[args.kind], run)
    print(json.dumps(result))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

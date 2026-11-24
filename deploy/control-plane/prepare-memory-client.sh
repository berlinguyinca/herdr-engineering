#!/usr/bin/env bash
# Build the locked CLI only; never install on the host or export client profiles.
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
exec python3 - "$ROOT" "$@" <<'PY'
import fcntl
import io
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile

import yaml


def git(source, *args):
    return subprocess.check_output(
        ['git', '-C', str(source), *args], stderr=subprocess.DEVNULL)


def private_cache_directory(path):
    """Create or secure only our managed directories, without following links."""
    created = False
    try:
        path.mkdir(mode=0o700)
        created = True
    except FileExistsError:
        pass
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
            raise ValueError('managed memory cache must be a user-owned directory')
        os.fchmod(descriptor, 0o700)
    finally:
        os.close(descriptor)
    return created


def publish_cache(staged, destination):
    """Serialize cooperating publishers using a persistent private POSIX lock."""
    lock = destination.parent / '.herdr-memory-cache.lock'
    descriptor = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
            raise ValueError('managed memory cache lock must be a user-owned regular file')
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        if not os.path.lexists(destination):
            os.rename(staged, destination)
    finally:
        # Keep the lock inode: unlinking could let writers lock different files.
        os.close(descriptor)


def main():
    root = Path(sys.argv[1])
    args = sys.argv[2:]
    dry_run = bool(args and args[0] == '--dry-run')
    if dry_run:
        args = args[1:]
    if args and (len(args) != 2 or args[0] != '--source'):
        raise ValueError('usage: prepare-memory-client.sh [--dry-run] [--source PATH]')
    lock = yaml.safe_load((root / 'lock/upstreams.yaml').read_text())
    client = lock['dependencies']['herdr-memory']
    pin = client.get('revision')
    if client.get('source') != 'berlinguyinca/herdr-memory':
        raise ValueError('memory client upstream must be berlinguyinca/herdr-memory')
    if not isinstance(pin, str) or not re.fullmatch(r'[0-9a-f]{40}', pin):
        raise ValueError('memory client revision must be a full immutable 40-character SHA')
    tag = 'herdr-memory-client:' + pin
    if dry_run:
        print(tag)
        return
    remote = 'git@github.com:berlinguyinca/herdr-memory.git'
    if args:
        source = Path(args[1]).expanduser().resolve()
    else:
        source = Path(os.environ.get('XDG_CACHE_HOME', str(Path.home() / '.cache'))) / 'herdr-engineering/herdr-memory'
        source.parent.parent.mkdir(parents=True, exist_ok=True)
        private_cache_directory(source.parent)
        if not os.path.lexists(source):
            # Clone privately off-path. A failure cleans up the incomplete mirror,
            # and cooperating publishers preserve the first completed cache.
            with tempfile.TemporaryDirectory(prefix='.herdr-memory-clone-', dir=source.parent) as temporary:
                staged = Path(temporary)
                print('memory client: caching canonical private SSH upstream', file=sys.stderr)
                subprocess.run(['git', 'clone', '--mirror', remote, str(staged)],
                               check=True, stdout=sys.stderr, stderr=subprocess.DEVNULL)
                publish_cache(staged, source)
        # Reject symlinks/foreign owners and secure the winning cache before Git.
        private_cache_directory(source)
        try:
            git(source, 'cat-file', '-e', pin + '^{commit}')
        except subprocess.CalledProcessError:
            subprocess.run(['git', '-C', str(source), 'fetch', remote, pin],
                           check=True, stdout=sys.stderr, stderr=subprocess.DEVNULL)
    # No checkout/reset/stash: even a dirty source at a different HEAD is read-only.
    if git(source, 'rev-parse', '--verify', pin + '^{commit}').decode().strip() != pin:
        raise ValueError('memory client pin does not resolve to the exact commit')
    archive = git(source, 'archive', '--format=tar', pin, '--', 'src', 'pyproject.toml', 'README.md')
    with tempfile.TemporaryDirectory(prefix='herdr-memory-client-') as temporary:
        context = Path(temporary)
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            members = tar.getmembers()
            for member in members:
                path = PurePosixPath(member.name)
                if (path.is_absolute() or '..' in path.parts
                        or not (member.isfile() or member.isdir())
                        or not path.parts
                        or path.parts[0] not in ('src', 'pyproject.toml', 'README.md')
                        or any(part in ('.git', '.env', 'tests', 'models', 'credentials.json')
                               or part.startswith('.env.') for part in path.parts)):
                    raise ValueError('unsafe or non-client member in pinned archive')
            # Validate every member before writing any files; do not use tar extraction.
            for member in members:
                destination = context / member.name
                if member.isdir():
                    destination.mkdir(parents=True, exist_ok=True)
                else:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with tar.extractfile(member) as incoming, destination.open('wb') as outgoing:
                        shutil.copyfileobj(incoming, outgoing)
        if not (context / 'src').is_dir() or not all(
                (context / name).is_file() for name in ('pyproject.toml', 'README.md')):
            raise ValueError('pinned archive is missing required client files')
        shutil.copyfile(root / 'deploy/control-plane/Dockerfile.memory-client', context / 'Dockerfile')
        print('memory client: building locked CLI image', file=sys.stderr)
        subprocess.run(['docker', 'build', '--tag', tag, str(context)], check=True,
                       stdout=sys.stderr, stderr=sys.stderr)
    print(tag)


try:
    main()
except (OSError, ValueError, KeyError, TypeError, tarfile.TarError,
        yaml.YAMLError, subprocess.CalledProcessError) as error:
    # Do not echo source paths, Git output, lock contents or environment values.
    message = str(error) if isinstance(error, ValueError) else type(error).__name__
    print('memory client preparation failed: ' + message, file=sys.stderr)
    sys.exit(1)
PY

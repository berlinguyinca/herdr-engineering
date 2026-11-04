#!/usr/bin/env bash
# Host-only Git access. stdout is solely the built runtime image name.
# --host-install PY installs the same pin's CLI and shared skill, without hooks.
set -euo pipefail
umask 077
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(git -C "$HERE" rev-parse --show-toplevel)"
HOST_PY=""
if [ "${1:-}" = --host-install ] && [ "$#" = 2 ]; then
  HOST_PY="$2"
elif [ "$#" != 0 ]; then
  echo 'usage: prepare-memory-dependency.sh [--host-install PY]' >&2; exit 2
fi
LOCK="${HERDR_MEMORY_LOCK_FILE:-$ROOT/herdr-memory.lock.json}"
# Accept only the canonical SSH repository and a complete immutable commit ID.
REV="$(python3 - "$LOCK" <<'PY'
import json, re, sys
try:
    data = json.load(open(sys.argv[1]))
    if data.get('repository') != 'git@github.com:berlinguyinca/herdr-memory.git':
        raise ValueError()
    revision = data.get('revision')
    if not isinstance(revision, str) or not re.fullmatch(r'[0-9a-f]{40}', revision):
        raise ValueError()
except (OSError, ValueError, TypeError, AttributeError):
    sys.exit('memory dependency requires a lock with the canonical SSH repository and final full revision')
print(revision)
PY
)"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/herdr-memory.XXXXXXXX")"
trap 'rm -rf "$TMP"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM HUP
SOURCE="${HERDR_MEMORY_SOURCE_REPO:-}"
if [ -z "$SOURCE" ]; then
  CACHE="${HERDR_MEMORY_CACHE_DIR:-${XDG_CACHE_HOME:-$HOME/.cache}/herdr-memory}"
  mkdir -p "$CACHE"
  # Publish a complete immutable checkout atomically. No Linux-only flock
  # command is needed by macOS host installations.
  SOURCE="$CACHE/$REV"
  if [ ! -d "$SOURCE" ]; then
    git clone --quiet --no-checkout git@github.com:berlinguyinca/herdr-memory.git "$TMP/clone" >&2
    git -C "$TMP/clone" fetch --quiet origin "$REV" >&2
    git -C "$TMP/clone" checkout --quiet --detach "$REV" >&2
    python3 - "$TMP/clone" "$SOURCE" <<'PY'
import errno, os, sys
try:
    os.rename(sys.argv[1], sys.argv[2])
except OSError as error:
    if error.errno not in (errno.EEXIST, errno.ENOTEMPTY) or not os.path.isdir(sys.argv[2]):
        raise
PY
  fi
fi
[ "$(git -C "$SOURCE" rev-parse --verify HEAD)" = "$REV" ] || {
  echo 'memory dependency HEAD does not match lock revision' >&2; exit 1;
}
[ -z "$(git -C "$SOURCE" status --porcelain --untracked-files=no)" ] || {
  echo 'memory dependency has tracked changes' >&2; exit 1;
}
# Archive the commit, never the working directory, Git metadata or credentials.
mkdir "$TMP/context"
FILES=(src pyproject.toml)
if git -C "$SOURCE" cat-file -e "$REV:README.md" 2>/dev/null; then FILES+=(README.md); fi
git -C "$SOURCE" archive "$REV" -- "${FILES[@]}" | tar -xf - -C "$TMP/context"
[ -d "$TMP/context/src" ] && [ -f "$TMP/context/pyproject.toml" ] || {
  echo 'memory dependency must contain src and pyproject.toml' >&2; exit 1;
}
# Reject archive symlinks: neither build nor host installation may escape this context.
if [ -n "$(find "$TMP/context" -type l -print -quit)" ]; then
  echo 'memory dependency archive contains symlinks' >&2; exit 1
fi
chmod -R u=rwX,go=rX "$TMP/context"
if [ -n "$HOST_PY" ]; then
  mkdir "$TMP/skill"
  git -C "$SOURCE" archive "$REV" -- skill/herdr-memory | tar -xf - -C "$TMP/skill"
  [ -f "$TMP/skill/skill/herdr-memory/SKILL.md" ] || {
    echo 'memory dependency is missing skill/herdr-memory/SKILL.md' >&2; exit 1;
  }
  [ -z "$(find "$TMP/skill" -type l -print -quit)" ] || exit 1
  "$HOST_PY" -m pip install --quiet "$TMP/context" >&2
  # Staging is an explicit root, never a HOME/CODEX_HOME reassignment.
  USER_ROOT="${HERDR_MEMORY_USER_ROOT:-$HOME}"
  SKILL_DIR="${HERDR_MEMORY_SKILL_DIR:-$USER_ROOT/.agents/skills/herdr-memory}"
  BIN_DIR="${HERDR_MEMORY_BIN_DIR:-$USER_ROOT/.local/bin}"
  python3 - "$USER_ROOT" "$TMP/skill/skill/herdr-memory" "$REV" "$SKILL_DIR" "$BIN_DIR" "$HOST_PY" <<'PYHOST'
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile

root, source, revision, skill, bindir, host_python = sys.argv[1:]
root = Path(root).absolute().resolve()
source, skill, bindir = Path(source), Path(skill).absolute(), Path(bindir).absolute()
state = root / '.local/state/herdr-engineering/memory-install'
cache = root / '.local/share/herdr-engineering/memory-skills'
for path in (state, cache):
    if not path.resolve().is_relative_to(root):
        sys.exit('memory installation state must remain within the installation root')
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.stat().st_uid != os.getuid():
        sys.exit('memory installation state must be user owned')
    path.chmod(0o700)


def tree(path):
    result = {}
    for item in sorted(path.rglob('*')):
        if item.is_symlink() or not (item.is_file() or item.is_dir()):
            raise ValueError('memory skill runtime must contain only regular files/directories')
        if item.is_file():
            result[str(item.relative_to(path))] = hashlib.sha256(item.read_bytes()).hexdigest()
    return result


def atomic_json(path, value):
    data = (json.dumps(value, sort_keys=True, indent=2) + '\n').encode()
    if path.exists() and path.read_bytes() == data:
        return
    fd, temporary = tempfile.mkstemp(prefix='.manifest-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


lock_fd = os.open(state / 'install.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
try:
    info = os.fstat(lock_fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
        sys.exit('unsafe memory installer lock')
    os.fchmod(lock_fd, 0o600)
    fcntl.flock(lock_fd, fcntl.LOCK_EX)
    manifest = state / 'ownership-manifest.json'
    owned = {}
    if manifest.exists() or manifest.is_symlink():
        fd = os.open(manifest, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd) as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                sys.exit('unsafe memory ownership manifest')
            try:
                document = json.load(stream)
            except ValueError:
                sys.exit('invalid memory ownership manifest')
        if not isinstance(document, dict) or document.get('version') != 1:
            sys.exit('invalid memory ownership manifest')
        owned = document.get('links')
        if not isinstance(owned, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in owned.items()):
            sys.exit('invalid memory ownership links')

    # Publish the validated archive, never a link into the mutable source checkout.
    target = cache / revision
    if target.exists() or target.is_symlink():
        if target.is_symlink() or not target.is_dir() or tree(target) != tree(source):
            sys.exit('existing pinned memory skill runtime preserved: collision')
    else:
        temporary = Path(tempfile.mkdtemp(prefix='.skill-', dir=cache))
        try:
            shutil.copytree(source, temporary / 'skill')
            os.rename(temporary / 'skill', target)
        finally:
            shutil.rmtree(temporary)

    binary = Path(host_python).absolute().parent / 'herdr-memory'
    if not binary.is_file() or not os.access(binary, os.X_OK):
        sys.exit('installed memory CLI executable is missing')

    for path, destination in ((skill, target), (bindir / 'herdr-memory', binary)):
        exists = path.exists() or path.is_symlink()
        current = os.readlink(path) if path.is_symlink() else None
        if exists and (current is None or owned.get(str(path)) != current):
            print(f'existing memory link preserved (collision): {path}', file=sys.stderr)
            continue
        expected = str(destination)
        if current != expected:
            path.parent.mkdir(parents=True, exist_ok=True)
            if not exists:
                # Exclusive creation preserves a destination appearing concurrently.
                try:
                    path.symlink_to(destination)
                except FileExistsError:
                    print(f'existing memory link preserved (collision): {path}', file=sys.stderr)
                    continue
            else:
                temporary = Path(tempfile.mkdtemp(prefix='.link-', dir=path.parent))
                try:
                    (temporary / 'link').symlink_to(destination)
                    if not path.is_symlink() or os.readlink(path) != current:
                        print(f'changed memory link preserved (collision): {path}', file=sys.stderr)
                        continue
                    os.replace(temporary / 'link', path)
                finally:
                    shutil.rmtree(temporary)
        owned[str(path)] = expected
    atomic_json(manifest, {'version': 1, 'links': owned})
finally:
    os.close(lock_fd)
PYHOST
else
  IMAGE="herdr-memory-runtime:$REV"
  cp "$HERE/Dockerfile.memory-base" "$TMP/context/Dockerfile"
  docker build -q -t "$IMAGE" "$TMP/context" >&2
  printf '%s\n' "$IMAGE"
fi

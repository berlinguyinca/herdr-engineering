"""Per-workspace Cargo output on the selected storage tier."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import subprocess
import time
import tomllib
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

TARGET_MARKER = ".herdr-cargo-target.json"


def _owned_json(path: Path) -> dict[str, object] | None:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 16384:
            return None
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _alias_record(state: Path, workspace: Path) -> Path:
    key = hashlib.sha256(str(workspace.resolve()).encode()).hexdigest()
    return state / "target-aliases" / f"{key}.json"


def remember_alias(state: Path, workspace: Path, destination: Path) -> None:
    record = _alias_record(state, workspace)
    record.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = record.with_name(record.name + "." + uuid4().hex)
    _write_marker(temporary, {"workspace": str(workspace.resolve()), "destination": str(destination),
                              "owner_uid": os.getuid(), "version": 1})
    os.replace(temporary, record)


def alias_owned(state: Path, workspace: Path) -> bool:
    alias = workspace / "target"
    if not alias.is_symlink():
        return False
    payload = os.readlink(alias)
    destination = Path(os.path.normpath(payload if os.path.isabs(payload) else str(workspace / payload)))
    record = _owned_json(_alias_record(state, workspace))
    if (record and record.get("workspace") == str(workspace.resolve())
            and record.get("destination") == str(destination) and record.get("owner_uid") == os.getuid()):
        return True
    marker = _owned_json(destination / TARGET_MARKER)
    return bool(marker and marker.get("workspace") == str(workspace.resolve())
                and marker.get("owner_uid") == os.getuid() and marker.get("version") == 1)


def _compatibility_alias_locked(state: Path, workspace: Path, destination: Path) -> bool:
    """Keep ./target consumers working; never replace operator directories/links."""
    alias = workspace / "target"
    if alias.exists() or alias.is_symlink():
        if not alias_owned(state, workspace):
            return False
        if os.readlink(alias) == str(destination):
            remember_alias(state, workspace, destination)
            return True
        temporary = alias.with_name(".target.herdr-link-" + uuid4().hex)
        temporary.symlink_to(destination, target_is_directory=True)
        try:
            os.replace(temporary, alias)
        finally:
            temporary.unlink(missing_ok=True)
    else:
        alias.symlink_to(destination, target_is_directory=True)
    remember_alias(state, workspace, destination)
    return True


def compatibility_alias(state: Path, workspace: Path, destination: Path) -> bool:
    record = _alias_record(state, workspace)
    record.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(record.with_suffix(".lock"), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        return _compatibility_alias_locked(state, workspace, destination)
    finally:
        os.close(fd)


def seed_existing_aliases(cache: Path, state: Path) -> int:
    count = 0
    root = cache.parent / "targets"
    if not root.is_dir():
        return count
    for marker in root.glob(f"*/{TARGET_MARKER}"):
        data = _owned_json(marker)
        if not data or not isinstance(data.get("workspace"), str):
            continue
        workspace = Path(str(data["workspace"]))
        if alias_owned(state, workspace) and (workspace / "target").resolve() == marker.parent.resolve():
            remember_alias(state, workspace, marker.parent)
            count += 1
    return count


@contextmanager
def maintenance_lock(state: Path, *, exclusive: bool = False) -> Iterator[None]:
    state.mkdir(parents=True, exist_ok=True)
    fd = os.open(state / "build-maintenance.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        yield
    finally:
        os.close(fd)


def _target_setting(path: Path) -> Path | bool:
    """The ``build.target-dir`` a Cargo config sets (relative to the config's project), True when it
    overrides something unreadable or unresolvable, False when it sets nothing."""
    try:
        config = tomllib.loads(path.read_text())
    except FileNotFoundError:
        return False
    except (OSError, ValueError):
        return True  # Unknown configuration is never overridden.
    if config.get("include"):
        return True
    value = config.get("build", {}).get("target-dir")
    if not value:
        return False
    return Path(value) if isinstance(value, str) and Path(value).is_absolute() else (
        path.parent.parent / value if isinstance(value, str) and path.parent.name == ".cargo" else True)


def target_override(arguments: list[str], env: Mapping[str, str], cwd: Path) -> Path | bool:
    """Where the caller already directs Cargo's output: the directory when it can be resolved, True when
    something overrides it but the directory is unknown (a ``--config`` file), False when nothing does."""
    arguments = arguments[:arguments.index("--")] if "--" in arguments else arguments
    config_value: Path | bool = False
    for index, arg in enumerate(arguments):
        if arg == "--target-dir" and index + 1 < len(arguments):
            return cwd / arguments[index + 1]
        if arg.startswith("--target-dir="):
            return cwd / arg.split("=", 1)[1]
        if arg == "--target-dir":
            return True
        if arg == "--config" and index + 1 < len(arguments):
            value = arguments[index + 1]
        elif arg.startswith("--config="):
            value = arg.split("=", 1)[1]
        else:
            continue
        if "=" not in value:
            config_value = True  # A separately supplied config file may set it, and the last one wins.
            continue
        try:
            directory = tomllib.loads(value).get("build", {}).get("target-dir")
        except ValueError:
            config_value = True
            continue
        if directory:
            config_value = cwd / directory if isinstance(directory, str) else True  # repeated --config: the last wins
    # Cargo's precedence: --target-dir, then --config, then the environment, then config files.
    if config_value:
        return config_value
    for key in ("CARGO_TARGET_DIR", "CARGO_BUILD_TARGET_DIR"):
        if env.get(key):
            return cwd / env[key]
    for directory in (cwd, *cwd.parents):
        for name in ("config", "config.toml"):
            found = _target_setting(directory / ".cargo" / name)
            if found:
                return found
    home = Path(env.get("CARGO_HOME", str(Path.home() / ".cargo")))
    for name in ("config", "config.toml"):
        found = _target_setting(home / name)
        if found:
            return found
    return False


def workspace_root(real_cargo: Path, arguments: list[str], cwd: Path) -> Path:
    arguments = arguments[:arguments.index("--")] if "--" in arguments else arguments
    command = [str(real_cargo)]
    command.extend(arg for arg in arguments[:1] if arg.startswith("+"))
    command.extend(["locate-project", "--workspace", "--message-format", "plain"])
    for index, arg in enumerate(arguments):
        if arg == "--manifest-path" and index + 1 < len(arguments):
            command += ["--manifest-path", arguments[index + 1]]
        elif arg.startswith("--manifest-path="):
            command += ["--manifest-path", arg.split("=", 1)[1]]
    result = subprocess.run(command, cwd=cwd, capture_output=True, text=True, timeout=10)
    if result.returncode:
        raise ValueError("Cannot determine Cargo workspace; target storage was not changed")
    manifest = Path(result.stdout.strip())
    if not manifest.is_file() or manifest.name != "Cargo.toml":
        raise ValueError("Cargo returned an invalid workspace manifest")
    return manifest.resolve().parent


def target_directory(cache: Path, workspace: Path) -> Path:
    identity = hashlib.sha256(str(workspace.resolve()).encode()).hexdigest()[:20]
    label = re.sub(r"[^A-Za-z0-9._-]", "-", workspace.name)[:48] or "workspace"
    return cache.parent / "targets" / f"{label}-{identity}"


def _write_marker(path: Path, payload: dict[str, object]) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as output:
        json.dump(payload, output, sort_keys=True)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())


TOMBSTONE = re.compile(r"\.(?P<name>[^/]+?)\.deleting-[0-9a-f]{12}(?:\.claimed-(?P<pid>[0-9]+))?")


def tombstone_path(target: Path, claimed: bool = False) -> Path:
    """Where a managed target's contents go (same directory, so same filesystem) before they are deleted:
    a half-deleted target can then never be taken for a target (it is collected as a tombstone).
    ``claimed`` names it for this process from the start, so nothing else collects it."""
    grave = target.with_name(f".{target.name}.deleting-{uuid4().hex[:12]}")
    return grave.with_name(f"{grave.name}.claimed-{os.getpid()}") if claimed else grave


def claim_tombstone(grave: Path) -> Path | None:
    """Atomically take a tombstone for this process by renaming it to ``<name>.claimed-<pid>``.

    None when someone else holds it (a live process claimed it, or it is gone). A claim of a dead
    process is taken over, so a crashed deleter never strands a tombstone.
    """
    match = TOMBSTONE.fullmatch(grave.name)
    if match is None:
        return None
    holder = match.group("pid")
    if holder is not None:
        if int(holder) == os.getpid():
            return grave
        try:
            os.kill(int(holder), 0)
            return None  # its deleter is alive
        except ProcessLookupError:
            pass
        except PermissionError:
            return None
    base = grave.name.split(".claimed-")[0]
    claimed = grave.with_name(f"{base}.claimed-{os.getpid()}")
    try:
        os.rename(grave, claimed)
    except FileNotFoundError:
        return None
    return claimed


def _empty_directory(fd: int, deadline: float | None) -> bool:
    with os.scandir(fd) as entries:
        children = [(entry.name, entry.is_dir(follow_symlinks=False)) for entry in entries]
    for name, is_dir in children:
        if deadline is not None and time.monotonic() >= deadline:
            return False
        if not is_dir:
            os.unlink(name, dir_fd=fd)
            continue
        child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
        try:
            finished = _empty_directory(child, deadline)
        finally:
            os.close(child)
        if not finished:
            return False
        os.rmdir(name, dir_fd=fd)
    return True


def remove_tree(path: Path, deadline: float | None = None) -> bool:
    """Delete ``path`` (symlink-safe, descriptor based) until ``deadline`` (``time.monotonic``).

    True when it is gone; False when time ran out first (what is left is a smaller tree, so a later run
    simply continues). OSError propagates.
    """
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        finished = _empty_directory(fd, deadline)
    finally:
        os.close(fd)
    if finished:
        os.rmdir(path)
    return finished


@contextmanager
def _manager_lock(root: Path) -> Iterator[None]:
    fd = os.open(root / ".target-manager.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def prepare_target(cache: Path, workspace: Path) -> Path:
    """The workspace's managed target, created and ownership-checked.

    No lock of ours protects a running build: Cargo flocks ``.cargo-lock`` in the target's profile
    directories for every build, from this wrapper, an IDE or a script alike, and the sweep honours
    exactly those locks. A sweep never moves a lock file or the target's marker (it moves the rest of the
    contents into a tombstone), so a cargo waiting on a lock holds it on the real path and this function
    always sees a whole, owned target.
    """
    if not cache.is_dir():
        raise ValueError("Selected storage disappeared; refusing to create a mount directory")
    target = target_directory(cache, workspace)
    root = target.parent
    if root.is_symlink():
        raise ValueError("Managed target root cannot be a symlink")
    root.mkdir(exist_ok=True, mode=0o700)  # parent must already exist on selected storage
    with _manager_lock(root):
        root_marker = root / ".herdr-target-root.json"
        if not root_marker.exists():
            if any(not p.name.endswith(".lock") for p in root.iterdir()):
                raise ValueError("Existing target root is not managed by Herdr")
            _write_marker(root_marker, {"version": 1, "owner_uid": os.getuid()})
        else:
            metadata = json.loads(root_marker.read_text())
            if (root_marker.is_symlink() or not isinstance(metadata, dict)
                    or metadata.get("version") != 1 or metadata.get("owner_uid") != os.getuid()):
                raise ValueError("Target root ownership does not match")
    with _manager_lock(root):
        if target.is_symlink():
            raise ValueError("Managed target directory cannot be a symlink")
        target.mkdir(exist_ok=True, mode=0o700)
        marker = target / TARGET_MARKER
        expected = {"version": 1, "workspace": str(workspace.resolve()), "owner_uid": os.getuid()}
        if marker.exists():
            if marker.is_symlink() or json.loads(marker.read_text()) != expected:
                raise ValueError("Target directory belongs to a different workspace")
        else:
            if any(target.iterdir()):
                raise ValueError("Existing target directory has no ownership marker")
            _write_marker(marker, expected)
        os.utime(marker, None, follow_symlinks=False)
    return target


# --- Retired shared target pool ---------------------------------------------------------------
# The pool (``shared-targets/<repo-id>/<slot>/target``) is gone: every worktree has one managed
# target. Hosts that already filled a pool keep it swept by build-cleanup until it is idle.


def shared_targets_root(state: Path | None = None) -> Path:
    """Directory of a retired shared target pool (swept, never written)."""
    if state is None:
        from .build_coord import _default_state_dir
        state = _default_state_dir()
    return state / "shared-targets"

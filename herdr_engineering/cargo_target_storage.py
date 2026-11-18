"""Per-workspace Cargo output on the selected storage tier."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import subprocess
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


def _target_setting(path: Path) -> bool:
    try:
        config = tomllib.loads(path.read_text())
    except FileNotFoundError:
        return False
    except (OSError, ValueError):
        return True  # Unknown configuration is never overridden.
    return bool(config.get("include") or config.get("build", {}).get("target-dir"))


def target_override(arguments: list[str], env: Mapping[str, str], cwd: Path) -> bool:
    arguments = arguments[:arguments.index("--")] if "--" in arguments else arguments
    if any(env.get(key) for key in ("CARGO_TARGET_DIR", "CARGO_BUILD_TARGET_DIR")):
        return True
    for index, arg in enumerate(arguments):
        if arg == "--target-dir" or arg.startswith("--target-dir="):
            return True
        if arg == "--config" and index + 1 < len(arguments):
            value = arguments[index + 1]
        elif arg.startswith("--config="):
            value = arg.split("=", 1)[1]
        else:
            continue
        if "=" not in value:
            return True  # A separately supplied config file remains authoritative.
        try:
            if tomllib.loads(value).get("build", {}).get("target-dir"):
                return True
        except ValueError:
            return True
    for directory in (cwd, *cwd.parents):
        if any(_target_setting(directory / ".cargo" / name) for name in ("config", "config.toml")):
            return True
    home = Path(env.get("CARGO_HOME", str(Path.home() / ".cargo")))
    return any(_target_setting(home / name) for name in ("config", "config.toml"))


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


def prepare_target(cache: Path, workspace: Path) -> Path:
    if not cache.is_dir():
        raise ValueError("Selected storage disappeared; refusing to create a mount directory")
    target = target_directory(cache, workspace)
    root = target.parent
    if root.is_symlink():
        raise ValueError("Managed target root cannot be a symlink")
    root.mkdir(exist_ok=True, mode=0o700)  # parent must already exist on selected storage
    fd = os.open(root / ".target-manager.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        root_marker = root / ".herdr-target-root.json"
        if not root_marker.exists():
            if any(p.name != ".target-manager.lock" for p in root.iterdir()):
                raise ValueError("Existing target root is not managed by Herdr")
            _write_marker(root_marker, {"version": 1, "owner_uid": os.getuid()})
        else:
            metadata = json.loads(root_marker.read_text())
            if (root_marker.is_symlink() or not isinstance(metadata, dict)
                    or metadata.get("version") != 1 or metadata.get("owner_uid") != os.getuid()):
                raise ValueError("Target root ownership does not match")
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
    finally:
        os.close(fd)


# --- Shared per-repo/toolchain target pool ---------------------------------------------------
SLOT_LOCK = ".herdr-slot.lock"


def shared_targets_root(state: Path | None = None) -> Path:
    """Directory holding every shared target pool (``<repo-id>/<toolchain>-<slot>/target``)."""
    if state is None:
        from .build_coord import _default_state_dir
        state = _default_state_dir()
    return state / "shared-targets"


def repo_identity(workspace: Path) -> str:
    """Stable id shared by every worktree of one repository (its git common directory)."""
    result = subprocess.run(["git", "-C", str(workspace), "rev-parse", "--git-common-dir"],
                            capture_output=True, text=True, timeout=10, check=False)
    common = result.stdout.strip()
    key = (Path(common) if Path(common).is_absolute() else workspace / common) if (
        result.returncode == 0 and common) else workspace
    key = key.resolve()
    label_source = key.parent if key.name == ".git" else key
    label = re.sub(r"[^A-Za-z0-9._-]", "-", label_source.name)[:48] or "repo"
    return f"{label}-{hashlib.sha256(str(key).encode()).hexdigest()[:12]}"


def toolchain_identity(version_text: str) -> str:
    return hashlib.sha256(version_text.encode()).hexdigest()[:12]


def _toolchain_cache_key(real: Path, argv: list[str], cwd: Path) -> str:
    """Everything that decides which toolchain a cargo invocation resolves to (rustup proxy rules)."""
    stat = real.resolve().stat()
    parts = [str(real.resolve()), str(stat.st_mtime_ns), str(stat.st_size),
             *[a for a in argv[:1] if a.startswith("+")], os.environ.get("RUSTUP_TOOLCHAIN", "")]
    for directory in (cwd.resolve(), *cwd.resolve().parents):
        for name in ("rust-toolchain.toml", "rust-toolchain"):
            candidate = directory / name
            if candidate.is_file():
                parts += [str(candidate), candidate.read_text(errors="replace")]
                break
        else:
            continue
        break
    rustup_settings = Path(os.environ.get("RUSTUP_HOME", Path.home() / ".rustup")) / "settings.toml"
    if rustup_settings.is_file():
        parts.append(str(rustup_settings.stat().st_mtime_ns))
    return hashlib.sha256("\0".join(parts).encode()).hexdigest()[:20]


def cached_toolchain_identity(real: Path, argv: list[str], cwd: Path, state: Path) -> str:
    """Toolchain id of ``cargo -vV``, spawning cargo only when what selects the toolchain changed."""
    cache = state / f"toolchain-{_toolchain_cache_key(real, argv, cwd)}.json"
    try:
        return str(json.loads(cache.read_text())["identity"])
    except (OSError, ValueError, KeyError, TypeError):
        pass
    version = subprocess.run([str(real), *[a for a in argv[:1] if a.startswith("+")], "-vV"],
                             cwd=cwd, capture_output=True, text=True, timeout=30, check=True).stdout
    identity = toolchain_identity(version)
    try:
        cache.write_text(json.dumps({"identity": identity}))
    except OSError:
        pass  # cache only: the next build asks cargo again
    return identity


def slot_name(toolchain_id: str, workspace: Path, index: int) -> str:
    """Pool slot directory of one worktree: slots are owned by a worktree, never shared between them."""
    owner = hashlib.sha256(str(workspace.resolve()).encode()).hexdigest()[:10]
    return f"{toolchain_id}-{owner}-{index}"


def acquire_shared_target(root: Path, repo_id: str, toolchain_id: str, pool_size: int,
                          workspace: Path) -> tuple[Path, int]:
    """Pick one of this worktree's pool slots and hold its lock while the returned descriptor is open.

    Slots are owned by a worktree (the name embeds its path hash), so two worktrees, which may
    be on different branches, never write the same final artifacts. The lowest free slot of the
    worktree wins, so sequential builds reuse slot 0 while concurrent builds spill to the next
    slot instead of queuing behind one cargo lock; when every slot is busy the last one is used
    (cargo itself serialises the build). The descriptor is inheritable so the lock survives
    ``exec`` into cargo and is released when the build exits; cleanup treats a held lock as an
    active build.
    """
    if root.is_symlink():
        raise ValueError("Shared target root cannot be a symlink")
    parent = root / repo_id
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    chosen: tuple[Path, int] | None = None
    for index in range(pool_size):
        slot = parent / slot_name(toolchain_id, workspace, index)
        slot.mkdir(exist_ok=True, mode=0o700)
        fd = os.open(slot / SLOT_LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            continue
        chosen = (slot / "target", fd)
        break
    if chosen is None:
        slot = parent / slot_name(toolchain_id, workspace, pool_size - 1)
        fd = os.open(slot / SLOT_LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        chosen = (slot / "target", fd)  # unlocked: another build owns the slot lock
    os.set_inheritable(chosen[1], True)
    return chosen

"""Verified migration of idle Cargo output; source checkouts never move."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from .cargo_target_storage import (
    TARGET_MARKER,
    maintenance_lock,
    prepare_target,
    remember_alias,
    target_directory,
)


@dataclass(frozen=True)
class Entry:
    kind: str
    size: int = 0
    digest: str = ""
    link: str = ""
    group: str = ""
    mode: int = 0


def tree_manifest(root: Path) -> dict[str, Entry]:
    """Hash actual content, preserve hardlink equivalence, and never follow links."""
    result: dict[str, Entry] = {}
    seen: dict[tuple[int, int], tuple[str, str]] = {}
    pending = [root]
    device = root.stat().st_dev
    canonical_root = root.resolve()
    while pending:
        path = pending.pop()
        name = path.relative_to(root).as_posix()
        info = path.lstat()
        if len(result) > 500_000 or info.st_uid != os.getuid() or info.st_dev != device:
            raise ValueError("Target inspection limit, ownership, or mount boundary failed")
        if path != root and path.name in {"Cargo.toml", ".git"}:
            raise ValueError("Source/repository marker inside generated output; preserving it")
        if stat.S_ISLNK(info.st_mode):
            payload = os.readlink(path)
            lexical = Path(os.path.normpath(payload if os.path.isabs(payload) else str(path.parent / payload)))
            if not lexical.is_relative_to(canonical_root) or not path.resolve().is_relative_to(canonical_root):
                raise ValueError("Target contains an escaping symlink; preserving it")
            result[name] = Entry("link", link=payload)
        elif stat.S_ISDIR(info.st_mode):
            result[name] = Entry("directory", mode=stat.S_IMODE(info.st_mode))
            pending.extend(sorted(path.iterdir(), reverse=True))
        elif stat.S_ISREG(info.st_mode):
            identity = (info.st_dev, info.st_ino)
            if identity not in seen:
                digest = hashlib.sha256()
                with path.open("rb") as stream:
                    for data in iter(lambda: stream.read(8 * 1024 * 1024), b""):
                        digest.update(data)
                seen[identity] = (name, digest.hexdigest())
            group, digest_text = seen[identity]
            result[name] = Entry("file", info.st_size, digest_text, group=group,
                                 mode=stat.S_IMODE(info.st_mode))
        else:
            raise ValueError("Special file inside target; preserving it")
    return result


def process_paths() -> tuple[set[Path], set[Path]]:
    """Native open-file and compiler-CWD inspection; failure blocks migration."""
    result = subprocess.run(["lsof", "-n", "-w", "-a", "-u", str(os.getuid()), "-Fpcfn"],
                            capture_output=True, text=True, timeout=20)
    if result.returncode != 0:
        raise ValueError("Native process inspection unavailable; migration was not attempted")
    paths: set[Path] = set()
    compilers: set[Path] = set()
    command, descriptor = "", ""
    for line in result.stdout.splitlines():
        if line.startswith("c"):
            command = Path(line[1:]).name
        elif line.startswith("f"):
            descriptor = line[1:]
        elif line.startswith("n/"):
            # lsof already supplies absolute names. Resolving every unrelated
            # descriptor can block on remote CoreDevice/FUSE filesystems.
            path = Path(os.path.normpath(line[1:].removesuffix(" (deleted)")))
            paths.add(path)
            if descriptor == "cwd" and command in {
                "cargo", "rustc", "rustup", "clippy-driver", "rust-analyzer", "clang", "cc1", "ld", "lld",
            }:
                compilers.add(path)
    return paths, compilers


def assert_idle(source: Path, workspace: Path) -> None:
    paths, compilers = process_paths()
    canonical = source.resolve()
    if any(path == canonical or path.is_relative_to(canonical) for path in paths):
        raise ValueError("Target has open files; preserving active output")
    if any(path == workspace or path.is_relative_to(workspace) for path in compilers):
        raise ValueError("Workspace has an active compiler; preserving output")


def recognized_source(source: Path, workspace: Path) -> None:
    if (source.name != "target" or source.is_symlink() or not source.is_dir()
            or source.parent.resolve() != workspace.resolve()
            or not (workspace / "Cargo.toml").is_file()
            or not any((source / marker).exists() for marker in
                       (".rustc_info.json", "debug/.fingerprint", "release/.fingerprint"))):
        raise ValueError("Not a recognized default Cargo target directory")
    tracked = subprocess.run(["git", "-C", str(workspace), "ls-files", "--", "target"],
                             capture_output=True, timeout=15)
    if tracked.returncode or tracked.stdout:
        raise ValueError("Git could not verify target output is untracked")


def migrate_target(source: Path, cache: Path, workspace: Path, state: Path, *,
                   quiescent: bool = False) -> dict[str, object]:
    """Copy, verify, link and reclaim under the build-maintenance exclusive lock."""
    if not quiescent:
        raise ValueError("Pause uncoordinated Cargo/IDE clients before publishing a migration")
    workspace = workspace.resolve()
    with maintenance_lock(state, exclusive=True):
        recognized_source(source, workspace)
        assert_idle(source, workspace)
        before = tree_manifest(source)
        source_inode = source.stat().st_ino
        destination = prepare_target(cache, workspace)
        if any(path.name != TARGET_MARKER for path in destination.iterdir()):
            # Never merge old fingerprints into a newer managed target tree.
            destination = destination.with_name(destination.name + "-legacy-" + uuid4().hex[:12])
        stage = destination.with_name(destination.name + ".copying-" + uuid4().hex)
        stage.mkdir(mode=0o700)
        backup = source.with_name(".target.herdr-backup-" + uuid4().hex)
        published = False
        try:
            subprocess.run(["rsync", "-aH", str(source) + "/", str(stage) + "/"],
                           check=True, timeout=3600)
            if tree_manifest(stage) != before or tree_manifest(source) != before:
                raise ValueError("Target copy/checksum verification failed; original preserved")
            assert_idle(source, workspace)
            if not cache.is_dir() or cache.stat().st_dev != stage.stat().st_dev:
                raise ValueError("Selected storage changed during copy; original preserved")
            marker = stage / TARGET_MARKER
            if marker.exists():
                marker.unlink()
            marker.write_text(json.dumps({"version": 1, "workspace": str(workspace),
                                          "owner_uid": os.getuid()}, sort_keys=True) + "\n")
            if destination.exists():
                if list(destination.iterdir()) != [destination / TARGET_MARKER]:
                    raise ValueError("Destination became active; original preserved")
                (destination / TARGET_MARKER).unlink()
                destination.rmdir()
            os.rename(stage, destination)
            source.rename(backup)
            try:
                source.symlink_to(destination, target_is_directory=True)
            except OSError:
                if not source.exists() and not source.is_symlink():
                    backup.rename(source)
                raise
            published = True
            remember_alias(state, workspace, destination)
            # Existing uncoordinated clients are checked again after publication.
            paths, _compilers = process_paths()
            if any(path == backup or path.is_relative_to(backup) for path in paths):
                return {"source": str(source), "destination": str(destination),
                        "status": "linked_backup_in_use", "backup": str(backup)}
            if backup.stat().st_ino != source_inode or tree_manifest(backup) != before:
                return {"source": str(source), "destination": str(destination),
                        "status": "linked_backup_changed", "backup": str(backup)}
            shutil.rmtree(backup)
            return {"source": str(source), "destination": str(destination), "status": "moved_verified",
                    "files": sum(entry.kind == "file" for entry in before.values()),
                    "bytes": sum(entry.size for name, entry in before.items()
                                 if entry.kind == "file" and entry.group == name)}
        finally:
            if stage.exists():
                shutil.rmtree(stage)
            if not published and backup.exists() and not source.exists():
                backup.rename(source)


def planned_destination(cache: Path, workspace: Path) -> Path:
    return target_directory(cache, workspace)


def migration_run(roots: list[Path], *, apply: bool = False, quiescent: bool = False) -> dict[str, object]:
    from .build_cache_location import select_storage
    from .build_cleanup import _discover
    from .build_coord import _default_state_dir
    from .cargo_host import _real_cargo
    from .cargo_target_storage import target_override, workspace_root
    from .cli import _build_cache_config
    if not roots:
        raise ValueError("Pass explicit project roots with --root")
    if apply and not quiescent:
        raise ValueError("Pause Cargo/IDE clients and pass --quiescent before publication")
    config = _build_cache_config()
    if config is None:
        raise ValueError("Enabled compiler-cache storage configuration is required")
    chosen = select_storage(config.local.storage_tiers, _default_state_dir() / "sccache",
                            config.local.min_free_disk_percent)
    if chosen.directory is None:
        raise ValueError("No eligible storage tier")
    real = _real_cargo(Path.home())
    items: list[dict[str, object]] = []
    for root in roots:
        for source in _discover(root.expanduser().resolve()):
            try:
                workspace = workspace_root(real, [], source.parent)
                recognized_source(source, workspace)
                if target_override([], os.environ, workspace):
                    raise ValueError("Explicit target setting remains authoritative")
                if source.stat().st_dev == chosen.directory.stat().st_dev:
                    raise ValueError("Destination is on the same filesystem; no space would be recovered")
                assert_idle(source, workspace)
                if apply:
                    items.append(migrate_target(source, chosen.directory, workspace, _default_state_dir(),
                                                quiescent=quiescent))
                else:
                    items.append({"source": str(source), "destination": str(target_directory(chosen.directory, workspace)),
                                  "status": "eligible"})
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                items.append({"source": str(source), "status": "preserved", "reason": str(error)})
    return {"dry_run": not apply, "targets": items}

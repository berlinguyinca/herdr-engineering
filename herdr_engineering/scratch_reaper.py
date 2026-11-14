"""Bounded retention for verified generated output in agent scratch trees.

Session liveness protects session data, not independently identified idle build
components. Sources, diagnostics, active processes and unverifiable state win
against every retention or pressure limit. Dry run is the default.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import plistlib
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import NoReturn, cast
from xml.parsers.expat import ExpatError

from .scratch_safety import (
    InspectionUnavailable,
    Snapshot,
    TreeInfo,
    inspect_tree,
    keep_ancestor,
    open_parent,
    process_snapshot,
    repository_reason,
    tracked_reason,
)

_GIB = 2**30
_UUID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
_XCODE = {
    "Build",
    "CompilationCache.noindex",
    "Index.noindex",
    "ModuleCache.noindex",
    "SDKExplicitPrecompiledModules",
    "SDKStatCaches.noindex",
    "SourcePackages",
}
_PRUNE = {".git", "node_modules", ".venv", "venv", "__pycache__"}
_DETAILS = 200


class RunStatus(StrEnum):
    OK = "ok"
    DRY_RUN = "dry_run"
    BUSY = "busy"
    ERROR = "error"
    PRESSURE_BLOCKED = "pressure_blocked"


class Kind(StrEnum):
    XCODE = "derived_data"
    CARGO = "cargo"
    SWIFT = "swiftpm"
    SESSION = "session"


@dataclass(frozen=True)
class Policy:
    ttl_seconds: float = 6 * 3600
    idle_seconds: float = 30 * 60
    max_bytes: int = 32 * _GIB
    min_free_bytes: int = 64 * _GIB
    session_ttl_seconds: float = 48 * 3600
    quarantine_ttl_seconds: float = 7 * 86400
    quarantine_max_bytes: int = 4 * _GIB
    live_seconds: float = 3600

    def validate(self) -> None:
        if any(
            isinstance(value, bool) or not math.isfinite(value) or value < 0
            for value in asdict(self).values()
        ):
            raise ValueError("retention policy values must be finite and nonnegative")


@dataclass(frozen=True)
class Candidate:
    path: Path
    anchor: Path
    root: Path
    kind: Kind
    newest: float
    allocated_bytes: int
    device: int
    inode: int
    source_root: Path | None = None
    quarantine: bool = False
    reserved_bytes: int = 0


@dataclass(frozen=True)
class Protection:
    path: Path
    reason: str
    allocated_bytes: int = 0


@dataclass
class Report:
    status: RunStatus
    dry_run: bool
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    elapsed_seconds: float = 0.0
    removed: list[Candidate] = field(default_factory=list)
    would_remove: list[Candidate] = field(default_factory=list)
    quarantined: list[Candidate] = field(default_factory=list)
    protected: list[Protection] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    scanned_candidates: int = 0
    generated_bytes: int = 0
    quarantine_bytes: int | None = 0
    quarantine_reserved_bytes: int | None = 0
    protected_bytes: int = 0
    protected_count: int = 0
    removed_bytes: int = 0
    free_before: int | None = None
    free_after: int | None = None

    def protect(self, candidate: Candidate, reason: str) -> None:
        self.protect_path(candidate.path, reason, candidate.allocated_bytes)

    def protect_path(self, path: Path, reason: str, allocated_bytes: int = 0) -> None:
        self.protected_count += 1
        self.protected_bytes += allocated_bytes
        if len(self.protected) < _DETAILS:
            self.protected.append(Protection(path, reason, allocated_bytes))

    def fault(self, path: Path, error: Exception) -> None:
        self.protect_path(path, "inspection_error")
        if len(self.errors) < _DETAILS:
            self.errors.append(f"{path}: {type(error).__name__}: {str(error)[:500]}")

    def to_dict(self) -> dict[str, object]:
        value = asdict(self)
        for key in ("removed", "would_remove", "quarantined"):
            value[key] = value[key][:_DETAILS]
        value["removed_count"] = len(self.removed)
        value["would_remove_count"] = len(self.would_remove)
        value["quarantined_count"] = len(self.quarantined)
        value["details_truncated"] = (
            any(
                len(items) > _DETAILS
                for items in (self.removed, self.would_remove, self.quarantined)
            )
            or self.protected_count > _DETAILS
        )
        value["observed_reclaimed_bytes"] = (
            self.free_after - self.free_before
            if self.free_after is not None and self.free_before is not None
            else None
        )
        while len(json.dumps(value, default=str).encode()) > 1024**2:
            for key in ("removed", "would_remove", "quarantined", "protected"):
                value[key] = value[key][: len(value[key]) // 2]
            value["details_truncated"] = True
        return cast(dict[str, object], value)


def _small_json(path: Path) -> dict[str, object]:
    if path.is_symlink() or path.stat().st_size > 2**20:
        raise ValueError("unsafe metadata")
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise ValueError("metadata must be an object")
    return data


def _derived_source(path: Path) -> Path | None:
    info = path / "info.plist"
    if info.is_symlink() or not info.is_file() or info.stat().st_size > 2**20:
        return None
    try:
        data = plistlib.loads(info.read_bytes())
    except (ValueError, plistlib.InvalidFileException, ExpatError):
        return None
    workspace = data.get("WorkspacePath") if isinstance(data, dict) else None
    if not isinstance(workspace, str) or not workspace.startswith("/"):
        return None
    source = Path(workspace).parent
    if source.suffix == ".xcodeproj":
        source = source.parent
    return source


def _swift_state(path: Path) -> bool:
    state = path / "workspace-state.json"
    if not state.is_file():
        return False
    try:
        metadata = _small_json(state)
    except (ValueError, OSError):
        return False
    content = metadata.get("object")
    return (
        type(metadata.get("version")) is int
        and isinstance(content, dict)
        and isinstance(content.get("dependencies"), list)
        and isinstance(content.get("artifacts"), list)
    )


def _swift_declared(path: Path) -> bool:
    if path.name == ".build" and (path.parent / "Package.swift").is_file():
        return True
    tag = path / "CACHEDIR.TAG"
    if tag.is_file() and not tag.is_symlink():
        with tag.open("rb") as stream:
            if stream.read(43) == b"Signature: 8a477f597d28d172789f06886806bc55":
                return True
    database = path / "build.db"
    if database.is_file() and not database.is_symlink():
        with database.open("rb") as stream:
            return stream.read(16) == b"SQLite format 3\x00"
    return False


def _recognized(path: Path) -> tuple[Kind, list[Path], Path | None] | None:
    source = _derived_source(path)
    if source is not None:
        components = [
            path / name
            for name in sorted(_XCODE)
            if (path / name).is_dir() or (path / name).is_symlink()
        ]
        if components:
            return Kind.XCODE, components, source
    if path.name == "target" and (path.parent / "Cargo.toml").is_file():
        if (path / ".rustc_info.json").is_file() or any(
            (path / mode / ".fingerprint").is_dir() for mode in ("debug", "release")
        ):
            return Kind.CARGO, [path], path.parent
    if _swift_state(path) and _swift_declared(path):
        components = [
            p
            for p in path.iterdir()
            if (p.is_dir() or p.is_symlink())
            and (
                p.name in {"plugins", "out", "debug", "release"}
                or re.match(r"^[\w]+-(apple|unknown|pc|w64)-", p.name)
            )
        ]
        source = path.parent if (path.parent / "Package.swift").is_file() else None
        return Kind.SWIFT, components, source
    return None


@contextmanager
def _local_fault(report: Report, path: Path) -> Iterator[None]:
    try:
        yield
    except InspectionUnavailable:
        raise
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        report.fault(path, error)


def _discovery_branches(root: Path, report: Report, quarantine: bool) -> Iterator[Path]:
    with os.scandir(root) as entries:
        for entry in entries:
            if entry.name in _PRUNE or not entry.is_dir(follow_symlinks=False):
                continue
            top = Path(entry.path)
            with _local_fault(report, top):
                container = top.name.startswith("-") or (
                    quarantine and re.fullmatch(r"\d{4}-\d{2}-\d{2}", top.name)
                )
                if container and _recognized(top) is None:
                    with os.scandir(top) as children:
                        for child in children:
                            if child.name not in _PRUNE and child.is_dir(follow_symlinks=False):
                                yield Path(child.path)
                else:
                    yield top


def _discover(
    root: Path, *, quarantine: bool = False, report: Report | None = None, max_dirs: int = 20_000
) -> list[Candidate]:
    result: list[Candidate] = []
    report = report if report is not None else Report(RunStatus.DRY_RUN, True)
    device = root.stat().st_dev
    for branch in _discovery_branches(root, report, quarantine):
        pending = [(branch, 0)]
        seen = 0
        with _local_fault(report, branch):
            while pending:
                path, depth = pending.pop()
                seen += 1
                if seen > max_dirs:
                    raise OSError("discovery limit for this branch; remainder retained")
                metadata = path.lstat()
                if metadata.st_uid != os.getuid() or metadata.st_dev != device:
                    raise OSError("foreign directory in managed branch")
                recognized = _recognized(path)
                if recognized is None:
                    if depth < 11:
                        with os.scandir(path) as entries:
                            for entry in entries:
                                if entry.name in _PRUNE or not entry.is_dir(follow_symlinks=False):
                                    continue
                                if seen + len(pending) >= max_dirs:
                                    raise OSError(
                                        "discovery limit for this branch; remainder retained"
                                    )
                                pending.append((Path(entry.path), depth + 1))
                    continue
                kind, components, source = recognized
                for component in components:
                    if component.is_symlink():
                        report.protect_path(component, "symlink_component")
                        continue
                    with _local_fault(report, component):
                        info = inspect_tree(component)
                        newest = info.newest
                        markers = (
                            ["info.plist"]
                            if kind is Kind.XCODE
                            else (
                                ["workspace-state.json", "manifest.pif", "debug.yaml"]
                                if kind is Kind.SWIFT
                                else []
                            )
                        )
                        for name in markers:
                            marker_path = path / name
                            if marker_path.is_file() and not marker_path.is_symlink():
                                marker = marker_path.stat()
                                newest = max(newest, marker.st_mtime, marker.st_ctime)
                        result.append(
                            Candidate(
                                component,
                                path,
                                root,
                                kind,
                                newest,
                                info.allocated_bytes,
                                info.device,
                                info.inode,
                                source,
                                quarantine,
                                info.reserved_bytes,
                            )
                        )
    return result


def _lease_reason(gate: Path, candidate: Candidate) -> str | None:
    from .build_registry import RegistryError, active_scopes, db_path

    if not os.path.lexists(db_path(gate)):
        return None
    try:
        scopes = active_scopes(gate)
    except RegistryError as error:
        raise InspectionUnavailable("build registry is unreadable; cleanup blocked") from error
    for value in scopes:
        if not value.startswith("/"):
            continue
        scope = Path(value)
        for path in (candidate.anchor, candidate.source_root):
            if path is not None and (path.is_relative_to(scope) or scope.is_relative_to(path)):
                return "active_lease"
    return None


def _has_edits(value: object) -> bool:
    if isinstance(value, dict):
        return "edited" in value or any(_has_edits(item) for item in value.values())
    if isinstance(value, list):
        return any(_has_edits(item) for item in value)
    return value == "edited"


def _format_reason(candidate: Candidate, path: Path) -> str | None:
    if candidate.kind is Kind.CARGO:
        markers = (path / ".rustc_info.json").is_file() or any(
            (path / mode / ".fingerprint").is_dir() for mode in ("debug", "release")
        )
        if not (candidate.path.parent / "Cargo.toml").is_file() or not markers:
            return "changed_format"
    elif candidate.kind is Kind.SWIFT:
        if not _swift_state(candidate.anchor) or not _swift_declared(candidate.anchor):
            return "changed_format"
    elif candidate.kind is Kind.XCODE:
        if _derived_source(candidate.anchor) != candidate.source_root:
            return "changed_format"
    return None


def _reason(
    candidate: Candidate,
    snapshot: Snapshot,
    gate: Path,
    *,
    actual: Path | None = None,
    claimed: bool = False,
    live: set[str] | None = None,
) -> tuple[str | None, float]:
    path = actual or candidate.path
    info = inspect_tree(path, ignore_root_ctime=claimed)
    if any(ord(char) < 32 or char in "\\\"'\x7f" for char in str(candidate.anchor)):
        return "unverifiable_process_path", info.newest
    if (info.device, info.inode) != (candidate.device, candidate.inode):
        return "changed_identity", info.newest
    if info.keep or keep_ancestor(candidate.path, candidate.root):
        return "keep_marker", info.newest
    if live and (info.session_ids | set(_UUID.findall(candidate.path.name))) & live:
        return "live_session", info.newest
    if candidate.kind is Kind.SESSION and info.repositories:
        return "source_repository", info.newest
    if snapshot.uses(candidate.anchor, candidate.source_root) or (claimed and snapshot.uses(path)):
        return "process_in_use", info.newest
    format_reason = _format_reason(candidate, path)
    if format_reason:
        return format_reason, info.newest
    lease = _lease_reason(gate, candidate)
    if lease:
        return lease, info.newest
    tracked = tracked_reason(candidate.path)
    if tracked:
        return tracked, info.newest
    if candidate.path.name == "SourcePackages":
        workspace = path / "workspace-state.json"
        if workspace.exists() and _has_edits(_small_json(workspace)):
            return "edited_dependency", info.newest
    for repo in info.repositories:
        unsafe = repository_reason(repo)
        if unsafe:
            return unsafe, info.newest
    if candidate.kind is Kind.XCODE:
        source = _derived_source(candidate.anchor)
        if source is None:
            return "changed_format", info.newest
        marker = (candidate.anchor / "info.plist").stat()
        newest = max(info.newest, marker.st_mtime, marker.st_ctime)
    elif candidate.kind is Kind.SWIFT:
        newest = info.newest
        for name in ("workspace-state.json", "manifest.pif", "debug.yaml"):
            marker_path = candidate.anchor / name
            if marker_path.is_file() and not marker_path.is_symlink():
                marker = marker_path.stat()
                newest = max(newest, marker.st_mtime, marker.st_ctime)
    else:
        newest = info.newest
    return None, newest


def _restore(claim: Path, parent: int, original: str) -> None:
    try:
        os.mkdir(original, mode=0o700, dir_fd=parent)
    except FileExistsError as error:
        raise OSError(f"source was recreated; preserved claimed data at {claim}") from error
    # Reserve an absent name first; a concurrent writer that populates it makes
    # rename fail rather than replacing that writer's files.
    os.rename(claim, original, dst_dir_fd=parent)


def _retire(
    candidate: Candidate,
    state: Path,
    gate: Path,
    cutoff: float,
    *,
    destination: Path | None = None,
    live_roots: list[Path] | None = None,
    live_seconds: float = 3600,
) -> str | None:
    parent = open_parent(candidate.root, candidate.path)
    claim = state / "retiring" / uuid.uuid4().hex
    receipt = claim.with_suffix(".json")
    moved = False
    try:
        metadata = os.stat(candidate.path.name, dir_fd=parent, follow_symlinks=False)
        if (metadata.st_dev, metadata.st_ino) != (candidate.device, candidate.inode):
            return "changed_identity"
        live = _live_ids(live_roots, time.time() - live_seconds) if live_roots is not None else None
        reason, newest = _reason(candidate, process_snapshot(), gate, live=live)
        if reason or newest > cutoff:
            return reason or "recent_activity"
        with receipt.open("x") as stream:
            receipt.chmod(0o600)
            json.dump(
                {
                    "original": str(candidate.path),
                    "root": str(candidate.root),
                    "device": candidate.device,
                    "inode": candidate.inode,
                },
                stream,
            )
            stream.flush()
            os.fsync(stream.fileno())
        os.rename(candidate.path.name, claim, src_dir_fd=parent)
        moved = True
        live = _live_ids(live_roots, time.time() - live_seconds) if live_roots is not None else None
        reason, newest = _reason(
            candidate, process_snapshot(), gate, actual=claim, claimed=True, live=live
        )
        if reason or newest > cutoff:
            _restore(claim, parent, candidate.path.name)
            moved = False
            return reason or "recent_activity"
        if destination is not None:
            target_parent = open_parent(destination.parent.parent, destination)
            try:
                if os.fstat(target_parent).st_dev != candidate.device:
                    raise OSError("quarantine must stay on the source filesystem")
                os.rename(claim, destination.name, dst_dir_fd=target_parent)
            finally:
                os.close(target_parent)
        else:
            if not shutil.rmtree.avoids_symlink_attacks:
                raise OSError("safe directory removal is unavailable")
            shutil.rmtree(claim)
        moved = False
        return None
    finally:
        try:
            if moved and claim.exists():
                _restore(claim, parent, candidate.path.name)
            if not claim.exists():
                receipt.unlink(missing_ok=True)
        finally:
            os.close(parent)


def _recover(state: Path, roots: list[Path], report: Report) -> set[Path]:
    retiring = state / "retiring"
    _secure_directory(retiring)
    blocked: set[Path] = set()
    for claim in sorted(retiring.iterdir()):
        if not os.path.lexists(claim):
            continue
        with _local_fault(report, claim):
            if claim.suffix == ".json":
                if claim.is_symlink() or not re.fullmatch(r"[0-9a-f]{32}\.json", claim.name):
                    raise OSError("unrecognized recovery receipt; retained")
                if not claim.with_suffix("").exists():
                    _small_json(claim)
                    claim.unlink()
                continue
            if claim.is_symlink() or not re.fullmatch(r"[0-9a-f]{32}", claim.name):
                raise OSError("unrecognized recovery entry; retained")
            receipt = _small_json(claim.with_suffix(".json"))
            original = Path(str(receipt.get("original", "")))
            root = Path(str(receipt.get("root", "")))
            metadata = claim.lstat()
            if (
                root not in roots
                or not original.is_relative_to(root)
                or original == root
                or ".." in original.relative_to(root).parts
                or (metadata.st_dev, metadata.st_ino)
                != (receipt.get("device"), receipt.get("inode"))
            ):
                raise OSError("unverifiable recovery entry; retained")
            if os.path.lexists(original):
                blocked.add(original)
                report.protect_path(original, "recovery_conflict")
                raise OSError("recreated original and claimed data both retained")
            parent = open_parent(root, original)
            try:
                _restore(claim, parent, original.name)
                claim.with_suffix(".json").unlink()
            finally:
                os.close(parent)
    return blocked


def _blocked(path: Path, protected: set[Path]) -> bool:
    return any(
        path == item or path.is_relative_to(item) or item.is_relative_to(path) for item in protected
    )


def _walk_error(error: OSError) -> NoReturn:
    raise error


def _live_ids(roots: list[Path], cutoff: float) -> set[str]:
    live: set[str] = set()
    inspected = 0
    for root in roots:
        if not root.exists():
            continue
        for directory, _dirs, files in os.walk(root, followlinks=False, onerror=_walk_error):
            # Claude's per-project memory alias is durable knowledge, not a
            # session journal. Reading it would also pin historical UUIDs.
            _dirs[:] = [name for name in _dirs if name not in {"memory", ".git", "node_modules"}]
            if any((Path(directory) / name).is_symlink() for name in _dirs):
                raise OSError("session activity directory contains an unverifiable alias")
            for name in files:
                inspected += 1
                if inspected > 50_000:
                    raise OSError("session activity inspection limit reached")
                path = Path(directory) / name
                if path.is_symlink() or path.stat().st_mtime < cutoff:
                    continue
                if _UUID.fullmatch(path.stem):
                    live.add(path.stem)
                with path.open("rb") as stream:
                    stream.seek(max(0, path.stat().st_size - 65536))
                    live.update(_UUID.findall(stream.read().decode(errors="replace")))
    return live


def _children(path: Path, report: Report) -> list[Path]:
    with _local_fault(report, path):
        return sorted(path.iterdir())
    return []


def _session_dirs(root: Path, report: Report) -> list[Path]:
    result = []
    for first in _children(root, report):
        if first.is_symlink() or not first.is_dir():
            continue
        if _UUID.fullmatch(first.name):
            result.append(first)
        else:
            result.extend(
                p
                for p in _children(first, report)
                if _UUID.fullmatch(p.name) and p.is_dir() and not p.is_symlink()
            )
    return result


def _quarantine_usage(quarantine: Path, report: Report) -> TreeInfo | None:
    try:
        return inspect_tree(quarantine, accounting_only=True) if quarantine.exists() else None
    except OSError as error:
        report.fault(quarantine, error)
        return None


def _quarantine_sizes(quarantine: Path, report: Report) -> tuple[int | None, int | None]:
    info = _quarantine_usage(quarantine, report)
    if info is not None:
        return info.allocated_bytes, info.reserved_bytes
    return (None, None) if quarantine.exists() else (0, 0)


def _general(
    root: Path,
    quarantine: Path,
    state: Path,
    gate: Path,
    policy: Policy,
    report: Report,
    apply: bool,
    live_roots: list[Path],
    blocked: set[Path] | None = None,
) -> None:
    now = time.time()
    live = _live_ids(live_roots, now - policy.live_seconds)
    blocked = blocked if blocked is not None else set()
    if quarantine.exists():
        report.quarantine_bytes, report.quarantine_reserved_bytes = _quarantine_sizes(
            quarantine, report
        )
        for day in _children(quarantine, report):
            if (
                day.is_symlink()
                or not day.is_dir()
                or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day.name)
            ):
                continue
            for path in _children(day, report):
                if _blocked(path, blocked):
                    report.protect_path(path, "recovery_conflict")
                    continue
                with _local_fault(report, path):
                    if path.is_symlink() or not path.is_dir():
                        continue
                    info = inspect_tree(path)
                    candidate = Candidate(
                        path,
                        path,
                        quarantine,
                        Kind.SESSION,
                        info.newest,
                        info.allocated_bytes,
                        info.device,
                        info.inode,
                        quarantine=True,
                        reserved_bytes=info.reserved_bytes,
                    )
                    if (
                        any(identity in path.name for identity in live)
                        or info.newest > now - policy.quarantine_ttl_seconds
                    ):
                        continue
                    reason, _ = _reason(candidate, process_snapshot(), gate, live=live)
                    if reason:
                        report.protect(candidate, reason)
                    elif apply:
                        reason = _retire(
                            candidate,
                            state,
                            gate,
                            now - policy.quarantine_ttl_seconds,
                            live_roots=live_roots,
                            live_seconds=policy.live_seconds,
                        )
                        if reason:
                            report.protect(candidate, reason)
                        else:
                            report.removed.append(candidate)
                            report.removed_bytes += candidate.allocated_bytes
                    else:
                        report.would_remove.append(candidate)
    _, admission_used = _quarantine_sizes(quarantine, report)
    if not apply and admission_used is not None:
        admission_used = max(
            0,
            admission_used
            - sum(
                max(c.allocated_bytes, c.reserved_bytes)
                for c in report.would_remove
                if c.quarantine
            ),
        )
    for path in _session_dirs(root, report):
        if _blocked(path, blocked):
            report.protect_path(path, "recovery_conflict")
            continue
        with _local_fault(report, path):
            if path.name in live:
                continue
            info = inspect_tree(path)
            if info.newest > now - policy.session_ttl_seconds:
                continue
            candidate = Candidate(
                path,
                path,
                root,
                Kind.SESSION,
                info.newest,
                info.allocated_bytes,
                info.device,
                info.inode,
                reserved_bytes=info.reserved_bytes,
            )
            reason, _ = _reason(candidate, process_snapshot(), gate, live=live)
            _, measured = _quarantine_sizes(quarantine, report) if apply else (None, admission_used)
            used = (
                max(admission_used, measured)
                if admission_used is not None and measured is not None
                else None
            )
            reservation = info.reserved_bytes + 8192
            if reason or used is None or used + reservation > policy.quarantine_max_bytes:
                report.protect(candidate, reason or "quarantine_capacity")
                continue
            if not apply:
                report.quarantined.append(candidate)
                assert admission_used is not None
                admission_used += reservation
                continue
            # Quarantine is recovery, not reclaimed capacity. Never copy across disks.
            day = quarantine / time.strftime("%Y-%m-%d")
            _secure_directory(quarantine)
            _secure_directory(day)
            destination = day / (path.parent.name + "__" + path.name + "__" + uuid.uuid4().hex[:8])
            reason = _retire(
                candidate,
                state,
                gate,
                now - policy.session_ttl_seconds,
                destination=destination,
                live_roots=live_roots,
                live_seconds=policy.live_seconds,
            )
            if reason:
                report.protect(candidate, reason)
            else:
                report.quarantined.append(candidate)
                admission_used = used + reservation


def _secure_directory(path: Path) -> None:
    if path.is_symlink():
        raise ValueError("state or quarantine directory is a symlink")
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    if path.stat().st_uid != os.getuid():
        raise ValueError("state or quarantine directory has a foreign owner")
    path.chmod(0o700)


def _status(state: Path, report: Report) -> None:
    descriptor, name = tempfile.mkstemp(prefix=".status-", dir=state)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(report.to_dict(), stream, default=str)
        os.replace(name, state / "status.json")
    finally:
        Path(name).unlink(missing_ok=True)


def run_once(
    root: Path,
    quarantine: Path,
    state_dir: Path,
    policy: Policy,
    *,
    apply: bool = False,
    gate_state: Path | None = None,
    live_roots: list[Path] | None = None,
) -> Report:
    started = time.monotonic()
    policy.validate()
    root, quarantine, state_dir = (p.expanduser().absolute() for p in (root, quarantine, state_dir))
    if root.is_symlink() or root.name != f"claude-{os.getuid()}":
        raise ValueError("managed root must be the current user's real claude-UID directory")
    if quarantine.is_symlink() or state_dir.is_symlink():
        raise ValueError("state and quarantine aliases are not allowed")
    root, quarantine, state = root.resolve(), quarantine.resolve(), state_dir.resolve()
    if (
        root == quarantine
        or root.is_relative_to(quarantine)
        or quarantine.is_relative_to(root)
        or state == root
        or state.is_relative_to(root)
        or root.is_relative_to(state)
        or state == quarantine
        or state.is_relative_to(quarantine)
    ):
        raise ValueError("scratch, quarantine and transaction state overlap")
    report = Report(RunStatus.OK if apply else RunStatus.DRY_RUN, not apply)
    _secure_directory(state)
    gate = (
        gate_state
        or Path(
            os.environ.get(
                "HERDR_ENGINEERING_STATE", str(Path.home() / ".local/state/herdr-engineering")
            )
        )
        / "build-gates"
    )
    gate = gate.expanduser().resolve()
    if any(
        gate == managed or gate.is_relative_to(managed) or managed.is_relative_to(gate)
        for managed in (root, quarantine)
    ):
        raise ValueError("coordination state must be outside managed scratch/quarantine trees")
    _secure_directory(gate)
    locks: list[int] = []
    report_lock = False
    try:
        for lock in (state / "reaper.lock", gate / "build-maintenance.lock"):
            descriptor = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            locks.append(descriptor)
            if os.fstat(descriptor).st_uid != os.getuid():
                raise OSError("foreign maintenance lock")
            try:
                mode = (
                    fcntl.LOCK_SH
                    if not apply and lock == gate / "build-maintenance.lock"
                    else fcntl.LOCK_EX
                )
                fcntl.flock(descriptor, mode | fcntl.LOCK_NB)
            except BlockingIOError:
                report.status = RunStatus.BUSY
                return report
            if lock == state / "reaper.lock":
                report_lock = True
        if not root.exists():
            return report
        if root.stat().st_uid != os.getuid() or not root.is_dir():
            raise OSError("foreign or invalid managed root")
        if state.stat().st_dev != root.stat().st_dev:
            raise OSError("transaction state must be on the scratch filesystem")
        if quarantine.exists() and (
            quarantine.is_symlink() or quarantine.stat().st_dev != root.stat().st_dev
        ):
            raise OSError("quarantine must be a real directory on the scratch filesystem")
        blocked: set[Path] = set()
        if apply:
            blocked = _recover(state, [root, quarantine], report)
        elif (state / "retiring").exists() and any((state / "retiring").iterdir()):
            raise OSError("pending transaction recovery; apply to restore it before cleanup")
        usage = shutil.disk_usage(root)
        if policy.min_free_bytes > usage.total:
            raise ValueError("free-space floor exceeds filesystem capacity")
        report.free_before = usage.free
        candidates = _discover(root, report=report)
        if quarantine.exists():
            candidates.extend(_discover(quarantine, quarantine=True, report=report))
        candidates.sort(key=lambda c: (c.newest, str(c.path)))
        report.scanned_candidates = len(candidates)
        report.generated_bytes = sum(c.allocated_bytes for c in candidates)
        remaining = report.generated_bytes
        qbytes, report.quarantine_reserved_bytes = _quarantine_sizes(quarantine, report)
        report.quarantine_bytes = qbytes
        snapshot = process_snapshot()
        for candidate in candidates:
            if _blocked(candidate.path, blocked):
                report.protect(candidate, "recovery_conflict")
                continue
            with _local_fault(report, candidate.path):
                free = shutil.disk_usage(root).free
                pressure = remaining > policy.max_bytes or free < policy.min_free_bytes
                pressure |= (
                    candidate.quarantine
                    and qbytes is not None
                    and qbytes > policy.quarantine_max_bytes
                )
                age = (
                    min(policy.idle_seconds, policy.ttl_seconds) if pressure else policy.ttl_seconds
                )
                cutoff = time.time() - age
                reason, newest = _reason(candidate, snapshot, gate)
                if reason or newest > cutoff:
                    report.protect(candidate, reason or "recent_activity")
                    continue
                if apply:
                    reason = _retire(candidate, state, gate, cutoff)
                    if reason:
                        report.protect(candidate, reason)
                        continue
                    report.removed.append(candidate)
                    report.removed_bytes += candidate.allocated_bytes
                else:
                    report.would_remove.append(candidate)
                remaining -= candidate.allocated_bytes
                if candidate.quarantine and qbytes is not None:
                    qbytes -= candidate.allocated_bytes
        defaults = [
            Path.home() / ".claude/projects",
            Path.home() / ".codex/sessions",
            Path.home() / ".pi/agent/sessions",
        ]
        _general(
            root,
            quarantine,
            state,
            gate,
            policy,
            report,
            apply,
            live_roots if live_roots is not None else defaults,
            blocked,
        )
        report.free_after = shutil.disk_usage(root).free
        report.quarantine_bytes, report.quarantine_reserved_bytes = _quarantine_sizes(
            quarantine, report
        )
        if apply and (
            remaining > policy.max_bytes
            or (report.free_after is not None and report.free_after < policy.min_free_bytes)
            or (
                report.quarantine_bytes is not None
                and report.quarantine_bytes > policy.quarantine_max_bytes
            )
            or (
                report.quarantine_reserved_bytes is not None
                and report.quarantine_reserved_bytes > policy.quarantine_max_bytes
            )
        ):
            report.status = RunStatus.PRESSURE_BLOCKED
        if report.errors:
            report.status = RunStatus.ERROR
    except Exception as error:
        report.errors.append(f"{type(error).__name__}: {str(error)[:1000]}")
        report.status = RunStatus.ERROR
    except BaseException:
        report.status = RunStatus.ERROR
        report.errors.append("sweep interrupted; retained receipts require recovery")
        raise
    finally:
        try:
            report.elapsed_seconds = round(time.monotonic() - started, 3)
            if report.free_before is not None:
                report.free_after = shutil.disk_usage(root).free
            if report_lock:
                _status(state, report)
        finally:
            for descriptor in reversed(locks):
                os.close(descriptor)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(f"/tmp/claude-{os.getuid()}"))
    parser.add_argument(
        "--quarantine", type=Path, default=Path.home() / ".local/state/scratch-reaper/quarantine"
    )
    parser.add_argument(
        "--state-dir", type=Path, default=Path.home() / ".local/state/scratch-reaper"
    )
    parser.add_argument("--gate-state-dir", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--ttl-minutes", type=float, default=360)
    parser.add_argument("--pressure-idle-minutes", type=float, default=30)
    parser.add_argument("--max-disposable-gib", type=float, default=32)
    parser.add_argument("--min-free-gib", type=float, default=64)
    parser.add_argument("--session-ttl-hours", type=float, default=48)
    parser.add_argument("--quarantine-days", type=float, default=7)
    parser.add_argument("--quarantine-max-gib", type=float, default=4)
    parser.add_argument("--live-minutes", type=float, default=60)
    parser.add_argument("--session-root", action="append", type=Path)
    parser.add_argument("--status", action="store_true", help="read the last bounded sweep report")
    args = parser.parse_args(argv)
    try:
        if args.status:
            path = args.state_dir.expanduser() / "status.json"
            print(json.dumps(_small_json(path) if path.exists() else {"status": "never_run"}))
            return 0
        policy = Policy(
            args.ttl_minutes * 60,
            args.pressure_idle_minutes * 60,
            int(args.max_disposable_gib * _GIB),
            int(args.min_free_gib * _GIB),
            args.session_ttl_hours * 3600,
            args.quarantine_days * 86400,
            int(args.quarantine_max_gib * _GIB),
            args.live_minutes * 60,
        )
        report = run_once(
            args.root,
            args.quarantine,
            args.state_dir,
            policy,
            apply=args.apply and not args.dry_run,
            gate_state=args.gate_state_dir,
            live_roots=args.session_root,
        )
    except (OSError, ValueError, OverflowError) as error:
        parser.error(str(error))
    print(json.dumps(report.to_dict(), default=str))
    return 2 if report.status is RunStatus.ERROR else 0


if __name__ == "__main__":
    raise SystemExit(main())

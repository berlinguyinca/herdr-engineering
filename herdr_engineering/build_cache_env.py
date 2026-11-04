"""Per-host sccache pilot (spec 03): plan the environment for ONE cargo command.

``plan`` is a pure function. It decides whether a coordinated cargo command may
run with ``RUSTC_WRAPPER`` pointing at a detected sccache and returns the env
overrides to merge into that child's environment only. Nothing here mutates
``os.environ``, creates directories, or runs ``cargo clean``.

Settings come from :class:`BuildCacheConfig` (YAML), never new env vars; the
environment is only what we SET for the child.

Cache keys are sccache's job: it hashes the compiler, flags, profile and
features itself, so cargo profile/features are deliberately NOT parsed here and
no hand-written cache namespace is added. The toolchain fingerprint exists only
to record/compare which compiler produced a build (events, spec 04 namespaces).
Shared (S3) level (spec 04): ``plan`` only DECIDES and renders text; the sccache
TOML is written by :func:`materialize` (called from ``run_gated``), and
credentials travel only in ``EnvPlan.secret_env`` (hidden from repr), never in
``env_overrides``, reasons, warnings or events.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import shutil
import socket
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from .build_cache_config import BuildCacheConfig, Mode
from .build_cache_storage import (
    SHARED_ENV_KEYS,
    Code,
    StorageError,
    prepare_shared,
    shared_env,
    write_config_atomic,
)
from .build_metrics import classify_cmd

TOOL_TIMEOUT_S = 3.0
_CACHEABLE_KINDS = ("cargo build", "cargo check", "cargo test", "cargo clippy")
_WRAPPER_VARS = ("RUSTC_WRAPPER", "RUSTC_WORKSPACE_WRAPPER", "CARGO_BUILD_RUSTC_WRAPPER")


class CmdClass(StrEnum):
    CACHEABLE_CARGO = "cacheable_cargo"
    OTHER = "other"


class Reason(StrEnum):
    APPLIED = "applied"
    DISABLED = "disabled"
    MODE_NOT_LOCAL = "mode_not_local"
    HOST_NOT_ALLOWED = "host_not_allowed"
    NOT_CACHEABLE_CMD = "not_cacheable_cmd"
    SCCACHE_NOT_FOUND = "sccache_not_found"
    RUSTC_NOT_FOUND = "rustc_not_found"
    WORKSPACE_INVALID = "workspace_invalid"
    USER_WRAPPER_PRESENT = "user_wrapper_present"
    LOW_FREE_DISK = "low_free_disk"


@dataclass(frozen=True)
class Tools:
    sccache: Path | None
    sccache_version: str | None
    rustc: Path | None
    cargo: Path | None
    rustc_vv: str | None = None  # raw `rustc -vV` text (keeps plan() pure)


@dataclass(frozen=True)
class Fingerprint:
    host: str
    release: str
    commit_hash: str
    llvm: str
    target: str | None
    digest: str


@dataclass(frozen=True)
class SharedStatus:
    """Event-safe outcome of the shared level: no endpoint, paths or secrets."""
    active: bool
    reason: str  # "active" | "not_requested" | "shared_unavailable:<code>"
    namespace_digest: str | None = None

    def as_event(self) -> dict[str, object]:
        return {"active": self.active, "reason": self.reason,
                "namespace_digest": self.namespace_digest}


NOT_REQUESTED = SharedStatus(False, "not_requested")


@dataclass(frozen=True)
class EnvPlan:
    applied: bool
    env_overrides: dict[str, str]
    reasons: list[str]
    warnings: list[str] = field(default_factory=list)
    fingerprint: Fingerprint | None = None
    shared: SharedStatus = NOT_REQUESTED
    secret_env: dict[str, str] = field(default_factory=dict, repr=False)
    shared_toml: str | None = field(default=None, repr=False)


def _run_text(argv: list[str]) -> str | None:
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=TOOL_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return None
    out = r.stdout.strip()
    return out if r.returncode == 0 and out else None


def detect_tools(path_env: str | None) -> Tools:
    """Find sccache/rustc/cargo on an explicit PATH; never raises."""
    def which(name: str) -> Path | None:
        found = shutil.which(name, path=path_env or "")
        return Path(found) if found else None
    scc, rustc, cargo = which("sccache"), which("rustc"), which("cargo")
    version = _run_text([str(scc), "--version"]) if scc else None
    vv = _run_text([str(rustc), "-vV"]) if rustc else None
    return Tools(scc, version.splitlines()[0] if version else None, rustc, cargo, vv)


def fingerprint_from_text(rustc_vv: str | None, target: str | None = None) -> Fingerprint | None:
    """Parse ``rustc -vV`` text. Only toolchain identity, never paths."""
    fields: dict[str, str] = {}
    for line in (rustc_vv or "").splitlines():
        key, sep, val = line.partition(":")
        if sep:
            fields[key.strip()] = val.strip()
    try:
        host, release = fields["host"], fields["release"]
        commit, llvm = fields["commit-hash"], fields["LLVM version"]
    except KeyError:
        return None
    canon = json.dumps({"host": host, "release": release, "commit_hash": commit,
                        "llvm": llvm, "target": target}, sort_keys=True,
                       separators=(",", ":"))
    return Fingerprint(host, release, commit, llvm, target,
                       hashlib.sha256(canon.encode()).hexdigest())


def toolchain_fingerprint(rustc: Path, target: str | None = None) -> Fingerprint | None:
    return fingerprint_from_text(_run_text([str(rustc), "-vV"]), target)


def classify(cmd: list[str]) -> CmdClass:
    return CmdClass.CACHEABLE_CARGO if classify_cmd(cmd) in _CACHEABLE_KINDS else CmdClass.OTHER


def _requested_target(cmd: list[str]) -> str | None:
    for i, tok in enumerate(cmd):
        if tok == "--target" and i + 1 < len(cmd):
            return cmd[i + 1]
        if tok.startswith("--target="):
            return tok.split("=", 1)[1]
    return None


def _free_percent(path: Path) -> float | None:
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        u = shutil.disk_usage(probe)
    except OSError:
        return None
    return 100.0 * u.free / u.total if u.total else None


def _gib_str(gib: float) -> str:
    return f"{int(gib)}G" if gib == int(gib) else f"{max(1, round(gib * 1024))}M"


def _no(reason: Reason, warnings: list[str] | None = None) -> EnvPlan:
    return EnvPlan(False, {}, [reason.value], warnings or [], None)


def _unavailable(code: Code) -> SharedStatus:
    return SharedStatus(False, f"shared_unavailable:{code.value}")


def plan(config: BuildCacheConfig, cmd: list[str], base_env: Mapping[str, str],
         tools: Tools, workspace: Path, state_dir: Path,
         shared_reachable: bool | None = None) -> EnvPlan:
    """Decide the cache environment for one command. No writes, no network.

    It may READ the registry and credentials files when the shared level is
    requested. ``shared_reachable`` is the caller's pre-build probe result
    (``build_cache_storage.check_shared``); ``None`` means not probed.
    """
    if not config.enabled:
        return _no(Reason.DISABLED)
    if config.effective_mode not in (Mode.LOCAL_ONLY, Mode.SHARED_CACHE):
        return _no(Reason.MODE_NOT_LOCAL)
    if config.allowed_hosts and socket.gethostname() not in config.allowed_hosts:
        return _no(Reason.HOST_NOT_ALLOWED)
    if classify(cmd) is not CmdClass.CACHEABLE_CARGO:
        return _no(Reason.NOT_CACHEABLE_CMD)
    if any(base_env.get(v) for v in _WRAPPER_VARS):
        return _no(Reason.USER_WRAPPER_PRESENT)
    if tools.sccache is None:
        return _no(Reason.SCCACHE_NOT_FOUND)
    fp = fingerprint_from_text(tools.rustc_vv, _requested_target(cmd)) if tools.rustc else None
    if tools.rustc is None or fp is None:
        return _no(Reason.RUSTC_NOT_FOUND)
    if not workspace.is_dir():
        return _no(Reason.WORKSPACE_INVALID)

    lo = config.local
    cache_dir = state_dir / "sccache"
    free = _free_percent(cache_dir)
    if free is not None and free < lo.min_free_disk_percent:
        return EnvPlan(False, {}, [Reason.LOW_FREE_DISK.value], [], fp)

    warnings: list[str] = []
    env = {
        "RUSTC_WRAPPER": str(tools.sccache),
        "SCCACHE_DIR": str(cache_dir),
        "SCCACHE_CACHE_SIZE": _gib_str(lo.capacity_gib),
    }
    if lo.incremental_disabled_for_cacheable_builds:
        env["CARGO_INCREMENTAL"] = "0"
    elif base_env.get("CARGO_INCREMENTAL") == "1":
        warnings.append("CARGO_INCREMENTAL=1: sccache errors on incremental builds")
    if base_env.get("CARGO_TARGET_DIR"):
        warnings.append("shared CARGO_TARGET_DIR is user-set; not managed")
    if lo.ram_scratch_gib > 0:
        warnings.append("ram_scratch not implemented")
    if any(t.startswith("+") for t in cmd[1:2]):
        warnings.append("cargo +toolchain override: fingerprint reflects default rustc")
    shared, secret, toml = _shared(config, tools, fp, state_dir, shared_reachable, env)
    return EnvPlan(True, env, [Reason.APPLIED.value], warnings, fp, shared, secret, toml)


def _shared(config: BuildCacheConfig, tools: Tools, fp: Fingerprint, state_dir: Path,
            reachable: bool | None, env: dict[str, str]
            ) -> tuple[SharedStatus, dict[str, str], str | None]:
    """Add the shared level to ``env`` (in place) or explain why not. Never raises."""
    if not config.shared_cache.enabled:
        return NOT_REQUESTED, {}, None
    if config.effective_mode is not Mode.SHARED_CACHE:
        return _unavailable(Code.NOT_SHARED_MODE), {}, None
    try:
        setup = prepare_shared(config, sccache_version=tools.sccache_version,
                               toolchain_digest=fp.digest, host_triple=fp.host,
                               state_dir=state_dir, reachable=reachable)
    except StorageError as exc:
        return _unavailable(exc.code), {}, None
    env.update(shared_env(setup.inputs, setup.conf_path))
    return (SharedStatus(True, "active", setup.namespace_digest),
            setup.credentials.as_child_env(), setup.toml)


def materialize(p: EnvPlan, state_dir: Path) -> EnvPlan:
    """Write the sccache TOML for an active shared plan (0600, atomic).

    On any failure the shared parts are removed and the build proceeds with the
    local cache only (reason ``shared_unavailable:config_write_failed``).
    """
    if p.shared_toml is None or not p.shared.active:
        return p
    try:
        write_config_atomic(state_dir / "sccache" / "sccache.toml", p.shared_toml)
    except OSError:
        env = {k: v for k, v in p.env_overrides.items() if k not in SHARED_ENV_KEYS}
        return dataclasses.replace(p, env_overrides=env, secret_env={}, shared_toml=None,
                                   shared=_unavailable(Code.CONFIG_WRITE_FAILED))
    return p

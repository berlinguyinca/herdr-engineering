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
Shared (S3) level (spec 04): ``plan`` only DECIDES. Credentials are never part
of the cargo environment (review H2); the shared level stays inactive with
reason ``shared_unavailable:shared_requires_server`` until an explicit sccache
server start exists. Each policy gets its own ``SCCACHE_SERVER_PORT`` (H3).
"""
from __future__ import annotations

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
from .build_cache_storage import Code, SharedSetup, StorageError, prepare_shared
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
    NO_STORAGE = "no_storage_tier_available"


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
    # Set when the shared request validated: the caller must start the server
    # (build_cache_server.activate) before it may report the tier active.
    shared_setup: SharedSetup | None = None

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


def _argv_sets_wrapper(cmd: list[str]) -> bool:
    """``--config build.rustc-wrapper=...`` (either spelling) on the command line."""
    return any("rustc-wrapper" in a or "rustc-workspace-wrapper" in a
               for a in cmd[1:] if "=" in a or a.startswith("--config"))


def _config_sets_wrapper(workspace: Path, base_env: Mapping[str, str]) -> bool:
    """A ``build.rustc-wrapper`` in cargo config: env RUSTC_WRAPPER (ours) would
    silently override it, so treat it like a user wrapper. Best effort, read-only."""
    import tomllib
    dirs = [workspace.resolve(), *workspace.resolve().parents]
    files = [d / ".cargo" / name for d in dirs for name in ("config.toml", "config")]
    home = base_env.get("CARGO_HOME") or str(Path.home() / ".cargo")
    files += [Path(home) / "config.toml", Path(home) / "config"]
    for f in files:
        try:
            build = tomllib.loads(f.read_text()).get("build", {})
        except (OSError, ValueError):
            continue
        if isinstance(build, dict):
            if build.get("rustc-workspace-wrapper"):
                return True
            wrapper = build.get("rustc-wrapper")
            if wrapper:
                from .cargo_host import is_managed_wrapper
                if not is_managed_wrapper(str(wrapper), Path(home).parent):
                    return True
    return False


def plan(config: BuildCacheConfig, cmd: list[str], base_env: Mapping[str, str],
         tools: Tools, workspace: Path, state_dir: Path,
         shared_reachable: bool | None = None, *, cache_dir: Path | None = None) -> EnvPlan:
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
    if (any(base_env.get(v) for v in _WRAPPER_VARS)
            or _argv_sets_wrapper(cmd) or _config_sets_wrapper(workspace, base_env)):
        return _no(Reason.USER_WRAPPER_PRESENT)
    if tools.sccache is None:
        return _no(Reason.SCCACHE_NOT_FOUND)
    fp = fingerprint_from_text(tools.rustc_vv, _requested_target(cmd)) if tools.rustc else None
    if tools.rustc is None or fp is None:
        return _no(Reason.RUSTC_NOT_FOUND)
    if not workspace.is_dir():
        return _no(Reason.WORKSPACE_INVALID)

    lo = config.local
    if config.local.storage_tiers and cache_dir is None:
        return _no(Reason.NO_STORAGE)
    cache_dir = cache_dir if cache_dir is not None else state_dir / "sccache"
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
    env["SCCACHE_SERVER_PORT"] = str(server_port(config, fp, cache_dir))
    env["SCCACHE_IGNORE_SERVER_IO_ERROR"] = "1"  # server trouble degrades to plain rustc
    shared, setup = _shared(config, tools, fp, state_dir, shared_reachable)
    return EnvPlan(True, env, [Reason.APPLIED.value], warnings, fp, shared, setup)


def _shared(config: BuildCacheConfig, tools: Tools, fp: Fingerprint, state_dir: Path,
            reachable: bool | None) -> tuple[SharedStatus, SharedSetup | None]:
    """Decide the shared level's status. Never raises, never yields secrets.

    Credentials must not be in the environment of cargo/rustc/build scripts, and
    an sccache server fixes its config at first start. ``plan`` therefore only
    validates: a fully valid request yields ``shared_requires_server`` plus the
    :class:`SharedSetup`, and :func:`activate_shared` starts the per-policy
    server (credentials in ITS env only) and upgrades the status to active once
    the running server is verified. Every other problem keeps its own reason.
    """
    if not config.shared_cache.enabled:
        return NOT_REQUESTED, None
    if config.effective_mode is not Mode.SHARED_CACHE:
        return _unavailable(Code.NOT_SHARED_MODE), None
    try:
        setup = prepare_shared(config, sccache_version=tools.sccache_version,
                               toolchain_digest=fp.digest, host_triple=fp.host,
                               state_dir=state_dir, reachable=reachable)
    except StorageError as exc:
        return _unavailable(exc.code), None
    return (SharedStatus(False, f"shared_unavailable:{Code.SHARED_REQUIRES_SERVER.value}",
                         setup.namespace_digest), setup)


def activate_shared(p: EnvPlan, tools: Tools, base_env: Mapping[str, str]) -> EnvPlan:
    """Start/verify the shared server for a validated plan; never raises.

    On success the plan is returned with ``shared.active`` true (env unchanged:
    the child only knows the port). On failure the child is moved to a distinct
    local-tier port so it can never reach a half-configured server, and the
    reason names the failure.
    """
    from dataclasses import replace

    from .build_cache_server import ensure_shared_server, local_fallback_port
    setup = p.shared_setup
    if setup is None or tools.sccache is None or not p.applied:
        return p
    try:
        port = int(p.env_overrides["SCCACHE_SERVER_PORT"])
        result = ensure_shared_server(setup, base_env, str(tools.sccache), port)
    except Exception:  # noqa: BLE001 - never break a build
        result = None
    digest = setup.namespace_digest
    if result is not None and result.ok:
        return replace(p, shared=SharedStatus(True, "active", digest), shared_setup=None)
    code = result.code if result is not None and result.code else Code.SERVER_START_FAILED
    env = dict(p.env_overrides)
    env["SCCACHE_SERVER_PORT"] = str(local_fallback_port(port))
    return replace(p, env_overrides=env, shared_setup=None,
                   shared=SharedStatus(False, f"shared_unavailable:{code.value}", digest))


def server_port(config: BuildCacheConfig, fp: Fingerprint, cache_dir: Path) -> int:
    """Per-policy ``SCCACHE_SERVER_PORT`` (review H3).

    An sccache server reads its config (dir, size, S3, RW mode, prefix) once, at
    first start, and serves every client on its port. Deriving the port from the
    effective policy means a build under a different policy (mode, writes,
    namespace, endpoint, toolchain, cache dir/size) talks to a different server
    and can never reuse one started under another. The server's lifetime is NOT
    managed (it exits on sccache's own idle timeout); a port clash with an
    unrelated local service is possible (12000 slots); the range sits below the
    Linux ephemeral range (32768-60999) and sccache I/O errors are ignored, so a
    clash degrades to an uncached compile instead of failing the build.
    """
    sc = config.shared_cache
    canon = json.dumps([config.effective_mode.value, bool(sc.enabled),
                        bool(config.shared_writes_effective), sc.namespace, sc.endpoint_ref,
                        fp.digest, str(cache_dir), config.local.capacity_gib],
                       separators=(",", ":"))
    return 20000 + int(hashlib.sha256(canon.encode()).hexdigest()[:8], 16) % 12000

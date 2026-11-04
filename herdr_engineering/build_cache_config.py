"""Validation for the default-off ``fabric_build_cache`` config section (spec 02).

Fails closed: unknown keys, wrong types, out-of-range values, inconsistent mode
flags, URL-shaped endpoints and embedded secrets all raise ``InvalidError`` with
the dotted key path. Secrets are referenced by absolute file PATH only; the
file itself is checked lazily by :func:`check_credentials_file`.
"""
from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from .errors import InvalidError

_ENDPOINT_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")
_SECRET_WORDS = ("secret", "password", "token", "access_key", "api_key")
LOCAL_TEST_SUFFIX = ".local-test"


class Mode(StrEnum):
    LOCAL_ONLY = "local_only"
    SHARED_CACHE = "shared_cache"
    SLURM_FULL_BUILD = "slurm_full_build"
    DISTRIBUTED = "distributed"


class RemoteFallback(StrEnum):
    LOCAL = "local"
    FAIL = "fail"


@dataclass(frozen=True)
class SharedCache:
    enabled: bool
    endpoint_ref: str | None
    namespace: str
    writes_enabled: bool
    max_total_gib: float
    tls_required: bool
    credentials_file: str | None
    registry_file: str | None = None


@dataclass(frozen=True)
class Local:
    capacity_gib: float
    ram_scratch_gib: float
    min_free_disk_percent: float
    incremental_disabled_for_cacheable_builds: bool


@dataclass(frozen=True)
class Execution:
    slurm_enabled: bool
    allowed_clusters: tuple[str, ...]
    allowed_partitions: tuple[str, ...]
    gpu_for_cpu_builds: bool
    remote_fallback: RemoteFallback
    max_build_slots: int


@dataclass(frozen=True)
class Cleanup:
    dry_run: bool
    min_idle_hours: float
    max_evict_per_pass_gib: float


@dataclass(frozen=True)
class Observability:
    enabled: bool
    export_existing_metrics_pipeline: bool


@dataclass(frozen=True)
class BuildCacheConfig:
    enabled: bool
    mode: Mode
    allowed_hosts: tuple[str, ...]
    shared_cache: SharedCache
    local: Local
    execution: Execution
    cleanup: Cleanup
    observability: Observability
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def effective_mode(self) -> Mode:
        return self.mode if self.enabled else Mode.LOCAL_ONLY

    @property
    def shared_writes_effective(self) -> bool:
        return self.enabled and self.shared_cache.enabled and self.shared_cache.writes_enabled

    @property
    def slurm_effective(self) -> bool:
        return self.enabled and self.execution.slurm_enabled

    def public_view(self) -> dict[str, Any]:
        """API/status-safe view: no credentials path, endpoint reference only."""
        sc, lo, ex, cl = self.shared_cache, self.local, self.execution, self.cleanup
        return {
            "enabled": self.enabled,
            "mode": self.mode.value,
            "effective_mode": self.effective_mode.value,
            "allowed_hosts": list(self.allowed_hosts),
            "shared_cache": {
                "enabled": sc.enabled, "endpoint_ref": sc.endpoint_ref,
                "namespace": sc.namespace, "writes_enabled": self.shared_writes_effective,
                "max_total_gib": sc.max_total_gib, "tls_required": sc.tls_required,
                "credentials_configured": sc.credentials_file is not None,
                "registry_configured": sc.registry_file is not None,
            },
            "local": {
                "capacity_gib": lo.capacity_gib, "ram_scratch_gib": lo.ram_scratch_gib,
                "min_free_disk_percent": lo.min_free_disk_percent,
                "incremental_disabled_for_cacheable_builds":
                    lo.incremental_disabled_for_cacheable_builds,
            },
            "execution": {
                "slurm_enabled": self.slurm_effective,
                "allowed_clusters": list(ex.allowed_clusters),
                "allowed_partitions": list(ex.allowed_partitions),
                "gpu_for_cpu_builds": ex.gpu_for_cpu_builds,
                "remote_fallback": ex.remote_fallback.value,
                "max_build_slots": ex.max_build_slots,
            },
            "cleanup": {
                "dry_run": cl.dry_run, "min_idle_hours": cl.min_idle_hours,
                "max_evict_per_pass_gib": cl.max_evict_per_pass_gib,
            },
            "observability": {
                "enabled": self.observability.enabled,
                "export_existing_metrics_pipeline":
                    self.observability.export_existing_metrics_pipeline,
            },
            "warnings": list(self.warnings),
        }


def _bad(path: str, why: str) -> InvalidError:
    return InvalidError(f"{path} {why}")


def _mapping(v: Any, path: str, allowed: set[str]) -> dict[str, Any]:
    if not isinstance(v, dict):
        raise _bad(path, "must be a mapping")
    for k in v:
        if not isinstance(k, str):
            raise _bad(f"{path}.{k}", "key must be a string")
        low = k.lower()
        if k != "credentials_file" and any(w in low for w in _SECRET_WORDS):
            raise _bad(f"{path}.{k}", "looks like an embedded secret; reference a file via "
                       "credentials_file instead")
    for k in v:
        if k not in allowed:
            raise _bad(f"{path}.{k}", "is not a known key")
    return v


def _bool(d: dict, key: str, path: str) -> bool:
    v = d[key]
    if not isinstance(v, bool):
        raise _bad(f"{path}.{key}", "must be a boolean (true/false), not a string or number")
    return v


def _num(d: dict, key: str, path: str, *, lo: float, lo_incl: bool, hi: float | None = None,
         hi_incl: bool = False, integer: bool = False) -> float:
    v = d[key]
    ok_type = isinstance(v, int) if integer else isinstance(v, (int, float))
    if isinstance(v, bool) or not ok_type:
        raise _bad(f"{path}.{key}", "must be " + ("an integer" if integer else "a number"))
    if v < lo or (v == lo and not lo_incl):
        raise _bad(f"{path}.{key}", f"must be {'>=' if lo_incl else '>'} {lo}")
    if hi is not None and (v > hi or (v == hi and not hi_incl)):
        raise _bad(f"{path}.{key}", f"must be {'<=' if hi_incl else '<'} {hi}")
    return v


def _strlist(d: dict, key: str, path: str) -> tuple[str, ...]:
    v = d[key]
    if not isinstance(v, list) or not all(isinstance(x, str) and x.strip() for x in v):
        raise _bad(f"{path}.{key}", "must be a list of non-empty strings")
    return tuple(v)


def _filled(section: dict, defaults: dict, path: str) -> dict:
    """Section with defaults filled in (a bare ``validate({})`` is the safe default)."""
    sec = _mapping(section if section is not None else {}, path, set(defaults))
    return {**defaults, **sec}


def _defaults() -> dict[str, Any]:
    # Local import: config.py imports this module lazily too; avoids a cycle.
    from .config import DEFAULTS
    return DEFAULTS["fabric_build_cache"]


def validate(section: dict[str, Any] | None) -> BuildCacheConfig:
    """Validate a ``fabric_build_cache`` mapping; raise ``InvalidError`` on any fault."""
    base = _defaults()
    p = "fabric_build_cache"
    top = _filled(section, base, p)
    warnings: list[str] = []

    enabled = _bool(top, "enabled", p)
    mode_raw = top["mode"]
    try:
        mode = Mode(mode_raw)
    except ValueError:
        raise _bad(f"{p}.mode", f"must be one of {[m.value for m in Mode]}") from None
    allowed_hosts = _strlist(top, "allowed_hosts", p)

    sp = f"{p}.shared_cache"
    s = _filled(top["shared_cache"], base["shared_cache"], sp)
    s_enabled = _bool(s, "enabled", sp)
    writes = _bool(s, "writes_enabled", sp)
    tls = _bool(s, "tls_required", sp)
    ep = s["endpoint_ref"]
    if ep is not None:
        if not isinstance(ep, str) or any(c in ep for c in ("@", "/", " ", "\t", "\n")) \
                or "://" in ep or not _ENDPOINT_RE.match(ep):
            raise _bad(f"{sp}.endpoint_ref", "must be an identifier like 'build-cache' "
                       "(^[a-z0-9][a-z0-9._-]{0,62}$), not a URL")
    if s_enabled and ep is None:
        raise _bad(f"{sp}.endpoint_ref", "is required when shared_cache.enabled")
    if not tls and not (ep is not None and ep.endswith(LOCAL_TEST_SUFFIX)):
        raise _bad(f"{sp}.tls_required", f"may be false only for endpoint_ref ending "
                   f"'{LOCAL_TEST_SUFFIX}' (loopback test)")
    if not isinstance(s["namespace"], str) or not _ENDPOINT_RE.match(s["namespace"]):
        raise _bad(f"{sp}.namespace", "must match ^[a-z0-9][a-z0-9._-]{0,62}$")
    max_total = _num(s, "max_total_gib", sp, lo=0, lo_incl=False)
    cred = s["credentials_file"]
    if cred is not None and (not isinstance(cred, str) or not os.path.isabs(cred)):
        raise _bad(f"{sp}.credentials_file", "must be an absolute path string (path only; "
                   "never the secret itself)")
    reg = s["registry_file"]
    if reg is not None and (not isinstance(reg, str) or not os.path.isabs(reg)):
        raise _bad(f"{sp}.registry_file", "must be an absolute path string to the protected "
                   "service registry file")
    if writes and not s_enabled:
        raise _bad(f"{sp}.writes_enabled", "requires shared_cache.enabled")

    lp = f"{p}.local"
    lo = _filled(top["local"], base["local"], lp)
    capacity = _num(lo, "capacity_gib", lp, lo=0, lo_incl=False)
    ram = _num(lo, "ram_scratch_gib", lp, lo=0, lo_incl=True)
    free = _num(lo, "min_free_disk_percent", lp, lo=0, lo_incl=True, hi=100)
    incr = _bool(lo, "incremental_disabled_for_cacheable_builds", lp)

    xp = f"{p}.execution"
    ex = _filled(top["execution"], base["execution"], xp)
    slurm = _bool(ex, "slurm_enabled", xp)
    clusters = _strlist(ex, "allowed_clusters", xp)
    parts = _strlist(ex, "allowed_partitions", xp)
    gpu = _bool(ex, "gpu_for_cpu_builds", xp)
    try:
        fb = RemoteFallback(ex["remote_fallback"])
    except ValueError:
        raise _bad(f"{xp}.remote_fallback", "must be 'local' or 'fail'") from None
    slots = int(_num(ex, "max_build_slots", xp, lo=1, lo_incl=True, hi=16, hi_incl=True,
                     integer=True))
    if gpu:
        warnings.append(f"{xp}.gpu_for_cpu_builds is true: CPU builds may occupy GPU nodes")

    cp = f"{p}.cleanup"
    cl = _filled(top["cleanup"], base["cleanup"], cp)
    dry = _bool(cl, "dry_run", cp)
    idle = _num(cl, "min_idle_hours", cp, lo=1, lo_incl=True)
    evict = _num(cl, "max_evict_per_pass_gib", cp, lo=0, lo_incl=False)
    if not dry:
        warnings.append(f"{cp}.dry_run is false: cleanup will really delete cache entries")

    op = f"{p}.observability"
    ob = _filled(top["observability"], base["observability"], op)
    obs = _bool(ob, "enabled", op)
    exp = _bool(ob, "export_existing_metrics_pipeline", op)

    if mode is Mode.SHARED_CACHE and not s_enabled:
        raise _bad(f"{p}.mode", "shared_cache requires shared_cache.enabled")
    if mode in (Mode.SLURM_FULL_BUILD, Mode.DISTRIBUTED):
        if not slurm:
            raise _bad(f"{p}.mode", f"{mode.value} requires execution.slurm_enabled")
        if not clusters:
            raise _bad(f"{xp}.allowed_clusters", f"must be non-empty for mode {mode.value}")
        if not parts:
            raise _bad(f"{xp}.allowed_partitions", f"must be non-empty for mode {mode.value}")

    return BuildCacheConfig(
        enabled=enabled, mode=mode, allowed_hosts=allowed_hosts,
        shared_cache=SharedCache(s_enabled, ep, s["namespace"], writes, max_total, tls, cred, reg),
        local=Local(capacity, ram, free, incr),
        execution=Execution(slurm, clusters, parts, gpu, fb, slots),
        cleanup=Cleanup(dry, idle, evict),
        observability=Observability(obs, exp),
        warnings=tuple(warnings),
    )


def check_credentials_file(path: str, label: str = "credentials_file") -> Path:
    """Lazy check: regular file, not a symlink, owned by us, no group/other bits.

    ``label`` names the config key in error messages (also used for registry_file).
    """
    p = Path(path)
    try:
        st = p.lstat()
    except OSError as exc:
        raise InvalidError(f"{label} {path} is not readable: {exc.strerror}") from None
    if stat.S_ISLNK(st.st_mode):
        raise InvalidError(f"{label} {path} must not be a symlink")
    if not stat.S_ISREG(st.st_mode):
        raise InvalidError(f"{label} {path} must be a regular file")
    if st.st_uid != os.geteuid():
        raise InvalidError(f"{label} {path} must be owned by the current user")
    if st.st_mode & 0o077:
        raise InvalidError(f"{label} {path} must not be accessible by group/other "
                           "(chmod 600)")
    return p

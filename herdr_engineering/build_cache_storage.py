"""Central store adapter (spec 04): sccache multilevel config for an S3 backend.

Everything here is configuration only. sccache keeps hashing compiler, flags and
inputs itself; we add POLICY partitioning (namespace / os-arch / toolchain digest
as the S3 key prefix) and never hand-write cache keys. See docs/build-cache-storage.md
for the upstream facts this relies on and what is still UNVERIFIED.

Safety rules enforced here:
* the config only names an endpoint; host/region/bucket come from a protected
  YAML registry file (regular file, no symlink, owner == me, mode 0600);
* credentials are read from a protected file and exist only as an env mapping
  for the child process (``Credentials.as_child_env``): never in the TOML, never
  in an event, never in ``repr``;
* any storage problem yields a typed :class:`StorageError` whose ``code`` becomes
  ``shared_unavailable:<code>``; the caller then builds with the local cache.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import ssl
import tempfile
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from .build_cache_config import BuildCacheConfig, Mode, check_credentials_file
from .errors import InvalidError

# Oldest sccache we rely on: 0.19.0 release notes carry the multilevel fixes we
# need (optional slower level no longer blocks start; per-level write policy).
# Multilevel itself first shipped in 0.15.0 (UNVERIFIED config spelling there).
MIN_SCCACHE_VERSION: tuple[int, int, int] = (0, 19, 0)
MAX_PREFIX_LEN = 160
HEALTH_TIMEOUT_S = 2.0
HEALTH_NEGATIVE_TTL_S = 30.0

_HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?(:[0-9]{1,5})?$")
_BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
_REGION_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
_PREFIX_PART_RE = re.compile(r"[^a-z0-9._-]+")
_CRED_VALUE_RE = re.compile(r"^[\x21-\x7e]{1,2048}$")  # printable ASCII, no whitespace
_VERSION_RE = re.compile(r"\bsccache\s+v?(\d+)\.(\d+)\.(\d+)")


class Code(StrEnum):
    """Stable reasons a shared cache is unavailable (suffix of shared_unavailable:)."""
    NOT_SHARED_MODE = "mode_not_shared"
    NOT_ENABLED = "not_enabled"
    NO_REGISTRY = "registry_not_configured"
    REGISTRY_UNREADABLE = "registry_unreadable"
    REGISTRY_INVALID = "registry_invalid"
    UNKNOWN_ENDPOINT = "unknown_endpoint"
    TLS_REQUIRED = "tls_required"
    NO_CREDENTIALS = "credentials_not_configured"
    CREDENTIALS_UNREADABLE = "credentials_unreadable"
    CREDENTIALS_INVALID = "credentials_invalid"
    SCCACHE_VERSION_UNKNOWN = "sccache_version_unknown"
    SCCACHE_VERSION_UNSUPPORTED = "sccache_version_unsupported"
    ENDPOINT_UNREACHABLE = "endpoint_unreachable"
    CONFIG_WRITE_FAILED = "config_write_failed"
    NO_FINGERPRINT = "no_fingerprint"
    # Credentials must never reach cargo/rustc/build scripts; until an explicit
    # sccache server start (credentials in the SERVER env only) exists, a fully
    # valid shared request runs with the local cache only.
    SHARED_REQUIRES_SERVER = "shared_requires_server"


class StorageError(Exception):
    """Typed storage problem. Message never contains secrets."""

    def __init__(self, code: Code, message: str = "") -> None:
        super().__init__(message or code.value)
        self.code = code


@dataclass(frozen=True)
class Endpoint:
    url_host: str  # host[:port], no scheme
    region: str
    bucket: str
    use_ssl: bool


# --- 1. registry --------------------------------------------------------------

def resolve_endpoint(endpoint_ref: str | None, registry_file: str | None) -> Endpoint:
    """Map the config identifier to host/region/bucket via the protected registry.

    Registry YAML::

        endpoints:
          build-cache:
            host: s3.example.org:9000   # host[:port], never a URL
            region: us-east-1
            bucket: sccache
            use_ssl: true               # optional, default true
    """
    if not endpoint_ref:
        raise StorageError(Code.UNKNOWN_ENDPOINT, "no endpoint_ref configured")
    if not registry_file:
        raise StorageError(Code.NO_REGISTRY)
    try:
        path = check_credentials_file(registry_file, "registry_file")
    except InvalidError as exc:
        raise StorageError(Code.REGISTRY_UNREADABLE, str(exc)) from None
    data = _load_yaml(path, Code.REGISTRY_UNREADABLE, Code.REGISTRY_INVALID)
    eps = data.get("endpoints") if isinstance(data, dict) else None
    if not isinstance(eps, dict) or set(data) - {"endpoints"}:
        raise StorageError(Code.REGISTRY_INVALID, "registry must be {endpoints: {...}}")
    entry = eps.get(endpoint_ref)
    if entry is None:
        raise StorageError(Code.UNKNOWN_ENDPOINT, "endpoint_ref is not in the registry")
    if not isinstance(entry, dict) or set(entry) - {"host", "region", "bucket", "use_ssl"}:
        raise StorageError(Code.REGISTRY_INVALID, "endpoint entry has unknown keys")
    host, region, bucket = entry.get("host"), entry.get("region"), entry.get("bucket")
    use_ssl = entry.get("use_ssl", True)
    ok = (isinstance(host, str) and _HOST_RE.match(host) and isinstance(region, str)
          and _REGION_RE.match(region) and isinstance(bucket, str)
          and _BUCKET_RE.match(bucket) and isinstance(use_ssl, bool))
    if not ok:
        raise StorageError(Code.REGISTRY_INVALID, "endpoint entry has invalid host/region/bucket")
    if ":" in host and not 0 < int(host.rsplit(":", 1)[1]) < 65536:
        raise StorageError(Code.REGISTRY_INVALID, "endpoint port out of range")
    return Endpoint(host, region, bucket, use_ssl)


def _load_yaml(path: Path, unreadable: Code, invalid: Code) -> Any:
    try:
        import yaml
    except ImportError:  # pragma: no cover - PyYAML is a declared dependency
        raise StorageError(unreadable, "PyYAML is required") from None
    try:
        return yaml.safe_load(path.read_text())
    except (OSError, UnicodeDecodeError):
        raise StorageError(unreadable, f"{path.name} is not readable") from None
    except yaml.YAMLError:
        raise StorageError(invalid, f"{path.name} is not valid YAML") from None


# --- 2. namespace prefix ------------------------------------------------------

def _clean(part: str) -> str:
    return _PREFIX_PART_RE.sub("-", part.lower()).strip("-.") or "unknown"


def namespace_prefix(namespace: str, toolchain_digest: str, host_os: str, arch: str) -> str:
    """``<namespace>/<os>-<arch>/<toolchain digest[:16]>/`` (policy partition only).

    Only ``[a-z0-9._-/]``; no hostnames or paths. sccache still hashes compiler,
    flags and inputs itself: this prefix only separates trust domain, OS/arch and
    toolchain so a different one can never be served.
    """
    digest = re.sub(r"[^0-9a-f]", "", toolchain_digest.lower())[:16]
    if len(digest) < 16:
        raise StorageError(Code.NO_FINGERPRINT, "toolchain digest too short")
    prefix = f"{_clean(namespace)}/{_clean(host_os)[:24]}-{_clean(arch)[:24]}/{digest}/"
    if len(prefix) > MAX_PREFIX_LEN:
        raise StorageError(Code.REGISTRY_INVALID, "namespace prefix too long")
    return prefix


def os_arch_from_triple(triple: str) -> tuple[str, str]:
    """('linux', 'x86_64') from a rustc host triple; pure, no platform probing."""
    parts = triple.split("-")
    arch = parts[0] if parts else "unknown"
    if "darwin" in triple:
        return "darwin", arch
    if len(parts) >= 3:
        return parts[2] if parts[2] != "none" else parts[-1], arch
    return "unknown", arch


def namespace_digest(prefix: str) -> str:
    """Short digest of the prefix for events (does not reveal the namespace text)."""
    return hashlib.sha256(prefix.encode()).hexdigest()[:16]


# --- 3. credentials -----------------------------------------------------------

@dataclass(frozen=True)
class Credentials:
    """Static S3 credentials; fields are hidden from repr/str."""
    access_key_id: str
    secret_access_key: str
    session_token: str | None = None

    def __repr__(self) -> str:
        return "Credentials(<redacted>)"

    __str__ = __repr__

    def as_child_env(self) -> dict[str, str]:
        env = {"AWS_ACCESS_KEY_ID": self.access_key_id,
               "AWS_SECRET_ACCESS_KEY": self.secret_access_key}
        if self.session_token:
            env["AWS_SESSION_TOKEN"] = self.session_token
        return env


def read_credentials(credentials_file: str | None) -> Credentials:
    """Read ``access_key_id`` / ``secret_access_key`` [/ ``session_token``] (YAML)."""
    if not credentials_file:
        raise StorageError(Code.NO_CREDENTIALS)
    try:
        path = check_credentials_file(credentials_file)
    except InvalidError as exc:
        raise StorageError(Code.CREDENTIALS_UNREADABLE, str(exc)) from None
    data = _load_yaml(path, Code.CREDENTIALS_UNREADABLE, Code.CREDENTIALS_INVALID)
    allowed = {"access_key_id", "secret_access_key", "session_token"}
    if not isinstance(data, dict) or set(data) - allowed:
        raise StorageError(Code.CREDENTIALS_INVALID, "unexpected credentials file layout")
    values = {k: data.get(k) for k in allowed}
    for key in ("access_key_id", "secret_access_key"):
        if not isinstance(values[key], str) or not _CRED_VALUE_RE.match(values[key]):
            raise StorageError(Code.CREDENTIALS_INVALID, f"{key} missing or malformed")
    tok = values["session_token"]
    if tok is not None and (not isinstance(tok, str) or not _CRED_VALUE_RE.match(tok)):
        raise StorageError(Code.CREDENTIALS_INVALID, "session_token malformed")
    return Credentials(values["access_key_id"], values["secret_access_key"], tok)


# --- version gate -------------------------------------------------------------

def parse_sccache_version(text: str | None) -> tuple[int, int, int] | None:
    m = _VERSION_RE.search(text or "")
    return (int(m[1]), int(m[2]), int(m[3])) if m else None


def check_sccache_version(version_text: str | None) -> None:
    v = parse_sccache_version(version_text)
    if v is None:
        raise StorageError(Code.SCCACHE_VERSION_UNKNOWN)
    if v < MIN_SCCACHE_VERSION:
        raise StorageError(Code.SCCACHE_VERSION_UNSUPPORTED,
                           f"sccache {'.'.join(map(str, v))} < "
                           f"{'.'.join(map(str, MIN_SCCACHE_VERSION))}")


# --- 4/5. sccache config ------------------------------------------------------

@dataclass(frozen=True)
class StorageInputs:
    endpoint: Endpoint
    prefix: str
    disk_dir: Path
    disk_size_bytes: int
    writes_enabled: bool
    tls_required: bool


def _q(value: str) -> str:
    """TOML basic string. Inputs are validated ASCII; json escaping is TOML-compatible."""
    return json.dumps(value, ensure_ascii=True)


def render_sccache_config(inp: StorageInputs) -> str:
    """TOML for SCCACHE_CONF: disk level then s3 level. Contains NO secrets.

    ``write_error_policy = "ignore"``: a failing write to any level never fails a
    compile. Read-only is NOT in the file: the documented mechanism is the env var
    SCCACHE_S3_RW_MODE=READ_ONLY (see :func:`shared_env`); a TOML ``rw_mode`` key
    for s3 is UNVERIFIED so it is deliberately not emitted.
    """
    ep = inp.endpoint
    if inp.tls_required and not ep.use_ssl:
        raise StorageError(Code.TLS_REQUIRED, "plaintext endpoint refused (tls_required)")
    return "\n".join([
        "# generated by herdr-eng build-coordinate; credentials come from the child env only",
        "[cache.multilevel]",
        'chain = ["disk", "s3"]',
        'write_error_policy = "ignore"',
        "",
        "[cache.disk]",
        f"dir = {_q(str(inp.disk_dir))}",
        f"size = {int(inp.disk_size_bytes)}",
        "",
        "[cache.s3]",
        f"bucket = {_q(ep.bucket)}",
        f"endpoint = {_q(ep.url_host)}",
        f"region = {_q(ep.region)}",
        f"key_prefix = {_q(inp.prefix)}",
        f"use_ssl = {'true' if ep.use_ssl else 'false'}",
        "",
    ])


def shared_env(inp: StorageInputs, conf_path: Path) -> dict[str, str]:
    """Non-secret env additions for the child when the shared level is active."""
    env = {"SCCACHE_CONF": str(conf_path)}
    if not inp.writes_enabled:
        env["SCCACHE_S3_RW_MODE"] = "READ_ONLY"
    return env


SHARED_ENV_KEYS = ("SCCACHE_CONF", "SCCACHE_S3_RW_MODE")


def write_config_atomic(path: Path, text: str) -> None:
    """tempfile in the same dir (0600) + os.replace; raises OSError on failure."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(prefix=".sccache.toml.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --- plan integration helper --------------------------------------------------

@dataclass(frozen=True)
class SharedSetup:
    inputs: StorageInputs
    credentials: Credentials
    conf_path: Path
    toml: str
    namespace_digest: str


def prepare_shared(config: BuildCacheConfig, *, sccache_version: str | None,
                   toolchain_digest: str, host_triple: str, state_dir: Path,
                   reachable: bool | None) -> SharedSetup:
    """Resolve everything the shared level needs or raise StorageError. No writes."""
    sc = config.shared_cache
    if not sc.enabled:
        raise StorageError(Code.NOT_ENABLED)
    check_sccache_version(sccache_version)
    endpoint = resolve_endpoint(sc.endpoint_ref, sc.registry_file)
    if sc.tls_required and not endpoint.use_ssl:
        raise StorageError(Code.TLS_REQUIRED, "registry endpoint is plaintext")
    creds = read_credentials(sc.credentials_file)
    if reachable is False:
        raise StorageError(Code.ENDPOINT_UNREACHABLE)
    host_os, arch = os_arch_from_triple(host_triple)
    prefix = namespace_prefix(sc.namespace, toolchain_digest, host_os, arch)
    cache_dir = state_dir / "sccache"
    inp = StorageInputs(endpoint, prefix, cache_dir,
                        max(1, round(config.local.capacity_gib * 1024 ** 3)),
                        config.shared_writes_effective, sc.tls_required)
    return SharedSetup(inp, creds, cache_dir / "sccache.toml", render_sccache_config(inp),
                       namespace_digest(prefix))


# --- 6. health probe ----------------------------------------------------------

@dataclass(frozen=True)
class Health:
    ok: bool
    code: str  # "ok" | "cached_down" | "connect_failed" | "tls_failed"


def _neg_path(state_dir: Path, ep: Endpoint) -> Path:
    key = hashlib.sha256(f"{ep.url_host}|{ep.use_ssl}".encode()).hexdigest()[:16]
    return state_dir / "sccache" / f"health-down-{key}"


def probe_health(ep: Endpoint, timeout: float = HEALTH_TIMEOUT_S, *,
                 state_dir: Path | None = None, ttl: float = HEALTH_NEGATIVE_TTL_S,
                 clock=time.time, context: ssl.SSLContext | None = None) -> Health:
    """Bounded TCP (+TLS handshake) reachability check. No S3 call, no credentials.

    A failure is remembered for ``ttl`` seconds in ``state_dir`` so a dead endpoint
    costs one probe per TTL, not one per build. Successes are never cached.
    """
    neg = _neg_path(state_dir, ep) if state_dir is not None else None
    if neg is not None:
        try:
            if clock() - neg.stat().st_mtime < ttl:
                return Health(False, "cached_down")
        except OSError:
            pass
    result = _probe(ep, timeout, context)
    if neg is not None:
        try:
            if result.ok:
                neg.unlink(missing_ok=True)
            else:
                neg.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                neg.write_text("")
                os.utime(neg, (clock(), clock()))
        except OSError:
            pass
    return result


def _probe(ep: Endpoint, timeout: float, context: ssl.SSLContext | None) -> Health:
    host, _, port_s = ep.url_host.partition(":")
    port = int(port_s) if port_s else (443 if ep.use_ssl else 80)
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
    except OSError:
        return Health(False, "connect_failed")
    try:
        if ep.use_ssl:
            ctx = context or ssl.create_default_context()
            sock.settimeout(timeout)
            with ctx.wrap_socket(sock, server_hostname=host):
                pass
        return Health(True, "ok")
    except (OSError, ssl.SSLError):
        return Health(False, "tls_failed")
    finally:
        sock.close()


def check_shared(config: BuildCacheConfig, state_dir: Path,
                 timeout: float = HEALTH_TIMEOUT_S) -> bool | None:
    """Pre-build reachability: True/False, or None when not applicable / unresolvable.

    Unresolvable endpoints return None; ``plan`` reports the precise code itself.
    """
    sc = config.shared_cache
    if not (config.enabled and sc.enabled and config.effective_mode is Mode.SHARED_CACHE):
        return None
    try:
        ep = resolve_endpoint(sc.endpoint_ref, sc.registry_file)
    except StorageError:
        return None
    return probe_health(ep, timeout, state_dir=state_dir).ok


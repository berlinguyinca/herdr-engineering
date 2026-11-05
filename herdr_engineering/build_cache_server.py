"""Explicit per-policy sccache server start for the shared (rustfs) tier.

sccache fixes its configuration when the server starts, and a server inherits
the environment of whoever spawned it. The shared tier therefore needs the build
wrapper to start (or reuse) the server on the policy port BEFORE cargo runs:

* the credentials and the rustfs settings live in THAT process's environment
  only; cargo, rustc and build scripts get neither (they only learn the port);
* an already-listening server is trusted only after ``--show-stats`` reports a
  multilevel chain whose last level is the expected bucket and key prefix; a
  local-only server that a bare ``rustc`` wrapper spawned on that port (client
  env, no credentials) is stopped and replaced;
* the server is started with ``close_fds=True`` so it holds none of the gate's
  slot or maintenance lock descriptors, and it is reaped by
  ``SCCACHE_IDLE_TIMEOUT``;
* the tier is reported active only after that check. Any failure returns a
  :class:`Code` and the caller builds on the local tier (a distinct port).

Limits (documented, measured): sccache does not authenticate cached objects, and
code running as the same user can read the server's ``/proc/<pid>/environ`` and
the 0600 credentials file. The mitigation is a bucket-limited rustfs user, a
bucket quota, and the read-only user for untrusted canaries.
"""
from __future__ import annotations

import datetime
import hashlib
import hmac
import http.client
import json
import os
import socket
import subprocess
import time
from collections.abc import Mapping
from dataclasses import dataclass

from .build_cache_storage import Code, Credentials, Endpoint, SharedSetup, write_config_atomic

IDLE_TIMEOUT_S = 600
START_TIMEOUT_S = 20.0
STATS_TIMEOUT_S = 10.0
_STOP_WAIT_S = 5.0
# Everything sccache or the AWS SDK would read from the ambient environment.
_STRIPPED_PREFIXES = ("SCCACHE_", "AWS_")
_STRIPPED = ("RUSTC_WRAPPER", "RUSTC_WORKSPACE_WRAPPER", "CARGO_BUILD_RUSTC_WRAPPER")


@dataclass(frozen=True)
class ServerResult:
    ok: bool
    code: Code | None = None  # None when ok


def server_env(setup: SharedSetup, base_env: Mapping[str, str], port: int,
               idle_timeout: int = IDLE_TIMEOUT_S) -> dict[str, str]:
    """The environment of the sccache SERVER process (the only one with secrets)."""
    env = {k: v for k, v in base_env.items()
           if not k.startswith(_STRIPPED_PREFIXES) and k not in _STRIPPED}
    inp = setup.inputs
    env.update(setup.credentials.as_child_env())
    env.update({
        "SCCACHE_CONF": str(setup.conf_path),
        "SCCACHE_SERVER_PORT": str(port),
        "SCCACHE_DIR": str(inp.disk_dir),
        "SCCACHE_CACHE_SIZE": str(inp.disk_size_bytes),
        "SCCACHE_IDLE_TIMEOUT": str(int(idle_timeout)),
    })
    if not inp.writes_enabled:
        env["SCCACHE_S3_RW_MODE"] = "READ_ONLY"
    return env


def _listening(port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def _stats(sccache: str, port: int) -> dict | None:
    """``--show-stats`` of the server ON ``port`` (never starts one: caller checked)."""
    env = {"PATH": os.environ.get("PATH", ""), "SCCACHE_SERVER_PORT": str(port)}
    try:
        r = subprocess.run([sccache, "--show-stats", "--stats-format", "json"], env=env,
                           capture_output=True, text=True, timeout=STATS_TIMEOUT_S,
                           close_fds=True, stdin=subprocess.DEVNULL)
        data = json.loads(r.stdout) if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def serves_expected_remote(stats: dict | None, bucket: str, prefix: str) -> bool:
    """True when the last cache level is s3 on ``bucket`` under ``prefix``."""
    levels = (stats or {}).get("stats", {}).get("multi_level") if stats else None
    if not isinstance(levels, list) or len(levels) < 2:
        return False
    loc = str(levels[-1].get("location", "")) if isinstance(levels[-1], dict) else ""
    return (loc.startswith("s3") and f"name: {bucket}," in loc
            and f"prefix: /{prefix.strip('/')}/" in loc)


def stop_server(sccache: str, port: int) -> None:
    env = {"PATH": os.environ.get("PATH", ""), "SCCACHE_SERVER_PORT": str(port)}
    try:
        subprocess.run([sccache, "--stop-server"], env=env, close_fds=True,
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        pass
    deadline = time.monotonic() + _STOP_WAIT_S
    while _listening(port, 0.2) and time.monotonic() < deadline:
        time.sleep(0.1)


def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def check_access(ep: Endpoint, creds: Credentials, prefix: str,
                 timeout: float = 5.0) -> Code | None:
    """One SigV4-signed, path-style ``ListObjectsV2`` (max-keys=1) under ``prefix``.

    sccache itself starts happily with rejected credentials and then only fails
    writes quietly, so the tier is verified here before it may be called active.
    Returns None when the endpoint accepted the request. Runs in the build
    wrapper (never in cargo); the secret is only used to sign, never sent.
    """
    host = ep.url_host
    query = f"list-type=2&max-keys=1&prefix={prefix.replace('/', '%2F')}"
    now = datetime.datetime.now(datetime.UTC)
    amz, day = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
    empty = hashlib.sha256(b"").hexdigest()
    headers = {"host": host, "x-amz-content-sha256": empty, "x-amz-date": amz}
    if creds.session_token:
        headers["x-amz-security-token"] = creds.session_token
    signed = ";".join(sorted(headers))
    canon = "\n".join(["GET", f"/{ep.bucket}/", query,
                       "".join(f"{k}:{headers[k]}\n" for k in sorted(headers)), signed, empty])
    scope = f"{day}/{ep.region}/s3/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz, scope, hashlib.sha256(canon.encode()).hexdigest()])
    k = _sign(_sign(_sign(_sign(("AWS4" + creds.secret_access_key).encode(), day), ep.region),
                    "s3"), "aws4_request")
    sig = hmac.new(k, to_sign.encode(), hashlib.sha256).hexdigest()
    auth = (f"AWS4-HMAC-SHA256 Credential={creds.access_key_id}/{scope}, "
            f"SignedHeaders={signed}, Signature={sig}")
    send = {**{h: v for h, v in headers.items() if h != "host"}, "Authorization": auth}
    name, _, port = host.partition(":")
    try:
        conn_cls = http.client.HTTPSConnection if ep.use_ssl else http.client.HTTPConnection
        conn = conn_cls(name, int(port) if port else None, timeout=timeout)
        try:
            conn.request("GET", f"/{ep.bucket}/?{query}", headers=send)
            status = conn.getresponse().status
        finally:
            conn.close()
    except (OSError, http.client.HTTPException):
        return Code.ENDPOINT_UNREACHABLE
    if status == 200:
        return None
    return Code.CREDENTIALS_REJECTED if status in (401, 403) else Code.SERVER_NOT_SHARED


def ensure_shared_server(setup: SharedSetup, base_env: Mapping[str, str], sccache: str,
                         port: int, idle_timeout: int = IDLE_TIMEOUT_S) -> ServerResult:
    """Start or reuse the policy server and verify it serves the shared level."""
    bucket, prefix = setup.inputs.endpoint.bucket, setup.inputs.prefix
    denied = check_access(setup.inputs.endpoint, setup.credentials, prefix)
    if denied is not None:
        return ServerResult(False, denied)
    if _listening(port):
        if serves_expected_remote(_stats(sccache, port), bucket, prefix):
            return ServerResult(True)
        stop_server(sccache, port)  # foreign / local-only server on our port
        if _listening(port, 0.2):
            return ServerResult(False, Code.SERVER_NOT_SHARED)
    try:
        write_config_atomic(setup.conf_path, setup.toml)
    except OSError:
        return ServerResult(False, Code.CONFIG_WRITE_FAILED)
    try:
        subprocess.run([sccache, "--start-server"], env=server_env(setup, base_env, port,
                                                                  idle_timeout),
                       close_fds=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=START_TIMEOUT_S, check=False)
    except (OSError, subprocess.SubprocessError):
        return ServerResult(False, Code.SERVER_START_FAILED)
    if not _listening(port):
        return ServerResult(False, Code.SERVER_START_FAILED)
    if serves_expected_remote(_stats(sccache, port), bucket, prefix):
        return ServerResult(True)
    stop_server(sccache, port)
    return ServerResult(False, Code.SERVER_NOT_SHARED)


def local_fallback_port(shared_port: int) -> int:
    """A distinct port for the local-tier server used when the shared tier fails."""
    base = 20000
    return base + (shared_port - base + 6007) % 12000


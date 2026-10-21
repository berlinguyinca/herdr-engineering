"""Host-side client for the central fleet configuration.

A host holds only ``~/.config/herdr-engineering/host.json`` (control plane URL,
host id, token). Everything else is fetched from ``GET /api/v1/fleet/config`` and
cached in ``~/.cache/herdr-engineering/fleet-config.json`` (0600) so a host keeps
working while the control plane is unreachable. A revoked token (HTTP 401)
deletes the cache: a revoked host must not keep running on stale secrets.

Neither the token nor any configuration value is ever put in an exception
message or log line.

Dotted-key vocabulary (shared with the JS plugin and pi-engineering):
ingest.api_key, agent.api_key, ci.url, ci.token, llm.upstream_url,
llm.upstream_key, inspector.enabled, inspector.reasoning, github.token,
fabric.url, fabric.token.
"""
from __future__ import annotations

import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

HOST_JSON_REL = Path(".config/herdr-engineering/host.json")
CACHE_REL = Path(".cache/herdr-engineering/fleet-config.json")
FLEET_PATH = "/api/v1/fleet/config"
APPLIED_PATH = "/api/v1/fleet/config/applied"
JOIN_HINT = "run `herdr-eng fleet join` for this host on the control-plane host"


class HostConfigUnavailable(RuntimeError):
    """No usable host configuration (never carries a token or a value).

    ``revoked`` is True only for HTTP 401 from the fleet endpoint."""

    def __init__(self, message: str = "", *, revoked: bool = False):
        super().__init__(message)
        self.revoked = revoked


class HostConfig:
    def __init__(self, control_plane_url: str, host_id: str, revision: str,
                 values: dict[str, dict[str, Any]], source: str = "fetch"):
        self.source = source  # "fetch" (fresh from the control plane) or "cache"
        self.control_plane_url = control_plane_url
        self.host_id = host_id
        self.revision = revision
        self._values = values

    def get(self, dotted_key: str, default: Any = None) -> Any:
        section, _, key = dotted_key.partition(".")
        node = self._values.get(section)
        if not isinstance(node, dict) or key not in node:
            return default
        value = node[key]
        return default if value is None else value

    @property
    def api_key(self) -> str | None:
        return self.get("ingest.api_key")

    def __repr__(self) -> str:  # never leak values
        return f"HostConfig(host_id={self.host_id!r}, revision={self.revision!r})"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: the token must go only to host.json's URL."""

    def redirect_request(self, *args, **kwargs):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect())


def _default_fetch(url: str, *, token: str, timeout: float, method: str = "GET",
                   body: dict | None = None) -> tuple[int, Any]:
    """(status, parsed JSON or None). HTTP errors return their status; network
    errors and timeouts raise OSError."""
    data = json.dumps(body).encode() if body is not None else None
    headers = {"X-Host-Token": token, "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with _OPENER.open(req, timeout=timeout) as resp:  # noqa: S310
            raw = resp.read(4 * 1024 * 1024)
            status = resp.status
    except urllib.error.HTTPError as exc:
        return exc.code, None
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise OSError(type(exc).__name__) from None
    try:
        return status, json.loads(raw) if raw else None
    except ValueError:
        return status, None


def _read_host_json(home: Path) -> dict:
    path = home / HOST_JSON_REL
    try:
        data = json.loads(path.read_text())
        url, hid, tok = data["control_plane_url"], data["host_id"], data["token"]
    except (OSError, ValueError, KeyError, TypeError):
        raise HostConfigUnavailable(
            f"host configuration missing or unreadable at {path}; {JOIN_HINT}") from None
    if not all(isinstance(x, str) and x for x in (url, hid, tok)):
        raise HostConfigUnavailable(f"host configuration at {path} is incomplete; {JOIN_HINT}")
    return {"control_plane_url": url.rstrip("/"), "host_id": hid, "token": tok}


def _write_cache(path: Path, payload: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(payload, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_cache(path: Path, host_id: str) -> tuple[str, dict] | None:
    try:
        data = json.loads(path.read_text())
        if data.get("host_id") != host_id:
            return None
        rev, values = data["revision"], data["values"]
    except (OSError, ValueError, KeyError, AttributeError, TypeError):
        return None
    return (rev, values) if isinstance(rev, str) and isinstance(values, dict) else None


def _discard(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def load_host_config(*, home: Path | None = None, fetch: Callable | None = None,
                     timeout: float = 3.0) -> HostConfig:
    home = Path(home) if home is not None else Path.home()
    fetch = fetch or _default_fetch
    hj = _read_host_json(home)
    cache_path = home / CACHE_REL
    base, token, host_id = hj["control_plane_url"], hj["token"], hj["host_id"]

    status, payload = None, None
    try:
        status, payload = fetch(base + FLEET_PATH, token=token, timeout=timeout)
    except Exception:  # network failure, timeout: fall through to the cache
        status = None
    if status == 401:
        _discard(cache_path)
        raise HostConfigUnavailable(
            "the control plane rejected this host's token (revoked or unknown); "
            f"cached configuration was discarded; {JOIN_HINT}", revoked=True)
    if status == 200 and isinstance(payload, dict) \
            and isinstance(payload.get("revision"), str) and isinstance(payload.get("values"), dict):
        revision, values = payload["revision"], payload["values"]
        try:
            _write_cache(cache_path, {"host_id": host_id, "revision": revision, "values": values})
        except OSError:
            pass  # a cache we cannot write must not break a live fetch
        try:
            fetch(base + APPLIED_PATH, token=token, timeout=timeout, method="POST",
                  body={"host_id": host_id, "revision": revision})
        except Exception:
            pass
        return HostConfig(base, host_id, revision, values, "fetch")
    if status is not None and 400 <= status < 500:
        raise HostConfigUnavailable(
            f"the control plane refused the configuration request (HTTP {status})")
    cached = _read_cache(cache_path, host_id)  # unreachable, 5xx or malformed reply
    if cached is None:
        raise HostConfigUnavailable(
            f"control plane at {base} unreachable and no cached configuration exists")
    return HostConfig(base, host_id, cached[0], cached[1], "cache")


# --------------------------------------------------------------------------
# process-wide accessors
# --------------------------------------------------------------------------

_MEMO: dict[str, Any] = {}
_SOURCE: list[Callable[[], HostConfig]] = []


def install_source(source: Callable[[], HostConfig] | None) -> None:
    """Let a process that owns the fleet file (the control-plane web) answer
    ``host_config()`` from it directly instead of fetching from itself."""
    _SOURCE.clear()
    if source is not None:
        _SOURCE.append(source)


def host_config() -> HostConfig:
    """Memoised for tools that read the configuration once at startup. A failure
    is memoised too, so a hot path does not retry the network on every call."""
    if _SOURCE:
        return _SOURCE[0]()
    if "v" not in _MEMO:
        try:
            _MEMO["v"] = load_host_config()
        except HostConfigUnavailable as exc:
            _MEMO["v"] = exc
    value = _MEMO["v"]
    if isinstance(value, HostConfigUnavailable):
        raise HostConfigUnavailable(str(value), revoked=value.revoked)
    return value


def reset_host_config() -> None:
    _MEMO.clear()
    _SOURCE.clear()


def host_config_or_none() -> HostConfig | None:
    try:
        return host_config()
    except HostConfigUnavailable:
        return None


def host_value(dotted_key: str, default: Any = None) -> Any:
    """Soft read for library code: the value, or ``default`` when this host has no
    configuration (callers then behave as 'not configured')."""
    cfg = host_config_or_none()
    return default if cfg is None else cfg.get(dotted_key, default)


def control_plane_url_or(default: str = "") -> str:
    cfg = host_config_or_none()
    return cfg.control_plane_url if cfg else default


REFRESH_INTERVAL = 300.0


class ConfigRefresher:
    """Keeps a long-running daemon's host config current (shared by every
    Python loop daemon; mirrors the JS plugin).

    ``current()`` re-runs ``load_host_config()`` every ``interval`` seconds, and on
    the next call after ``note_auth_rejected()`` (an ingest POST answered 401/403).
    Only a fresh fetch (``source == "fetch"``) replaces the live config; a cache
    fallback or any non-revoked failure keeps it and the daemon carries on, with
    one stderr line per outage. A refresh forced by an auth rejection that yields
    no fresh fetch does not advance the timer, so the next pass retries. A revoked
    token raises HostConfigUnavailable(revoked=True): the daemon must exit 2.
    """

    def __init__(self, initial: HostConfig, *, loader: Callable[[], HostConfig] | None = None,
                 interval: float = REFRESH_INTERVAL, clock: Callable[[], float] = time.monotonic,
                 err=None):
        self._live = initial
        self._loader = loader
        self._interval = interval
        self._clock = clock
        self._err = err
        self._loaded_at = clock()
        self._rejected = False
        self._outage = False

    def note_auth_rejected(self) -> None:
        self._rejected = True

    def current(self) -> HostConfig:
        forced, self._rejected = self._rejected, False
        if forced or self._clock() - self._loaded_at >= self._interval:
            self._refresh(advance_on_stale=not forced)
        return self._live

    def sync(self, client, *, follow_url: bool = True, follow_key: bool = True) -> HostConfig:
        """One daemon pass: note an auth rejection seen by ``client`` (it has
        ``auth_rejected``, ``retarget(url, api_key)``), refresh if due, and point the
        client at the live config. Raises HostConfigUnavailable(revoked=True)."""
        if client.auth_rejected:
            self.note_auth_rejected()
        client.auth_rejected = False
        cfg = self.current()
        client.retarget(cfg.control_plane_url if follow_url else None,
                        (cfg.api_key or "") if follow_key else None)
        return cfg

    def _warn(self) -> None:
        if not self._outage:
            self._outage = True
            print("herdr-eng: host config refresh did not reach the control plane; "
                  "continuing with the last known configuration",
                  file=self._err or sys.stderr, flush=True)

    def _refresh(self, *, advance_on_stale: bool) -> None:
        try:
            cfg = (self._loader or (lambda: load_host_config()))()
        except HostConfigUnavailable as exc:
            if exc.revoked:
                raise
            if advance_on_stale:
                self._loaded_at = self._clock()
            self._warn()
            return
        if cfg.source == "fetch":
            self._live, self._loaded_at, self._outage = cfg, self._clock(), False
            return
        if advance_on_stale:
            self._loaded_at = self._clock()
        self._warn()


def resolve_control_plane(explicit: str | None = None) -> str:
    """``--control-plane`` override if given, else host.json's URL (raises
    HostConfigUnavailable). Explicit values never touch the network."""
    if explicit:
        return explicit.rstrip("/")
    return host_config().control_plane_url


def exit_unavailable(exc: HostConfigUnavailable) -> int:
    """Daemon startup: message to stderr, exit status 2, no traceback."""
    print(f"herdr-eng: {exc}", file=sys.stderr)
    return 2

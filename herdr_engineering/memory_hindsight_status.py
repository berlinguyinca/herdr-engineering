"""Status of the Hindsight shared-memory backend, for ``herdr-eng doctor`` and ``herdr-eng memory`` installers.

Two roles, both read-only, both honest:

* BACKEND host (``~/.local/state/herdr-engineering/memory/deployed.json`` exists, written by
  ``deploy.sh --with-memory`` on success): container health of ``herdr-hindsight`` and ``herdr-hindsight-db``,
  the loopback ``/health``, the age of the newest backup, and the backup timer.
* CLIENT host (a herdr-memory config with a ``url``, or the fleet slice's ``memory.url``): the endpoint must be
  https (or loopback) and ``<url>/health`` is probed.

Status vocabulary: ``ok | warn | fail | unknown``. ``unknown`` means "could not find out" (no docker, daemon not
reachable, host unreachable, directory unreadable); it is never reported as ok. A fact that was learned (a container
that is not there, an unhealthy container, a stale backup) is ``warn``/``fail``. No value of any secret is read:
only container state, an unauthenticated ``/health`` GET, file names and mtimes.

``discover_endpoint`` is the hand-off for ``herdr-eng memory install``: it returns the URL a host should write as
``url`` in ``~/.config/herdr-memory/config.json`` (the file's own url, when present, always wins).
"""
from __future__ import annotations

import ipaddress
import json
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

DB_CONTAINER = "herdr-hindsight-db"
API_CONTAINER = "herdr-hindsight"
BACKUP_TIMER = "herdr-hindsight-backup.timer"
MARKER_REL = Path(".local/state/herdr-engineering/memory/deployed.json")
BACKUP_DIR_REL = Path(".local/share/herdr-memory/backups")
CLIENT_CONFIG_REL = Path(".config/herdr-memory/config.json")
BACKUP_WARN_SECONDS = 36 * 3600        # the timer is daily (+15 min jitter): one missed run is a warning
BACKUP_FAIL_SECONDS = 7 * 24 * 3600
_INSPECT_FMT = "{{.State.Status}} {{.State.Restarting}} {{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}"


def _check(name: str, status: str, detail: str, **data: Any) -> dict[str, Any]:
    return {"name": name, "status": status, "detail": detail, "data": data}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _http_get(url: str, timeout: float = 3.0) -> int:
    """Status code of an unauthenticated GET; raises OSError on any transport failure. Never follows redirects."""
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(urllib.request.Request(url, method="GET"), timeout=timeout) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise OSError(type(exc).__name__) from None


def valid_endpoint(url: Any) -> str | None:
    """The URL if the client would accept it: https anywhere, plain http ONLY on loopback. Else None."""
    if not isinstance(url, str):
        return None
    try:
        p = urllib.parse.urlsplit(url)
        _ = p.port
    except ValueError:
        return None
    if not p.hostname or p.username or p.password or p.query or p.fragment:
        return None
    if p.scheme == "https":
        return url.rstrip("/")
    if p.scheme == "http":
        try:
            loop = ipaddress.ip_address(p.hostname).is_loopback
        except ValueError:
            loop = p.hostname == "localhost"
        return url.rstrip("/") if loop else None
    return None


def _cached_fleet_value(home: Path) -> Callable[[str, Any], Any]:
    """Reads ``memory.url`` from the host's CACHED fleet slice only (no network, so doctor stays offline-safe).
    ``herdr-eng memory install`` may use the live ``host_config.host_value`` instead."""
    def get(dotted: str, default: Any = None) -> Any:
        from . import host_config as hc
        try:
            hid = hc._read_host_json(home)["host_id"]
            cached = hc._read_cache(home / hc.CACHE_REL, hid)
        except Exception:  # noqa: BLE001 - not enrolled / no cache
            return default
        if cached is None:
            return default
        section, _, key = dotted.partition(".")
        node = cached[1].get(section)
        return node.get(key, default) if isinstance(node, dict) else default
    return get


def discover_endpoint(home: Path | None = None, fleet_value: Callable[[str, Any], Any] | None = None) -> dict[str, Any]:
    """``{"url": str|None, "source": "herdr-memory-config"|"fleet"|None, "problem": str|None}``.

    1. the ``url`` already in ``~/.config/herdr-memory/config.json`` (user-owned; never overridden),
    2. else the fleet slice's ``memory.url`` (published by ``fleet config memory init``; fetched and cached by
       ``host_config``). ``fleet_value`` is injectable for tests."""
    home = Path(home) if home else Path.home()
    cfg = home / CLIENT_CONFIG_REL
    if cfg.is_file():
        try:
            url = json.loads(cfg.read_text()).get("url")
        except (OSError, ValueError, AttributeError):
            return {"url": None, "source": None, "problem": "herdr-memory config unreadable"}
        if url:
            ok = valid_endpoint(url)
            return {"url": ok, "source": "herdr-memory-config",
                    "problem": None if ok else "url must be https (plain http is only accepted on loopback)"}
    fleet_value = fleet_value or _cached_fleet_value(home)
    try:
        url = fleet_value("memory.url", None)
    except Exception:  # noqa: BLE001 - no host.json / control plane down and no cache: simply unknown
        return {"url": None, "source": None, "problem": None}
    if url:
        ok = valid_endpoint(url)
        return {"url": ok, "source": "fleet", "problem": None if ok else "fleet memory.url is not an https URL"}
    return {"url": None, "source": None, "problem": None}


def _container(name: str, run: Callable, which: Callable) -> dict[str, Any]:
    label = f"memory_container:{name}"
    if not which("docker"):
        return _check(label, "unknown", "docker not available; cannot inspect the container")
    try:
        proc = run(["docker", "inspect", "-f", _INSPECT_FMT, name], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return _check(label, "unknown", "docker did not answer")
    if proc.returncode != 0:
        if "no such" in (proc.stderr or "").lower():
            return _check(label, "fail", "container is missing although this host recorded a memory deploy")
        return _check(label, "unknown", "docker daemon not reachable (permissions or not running)")
    parts = (proc.stdout or "").split()
    if len(parts) < 3:
        return _check(label, "unknown", "unexpected docker output")
    state, restarting, health = parts[0], parts[1] == "true", parts[2]
    if state != "running":
        return _check(label, "fail", f"container is {state}")
    if restarting:
        return _check(label, "fail", "container is restarting (crash loop)")
    if health == "healthy":
        return _check(label, "ok", "running and healthy")
    if health == "starting":
        return _check(label, "warn", "running, health check still starting")
    if health == "unhealthy":
        return _check(label, "fail", "running but unhealthy")
    return _check(label, "unknown", "running with no health check to consult")


def _backup(home: Path, now: float) -> dict[str, Any]:
    d = home / BACKUP_DIR_REL
    try:
        exists = d.is_dir()
        dumps = sorted(d.glob("memory-*.dump"), key=lambda p: p.stat().st_mtime) if exists else []
    except OSError:
        return _check("memory_backup", "unknown", "backup directory not readable")
    if not exists or not dumps:
        return _check("memory_backup", "warn", "no backup exists yet (run memory-backup.sh backup)")
    try:
        age = max(0, int(now - dumps[-1].stat().st_mtime))
    except OSError:
        return _check("memory_backup", "unknown", "newest backup not readable")
    hours = age // 3600
    if age > BACKUP_FAIL_SECONDS:
        return _check("memory_backup", "fail", f"newest backup is {hours} h old", age_seconds=age)
    if age > BACKUP_WARN_SECONDS:
        return _check("memory_backup", "warn", f"newest backup is {hours} h old (daily expected)", age_seconds=age)
    return _check("memory_backup", "ok", f"newest backup is {hours} h old", age_seconds=age)


def _timer(run: Callable, which: Callable) -> dict[str, Any]:
    if not which("systemctl"):
        return _check("memory_backup_timer", "unknown", "systemctl not available")
    try:
        proc = run(["systemctl", "--user", "is-active", BACKUP_TIMER], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return _check("memory_backup_timer", "unknown", "systemctl did not answer")
    out = (proc.stdout or "").strip()
    if out == "active":
        return _check("memory_backup_timer", "ok", "timer active")
    if out in ("inactive", "failed"):
        return _check("memory_backup_timer", "warn", f"timer is {out}")
    return _check("memory_backup_timer", "unknown", "user systemd not reachable")


def check_memory_backend(home: Path | None = None, *, run: Callable | None = None, which: Callable | None = None,
                         http_get: Callable | None = None, now: Callable[[], float] = time.time,
                         fleet_value: Callable | None = None) -> list[dict[str, Any]]:
    """All memory checks that apply to this host; ``[]`` when this host neither runs nor uses the backend."""
    home = Path(home) if home else Path.home()
    run = run or subprocess.run
    which = which or shutil.which
    http_get = http_get or _http_get
    checks: list[dict[str, Any]] = []
    marker = home / MARKER_REL
    port: int | None = None
    if marker.is_file():
        try:
            data = json.loads(marker.read_text())
            port = int(data["port"])
        except (OSError, ValueError, KeyError, TypeError):
            checks.append(_check("memory_deploy", "unknown", "deploy marker unreadable"))
        else:
            checks.append(_check("memory_deploy", "ok", f"memory deployed (image {str(data.get('image_tag'))[:20]})",
                                 port=port))
        checks.append(_container(DB_CONTAINER, run, which))
        checks.append(_container(API_CONTAINER, run, which))
        if port is not None:
            try:
                code = http_get(f"http://127.0.0.1:{port}/health")
                checks.append(_check("memory_health", "ok" if code == 200 else "fail",
                                     "loopback /health answered 200" if code == 200 else f"loopback /health answered {code}"))
            except OSError:
                checks.append(_check("memory_health", "unknown", "loopback /health unreachable"))
        checks.append(_backup(home, now()))
        checks.append(_timer(run, which))
    ep = discover_endpoint(home, fleet_value)
    if ep["problem"]:
        checks.append(_check("memory_endpoint", "fail", ep["problem"], source=ep["source"]))
    elif ep["url"]:
        try:
            code = http_get(ep["url"] + "/health")
            checks.append(_check("memory_endpoint", "ok" if code == 200 else "fail",
                                 "endpoint /health answered 200" if code == 200 else f"endpoint /health answered {code}",
                                 source=ep["source"]))
        except OSError:
            checks.append(_check("memory_endpoint", "unknown", "endpoint unreachable from this host", source=ep["source"]))
    return checks

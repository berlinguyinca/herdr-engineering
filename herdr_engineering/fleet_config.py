"""Central fleet configuration: one file on the control-plane host, served to
each host scoped by role.

The file (mode 0600, outside git) holds ``sections`` (each tagged with the
roles allowed to read it) and ``hosts`` (roles, a sha256 of the host's token,
optional per-section overrides). Only hashes of host tokens are stored.

Errors and ``status()`` never carry configuration values.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import secrets
import stat
import threading
from pathlib import Path
from typing import Any

import yaml

SCHEMA_VERSION = 1
ALL_ROLE = "all"
# Read directly by the control-plane web process; never served to any host.
CONTROL_PLANE_SECTION = "control_plane"
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


class FleetConfigError(ValueError):
    """The fleet config file is unreadable, unsafe, or invalid."""


def _roles(raw: Any, where: str) -> list[str]:
    if not isinstance(raw, list) or not all(isinstance(r, str) and r for r in raw):
        raise FleetConfigError(f"{where}: roles must be a list of non-empty strings")
    return list(raw)


def _deep_merge(base: dict, over: dict) -> dict:
    for key, val in over.items():
        if isinstance(val, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], val)
        else:
            base[key] = copy.deepcopy(val)
    return base


class FleetConfig:
    def __init__(self, sections: dict[str, dict], hosts: dict[str, dict]):
        self._sections = sections
        self._hosts = hosts
        canonical = json.dumps({"sections": sections, "hosts": hosts}, sort_keys=True)
        self.revision = hashlib.sha256(canonical.encode()).hexdigest()[:12]

    def host_for_token(self, token: str) -> str | None:
        """Host id owning ``token``; compares every stored hash in constant time."""
        digest = hashlib.sha256((token or "").encode()).hexdigest()
        found: str | None = None
        if not token:
            return None
        for host_id, host in self._hosts.items():
            if secrets.compare_digest(digest, host["token_sha256"]):
                found = host_id
        return found

    def slice_for(self, host_id: str) -> dict:
        """Sections visible to the host (``all`` plus role matches), then its overrides.

        Returns ``{section: values}``. Overrides are ``{section: {key: value}}``
        and deep-merge over the section. The control-plane section is never served.
        """
        host = self._hosts.get(host_id)
        if host is None:
            raise FleetConfigError(f"unknown host: {host_id}")
        host_roles = set(host["roles"])
        out: dict[str, dict] = {}
        for name, section in self._sections.items():
            if name == CONTROL_PLANE_SECTION:
                continue
            roles = set(section["roles"])
            if ALL_ROLE in roles or roles & host_roles:
                out[name] = copy.deepcopy(section["values"])
        for name, over in host["overrides"].items():
            if name == CONTROL_PLANE_SECTION:
                continue
            _deep_merge(out.setdefault(name, {}), over)
        return out

    def section(self, name: str) -> dict:
        """Values of a section regardless of roles (control-plane process only)."""
        if name not in self._sections:
            raise FleetConfigError(f"unknown section: {name}")
        return copy.deepcopy(self._sections[name]["values"])

    def status(self) -> dict:
        """Revision and host ids/roles. No values, no token hashes."""
        return {
            "revision": self.revision,
            "hosts": {h: list(v["roles"]) for h, v in self._hosts.items()},
        }


def _parse(data: Any) -> FleetConfig:
    if not isinstance(data, dict):
        raise FleetConfigError("top level must be a mapping")
    if data.get("version") != SCHEMA_VERSION:
        raise FleetConfigError(f"version must be {SCHEMA_VERSION}")
    raw_sections = data.get("sections")
    raw_hosts = data.get("hosts")
    if not isinstance(raw_sections, dict) or not isinstance(raw_hosts, dict):
        raise FleetConfigError("sections and hosts must be mappings")

    sections: dict[str, dict] = {}
    for name, sec in raw_sections.items():
        if not isinstance(name, str) or not isinstance(sec, dict):
            raise FleetConfigError(f"section {name!r}: must be a mapping")
        roles = _roles(sec.get("roles"), f"section {name}")
        if not isinstance(sec.get("values"), dict):
            raise FleetConfigError(f"section {name}: values must be a mapping")
        if name == CONTROL_PLANE_SECTION and ALL_ROLE in roles:
            raise FleetConfigError(f"section {name}: must not be readable by role 'all'")
        sections[name] = {"roles": roles, "values": sec["values"]}

    hosts: dict[str, dict] = {}
    seen: set[str] = set()
    for host_id, host in raw_hosts.items():
        if not isinstance(host_id, str) or not isinstance(host, dict):
            raise FleetConfigError(f"host {host_id!r}: must be a mapping")
        roles = _roles(host.get("roles", []), f"host {host_id}")
        digest = host.get("token_sha256")
        if not isinstance(digest, str) or not _SHA256_HEX.match(digest):
            raise FleetConfigError(f"host {host_id}: token_sha256 must be 64 lowercase hex chars")
        if digest in seen:
            raise FleetConfigError(f"host {host_id}: token_sha256 duplicates another host")
        seen.add(digest)
        overrides = host.get("overrides", {})
        if not isinstance(overrides, dict) or not all(isinstance(v, dict) for v in overrides.values()):
            raise FleetConfigError(f"host {host_id}: overrides must map section names to mappings")
        hosts[host_id] = {"roles": roles, "token_sha256": digest, "overrides": overrides}
    try:
        return FleetConfig(sections, hosts)
    except (TypeError, ValueError):
        raise FleetConfigError("values must be JSON-serializable") from None


def load_fleet_config(path: Path) -> FleetConfig:
    """Load and validate the central file; refuse group/other-accessible files."""
    path = Path(path)
    try:
        mode = path.stat().st_mode
    except OSError as exc:
        raise FleetConfigError(f"cannot read {path}: {exc.strerror or 'error'}") from None
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise FleetConfigError(f"{path} must not be accessible by group or other (chmod 600)")
    try:
        data = yaml.safe_load(path.read_text())
    except (OSError, UnicodeDecodeError):
        raise FleetConfigError(f"cannot read {path}") from None
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" at line {mark.line + 1}" if mark else ""
        raise FleetConfigError(f"{path} is not valid YAML{where}") from None
    return _parse(data)


class FleetConfigStore:
    """Serves the last good FleetConfig, reloading when the file changes.

    A failed reload keeps the previous config and records ``last_error`` (a short
    message that never contains configuration values). The first load must
    succeed.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._applied: dict[str, str] = {}
        self.last_error: str | None = None
        self._sig: tuple | None = None
        self._config = load_fleet_config(self.path)
        self._sig = self._signature()

    def _signature(self) -> tuple | None:
        try:
            st = self.path.stat()
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size, st.st_ino, st.st_mode)

    def reload(self) -> bool:
        """Reload now. True when a new config was adopted; False otherwise."""
        with self._lock:
            return self._reload_locked()

    def _reload_locked(self) -> bool:
        sig = self._signature()
        try:
            new = load_fleet_config(self.path)
        except FleetConfigError as exc:
            self.last_error = str(exc)[:200]
            self._sig = sig
            return False
        self._sig = sig
        self.last_error = None
        changed = new.revision != self._config.revision
        self._config = new
        return changed

    def current(self) -> FleetConfig:
        with self._lock:
            if self._signature() != self._sig:
                self._reload_locked()
            return self._config

    def record_applied(self, host_id: str, revision: str) -> None:
        with self._lock:
            self._applied[host_id] = revision

    def applied(self) -> dict[str, str]:
        with self._lock:
            return dict(self._applied)

    def status(self) -> dict:
        cfg = self.current()
        applied = self.applied()
        base = cfg.status()
        return {
            "revision": base["revision"],
            "last_error": self.last_error,
            "hosts": [{"id": h, "roles": roles, "applied_revision": applied.get(h)}
                      for h, roles in base["hosts"].items()],
        }

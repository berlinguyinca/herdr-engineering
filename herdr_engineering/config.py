"""Configuration loading with explicit precedence and safe merging.

Precedence (spec 0050):
    shipped safe defaults < fleet/site config < user config < repository
    config < session override
Merging must preserve unrelated user settings (deep merge, never wholesale
overwrite).
"""
from __future__ import annotations

import copy
import logging
import os
from pathlib import Path
from typing import Any

from .errors import InvalidError

# github.sync (control-plane-web, read live from the fleet file's github section):
# seconds between passes, seconds between scheduled full reads of one repository, and
# the GraphQL points (rateLimit.cost) one pass may spend before deferring the rest.
# 400 points every 300 s stays under GitHub's 5000/hour even when every pass spends it.
GITHUB_POINTS_PER_HOUR = 5000  # GitHub GraphQL primary rate limit
GITHUB_SYNC_DEFAULTS = {"interval": 300, "full_reconcile_interval": 86400, "point_budget": 400}
GITHUB_PROJECTS_DEFAULTS = {"enabled": False, "item_limit": 2000}

DEFAULTS: dict[str, Any] = {
    "version": 1,
    "build_cleanup": {
        "enabled": False,
        "roots": [],
        "interval_seconds": 900,
        "high_water_percent": 85,
        "low_water_percent": 80,
        "min_age_hours": 24,
    },
    "herdr": {"capability_cache_ttl_seconds": 30},
    "web": {"private_only": True, "host": "127.0.0.1", "port": 8787},
    "artifacts": {
        "provider": "filesystem",
        "root": "${HERDR_ENGINEERING_ARTIFACT_ROOT:-~/.local/share/herdr-engineering/artifacts}",
        "retention_days": 30,
    },
    "dev_fabric": {
        "enabled": True,
        "logical_service_name": "dev",
        "external_port_range": [18000, 28999],
        "lease_ttl_seconds": 90,
        "heartbeat_seconds": 30,
        "private_only": True,
        "allowed_protocols": ["http", "https", "tcp"],
    },
    # Fabric gateway (spec 0110 component #3): the fleet front door that gives
    # every lease a stable dev:<port> address. gateway_url empty = local mode
    # (no gateway process running). Token is a secret: the
    # central host config (fabric.token) or ~/.config/herdr-engineering/fabric-token.
    "fabric": {
        "enabled": True,
        "gateway_url": "",
        "control_port": 29999,
        "bind": "auto",  # auto = loopback + tailnet IPv4
        "url_base": "",  # empty = auto (MagicDNS name, else tailnet IP)
        "probe_interval_seconds": 15,
        # Auto-registration agent noise filter (spec 0110): only register web
        # services on ports in [agent_web_port_min, agent_ephemeral_min) that
        # are not in agent_exclude_ports (well-known daemons are also skipped).
        "agent_exclude_ports": [],
        "agent_web_port_min": 1024,
        "agent_ephemeral_min": 32768,
        # The auto-registration agent reports every discovered service to this
        # control-plane base URL (e.g. http://dev.lan) so leases show up on the
        # fleet Services page. Empty = reporting disabled. Defaults to the
        # control_plane_url in host.json.
        "control_plane_url": "",
    },
    # LLM env injection for sessions started via `herdr-eng session run`: a
    # per-project gateway key goes into the session's environment only.
    # key_mode: "session" = mint per session, revoke at end; "project" = reuse a
    # cached project key. inject:false opts out.
    "llm": {
        "inject": True,
        "key_mode": "session",
        "control_plane_url": "",  # empty = fabric.control_plane_url, then http://dev.lan
        "gateway_url": "http://dev.lan:8088/v1",
    },
    "tests": {"adapters": ["pytest", "go", "cargo", "junit", "scala"]},
    "ci": {"provider": "woodpecker", "pileated_extensions": "auto"},
    "notifications": {"enabled": True, "minimum_severity": "notice",
                      "quiet_hours": None, "dedupe_window_seconds": 300},
    "security": {"allow_public_listeners": False, "persist_hidden_reasoning": False},
    # GitHub catalog (control-plane-web). organizations: allowlist of owner
    # logins (organizations and your own account); empty = every owner the
    # token sees. Excluded owners cost no issue calls and are purged from the
    # stored catalog on the next pass.
    # sync: interval between passes and between scheduled full reads (seconds).
    # projects: read-only Projects v2 boards of the in-scope owners (off by default).
    "github": {"organizations": [], "sync": dict(GITHUB_SYNC_DEFAULTS),
               "projects": dict(GITHUB_PROJECTS_DEFAULTS)},
}


def github_sync(value: Any) -> dict[str, int]:
    """Validate ``github.sync``; the settings with defaults filled in."""
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise InvalidError("github.sync must be a mapping")
    settings = dict(GITHUB_SYNC_DEFAULTS)
    for key in GITHUB_SYNC_DEFAULTS:
        item = value.get(key)
        if item is None:
            continue
        if isinstance(item, bool) or not isinstance(item, int) or item <= 0:
            raise InvalidError(f"github.sync.{key} must be a positive whole number")
        settings[key] = item
    hourly = settings["point_budget"] * 3600 / max(30, settings["interval"])
    if hourly > GITHUB_POINTS_PER_HOUR:
        raise InvalidError(
            f"github.sync.point_budget is spent once per pass: {settings['point_budget']} points every "
            f"{settings['interval']}s is {hourly:.0f} points per hour, over GitHub's {GITHUB_POINTS_PER_HOUR} "
            "per-hour GraphQL limit; lower point_budget or raise interval")
    return settings


def github_projects(value: Any) -> dict[str, Any]:
    """Validate ``github.projects``; the settings with defaults filled in."""
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise InvalidError("github.projects must be a mapping")
    settings = dict(GITHUB_PROJECTS_DEFAULTS)
    enabled = value.get("enabled")
    if enabled is not None:
        if not isinstance(enabled, bool):
            raise InvalidError("github.projects.enabled must be true or false")
        settings["enabled"] = enabled
    limit = value.get("item_limit")
    if limit is not None:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise InvalidError("github.projects.item_limit must be a positive whole number")
        settings["item_limit"] = limit
    return settings


def github_organizations(value: Any) -> list[str]:
    """Validate ``github.organizations``; the normalised (lower-case, unique) allowlist.

    ``None`` or ``[]`` means every owner. GitHub logins are case-insensitive.
    """
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise InvalidError("github.organizations must be a list of non-empty GitHub owner logins "
                           "(organizations or your own account); leave it empty for every owner")
    return sorted({item.strip().lower() for item in value})


def validate_github(section: Any) -> None:
    """Reject an invalid ``github`` section with a message naming the key."""
    if section is None:
        return
    if not isinstance(section, dict):
        raise InvalidError("github must be a mapping")
    github_organizations(section.get("organizations"))
    github_sync(section.get("sync"))
    github_projects(section.get("projects"))


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge override into base, preserving unrelated base keys."""
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _expand_env(value: Any) -> Any:
    if isinstance(value, str):
        # support ${VAR:-default} and ${VAR}
        import re
        pattern = re.compile(r"\$\{([A-Z0-9_]+)(?::-([^}]*))?\}")
        def repl(m):
            name, default = m.group(1), m.group(2)
            return os.environ.get(name, default if default is not None else "")
        return pattern.sub(repl, value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


def _discover_config_paths() -> list[Path]:
    """Default config discovery, in increasing precedence (later overrides).

    1. machine-local: ``~/.config/herdr-engineering/config.yaml``
       (written by ``scripts/install.sh``; per-host settings)
    2. repository:     ``./config/herdr-engineering.yaml`` or ``./herdr-engineering.yaml``
    3. explicit env:   ``$HERDR_ENGINEERING_CONFIG`` (highest)
    """
    home = Path.home()
    candidates = [
        home / ".config" / "herdr-engineering" / "config.yaml",
        Path.cwd() / "config" / "herdr-engineering.yaml",
        Path.cwd() / "herdr-engineering.yaml",
    ]
    env = os.environ.get("HERDR_ENGINEERING_CONFIG")
    if env:
        candidates.append(Path(env))
    return [c for c in candidates if c.exists()]


def load_config(paths: list[Path] | None = None) -> dict[str, Any]:
    """Load config by precedence. Each path may be missing (skipped).

    When ``paths`` is omitted, a sensible default set is discovered (machine-
    local, then repository, then ``$HERDR_ENGINEERING_CONFIG``) so that the
    file written by the installer is actually used.
    """
    cfg = copy.deepcopy(DEFAULTS)
    for path in (paths if paths is not None else _discover_config_paths()):
        if not path.exists():
            continue
        try:
            import yaml  # optional; fall back to a tiny parser
            data = yaml.safe_load(path.read_text()) or {}
        except ImportError:
            data = _read_simple_yaml(path)
        if not isinstance(data, dict):
            raise InvalidError(f"config file {path} is not a mapping")
        # A config may omit `version` (minimal per-host files written by
        # Ansible carry a single key); an absent version means the only
        # supported schema (1). Only an explicitly wrong version is rejected.
        if "version" in data and data["version"] != 1:
            raise InvalidError(f"config file {path} has unsupported version")
        if data.get("github"):
            logging.getLogger(__name__).warning(
                "%s: github.* is validated but not read from this file; control-plane-web reads "
                "github.organizations and github.sync from the central fleet config (control_plane "
                "fleet file), so this section has no effect", path)
        cfg = _deep_merge(cfg, data)
    validate_github(cfg.get("github"))
    return _expand_env(cfg)


def _read_simple_yaml(path: Path) -> dict[str, Any]:
    """Minimal indentation-based YAML subset (no PyYAML dependency).

    Supports nested mappings by indentation. The root is always kept on the
    stack so a top-level scalar (e.g. ``version: 1``) never empties it.
    """
    result: dict[str, Any] = {}
    stack: list[tuple] = [(0, result)]
    for raw in path.read_text().splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        line = raw.strip()
        if ":" in line and not line.startswith("-"):
            key, _, value = line.partition(":")
            value = value.strip()
            # pop back to the parent of the current indent, never the root
            while len(stack) > 1 and stack[-1][0] >= indent:
                stack.pop()
            node = stack[-1][1]
            if value == "":
                child: dict[str, Any] = {}
                node[key.strip()] = child
                stack.append((indent, child))
            else:
                node[key.strip()] = _scalar(value)
    return result


def _scalar(value: str) -> Any:
    value = value.strip()
    if value == "true":
        return True
    if value == "false":
        return False
    if value.isdigit():
        return int(value)
    return value.strip('"')

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
    # Disk hygiene is ON by default; opt out with ``build_cleanup: {enabled: false}``.
    # Normal sweeps need ``min_age_hours`` idle; once usage reaches ``pressure_percent`` the
    # idle floor drops to ``pressure_min_age_hours`` (live process/lease/lock guards still apply).
    # Cargo wrapper / build-coordinate refuse new builds at ``refuse_builds_percent``
    # when the build's filesystem also has less than ``refuse_builds_min_free_gib`` available,
    # unless HERDR_DISK_GUARD_OVERRIDE=1 is set. ``enabled: false`` disables the guard too.
    "build_cleanup": {
        "enabled": True,
        "roots": ["~/IdeaProjects", "~/work", "~/.cache"],
        "interval_seconds": 900,
        "require_herdr_server": False,  # true: the watch loop only sweeps while a herdr server runs
        "high_water_percent": 85,
        "low_water_percent": 80,
        "min_age_hours": 24,
        "pressure_percent": 90,
        "pressure_min_age_hours": 2,
        "refuse_builds_percent": 90,
        "refuse_builds_min_free_gib": 50,
    },
    # Spec 02: on by default (local_only; shared cache/slurm stay off); validated by
    # build_cache_config.validate (fails closed). Opt out with ``enabled: false``.
    "fabric_build_cache": {
        "enabled": True,
        "mode": "local_only",
        "allowed_hosts": [],
        "shared_cache": {
            "enabled": False, "endpoint_ref": None, "namespace": "trusted-dev",
            "writes_enabled": False, "max_total_gib": 250, "tls_required": True,
            "credentials_file": None, "registry_file": None,
        },
        "local": {
            "capacity_gib": 20, "ram_scratch_gib": 0, "min_free_disk_percent": 20,
            "incremental_disabled_for_cacheable_builds": True,
            "storage_tiers": [],
            "manage_targets": True,
            "target_retention_enabled": False,
            "target_internal_gib": 32,
            "target_external_gib": 256,
            "target_min_idle_hours": 168,
        },
        "execution": {
            "slurm_enabled": False, "allowed_clusters": [], "allowed_partitions": [],
            "gpu_for_cpu_builds": False, "remote_fallback": "local", "max_build_slots": 2,
        },
        "slurm": {
            "enabled": False, "account": None, "allowed_partitions": [], "cpus": 8,
            "mem_gb": 16, "time_limit_minutes": 60, "queue_wait_timeout_s": 900,
        },
        "cleanup": {"dry_run": True, "min_idle_hours": 24, "max_evict_per_pass_gib": 10},
        "eviction": {"destructive_enabled": False, "hard_cap_gib": 40,
                     "max_entries_per_run": 5000},
        "adaptive_slots": {"enabled": False, "min_slots": 1, "max_slots": 4},
        "observability": {"enabled": False, "export_existing_metrics_pipeline": False},
        # Opt-in sender of this host's builds to a control plane (`herdr-eng build-cache-report`).
        "report": {"enabled": False, "url": None, "key_file": None, "interval_s": 60, "host_id": None},
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
        if value is None and isinstance(result.get(key), dict):
            continue  # an empty ``section:`` key adds nothing; it must not erase the defaults
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


def _discover_config_sources() -> list[tuple[Path, bool]]:
    """Default config discovery, in increasing precedence (later overrides), as ``(path, trusted)``.

    1. machine-local: ``~/.config/herdr-engineering/config.yaml``
       (written by ``scripts/install.sh``; per-host settings)
    2. repository:     ``./config/herdr-engineering.yaml`` or ``./herdr-engineering.yaml``
       (untrusted: it is whatever checkout the command runs in)
    3. explicit env:   ``$HERDR_ENGINEERING_CONFIG`` (highest; the operator chose it, so it is machine config
       even when it names a file inside the current directory)
    """
    home = Path.home()
    candidates = [
        (home / ".config" / "herdr-engineering" / "config.yaml", True),
        (Path.cwd() / "config" / "herdr-engineering.yaml", False),
        (Path.cwd() / "herdr-engineering.yaml", False),
    ]
    env = os.environ.get("HERDR_ENGINEERING_CONFIG")
    if env:
        candidates.append((Path(env), True))
    return [(c, trusted) for c, trusted in candidates if c.exists()]


def _discover_config_paths() -> list[Path]:
    return [path for path, _ in _discover_config_sources()]


# What a checkout's own config may set: only keys that are harmless coming from untrusted input. Every
# other section and key (build_cleanup roots and thresholds, build-cache policy, credentials, endpoints
# such as ci.base_url, listeners such as web.host, security) is machine-local only: a repo being built
# must not choose what gets deleted, where a token is sent, or what the host listens on.
REPO_LOCAL_ALLOWED: dict[str, frozenset[str] | None] = {
    "version": None,  # scalar
    "web": frozenset({"port"}),
    "tests": frozenset({"adapters"}),
    "ci": frozenset({"provider", "pileated_extensions"}),
}
_warned_repo_local: set[tuple[str, tuple[str, ...]]] = set()


def _filter_repo_local(path: Path, data: dict[str, Any]) -> dict[str, Any]:
    """Keep only the allowed keys of an untrusted repo-local config; warn once, naming what was ignored."""
    kept: dict[str, Any] = {}
    ignored: list[str] = []
    for key, value in data.items():
        if key not in REPO_LOCAL_ALLOWED:
            ignored.append(key)
        elif REPO_LOCAL_ALLOWED[key] is None:
            kept[key] = value
        elif isinstance(value, dict):
            allowed = REPO_LOCAL_ALLOWED[key]
            kept[key] = {k: v for k, v in value.items() if k in allowed}
            ignored.extend(f"{key}.{k}" for k in value if k not in allowed)
        else:
            ignored.append(key)
    names = tuple(sorted(ignored))
    if names and (str(path), names) not in _warned_repo_local:
        _warned_repo_local.add((str(path), names))
        logging.getLogger(__name__).warning(
            "%s: ignoring %s; a repository's own config may only set %s. Put the rest in "
            "~/.config/herdr-engineering/config.yaml or $HERDR_ENGINEERING_CONFIG.",
            path, ", ".join(names), ", ".join(sorted(
                k if v is None else f"{k}.{{{','.join(sorted(v))}}}" for k, v in REPO_LOCAL_ALLOWED.items())))
    return kept


def _normalize_build_cleanup(cfg: dict[str, Any], path: Path) -> None:
    """``build_cleanup`` is a mapping; the shorthand ``build_cleanup: false`` (or ``true``) means disabled
    (or defaults). Checked here once, so no reader ever sees another type."""
    value = cfg.get("build_cleanup")
    if isinstance(value, dict):
        return
    if isinstance(value, bool):
        cfg["build_cleanup"] = {**copy.deepcopy(DEFAULTS["build_cleanup"]), "enabled": value}
        return
    raise InvalidError(f"{path}: build_cleanup must be a mapping or a boolean")


def load_config(paths: list[Path] | None = None, *, strict_build_cache: bool = False
                ) -> dict[str, Any]:
    """Load config by precedence. Each path may be missing (skipped).

    When ``paths`` is omitted, a sensible default set is discovered (machine-
    local, then repository, then ``$HERDR_ENGINEERING_CONFIG``) so that the
    file written by the installer is actually used.
    """
    cfg = copy.deepcopy(DEFAULTS)
    sources = ([(path, True) for path in paths] if paths is not None else _discover_config_sources())
    for path, trusted in sources:
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
        if not trusted:
            data = _filter_repo_local(path, data)
        cfg = _deep_merge(cfg, data)
        _normalize_build_cleanup(cfg, path)
    validate_github(cfg.get("github"))
    cfg = _expand_env(cfg)
    from .build_cache_config import validate as validate_build_cache
    try:
        validate_build_cache(cfg.get("fabric_build_cache"))
    except InvalidError as exc:
        if strict_build_cache:
            raise
        # Fail closed: an invalid build-cache section disables the feature
        # instead of breaking every unrelated command.
        logging.getLogger(__name__).warning(
            "fabric_build_cache config invalid, feature disabled: %s", exc)
        cfg["fabric_build_cache"] = copy.deepcopy(DEFAULTS["fabric_build_cache"])
        cfg["fabric_build_cache"]["enabled"] = False
    return cfg


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

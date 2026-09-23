"""Configuration loading with explicit precedence and safe merging.

Precedence (spec 0050):
    shipped safe defaults < fleet/site config < user config < repository
    config < session override
Merging must preserve unrelated user settings (deep merge, never wholesale
overwrite).
"""
from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

from .errors import InvalidError

DEFAULTS: dict[str, Any] = {
    "version": 1,
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
    "tests": {"adapters": ["pytest", "go", "cargo", "junit", "scala"]},
    "ci": {"provider": "woodpecker", "pileated_extensions": "auto"},
    "notifications": {"enabled": True, "minimum_severity": "notice",
                      "quiet_hours": None, "dedupe_window_seconds": 300},
    "security": {"allow_public_listeners": False, "persist_hidden_reasoning": False},
}


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
        if data.get("version") != 1:
            raise InvalidError(f"config file {path} has unsupported version")
        cfg = _deep_merge(cfg, data)
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

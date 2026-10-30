"""YAML configuration of one bridge process (no environment variables). Secrets are files, never values in the YAML."""
from __future__ import annotations

import stat
from pathlib import Path
from typing import Any

import yaml

KEYS = {"control_plane_url", "state_file"}


def load(path: str | Path) -> dict[str, Any]:
    cfg = yaml.safe_load(Path(path).read_text())
    if not isinstance(cfg, dict) or set(cfg) - KEYS:
        raise ValueError(f"the bridge config may only contain: {', '.join(sorted(KEYS))}")
    for key in KEYS:
        if not isinstance(cfg.get(key), str) or not cfg[key]:
            raise ValueError(f"{key} is required")
    return cfg


def read_secret(path: str) -> str:
    """A secret read from a file that only its owner can read. Adapters use this for their provider tokens."""
    p = Path(path).expanduser()
    if p.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise ValueError(f"{p} must not be readable by group or others (chmod 600)")
    return p.read_text().strip()

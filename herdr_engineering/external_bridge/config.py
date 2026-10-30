"""YAML configuration of one bridge process (no environment variables). Secrets are files, never values in the YAML."""
from __future__ import annotations

import stat
from pathlib import Path
from typing import Any

import yaml

from .relay import Adapter
from .telegram import Telegram

KEYS = {"control_plane_url", "state_file", "telegram"}


def load(path: str | Path) -> dict[str, Any]:
    cfg = yaml.safe_load(Path(path).read_text())
    if not isinstance(cfg, dict) or set(cfg) - KEYS:
        raise ValueError(f"the bridge config may only contain: {', '.join(sorted(KEYS))}")
    for key in ("control_plane_url", "state_file"):
        if not isinstance(cfg.get(key), str) or not cfg[key]:
            raise ValueError(f"{key} is required")
    if len([k for k in ("telegram",) if k in cfg]) != 1:
        raise ValueError("configure exactly one adapter: telegram")
    return cfg


def read_secret(path: str) -> str:
    p = Path(path).expanduser()
    if p.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise ValueError(f"{p} must not be readable by group or others (chmod 600)")
    return p.read_text().strip()


def build_adapter(cfg: dict[str, Any]) -> Adapter:
    t = cfg["telegram"]
    if not isinstance(t, dict) or set(t) - {"token_file", "chat_ids"}:
        raise ValueError("telegram takes token_file and chat_ids")
    ids = t.get("chat_ids")
    if not isinstance(ids, list) or not ids or not all(isinstance(i, int) and not isinstance(i, bool) for i in ids):
        raise ValueError("telegram.chat_ids must list the numeric chat ids that may be bridged")
    return Telegram(read_secret(t["token_file"]), ids)

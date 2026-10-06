"""Control-plane configuration normalization."""
from __future__ import annotations

from typing import Any

_DEFAULTS = {
    "postgres_dsn": "postgresql://herdr:herdr@127.0.0.1:5432/herdr",
    "rustfs_endpoint": "http://127.0.0.1:9000",
    "rustfs_bucket": "herdr-artifacts",
    "rustfs_region": "us-east-1",
}


def control_plane_config(cfg: dict[str, Any]) -> dict[str, str]:
    """Return normalized control-plane settings from a config dict."""
    cp = cfg.get("control_plane") or {}
    out = dict(_DEFAULTS)
    for key in _DEFAULTS:
        if key in cp and cp[key]:
            out[key] = str(cp[key])
    return out

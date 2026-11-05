"""Version stamp for the action judge: changes whenever the model, thresholds or question set change."""
import hashlib
import json
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

QUESTION_SET_VERSION = 1


def _endpoint_identity(endpoint: object) -> str:
    """Normalized scheme://host:port/path (case-folded, default port made explicit, no trailing slash)."""
    try:
        parts = urlsplit(str(endpoint or ""))
        host = parts.hostname or ""
        port = parts.port or {"http": 80, "https": 443}.get(parts.scheme.lower(), 0)
    except ValueError:
        return str(endpoint)
    if not host:
        return ""
    return f"{parts.scheme.lower()}://{host.lower()}:{port}{parts.path.rstrip('/')}"


def judge_version(judge_cfg: Mapping[str, Any]) -> str:
    """sha256 hex[:16] of the canonical JSON of model, normalized endpoint, thresholds and ``QUESTION_SET_VERSION``.

    Thresholds are coerced with ``float()`` so ``3`` and ``3.0`` hash the same. A different endpoint is a different backend,
    so repointing it resets grants and old labels. Timeout and calibration are deliberately excluded.
    """
    payload = {"model": judge_cfg["model"], "endpoint": _endpoint_identity(judge_cfg.get("endpoint", "")),
               "thresholds": {k: float(v) for k, v in judge_cfg["thresholds"].items()},
               "question_set_version": QUESTION_SET_VERSION}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]

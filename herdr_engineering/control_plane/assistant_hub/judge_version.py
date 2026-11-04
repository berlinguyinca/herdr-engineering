"""Version stamp for the action judge: changes whenever the model, thresholds or question set change."""
import hashlib
import json
from collections.abc import Mapping
from typing import Any

QUESTION_SET_VERSION = 1


def judge_version(judge_cfg: Mapping[str, Any]) -> str:
    """sha256 hex[:16] of the canonical JSON of model, thresholds and ``QUESTION_SET_VERSION``.

    Thresholds are coerced with ``float()`` so ``3`` and ``3.0`` hash the same. Endpoint, timeout and
    calibration are deliberately excluded.
    """
    payload = {"model": judge_cfg["model"], "thresholds": {k: float(v) for k, v in judge_cfg["thresholds"].items()},
               "question_set_version": QUESTION_SET_VERSION}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]

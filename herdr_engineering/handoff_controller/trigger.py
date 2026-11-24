"""Trigger decision: what to do with a session given its context reading."""
from enum import StrEnum

from ..handoff_adapters.base import ContextReading
from .config import ControllerConfig


class Decision(StrEnum):
    NONE = "none"
    DRAIN = "drain"
    OVER_CEILING = "over_ceiling"


def decide(reading: ContextReading | None, cfg: ControllerConfig) -> Decision:
    if reading is None:
        return Decision.NONE
    percent = reading.percent
    if percent >= cfg.ceiling_pct:
        return Decision.OVER_CEILING
    if percent >= cfg.drain_limit_pct:
        return Decision.DRAIN
    return Decision.NONE

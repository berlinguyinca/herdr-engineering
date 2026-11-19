from __future__ import annotations

from herdr_engineering.handoff_adapters.base import ContextReading
from herdr_engineering.handoff_controller.config import ControllerConfig
from herdr_engineering.handoff_controller.trigger import Decision, decide

CFG = ControllerConfig()


def reading(pct: float) -> ContextReading:
    return ContextReading(used_tokens=int(pct * 10), window_tokens=1000, source="t", window_source="recorded", measured_at=0.0)


def test_none_when_reading_missing() -> None:
    assert decide(None, CFG) is Decision.NONE


def test_none_below_limit() -> None:
    assert decide(reading(79.9), CFG) is Decision.NONE


def test_drain_at_exact_limit() -> None:
    assert decide(reading(80.0), CFG) is Decision.DRAIN
    assert decide(reading(91.9), CFG) is Decision.DRAIN


def test_over_ceiling_at_limit_plus_12() -> None:
    assert decide(reading(92.0), CFG) is Decision.OVER_CEILING
    assert decide(reading(100.0), CFG) is Decision.OVER_CEILING

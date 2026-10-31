"""Authenticated host telemetry normalization and conservative pressure rules."""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass(frozen=True)
class TelemetrySample:
    host_uid: str
    sequence: int
    captured_at: datetime
    cpu_utilization: float | None = None
    cpu_psi_avg10: float | None = None
    memory_available_ratio: float | None = None
    memory_psi_avg10: float | None = None
    swap_in_per_second: float | None = None
    disk_free_ratio: float | None = None
    io_psi_avg10: float | None = None
    confidence: str = "unknown"
    platform: str = "unknown"
    source_errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class TelemetryAggregate:
    cpu_utilization_ewma: float | None
    memory_available_ratio: float | None
    pressure: str
    sample_count: int


def _valid_time(value: datetime, now: datetime | None = None) -> bool:
    if value.tzinfo is None:
        return False
    now = now or datetime.now(UTC)
    age = (now - value).total_seconds()
    return -5 <= age <= 60


def accept_sample(previous: TelemetrySample | None, candidate: TelemetrySample, *, now: datetime | None = None) -> bool:
    """Accept only monotonic, bounded, host-matching observations."""
    if not candidate.host_uid or not _valid_time(candidate.captured_at, now):
        return False
    if previous is None:
        return True
    return candidate.host_uid == previous.host_uid and candidate.sequence > previous.sequence


def pressure_state(sample: TelemetrySample, *, previous: str | None = None) -> str:
    """Apply hard pressure thresholds; ``recovering`` adds one dwell step."""
    overload = (
        (sample.cpu_psi_avg10 is not None and sample.cpu_psi_avg10 >= 0.40)
        or (sample.memory_psi_avg10 is not None and sample.memory_psi_avg10 >= 0.10)
        or (sample.memory_available_ratio is not None and sample.memory_available_ratio < 0.15)
        or (sample.swap_in_per_second is not None and sample.swap_in_per_second > 10)
        or (sample.disk_free_ratio is not None and sample.disk_free_ratio < 0.10)
        or (sample.io_psi_avg10 is not None and sample.io_psi_avg10 >= 0.40)
    )
    if overload:
        return "overloaded"
    if previous == "overloaded":
        return "recovering"
    return "healthy"


def _ewma(values: list[float], alpha: float = 0.35) -> float | None:
    if not values:
        return None
    value = values[0]
    for item in values[1:]:
        value = alpha * item + (1 - alpha) * value
    return value


def aggregate_telemetry(samples: Iterable[TelemetrySample]) -> TelemetryAggregate:
    rows = list(samples)
    latest = rows[-1] if rows else None
    return TelemetryAggregate(
        cpu_utilization_ewma=_ewma([s.cpu_utilization for s in rows if s.cpu_utilization is not None]),
        memory_available_ratio=latest.memory_available_ratio if latest else None,
        pressure=pressure_state(latest) if latest else "unknown",
        sample_count=len(rows),
    )


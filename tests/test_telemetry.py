from datetime import UTC, datetime, timedelta

from herdr_engineering.telemetry import (
    TelemetrySample,
    accept_sample,
    aggregate_telemetry,
    pressure_state,
)


def sample(**changes):
    data = dict(
        host_uid="uid-beast",
        sequence=2,
        captured_at=datetime.now(UTC),
        cpu_utilization=0.4,
        cpu_psi_avg10=0.0,
        memory_available_ratio=0.7,
        memory_psi_avg10=0.0,
        swap_in_per_second=0.0,
        disk_free_ratio=0.8,
        io_psi_avg10=0.0,
        confidence="high",
        platform="linux",
    )
    data.update(changes)
    return TelemetrySample(**data)


def test_out_of_order_future_and_stale_samples_are_not_accepted():
    prior = sample(sequence=4)
    assert not accept_sample(prior, sample(sequence=3))
    assert not accept_sample(prior, sample(sequence=5, captured_at=datetime.now(UTC) + timedelta(minutes=2)))
    assert not accept_sample(prior, sample(sequence=5, captured_at=datetime.now(UTC) - timedelta(seconds=61)))
    assert accept_sample(prior, sample(sequence=5))


def test_pressure_requires_sustained_psi_or_memory_and_produces_hysteresis_hint():
    overloaded = sample(cpu_psi_avg10=0.45, memory_psi_avg10=0.12)
    assert pressure_state(overloaded) == "overloaded"
    recovering = sample(cpu_psi_avg10=0.05, memory_psi_avg10=0.01, memory_available_ratio=0.4)
    assert pressure_state(recovering, previous="overloaded") == "recovering"
    assert pressure_state(recovering, previous="recovering") == "healthy"


def test_aggregate_uses_ewma_and_never_invents_missing_values():
    first = sample(cpu_utilization=0.2, sequence=1)
    second = sample(cpu_utilization=0.8, memory_available_ratio=None, sequence=2)
    aggregate = aggregate_telemetry([first, second])
    assert 0.2 < aggregate.cpu_utilization_ewma < 0.8
    assert aggregate.memory_available_ratio is None
    assert aggregate.sample_count == 2


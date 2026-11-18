"""Shared types, fixed drain order, window table and registry of the handoff harness adapters."""
import pytest

from herdr_engineering.handoff_adapters import adapter_for
from herdr_engineering.handoff_adapters.base import (
    MAX_TYPED_BYTES,
    ContextReading,
    Harness,
    TypedInput,
)
from herdr_engineering.handoff_adapters.drain import (
    DRAIN_ORDER_KNOWN,
    DRAIN_ORDER_UNKNOWN_SUBAGENTS,
    drain_order,
)
from herdr_engineering.handoff_adapters.windows import FALLBACK_WINDOW, MODEL_WINDOWS, lookup_window


def test_percent_is_used_over_window():
    reading = ContextReading(used_tokens=50_000, window_tokens=200_000, source="usage",
                             window_source="recorded", measured_at=1.0)
    assert reading.percent == 25.0


def test_typed_input_over_900_bytes_raises_value_error():
    assert MAX_TYPED_BYTES == 900
    TypedInput(text="a" * 900, submit_keys=("Enter",))
    with pytest.raises(ValueError):
        TypedInput(text="a" * 901, submit_keys=("Enter",))
    with pytest.raises(ValueError):
        TypedInput(text="€" * 301, submit_keys=("Enter",))  # 301 chars, 903 bytes


def test_drain_order_known_lists_steps_in_spec_order():
    text = drain_order(True, Harness.PI).lower()
    positions = [text.index(part) for part in
                 ("no new work", "subagents", "commit", "herdr-handoff create", "stop")]
    assert positions == sorted(positions)
    assert "main" in text and "pi" in text


def test_drain_order_unknown_asks_to_list_and_wait_for_background_agents():
    text = drain_order(False, Harness.CODEX)
    assert "List your background agents, then wait for them to finish" in text
    assert text == DRAIN_ORDER_UNKNOWN_SUBAGENTS.format(target="codex")
    assert DRAIN_ORDER_KNOWN.format(target="codex") == drain_order(True, Harness.CODEX)


@pytest.mark.parametrize("harness", list(Harness))
@pytest.mark.parametrize("known", [True, False])
def test_drain_order_fits_900_bytes_for_every_harness(harness, known):
    text = drain_order(known, harness)
    assert harness.value in text
    TypedInput(text=text, submit_keys=("Enter",))


def test_known_model_resolves_from_table():
    model = next(iter(MODEL_WINDOWS))
    assert lookup_window(model) == (MODEL_WINDOWS[model], "model_table")


@pytest.mark.parametrize("model", ["no-such-model", "", None])
def test_unknown_model_resolves_to_fallback(model):
    assert lookup_window(model) == (FALLBACK_WINDOW, "fallback")
    assert FALLBACK_WINDOW == 128_000


@pytest.mark.skip(reason="adapter modules land in Tasks 2-5 and are wired in Task 7")
@pytest.mark.parametrize("harness", list(Harness))
def test_adapter_for_returns_adapter_per_harness(harness):
    assert adapter_for(harness).harness is harness

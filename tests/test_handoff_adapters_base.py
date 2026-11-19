"""Shared types, fixed drain order, window table and registry of the handoff harness adapters."""
from types import MappingProxyType

import pytest

from herdr_engineering.handoff_adapters import adapter_for
from herdr_engineering.handoff_adapters.base import (
    MAX_TYPED_BYTES,
    TYPE_TO_SUBMIT_DELAY_S,
    ContextReading,
    ExitPlan,
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
    assert MODEL_WINDOWS["claude-opus-4-5"] == 200_000
    assert lookup_window("claude-opus-4-5") == (200_000, "model_table")


@pytest.mark.parametrize("model", ["no-such-model", "", None])
def test_unknown_model_resolves_to_fallback(model):
    assert lookup_window(model) == (FALLBACK_WINDOW, "fallback")
    assert FALLBACK_WINDOW == 128_000


@pytest.mark.parametrize("harness", list(Harness))
def test_adapter_for_returns_adapter_per_harness(harness):
    assert adapter_for(harness).harness is harness


def test_context_reading_rejects_a_non_positive_window():
    for window in (0, -1):
        with pytest.raises(ValueError):
            ContextReading(used_tokens=1, window_tokens=window, source="usage", window_source="fallback", measured_at=1.0)


def test_model_windows_is_read_only():
    assert isinstance(MODEL_WINDOWS, MappingProxyType)
    with pytest.raises(TypeError):
        MODEL_WINDOWS["claude-opus-5-5"] = 1  # type: ignore[index]


@pytest.mark.parametrize("model", ["claude-sonnet-5-5", "claude-opus-5-5", "claude-opus-5",
                                   "claude-fable-5-1", "claude-fable-5-2"])
def test_every_claude_model_seen_in_recent_transcripts_resolves_to_a_1m_window(model):
    # Claude Code's own model catalogue lists runtime.max_input_tokens 1000000 for these (docs/operations/handoff-harness-capabilities.md)
    assert lookup_window(model) == (1_000_000, "model_table")


@pytest.mark.parametrize("model,window", [("claude-opus-4-5-20251101", 200_000), ("claude-sonnet-4-5-20250929", 200_000)])
def test_a_dated_model_id_resolves_to_its_undated_entry(model, window):
    assert lookup_window(model) == (window, "model_table")


def test_submit_delay_defaults_to_the_measured_pause():
    assert TypedInput("x", ("Enter",)).submit_delay_s == TYPE_TO_SUBMIT_DELAY_S
    assert ExitPlan("/exit", ("Enter",), 15.0).submit_delay_s == TYPE_TO_SUBMIT_DELAY_S
    assert TypedInput("x", ("Enter",), 0.0).submit_delay_s == 0.0


@pytest.mark.parametrize("harness", list(Harness))
def test_every_adapter_input_carries_the_submit_delay(harness):
    adapter = adapter_for(harness)
    assert adapter.drain_input("x").submit_delay_s == TYPE_TO_SUBMIT_DELAY_S
    assert adapter.compact_input().submit_delay_s == TYPE_TO_SUBMIT_DELAY_S
    assert adapter.exit_plan().submit_delay_s == TYPE_TO_SUBMIT_DELAY_S


_HOSTILE_SCREENS = [
    "\x1b[?25m❯ x\n\x1b[?1;2m›\x1b[>4;2m y",
    "❯ \x1b[38:2::1:2:3mx\x1b[0m\n" * 6,
    "\x1b[" + "9" * 400 + "m❯\x1b[;;;m\x1b[2;38;2m",
    "\x1b[\x1b[m\x1b\x1b[2",
    "┃\n╹▀\n┃\x00\ufffd\n",
    "",
    "\n\n",
]


@pytest.mark.parametrize("harness", list(Harness))
@pytest.mark.parametrize("screen", _HOSTILE_SCREENS)
def test_is_ready_for_input_never_raises_on_any_screen_bytes(harness, screen):
    assert adapter_for(harness).is_ready_for_input(screen, "idle") in (True, False)

"""Context-window sizes by model id, seeded from the autospec context monitor's MODEL_MAX tables."""
from collections.abc import Mapping

from .base import WindowSource

FALLBACK_WINDOW = 128_000

MODEL_WINDOWS: Mapping[str, int] = {
    "claude-sonnet-4-5": 200_000,
    "claude-sonnet-4-5-1m": 1_000_000,
    "claude-sonnet-4-6": 200_000,
    "claude-sonnet-4-7": 200_000,
    "claude-opus-4-5": 200_000,
    "claude-opus-4-7": 200_000,
    "claude-opus-4-8": 200_000,
    "claude-opus-4-8-1m": 1_000_000,
    "claude-opus-4-8[1m]": 1_000_000,
    "claude-haiku-4-5": 200_000,
    "claude-3-5-sonnet-20241022": 200_000,
    "claude-3-5-haiku-20241022": 200_000,
    "claude-3-opus-20240229": 200_000,
    "gpt-5": 200_000,
    "gpt-5-codex": 200_000,
    "gpt-4o": 128_000,
    "gpt-4o-mini": 128_000,
    "gpt-4-turbo": 128_000,
    "o1": 200_000,
    "o1-mini": 128_000,
    "o3": 200_000,
    "o3-mini": 200_000,
}


def lookup_window(model: str | None) -> tuple[int, WindowSource]:
    """Window for a model id when the harness recorded none: the table, else the fallback."""
    if model and model in MODEL_WINDOWS:
        return MODEL_WINDOWS[model], "model_table"
    return FALLBACK_WINDOW, "fallback"

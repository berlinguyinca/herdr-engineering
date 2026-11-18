"""Context-window sizes by model id.

The older entries are seeded from the autospec context monitor's MODEL_MAX tables. The 1M entries for the
claude-*-5 models are what Claude Code's own model catalogue lists as runtime.max_input_tokens
(docs/operations/handoff-harness-capabilities.md); a ``YYYYMMDD`` dated id resolves to its undated entry.
"""
import re
from collections.abc import Mapping
from types import MappingProxyType

from .base import WindowSource

FALLBACK_WINDOW = 128_000

_MODEL_WINDOWS: dict[str, int] = {
    "claude-sonnet-4-5": 200_000,
    "claude-sonnet-4-5-1m": 1_000_000,
    "claude-sonnet-4-6": 200_000,
    "claude-sonnet-4-7": 200_000,
    "claude-opus-4-5": 200_000,
    "claude-opus-4-7": 200_000,
    "claude-opus-4-8": 200_000,
    "claude-opus-4-8-1m": 1_000_000,
    "claude-opus-4-8[1m]": 1_000_000,
    "claude-sonnet-5-5": 1_000_000,
    "claude-opus-5": 1_000_000,
    "claude-opus-5-5": 1_000_000,
    "claude-fable-5-1": 1_000_000,
    "claude-fable-5-2": 1_000_000,
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


MODEL_WINDOWS: Mapping[str, int] = MappingProxyType(_MODEL_WINDOWS)

_DATE_SUFFIX = re.compile(r"-\d{8}$")


def strip_date_suffix(model: str) -> str:
    """The id without a trailing -YYYYMMDD, so a dated and an undated spelling of one model compare equal."""
    return _DATE_SUFFIX.sub("", model)


def lookup_window(model: str | None) -> tuple[int, WindowSource]:
    """Window for a model id when the harness recorded none: the table, else the fallback."""
    for candidate in (model, strip_date_suffix(model) if model else None):
        if candidate and candidate in MODEL_WINDOWS:
            return MODEL_WINDOWS[candidate], "model_table"
    return FALLBACK_WINDOW, "fallback"

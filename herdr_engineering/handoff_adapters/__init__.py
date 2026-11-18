"""Per-harness adapters the handoff controller uses to read context and drain a session."""
from importlib import import_module

from .base import Harness, HarnessAdapter

_ADAPTERS: dict[Harness, tuple[str, str]] = {
    Harness.CLAUDE: ("claude", "ClaudeAdapter"),
    Harness.CODEX: ("codex", "CodexAdapter"),
    Harness.PI: ("pi", "PiAdapter"),
    Harness.OPENCODE: ("opencode", "OpenCodeAdapter"),
}


def adapter_for(harness: Harness) -> HarnessAdapter:
    module, cls = _ADAPTERS[harness]
    return getattr(import_module(f"{__name__}.{module}"), cls)()

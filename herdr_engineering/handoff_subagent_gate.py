"""What the handoff controller asks the subagent registry: which drain order to type, and cleanup after the pane closes."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .handoff_adapters.base import Harness
from .handoff_adapters.drain import drain_order
from .handoff_subagent_registry import purge_pane, status


@dataclass(frozen=True)
class SubagentGate:
    order_text: str
    known: bool
    live: int


def prepare_drain(pane_id: str, harness: Harness, env: Mapping[str, str]) -> SubagentGate:
    """Only Claude Code emits the subagent hooks, so every other harness takes the unknown order without reading the registry."""
    if harness is not Harness.CLAUDE:
        return SubagentGate(drain_order(False, harness), known=False, live=0)
    seen = status(pane_id, env)
    return SubagentGate(drain_order(seen.known, harness), known=seen.known, live=seen.live)


def finish(pane_id: str, env: Mapping[str, str]) -> None:
    """Call after the source pane closes so a reused pane id starts clean. Never raises."""
    try:
        purge_pane(pane_id, env)
    except OSError:
        pass

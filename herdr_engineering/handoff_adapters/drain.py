"""The fixed drain order typed into a session whose context is nearly full."""
from .base import Harness

DRAIN_ORDER_KNOWN = (
    "Context is nearly full; hand off now. "
    "1) Start no new work. "
    "2) Wait for your subagents to finish. "
    "3) Commit and push to the feature branch, never main. "
    "4) Write the briefing with herdr-handoff create, target {target}. "
    "5) Stop."
)

DRAIN_ORDER_UNKNOWN_SUBAGENTS = (
    "Context is nearly full; hand off now. "
    "1) Start no new work. "
    "2) List your background agents, then wait for them to finish. "
    "3) Commit and push to the feature branch, never main. "
    "4) Write the briefing with herdr-handoff create, target {target}. "
    "5) Stop."
)


def drain_order(subagents_known: bool, handoff_target: Harness) -> str:
    template = DRAIN_ORDER_KNOWN if subagents_known else DRAIN_ORDER_UNKNOWN_SUBAGENTS
    return template.format(target=handoff_target.value)

"""What the pane says about a chain's phase and about why a chain ended: one table, in plain operator words."""
from __future__ import annotations

from ..handoff_controller.events import Phase, StuckReason
from .style import clean

_CUT = 48
_DETAIL_CUT = 24

REASON_PHRASES: dict[StuckReason, str] = {
    StuckReason.TAKEOVER_REFUSED_AND_COMPACTION_FAILED: "takeover refused, compaction failed",
    StuckReason.BLOCKED: "session waits on a dialog",
    StuckReason.WRONG_AGENT: "pane runs another harness",
    StuckReason.GIT_STATE_UNKNOWN: "git state unknown",
    StuckReason.ON_MAIN_WITH_CHANGES: "uncommitted work on main",
    StuckReason.NO_BRIEFING: "no briefing was written",
    StuckReason.NO_COMPACT_COMMAND: "harness has no compact command",
    StuckReason.COMPACTION_NOT_READY: "session never ready to compact",
    StuckReason.COMPACTION_NOT_STARTED: "compaction never started",
    StuckReason.COMPACTION_TIMEOUT: "compaction took too long",
    StuckReason.POINTER_TOO_LONG: "briefing pointer too long",
    StuckReason.SUCCESSOR_WITHOUT_TAKEOVER: "successor without a takeover",
    StuckReason.SOURCE_NOT_CLOSED_SUCCESSOR_LIVE: "successor live, old pane not closed",
    StuckReason.LAUNCH_STATE_UNKNOWN: "successor launch state unknown",
    StuckReason.DRAIN_TIMEOUT: "session never idle to take the order",
    StuckReason.DRAIN_PROMPT_UNCERTAIN: "unsure the order was typed",
    StuckReason.DRAIN_ORDER_TOO_LONG: "handoff order too long to type",
    StuckReason.NO_RECORD: "session record unreadable",
    StuckReason.RECORD_UNREADABLE: "handoff record unreadable",
    StuckReason.MERGE_IN_PROGRESS: "merge in progress, nothing committed",
    StuckReason.PANE_GONE: "pane closed mid-handoff",
    StuckReason.COMPACTION_STATE_UNKNOWN: "compaction state unknown",
    StuckReason.NO_LONGER_MANAGED: "no longer managed",
    StuckReason.INTERNAL_ERROR: "internal error",
}


def phrase(reason: str) -> str:
    """The operator phrase of a controller reason (`name`, `name:detail` or `name; text`); unknown reasons are cleaned and cut."""
    text = clean(reason).strip()
    name, _, detail = text.partition(":")
    name = name.split(";")[0].strip()
    try:
        known = REASON_PHRASES[StuckReason(name)]
    except (ValueError, KeyError):
        return text if len(text) <= _CUT else text[:_CUT - 1] + "…"
    shown = " ".join(detail.split(";")[0].replace("_", " ").split())[:_DETAIL_CUT]  # the part after the colon, as a few words
    return f"{known} ({shown})" if shown else known


def phase_label(phase: Phase, *, attempts: int, max_attempts: int, compacting: bool) -> str:
    """What the live row says a chain is doing."""
    if phase is Phase.DRAINING:
        return "asking the session to hand off"
    if phase is Phase.WAITING:
        return "waiting for the session to finish"
    if phase is Phase.BRIEFING:
        return "briefing written, preparing takeover"
    if phase is Phase.TAKEOVER:
        return "compacting (last resort)" if compacting else f"taking over (attempt {max(attempts, 1)} of {max_attempts})"
    return {Phase.DONE_GREEN: "handed off", Phase.DONE_ORANGE: "continued in compaction", Phase.DONE_RED: "stuck"}[phase]

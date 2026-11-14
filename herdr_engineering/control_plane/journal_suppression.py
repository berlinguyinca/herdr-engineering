"""``control_plane.journal_suppression``: which no-op writes the control plane keeps out of ``fabric_events``.

Measured on the production journal (2026-11-06 .. 2026-11-08), almost every row of three types repeated the one before:
``SessionUpdated`` 98.6% (once the volatile ``runtime`` and ``transcript`` keys are ignored), ``inspector.tool`` update rows
99.8% (the call also has an ``end`` row that carries the final output) and ``inspector.usage`` 20% (all-zero reports).

Every switch is on by default. What is suppressed is the durable write (and the Activity feed entry it would make); the
in-memory projection, the live SSE stream and every API answer are unchanged. Rows already in the journal replay exactly as
before: only new writes are affected.

YAML (every key optional, unknown keys are rejected)::

    control_plane:
      journal_suppression:
        session_noop: true                  # skip journaling SessionUpdated when nothing but runtime/transcript changed
        session_heartbeat_seconds: 300      # ... but journal one anyway after this long, so a replayed updated_at stays fresh
        inspector_tool_updates: true        # journal a tool call's update rows only if the call has no end row carrying its output
        inspector_zero_usage: true          # skip an all-zero usage report that repeats the session's latest report
"""
from __future__ import annotations

from dataclasses import dataclass, fields

_BOOLS = ("session_noop", "inspector_tool_updates", "inspector_zero_usage")
MAX_HEARTBEAT_SECONDS = 600.0      # stays inside the 900 s freshness window the service catalog uses for a replayed updated_at


@dataclass(frozen=True)
class JournalSuppression:
    session_noop: bool = True
    session_heartbeat_seconds: float = 300.0
    inspector_tool_updates: bool = True
    inspector_zero_usage: bool = True

    @classmethod
    def from_config(cls, raw) -> JournalSuppression:
        """Validated ``control_plane.journal_suppression`` (None = defaults). Raises ValueError naming the bad key."""
        if raw is None:
            return cls()
        if not isinstance(raw, dict):
            raise ValueError("control_plane.journal_suppression must be a mapping")
        unknown = sorted(set(raw) - {f.name for f in fields(cls)})
        if unknown:
            raise ValueError(f"control_plane.journal_suppression has an unknown key: {unknown[0]}")
        values: dict = {}
        for key in _BOOLS:
            if key in raw:
                if not isinstance(raw[key], bool):
                    raise ValueError(f"control_plane.journal_suppression.{key} must be true or false")
                values[key] = raw[key]
        if "session_heartbeat_seconds" in raw:
            seconds = raw["session_heartbeat_seconds"]
            if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not 1 <= seconds <= MAX_HEARTBEAT_SECONDS:
                raise ValueError("control_plane.journal_suppression.session_heartbeat_seconds must be a number "
                                 f"from 1 to {int(MAX_HEARTBEAT_SECONDS)}")
            values["session_heartbeat_seconds"] = float(seconds)
        return cls(**values)


OFF = JournalSuppression(session_noop=False, inspector_tool_updates=False, inspector_zero_usage=False)

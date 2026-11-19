"""When the side pane refreshes what: a clock-free scheduler, so tests drive it with plain numbers."""
from __future__ import annotations

EVENTS_REFRESH_S = 2.0  # focus, event log and live context gauge
CI_REFRESH_S = 90.0  # CI is read from the network; never faster than a minute


class Due:
    """True once at the start and then once per `interval_s`. A clock that goes backwards counts as due, so a wall
    clock set back never stalls a refresh."""

    def __init__(self, interval_s: float) -> None:
        self.interval_s = interval_s
        self._last: float | None = None

    def take(self, now: float) -> bool:
        if self._last is not None and 0 <= now - self._last < self.interval_s:
            return False
        self._last = now
        return True

    def reset(self) -> None:
        self._last = None

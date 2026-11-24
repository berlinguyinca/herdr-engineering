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


IDLE_REFRESH_S = 5.0  # the events refresh once nothing has changed for a minute
IDLE_AFTER_S = 60.0
STRUCTURE_REFRESH_S = 10.0  # the pane list is re-read at least this often; sooner when the agent list changed


class Backoff:
    """The refresh interval: `fast_s` while something changes, `slow_s` after `quiet_s` without a change. Clock-free; a
    clock that goes backwards counts as a change."""

    def __init__(self, fast_s: float = EVENTS_REFRESH_S, slow_s: float = IDLE_REFRESH_S, quiet_s: float = IDLE_AFTER_S) -> None:
        self._fast, self._slow, self._quiet = fast_s, slow_s, quiet_s
        self._changed_at: float | None = None
        self.interval_s = fast_s

    def observe(self, now: float, *, changed: bool) -> None:
        if changed or self._changed_at is None or now < self._changed_at:
            self._changed_at = now
        self.interval_s = self._slow if now - self._changed_at >= self._quiet else self._fast

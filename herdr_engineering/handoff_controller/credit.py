"""The controller's deadlines run on credited time: wall time while the process is awake and ticking, never more than
one interval per tick after a gap (a stopped process, a sleeping machine) in a running process.

`tick` is called once per loop pass with the wall clock, the monotonic clock and the wall time of the oldest open
chain's first event; `now` is the credited time, `credited(t)` maps a wall time read from the event log into the same
scale. A deadline is then `now - credited(event time)`, and a night of sleep never runs one out. A restart is not a gap:
the clock starts at the wall time, so an event's age after a restart is its wall-clock age.

`_breaks` holds the points where credited time and wall time parted. `credited(t)` needs, for the times it is asked
about, the break in effect at `t` and nothing older: those are an open chain's times (from its first event on) and the
end of a failed chain whose cool-down still runs (at most `_KEEP_S` back). `tick` drops every break older than the
earlier of the two cut-offs, except the one in effect at the cut-off itself, so the list is as long as the number of
sleeps in the last two hours.
"""
from __future__ import annotations

_GAP_TICKS = 3  # a tick later than this many intervals counts as a gap
_DRIFT_S = 1.0
_KEEP_S = 7200.0  # twice the longest cool-down after a failed chain (60 minutes)


class TickClock:
    def __init__(self, interval_s: float) -> None:
        self.interval_s = interval_s
        self._now: float | None = None
        self._mono = 0.0
        self._shift = 0.0  # wall time minus credited time, from the latest break on
        self._breaks: list[tuple[float, float]] = []  # (wall time the shift began at, shift)

    @property
    def started(self) -> bool:
        return self._now is not None

    def tick(self, wall: float, mono: float, oldest_open: float | None = None) -> float:
        if self._now is None:
            self._now = wall
        else:
            step = mono - self._mono
            self._now += self.interval_s if step > _GAP_TICKS * self.interval_s or step < 0 else step
        self._mono = mono
        shift = wall - self._now
        if abs(shift - self._shift) > _DRIFT_S:
            self._breaks.append((wall, shift))
            self._shift = shift
        self._prune(min(wall - _KEEP_S, oldest_open if oldest_open is not None else wall))
        return self._now

    def _prune(self, cutoff: float) -> None:
        """Drop the breaks that no time at or after `cutoff` can be in effect at."""
        keep = max((i for i, (start, _) in enumerate(self._breaks) if start <= cutoff), default=0)
        del self._breaks[:keep]

    def now(self, wall: float) -> float:
        """The credited time of the latest tick (`wall` is the fallback before the first one)."""
        return wall if self._now is None else self._now

    def credited(self, wall: float) -> float:
        shift = 0.0
        for start, value in self._breaks:
            if start <= wall:
                shift = value
        return wall - shift

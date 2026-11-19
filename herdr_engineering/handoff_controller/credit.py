"""The controller's deadlines run on credited time: wall time while the process is awake and ticking, never more than
one interval per tick after a gap (a stopped process, a sleeping machine) or a restart.

`tick` is called once per loop pass with the wall clock, the monotonic clock and the wall time of the newest open
chain's last event; `now` is the credited time, `credited(t)` maps a wall time read from the event log into the same
scale. A deadline is then `now - credited(event time)`, and a night of sleep never runs one out.
"""
from __future__ import annotations

_GAP_TICKS = 3  # a tick later than this many intervals counts as a gap
_DRIFT_S = 1.0


class TickClock:
    def __init__(self, interval_s: float) -> None:
        self.interval_s = interval_s
        self._now: float | None = None
        self._mono = 0.0
        self._shift = 0.0  # wall time minus credited time, from the latest break on
        self._breaks: list[tuple[float, float]] = []  # (wall time the shift began at, shift)
        self._floor = float("-inf")  # events older than a restart are credited as at most one interval old

    @property
    def started(self) -> bool:
        return self._now is not None

    def tick(self, wall: float, mono: float, newest_open: float | None) -> float:
        if self._now is None:
            self._now = wall
            if newest_open is not None and wall - newest_open > self.interval_s:
                self._floor = wall - self.interval_s
        else:
            step = mono - self._mono
            self._now += self.interval_s if step > _GAP_TICKS * self.interval_s or step < 0 else step
        self._mono = mono
        shift = wall - self._now
        if abs(shift - self._shift) > _DRIFT_S:
            self._breaks.append((wall, shift))
            self._shift = shift
        return self._now

    def now(self, wall: float) -> float:
        """The credited time of the latest tick (`wall` is the fallback before the first one)."""
        return wall if self._now is None else self._now

    def credited(self, wall: float) -> float:
        shift = 0.0
        for start, value in self._breaks:
            if start <= wall:
                shift = value
        return max(wall - shift, self._floor)

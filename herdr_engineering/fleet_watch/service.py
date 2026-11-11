"""The long-running side of the watcher (decision D11): poll cadence and the progress watchdog.

`next_sleep` is the pause between passes (D1/D2: 120 s +-20 s, stretched or backed off as the poller reports).
`Watchdog` implements the time limits: a SOFT limit only warns when one pass has been running long; a HARD limit
fires only after NO PROGRESS (no state/outbox/log write and no explicit beat) for that long. A healthy rollout that
takes hours because every host call keeps finishing is therefore never killed by a guessed wall-clock cap.
"""

from __future__ import annotations

import contextlib
import random
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path

from . import poll

SOFT_LIMIT_S = 3600.0
#: Longer than one apply (30 min) plus a health call, so only a real stall can reach it.
HARD_LIMIT_S = 5400.0
MIN_WAIT_S = 5
ORDINARY = ("new", "changed", "unchanged")


def next_sleep(report: dict, interval: int | None, rng: random.Random) -> int:
    """Seconds to wait before the next pass.

    Default: the soonest `wait_s` the poller reported (120 s +-20 s, 600 s when the rate limit is low, the backoff
    schedule after errors; a repo that is still backing off reports `retry_in_s`). `interval` (tests, operators)
    replaces the ordinary cadence but never shortens a backoff in which every repo is failing.
    """
    waits, failing = [], 0
    infos = list(report.get("repos", {}).values())
    for info in infos:
        wait = info.get("retry_in_s") if info.get("outcome") == "backoff" else info.get("wait_s")
        if wait is not None:
            waits.append(int(wait))
        if info.get("outcome") not in ORDINARY:
            failing += 1
    if interval is None:
        return max(MIN_WAIT_S, min(waits)) if waits else poll.INTERVAL_S + rng.randint(-poll.JITTER_S, poll.JITTER_S)
    jitter = min(poll.JITTER_S, interval // 6)
    base = max(1, interval + rng.randint(-jitter, jitter))
    if infos and failing == len(infos) and waits:
        return max(base, min(waits))
    return base


class Watchdog(threading.Thread):
    """Armed only while a pass runs (`with dog.guard()`); idle sleeps are never a stall."""

    def __init__(self, markers: Sequence[Path], *, soft_s: float, hard_s: float,
                 on_soft: Callable[[float], None], on_hard: Callable[[float], None], tick_s: float = 5.0):
        super().__init__(daemon=True, name="fleet-watch-watchdog")
        self.markers = list(markers)
        self.soft_s, self.hard_s, self.tick_s = soft_s, hard_s, tick_s
        self.on_soft, self.on_hard = on_soft, on_hard
        self._lock = threading.Lock()
        self._armed_at: float | None = None
        self._beat = 0.0
        self._warned = False
        self._stop = threading.Event()

    def beat(self) -> None:
        with self._lock:
            self._beat = time.time()

    @contextlib.contextmanager
    def guard(self) -> Iterator[None]:
        with self._lock:
            self._armed_at = self._beat = time.time()
            self._warned = False
        try:
            yield
        finally:
            with self._lock:
                self._armed_at = None

    def stop(self) -> None:
        self._stop.set()

    def _last_progress(self) -> float:
        latest = self._beat
        for path in self.markers:
            with contextlib.suppress(OSError):
                latest = max(latest, path.stat().st_mtime)
        return latest

    def run(self) -> None:
        while not self._stop.wait(self.tick_s):
            with self._lock:
                armed = self._armed_at
                if armed is None:
                    continue
                now = time.time()
                elapsed, stalled = now - armed, now - self._last_progress()
                warn = elapsed >= self.soft_s and not self._warned
                self._warned = self._warned or warn
            if warn:
                self.on_soft(elapsed)
            if stalled >= self.hard_s:
                self.on_hard(stalled)
                return

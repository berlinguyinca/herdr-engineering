"""Pause detector for the control-plane web process.

A 30 GiB heap and one process serving every request means a single long pause (a full garbage collection, a thread
holding the GIL) shows up as dozens of requests finishing at once. Two cheap probes say which it was:

* ``GcTimer`` logs any collection that takes ``GC_LOG_SECONDS`` or more, with its generation.
* ``StallWatch`` sleeps in short ticks; a tick that returns ``STALL_SECONDS`` late means the whole interpreter was
  paused, and the warning lists what each other thread was doing when it resumed.

``freeze_boot_heap`` moves everything allocated during start-up out of the collector's view, so the long-lived
projection is not re-scanned by every full collection.
"""
from __future__ import annotations

import gc
import logging
import sys
import threading
import time
import traceback

log = logging.getLogger(__name__)

GC_LOG_SECONDS = 0.5
STALL_SECONDS = 3.0
TICK_SECONDS = 0.5
_FRAMES_PER_THREAD = 3


class GcTimer:
    """``gc.callbacks`` hook; remembers the longest pause it saw."""

    def __init__(self, threshold: float = GC_LOG_SECONDS, clock=time.monotonic):
        self.threshold, self._clock = threshold, clock
        self._started: float | None = None
        self.longest = 0.0

    def __call__(self, phase: str, info: dict) -> None:
        if phase == "start":
            self._started = self._clock()
        elif phase == "stop" and self._started is not None:
            took, self._started = self._clock() - self._started, None
            self.longest = max(self.longest, took)
            if took >= self.threshold:
                log.warning("gc pause %.2fs generation=%s collected=%s", took, info.get("generation"), info.get("collected"))


class StallWatch(threading.Thread):
    def __init__(self, stall: float = STALL_SECONDS, tick: float = TICK_SECONDS):
        super().__init__(name="herdr-stall-watch", daemon=True)
        self.stall, self.tick = stall, tick
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        while not self._stop_event.is_set():
            before = time.monotonic()
            self._stop_event.wait(self.tick)
            late = time.monotonic() - before - self.tick
            if late >= self.stall and not self._stop_event.is_set():
                log.warning("process paused %.1fs (tick %.2fs); threads on resume:\n%s", late + self.tick, self.tick, self.threads())

    @staticmethod
    def threads() -> str:
        names = {t.ident: t.name for t in threading.enumerate()}
        lines = []
        for ident, frame in sys._current_frames().items():
            if ident == threading.get_ident():
                continue
            stack = traceback.extract_stack(frame)[-_FRAMES_PER_THREAD:]
            lines.append(f"  {names.get(ident, ident)}: " + " < ".join(f"{f.name} ({f.filename.rsplit('/', 1)[-1]}:{f.lineno})" for f in reversed(stack)))
        return "\n".join(sorted(lines))


def freeze_boot_heap() -> int:
    """Collect once, then exclude the surviving objects from future collections. Returns how many were frozen."""
    gc.collect()
    gc.freeze()
    return gc.get_freeze_count()


def install() -> StallWatch:
    gc.callbacks.append(GcTimer())
    watch = StallWatch()
    watch.start()
    return watch

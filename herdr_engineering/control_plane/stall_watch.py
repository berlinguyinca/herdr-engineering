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
from dataclasses import dataclass

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
                log.warning("process paused %.1fs (tick %.2fs); %s; threads on resume:\n%s",
                            late + self.tick, self.tick, gc_state(), self.threads())

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


@dataclass(frozen=True)
class GcTuning:
    """``control_plane.gc`` in the fleet config: collector thresholds plus whether to freeze the boot heap."""
    gen0: int = 700
    gen1: int = 10
    gen2: int = 1000
    freeze: bool = True


_GC_KEYS = ("gen0", "gen1", "gen2", "freeze")


def parse_gc_config(section) -> GcTuning:
    """Parse ``control_plane.gc`` (None = defaults); a malformed block raises ValueError so startup stops."""
    if section is None:
        return GcTuning()
    if not isinstance(section, dict):
        raise ValueError("control_plane.gc must be a mapping")
    unknown = sorted(set(section) - set(_GC_KEYS))
    if unknown:
        raise ValueError(f"control_plane.gc: unknown keys {unknown}")
    values = {}
    for key in ("gen0", "gen1", "gen2"):
        if key in section:
            v = section[key]
            if isinstance(v, bool) or not isinstance(v, int) or v < 1:
                raise ValueError(f"control_plane.gc.{key} must be a positive integer")
            values[key] = v
    if "freeze" in section:
        if not isinstance(section["freeze"], bool):
            raise ValueError("control_plane.gc.freeze must be true or false")
        values["freeze"] = section["freeze"]
    return GcTuning(**values)


def gc_state() -> str:
    return f"gc frozen={gc.get_freeze_count()} thresholds={gc.get_threshold()}"


def tune_gc(tuning: GcTuning) -> int:
    """Freeze the boot heap (when enabled) and raise the thresholds so full collections are rare. Returns frozen count."""
    frozen = freeze_boot_heap() if tuning.freeze else 0
    gc.set_threshold(tuning.gen0, tuning.gen1, tuning.gen2)
    log.info("gc tuned: gen0=%d gen1=%d gen2=%d freeze=%s frozen=%d", tuning.gen0, tuning.gen1, tuning.gen2, tuning.freeze, frozen)
    return frozen


def install() -> StallWatch:
    gc.callbacks.append(GcTimer())
    watch = StallWatch()
    watch.start()
    return watch

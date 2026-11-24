import gc
import logging
import re
import time

import pytest

from herdr_engineering.control_plane import stall_watch


def _hold_gil(seconds):
    """Run a regex that backtracks exponentially (the C matcher never releases the GIL) until it has held it that long."""
    n = 18
    while True:
        start = time.monotonic()
        re.match(r"(a+)+$", "a" * n + "b")
        if time.monotonic() - start >= seconds:
            return
        n += 1


class _SteppingClock:
    """A clock that moves by ``jump`` on every reading.

    Whether the watchdog saw a pause is a computation over two clock readings, so the test supplies the clock:
    the pause is created rather than raced against a scheduler. Asserting "no pause happened" against a real
    monotonic clock is exactly what made this suite red on loaded CI hosts (#874) — a starved tick returns late
    and the watchdog is right to say so.
    """

    def __init__(self, jump: float) -> None:
        self.jump, self.readings = jump, 0

    def __call__(self) -> float:
        self.readings += 1
        return self.readings * self.jump


def _wait_until(fact, deadline_seconds: float = 5.0) -> bool:
    """Wait for a fact the code records, instead of napping for a fixed wall-clock guess."""
    deadline = time.monotonic() + deadline_seconds
    while time.monotonic() < deadline:
        if fact():
            return True
        time.sleep(0.01)
    return False


def test_watch_reports_a_paused_interpreter_and_names_other_threads(caplog):
    caplog.set_level(logging.WARNING, logger=stall_watch.__name__)
    # tick 0.02 s, and the clock says each tick took 0.7 s: the pause is asserted against bookkeeping
    # (`watch.pauses`), not against how long a helper thread happened to be descheduled.
    clock = _SteppingClock(jump=0.7)
    watch = stall_watch.StallWatch(stall=0.2, tick=0.02, clock=clock)
    watch.start()
    try:
        assert _wait_until(lambda: watch.pauses), "a late tick must be reported and recorded"
    finally:
        watch.stop()
        watch.join(2)
    assert any("process paused" in r.message and "MainThread" in r.message for r in caplog.records)
    assert watch.pauses[0] >= 0.2


def test_watch_reports_a_real_gil_hold(caplog):
    """The clock-injected case above proves the arithmetic; this one proves the mechanism exists — a thread
    that will not release the GIL really does stall the tick. Presence is waited for, never timed."""
    caplog.set_level(logging.WARNING, logger=stall_watch.__name__)
    watch = stall_watch.StallWatch(stall=0.2, tick=0.02)
    watch.start()
    try:
        _hold_gil(0.6)
        assert _wait_until(lambda: watch.pauses), "holding the GIL longer than the stall window must register"
    finally:
        watch.stop()
        watch.join(2)
    assert any("process paused" in r.message and "MainThread" in r.message for r in caplog.records)


def test_watch_is_quiet_when_nothing_is_paused(caplog):
    caplog.set_level(logging.WARNING, logger=stall_watch.__name__)
    # The clock advances exactly one tick per reading: lateness is zero by construction, so this says what it
    # means to say — a watch that sees no lateness logs nothing — without depending on host load.
    clock = _SteppingClock(jump=0.02)
    watch = stall_watch.StallWatch(stall=0.5, tick=0.02, clock=clock)
    watch.start()
    try:
        assert _wait_until(lambda: clock.readings >= 6), "the watch really ran several ticks"
    finally:
        watch.stop()
        watch.join(2)
    assert not caplog.records
    assert watch.pauses == []


def test_gc_timer_logs_only_long_collections(caplog):
    caplog.set_level(logging.WARNING, logger=stall_watch.__name__)
    slow, quick = stall_watch.GcTimer(threshold=0), stall_watch.GcTimer(threshold=3600)
    # A serve test earlier in the same worker leaves its own GcTimer on gc.callbacks; isolate so only these two log.
    others = gc.callbacks[:]
    gc.callbacks[:] = [slow, quick]
    try:
        gc.collect()
    finally:
        gc.callbacks[:] = others
    assert any("gc pause" in r.message and "generation=2" in r.message for r in caplog.records)
    assert quick.longest >= 0 and len([r for r in caplog.records]) == 1


def test_freeze_boot_heap_moves_live_objects_out_of_the_collector():
    keep = [{"k": i} for i in range(1000)]
    try:
        assert stall_watch.freeze_boot_heap() >= 1000
        assert gc.get_freeze_count() >= 1000
    finally:
        gc.unfreeze()
        del keep


def test_parse_gc_config_defaults_keep_the_interpreters_gen0_gen1_and_raise_gen2():
    gen0, gen1, _ = gc.get_threshold()
    assert stall_watch.parse_gc_config(None) == stall_watch.GcTuning(gen0, gen1, 1000, True)
    assert stall_watch.parse_gc_config({"gen2": 50}) == stall_watch.GcTuning(gen0, gen1, 50, True)


def test_parse_gc_config_defaults_follow_the_running_interpreter():
    saved = gc.get_threshold()
    try:
        gc.set_threshold(2000, 7, 3)
        assert stall_watch.parse_gc_config(None) == stall_watch.GcTuning(2000, 7, 1000, True)
    finally:
        gc.set_threshold(*saved)


def test_parse_gc_config_rejects_values_gc_set_threshold_cannot_hold():
    with pytest.raises(ValueError, match=r"control_plane\.gc\.gen2"):
        stall_watch.parse_gc_config({"gen2": 2**31})
    assert stall_watch.parse_gc_config({"gen2": 2**31 - 1}).gen2 == 2**31 - 1
    got = stall_watch.parse_gc_config({"gen0": 500, "gen1": 5, "gen2": 4000, "freeze": False})
    assert got == stall_watch.GcTuning(500, 5, 4000, False)


@pytest.mark.parametrize("bad", [[], {"gen2": 0}, {"gen0": "x"}, {"gen1": True}, {"freeze": "yes"}, {"bogus": 1}])
def test_parse_gc_config_rejects_bad_sections(bad):
    with pytest.raises(ValueError):
        stall_watch.parse_gc_config(bad)


def test_tune_gc_freezes_boot_heap_and_applies_thresholds(caplog):
    caplog.set_level(logging.INFO, logger=stall_watch.__name__)
    saved = gc.get_threshold()
    keep = [{"k": i} for i in range(1000)]
    try:
        frozen = stall_watch.tune_gc(stall_watch.GcTuning(650, 9, 1234, True))
        assert frozen >= 1000 and gc.get_freeze_count() >= 1000
        assert gc.get_threshold() == (650, 9, 1234)
        assert any("gen2=1234" in r.message and "frozen=" in r.message for r in caplog.records)
        assert "frozen=" in stall_watch.gc_state()
    finally:
        gc.unfreeze()
        gc.set_threshold(*saved)
        del keep


def test_tune_gc_without_freeze_keeps_heap_collectable():
    gc.unfreeze()                      # order-independent: an earlier in-process web start may have frozen the heap
    saved = gc.get_threshold()
    try:
        assert stall_watch.tune_gc(stall_watch.GcTuning(700, 10, 1000, False)) == 0
        assert gc.get_freeze_count() == 0 and gc.get_threshold() == (700, 10, 1000)
    finally:
        gc.set_threshold(*saved)

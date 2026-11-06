import gc
import logging
import re
import time

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


def test_watch_reports_a_paused_interpreter_and_names_other_threads(caplog):
    caplog.set_level(logging.WARNING, logger=stall_watch.__name__)
    watch = stall_watch.StallWatch(stall=0.2, tick=0.02)
    watch.start()
    try:
        time.sleep(0.1)
        _hold_gil(0.6)
        time.sleep(0.2)
    finally:
        watch.stop()
        watch.join(2)
    assert any("process paused" in r.message and "MainThread" in r.message for r in caplog.records)


def test_watch_is_quiet_when_nothing_is_paused(caplog):
    caplog.set_level(logging.WARNING, logger=stall_watch.__name__)
    watch = stall_watch.StallWatch(stall=0.5, tick=0.02)
    watch.start()
    time.sleep(0.3)
    watch.stop()
    watch.join(2)
    assert not caplog.records


def test_gc_timer_logs_only_long_collections(caplog):
    caplog.set_level(logging.WARNING, logger=stall_watch.__name__)
    slow, quick = stall_watch.GcTimer(threshold=0), stall_watch.GcTimer(threshold=3600)
    gc.callbacks.extend([slow, quick])
    try:
        gc.collect()
    finally:
        gc.callbacks.remove(slow)
        gc.callbacks.remove(quick)
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

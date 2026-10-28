"""Admission must not invert ordinary event-write lock ordering."""
from contextlib import contextmanager
from threading import Event, RLock, Thread, current_thread

from herdr_engineering.control_plane.repo import ControlPlaneRepo


def test_admission_takes_event_lock_before_sink_transaction_lock():
    ordinary_ready, proceed, probed = Event(), Event(), Event()
    observed, errors = [], []

    class Sink:
        def __init__(self):
            self.lock, self.in_admission, self.write_failed = RLock(), False, False

        @contextmanager
        def scheduler_admission(self):
            with self.lock:
                self.in_admission = True
                try:
                    yield []
                finally:
                    self.in_admission = False

        def record_required(self, event):
            with self.lock:
                pass

        def record(self, event):
            # Bound the intentionally reproduced inversion so the test also
            # cleans up its threads against the old broken implementation.
            acquired = self.lock.acquire(timeout=.25)
            self.write_failed = not acquired
            if acquired:
                self.lock.release()

    sink = Sink()
    repo = ControlPlaneRepo(sink=sink)

    class EventLock:
        def __init__(self):
            self.lock = RLock()

        def __enter__(self):
            if current_thread().name == "admission-test" and not probed.is_set():
                observed.append(sink.in_admission)
                probed.set()
            self.lock.acquire()
            return self

        def __exit__(self, *args):
            self.lock.release()

    repo._event_lock = EventLock()

    def ordinary():
        with repo._event_lock:
            ordinary_ready.set()
            if proceed.wait(3):
                repo.record_event("note", "ordinary", "Observed")

    def admission():
        try:
            with repo.scheduler_admission():
                repo.save_scheduler_reservation({"reservation_id": "race", "state": "reserved"})
        except Exception as exc:
            errors.append(exc)

    first, second = Thread(target=ordinary), Thread(target=admission, name="admission-test")
    first.start()
    try:
        assert ordinary_ready.wait(3)
        second.start()
        assert probed.wait(3)
    finally:
        proceed.set()
        first.join(timeout=3)
        if second.ident is not None:
            second.join(timeout=3)
    assert not first.is_alive() and not second.is_alive()
    assert observed == [False], "admission held sink lock while waiting for event lock"
    assert not sink.write_failed and errors == []

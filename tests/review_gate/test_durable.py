"""Gate writes are durable-required: a failed write raises and leaves memory alone."""

import pytest

from herdr_engineering.control_plane.repo import ControlPlaneRepo
from herdr_engineering.review_gate.models import Author, PrKey, State
from herdr_engineering.review_gate.queue import ReviewQueue
from herdr_engineering.review_gate.registry import AuthorRegistry
from herdr_engineering.review_gate.store import PipelineStore

KEY = PrKey("github.com/acme/widgets", 7)
A = "a" * 40


class FlakySink:
    """Like DurableEventSink: ``record`` swallows, ``record_required`` raises."""

    def __init__(self):
        self.fail = False
        self.events = []

    def record_required(self, ev):
        if self.fail:
            raise RuntimeError("durable event insert was not acknowledged")
        self.events.append(ev)

    def record(self, ev):
        try:
            self.record_required(ev)
        except Exception:
            pass


@pytest.fixture
def sink():
    return FlakySink()


def test_store_write_failure_raises_and_does_not_advance_state(sink):
    repo = ControlPlaneRepo(sink=sink)
    store = PipelineStore(repo)
    assert store.on_push(KEY, A)
    sink.fail = True
    with pytest.raises(RuntimeError):
        store.on_ci_green(KEY, A)
    assert store.snapshot(KEY).state == State.CI_PENDING
    assert [e["event_type"] for e in repo.stream("pr_gate", str(KEY))] == ["push"]
    assert [e["event_type"] for e in sink.events] == ["push"]


def test_queue_write_failure_raises_and_leaves_queue_empty(sink):
    repo = ControlPlaneRepo(sink=sink)
    queue = ReviewQueue(repo)
    sink.fail = True
    with pytest.raises(RuntimeError):
        queue.enqueue(KEY, A, "m")
    assert queue.depth() == 0 and queue.snapshot() == []


def test_registry_pin_failure_raises_and_pins_nothing(sink):
    repo = ControlPlaneRepo(sink=sink)
    registry = AuthorRegistry(repo)
    sink.fail = True
    with pytest.raises(RuntimeError):
        registry.pin(KEY, Author(kind="pi", model="m"))
    assert repo.stream("pr_gate_author", str(KEY)) == []
    sink.fail = False
    registry.pin(KEY, Author(kind="pi", model="m2"))
    assert registry.author_of(KEY, "feat/x").model == "m2"


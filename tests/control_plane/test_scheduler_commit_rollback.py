"""An acknowledged scheduler write is not durable until admission COMMIT succeeds."""

import copy
from contextlib import contextmanager

import pytest

from herdr_engineering.control_plane.repo import ControlPlaneRepo


class FailingCommitSink:
    """Acknowledge INSERTs, then fail only after the transaction body returns."""

    def __init__(self):
        self.acknowledged = []
        self.rows = []

    def record_required(self, event):
        self.acknowledged.append(copy.deepcopy(event))

    record = record_required

    @contextmanager
    def scheduler_admission(self):
        yield copy.deepcopy(self.rows)
        raise RuntimeError("injected scheduler COMMIT failure")


@pytest.fixture(params=[
    "policy", "reservation-insert", "reservation-update", "lease-insert", "lease-update",
])
def failed_transaction(request):
    sink = FailingCommitSink()
    repo = ControlPlaneRepo(sink=sink)
    # Preserve unrelated committed history as well as the affected stream.
    repo.record_event("note", "existing-note", "NoteSaved", {"text": "keep this history"})
    case = request.param
    if case == "policy":
        entity_type, entity_id = "host", "policy-host"
        repo.upsert_host(
            entity_id, entity_id, None, host_uid="uid-policy-host", platform="linux",
            scheduler_owned=True, scheduler_trusted=True,
            scheduler_policy={"mode": "manual_only", "revision": 1},
        )

        def projection():
            return repo.get_host(entity_id)["scheduler_policy"]

        def mutate():
            # set_host_policy opens its own admission transaction.
            repo.set_host_policy(entity_id, mode="automatic", expected_revision=1)

        event_type = "HostPolicyChanged"
    else:
        kind, operation = case.split("-")
        entity_type, entity_id = f"scheduler_{kind}", f"{kind}-one"
        save = getattr(repo, f"save_scheduler_{kind}")
        projection = getattr(repo, f"list_scheduler_{kind}s")
        id_field = f"{kind}_id"
        initial_state = "reserved" if kind == "reservation" else "running"
        if operation == "update":
            save({id_field: entity_id, "state": initial_state})
        # A different committed row must survive rollback of either an insert or update.
        save({id_field: f"{kind}-unrelated", "state": initial_state})
        sink.rows = repo.list_scheduler_reservations()

        def mutate():
            with repo.scheduler_admission():
                save({id_field: entity_id, "state": "released"})

        event_type = "SchedulerReservationChanged" if kind == "reservation" else "SchedulerLeaseChanged"

    def snapshot():
        return copy.deepcopy({
            "projection": projection(),
            "stream": repo.stream(entity_type, entity_id),
            "recent_events": repo.recent_events(),
        })

    before = snapshot()
    published = []
    unsubscribe = repo.subscribe(lambda event: published.append(copy.deepcopy(event)))
    acknowledged_before = len(sink.acknowledged)
    try:
        with pytest.raises(RuntimeError, match="injected scheduler COMMIT failure"):
            mutate()
    finally:
        unsubscribe()
    # Confirm the write reached the sink; this is a COMMIT failure, not an INSERT failure.
    assert len(sink.acknowledged) == acknowledged_before + 1
    assert sink.acknowledged[-1]["event_type"] == event_type
    return before, snapshot(), published


def test_failed_commit_restores_projection_stream_and_event_feed(failed_transaction):
    before, after, _ = failed_transaction
    assert after == before


def test_failed_commit_does_not_publish_to_subscribers(failed_transaction):
    _, _, published = failed_transaction
    assert published == []


def test_successful_transaction_publishes_ordered_receipts_only_after_commit():
    class SuccessfulSink(FailingCommitSink):
        committed = False

        @contextmanager
        def scheduler_admission(self):
            yield []
            self.committed = True

    sink = SuccessfulSink()
    repo = ControlPlaneRepo(sink=sink)
    published = []
    repo.subscribe(lambda event: published.append((sink.committed, event["sequence"])))
    with repo.scheduler_admission():
        repo.save_scheduler_reservation({"reservation_id": "same", "state": "reserved"})
        repo.save_scheduler_reservation({"reservation_id": "same", "state": "released"})
        assert repo.stream("scheduler_reservation", "same") == []
        assert published == []
        with pytest.raises(RuntimeError, match="backup requires completed"):
            repo.export_state()
    assert published == [(True, 0), (True, 1)]
    assert [event["payload"]["state"] for event in repo.stream("scheduler_reservation", "same")] == ["reserved", "released"]


def test_derived_ingest_event_suppression_cannot_drop_durable_scheduler_receipts(acknowledged_sink):
    repo = ControlPlaneRepo(sink=acknowledged_sink)
    # Envelope projection temporarily suppresses derived events. A concurrent
    # server command must still persist its authoritative receipt.
    repo._suppress_record_events = True
    repo.save_scheduler_reservation({"reservation_id": "required", "state": "reserved"})
    assert len(acknowledged_sink.events) == 1
    assert acknowledged_sink.events[0]["entity_type"] == "scheduler_reservation"
    assert len(repo.stream("scheduler_reservation", "required")) == 1

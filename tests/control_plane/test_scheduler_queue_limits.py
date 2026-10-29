"""Queue admission limits must follow persisted rows, not a service's history."""

from contextlib import contextmanager

import pytest

from herdr_engineering.control_plane.repo import ControlPlaneRepo
from herdr_engineering.control_plane.scheduler_service import SchedulerService


def request(request_id, project="project-a"):
    return {
        "request_id": request_id, "agent_family": "codex", "project_id": project,
        "resource_profile": {"class": "coding", "cpu_cores": 2, "memory_gib": 2},
    }


def capacity(repo, cpu):
    repo.upsert_host(
        "queue-host", "queue-host", None, host_uid="uid-queue-host",
        aliases=["queue-host"], platform="linux",
        scheduler_owned=True, scheduler_trusted=True,
        scheduler_policy={"mode": "automatic", "revision": 1},
        scheduler_capacity={"total_cpu": cpu, "total_memory_gib": 64, "total_disk_gib": 100},
    )
    repo.record_telemetry("queue-host", {"freshness_seconds": 0})


@pytest.fixture
def repo(acknowledged_sink):
    result = ControlPlaneRepo(sink=acknowledged_sink)
    capacity(result, 0)
    return result


@pytest.mark.parametrize("scope", ["owner", "project", "global"])
@pytest.mark.parametrize("lifecycle", ["restart", "second-service"])
def test_queue_caps_include_requests_from_another_service(repo, acknowledged_sink, scope, lifecycle):
    limits = {f"{scope}_queue_limit": 1}
    first = SchedulerService(repo, **limits)
    # Create the other service before admission: constructor-only restoration is insufficient.
    second = SchedulerService(repo, **limits)
    queued = first.reserve(request("first"), "alice")
    assert queued["state"] == "queued"
    if lifecycle == "restart":
        restored = ControlPlaneRepo(sink=acknowledged_sink)
        restored.import_state(repo.export_state())
        second = SchedulerService(restored, **limits)

    owner = "alice" if scope == "owner" else "bob"
    project = "project-a" if scope == "project" else "project-b"
    with pytest.raises(RuntimeError, match=f"{scope} queue cap"):
        second.reserve(request("overflow", project), owner)

    assert second.repo.list_scheduler_reservations() == [queued]
    assert second.reserve(request("first"), "alice") == queued


def test_queue_cap_uses_transaction_rows_when_local_projection_is_empty(repo, acknowledged_sink, monkeypatch):
    queued = SchedulerService(repo).reserve(request("persisted-elsewhere"), "alice")

    @contextmanager
    def authoritative_rows():
        yield [queued]

    # Model the sink boundary: another replica sees durable rows only inside admission.
    monkeypatch.setattr(acknowledged_sink, "scheduler_admission", authoritative_rows, raising=False)
    replica_repo = ControlPlaneRepo(sink=acknowledged_sink)
    assert replica_repo.list_scheduler_reservations() == []
    replica = SchedulerService(replica_repo, owner_queue_limit=1)
    with pytest.raises(RuntimeError, match="owner queue cap"):
        replica.reserve(request("overflow"), "alice")
    assert replica_repo.list_scheduler_reservations() == []


@pytest.mark.parametrize("scope", ["owner", "project", "global"])
@pytest.mark.parametrize("method", ["retry", "drain"])
def test_lowered_caps_preserve_existing_queue_and_allow_admission(repo, scope, method):
    original = SchedulerService(repo)
    queued = [original.reserve(request(f"waiting-{i}"), "alice") for i in range(3)]
    service = SchedulerService(repo, **{f"{scope}_queue_limit": 1})

    # Existing requests may be read and retried above the new cap, without being dropped.
    assert service.reserve(request("waiting-0"), "alice") == queued[0]
    assert service.retry_queued(queued[0]["reservation_id"])["state"] == "queued"
    assert service.drain_queue() == []
    assert len(repo.list_scheduler_reservations()) == 3
    with pytest.raises(RuntimeError, match=f"{scope} queue cap"):
        service.reserve(request("new-overflow"), "alice")

    capacity(repo, 6)
    if method == "retry":
        admitted = [service.retry_queued(row["reservation_id"]) for row in queued]
    else:
        admitted = service.drain_queue()
    assert {row["reservation_id"] for row in admitted} == {
        row["reservation_id"] for row in queued
    }
    assert all(row["state"] == "reserved" for row in admitted)
    assert service.reserve(request("replacement"), "alice")["state"] == "queued"
    with pytest.raises(RuntimeError, match=f"{scope} queue cap"):
        service.reserve(request("second-overflow"), "alice")


@pytest.mark.parametrize("transition", ["cancel", "retry", "drain"])
@pytest.mark.parametrize("other_service", [False, True])
def test_departed_queue_entries_free_slots_for_the_original_service(repo, transition, other_service):
    service = SchedulerService(repo, owner_queue_limit=1, project_queue_limit=1, global_queue_limit=1)
    queued = service.reserve(request("waiting"), "alice")
    actor = SchedulerService(repo) if other_service else service
    if transition == "cancel":
        assert actor.release(queued["reservation_id"], state="cancelled")["state"] == "cancelled"
    else:
        capacity(repo, 2)
        if transition == "retry":
            assert actor.retry_queued(queued["reservation_id"])["state"] == "reserved"
        else:
            assert [row["reservation_id"] for row in actor.drain_queue()] == [queued["reservation_id"]]

    replacement = service.reserve(request("replacement"), "alice")
    assert replacement["state"] == "queued"
    assert [row["request_id"] for row in repo.list_scheduler_reservations()
            if row["state"] == "queued"] == ["replacement"]


@pytest.mark.parametrize("scope", ["owner", "project"])
def test_queue_caps_do_not_block_unrelated_owner_or_project(repo, scope):
    service = SchedulerService(repo, **{f"{scope}_queue_limit": 1})
    assert service.reserve(request("first"), "alice")["state"] == "queued"
    owner = "bob" if scope == "owner" else "alice"
    project = "project-b" if scope == "project" else "project-a"
    assert service.reserve(request("unrelated", project), owner)["state"] == "queued"


def test_full_queue_does_not_block_immediately_placeable_work(repo):
    service = SchedulerService(repo, global_queue_limit=1)
    assert service.reserve(request("waiting"), "alice")["state"] == "queued"
    capacity(repo, 2)
    assert service.reserve(request("placeable"), "alice")["state"] == "reserved"

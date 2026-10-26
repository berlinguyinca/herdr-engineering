from datetime import datetime, timedelta

import pytest

from herdr_engineering.control_plane.repo import ControlPlaneRepo
from herdr_engineering.control_plane.scheduler_service import SchedulerService


def host_body(request_id):
    return {
        "request_id": request_id,
        "agent_family": "codex",
        "placement_mode": "automatic",
        "resource_profile": {"class": "coding", "cpu_cores": 2, "memory_gib": 2},
    }


def configured_repo(sink=None):
    repo = ControlPlaneRepo(sink=sink)
    repo.upsert_host(
        "beast", "beast", "100.0.0.2", host_uid="uid-beast", aliases=["beast"],
        platform="linux", scheduler_owned=True, scheduler_trusted=True,
        scheduler_policy={"mode": "automatic", "revision": 1},
        scheduler_capacity={"total_cpu": 4, "total_memory_gib": 8, "total_disk_gib": 20},
    )
    repo.record_telemetry("beast", {"freshness_seconds": 1, "sequence": 1})
    return repo


def test_scheduler_reservation_survives_repo_export_import_and_service_restart():
    first_repo = configured_repo()
    first = SchedulerService(first_repo)
    reservation = first.reserve(host_body("restart-1"), "alice")
    assert reservation["state"] == "reserved"

    restored_repo = ControlPlaneRepo()
    restored_repo.import_state(first_repo.export_state())
    second = SchedulerService(restored_repo)
    replayed = second.reserve(host_body("restart-1"), "alice")
    assert replayed["reservation_id"] == reservation["reservation_id"]
    assert second.commit(replayed["reservation_id"])["state"] == "committed"


def test_scheduler_reservation_events_rehydrate_without_export_state():
    source = configured_repo()
    first = SchedulerService(source)
    reservation = first.reserve(host_body("event-restart"), "alice")

    restored = ControlPlaneRepo()
    restored.import_state({"version": 1, "events": source.export_state()["events"], "event_order": source.export_state()["event_order"]})
    replayed = SchedulerService(restored).reserve(host_body("event-restart"), "alice")
    assert replayed["reservation_id"] == reservation["reservation_id"]


def test_reconcile_persists_expired_reservation_state():
    repo = configured_repo()
    service = SchedulerService(repo, reservation_ttl=1)
    reservation = service.reserve(host_body("expiry-1"), "alice")
    expiry = datetime.fromisoformat(reservation["expires_at"])
    expired = service.reconcile(expiry + timedelta(seconds=1))
    assert expired[0]["reservation_id"] == reservation["reservation_id"]
    assert expired[0]["state"] == "expired"
    assert repo.list_scheduler_reservations()[0]["state"] == "expired"


def test_commit_persists_one_execution_lease_and_is_idempotent_after_restart():
    repo = configured_repo()
    service = SchedulerService(repo)
    reservation = service.reserve(host_body("lease-1"), "alice")
    committed = service.commit(reservation["reservation_id"])
    retried = service.commit(reservation["reservation_id"])
    leases = repo.list_scheduler_leases()
    assert committed["lease_id"] == retried["lease_id"]
    assert len(leases) == 1 and leases[0]["state"] == "running"

    restored = ControlPlaneRepo()
    restored.import_state(repo.export_state())
    assert restored.list_scheduler_leases()[0]["lease_id"] == committed["lease_id"]


def test_lease_heartbeat_renews_only_with_current_token_and_generation():
    repo = configured_repo()
    service = SchedulerService(repo)
    reservation = service.reserve(host_body("heartbeat-1"), "alice")
    service.commit(reservation["reservation_id"])
    lease = repo.list_scheduler_leases()[0]
    renewed = service.heartbeat(lease["lease_id"], generation=lease["generation"], token=lease["token"], ttl=120)
    assert renewed["expires_at"] != lease.get("expires_at")
    with pytest.raises(PermissionError, match="lease token"):
        service.heartbeat(lease["lease_id"], generation=lease["generation"], token="wrong", ttl=120)
    with pytest.raises(PermissionError, match="generation"):
        service.heartbeat(lease["lease_id"], generation=lease["generation"] - 1, token=lease["token"], ttl=120)


def test_reconcile_fences_expired_lease_and_persists_the_state():
    repo = configured_repo()
    service = SchedulerService(repo)
    reservation = service.reserve(host_body("heartbeat-expiry"), "alice")
    service.commit(reservation["reservation_id"])
    lease = repo.list_scheduler_leases()[0]
    expired = service.reconcile_leases(datetime.fromisoformat(lease["expires_at"]) + timedelta(seconds=1))
    assert expired[0]["lease_id"] == lease["lease_id"]
    assert expired[0]["state"] == "fenced"


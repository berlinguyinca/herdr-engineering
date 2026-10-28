import copy
from datetime import UTC, datetime, timedelta

import pytest

from herdr_engineering.control_plane.repo import ControlPlaneRepo
from herdr_engineering.control_plane.scheduler_service import SchedulerService


class AcknowledgedSink:
    def __init__(self):
        self.events = []

    def record_required(self, event):
        self.events.append(copy.deepcopy(event))

    record = record_required


def host_body(request_id):
    return {
        "request_id": request_id,
        "agent_family": "codex",
        "placement_mode": "automatic",
        "resource_profile": {"class": "coding", "cpu_cores": 2, "memory_gib": 2},
    }


def configured_repo(sink=None):
    repo = ControlPlaneRepo(sink=sink or AcknowledgedSink())
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

    restored_repo = ControlPlaneRepo(sink=AcknowledgedSink())
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


def test_commit_retry_does_not_revive_fenced_execution_lease():
    repo = configured_repo()
    service = SchedulerService(repo)
    reservation = service.reserve(host_body("fenced-retry"), "alice")
    service.commit(reservation["reservation_id"])
    lease = repo.list_scheduler_leases()[0]
    service.reconcile_leases(datetime.fromisoformat(lease["expires_at"]) + timedelta(seconds=1))
    with pytest.raises(RuntimeError, match="reconciliation"):
        service.commit(reservation["reservation_id"])
    assert repo.list_scheduler_leases()[0]["state"] == "fenced"


def test_commit_retry_preserves_existing_lease_expiry():
    repo = configured_repo()
    service = SchedulerService(repo)
    reservation = service.reserve(host_body("expiry-retry"), "alice")
    service.commit(reservation["reservation_id"])
    original = repo.list_scheduler_leases()[0]
    service.commit(reservation["reservation_id"])
    assert repo.list_scheduler_leases()[0] == original


def test_expired_lease_cannot_renew_before_reconciliation_sweep():
    repo = configured_repo()
    service = SchedulerService(repo)
    reservation = service.reserve(host_body("late-heartbeat"), "alice")
    service.commit(reservation["reservation_id"])
    lease = repo.list_scheduler_leases()[0]
    lease["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    repo.save_scheduler_lease(lease)
    with pytest.raises(PermissionError, match="expired"):
        service.heartbeat(lease["lease_id"], generation=lease["generation"], token=lease["token"])
    assert repo.list_scheduler_leases()[0]["expires_at"] == lease["expires_at"]


@pytest.mark.parametrize("kind", ["reservation", "lease"])
def test_scheduler_projection_does_not_acknowledge_failed_durable_write(kind):
    class FailedSink:
        def record(self, event):
            pass

        def record_required(self, event):
            raise RuntimeError("storage unavailable")

    repo = ControlPlaneRepo(sink=FailedSink())
    save = getattr(repo, f"save_scheduler_{kind}")
    listing = getattr(repo, f"list_scheduler_{kind}s")
    with pytest.raises(RuntimeError, match="storage unavailable"):
        save({f"{kind}_id": "failed-write", "state": "reserved"})
    assert listing() == []


def test_two_service_instances_share_authoritative_reservation_demand():
    from concurrent.futures import ThreadPoolExecutor

    repo = configured_repo()
    services = [SchedulerService(repo), SchedulerService(repo)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        rows = list(pool.map(lambda i: services[i % 2].reserve(host_body(f"parallel-{i}"), "alice"), range(20)))
    assert sum(row["state"] == "reserved" for row in rows) == 2
    assert sum(row["state"] == "queued" for row in rows) == 18


def test_execution_submission_can_be_claimed_only_once():
    repo = configured_repo()
    first = SchedulerService(repo)
    reservation = first.reserve(host_body("single-launch"), "alice")
    first.commit(reservation["reservation_id"])
    assert first.claim_execution(reservation["reservation_id"])["execution_claimed"]
    with pytest.raises(RuntimeError, match="reconciliation"):
        SchedulerService(repo).claim_execution(reservation["reservation_id"])


def test_expired_reservation_cannot_commit_before_sweep():
    repo = configured_repo()
    service = SchedulerService(repo)
    row = service.reserve(host_body("expired-bind"), "alice")
    row["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    repo.save_scheduler_reservation(row)
    with pytest.raises(RuntimeError, match="expired"):
        service.commit(row["reservation_id"])


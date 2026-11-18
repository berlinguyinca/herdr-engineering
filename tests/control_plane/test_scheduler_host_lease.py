"""A fleet host may renew only its own already-claimed execution lease."""

import copy
import json
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest

from herdr_engineering.control_plane.repo import ControlPlaneRepo
from herdr_engineering.control_plane.scheduler_service import SchedulerService

HOST = "worker-a"
HOST_UID = "host-" + "a" * 32
OTHER_HOST = "worker-b"
OTHER_UID = "host-" + "b" * 32


@pytest.fixture
def execution(acknowledged_sink):
    repo = ControlPlaneRepo(sink=acknowledged_sink)
    service = SchedulerService(repo)
    for host, uid in [(HOST, HOST_UID), (OTHER_HOST, OTHER_UID)]:
        service.observe_host(host, {
            "host_uid": uid, "captured_at": datetime.now(UTC).isoformat(),
            "sequence": 1, "platform": "linux", "cpu_count": 16, "cpu_utilization": .1,
            "memory_total_gib": 64, "memory_available_gib": 60,
            "disk_total_gib": 500, "disk_free_gib": 400, "pressure": {},
        })
    reservation = service.reserve({
        "request_id": "private-request", "agent_family": "codex",
        "placement_mode": "manual", "explicit_host_uid": HOST_UID,
        "resource_profile": {"class": "coding", "cpu_cores": 1},
    }, "private-operator")
    service.commit(reservation["reservation_id"])
    claimed = service.claim_execution(reservation["reservation_id"])
    lease = repo.list_scheduler_leases()[0]
    lease["expires_at"] = (datetime.now(UTC) + timedelta(seconds=30)).isoformat()
    repo.save_scheduler_lease(lease)
    return service, claimed, lease


def renew(service, lease, host=HOST, **changes):
    arguments = {"lease_id": lease["lease_id"], "generation": lease["generation"], "token": lease["token"]}
    return service.heartbeat_for_host(host, **(arguments | changes))


def assert_denied_without_writes(service, action):
    before = copy.deepcopy(service.repo.export_state())
    with pytest.raises(PermissionError) as error:
        action()
    assert str(error.value) == "lease renewal is not authorized"
    assert service.repo.export_state() == before


def test_observed_host_renews_claimed_lease_with_fixed_ttl_and_safe_response(execution):
    service, reservation, lease = execution
    result = renew(service, lease)
    saved = service.repo.list_scheduler_leases()[0]
    assert result == {key: saved[key] for key in ("lease_id", "state", "generation", "expires_at")}
    assert result["lease_id"] == lease["lease_id"]
    assert result["state"] == "running"
    assert result["generation"] == 1
    assert datetime.fromisoformat(result["expires_at"]) > datetime.fromisoformat(lease["expires_at"])
    assert (datetime.fromisoformat(saved["expires_at"]) - datetime.fromisoformat(saved["heartbeat_at"])).total_seconds() == 90
    assert saved["token"] == lease["token"]
    assert service.repo.list_scheduler_reservations() == [reservation]
    assert lease["token"] not in json.dumps(result)
    assert "private-operator" not in json.dumps(result)
    assert "private-request" not in json.dumps(result)


@pytest.mark.parametrize("host", [OTHER_HOST, "unknown-host", HOST_UID, "inventory-only", None])
def test_foreign_unobserved_and_raw_uid_hosts_cannot_renew(execution, host):
    service, _, lease = execution
    # Inventory claims are not authenticated resource observations, even if their UID matches.
    service.repo.upsert_host("inventory-only", "inventory-only", None, host_uid=HOST_UID)
    assert_denied_without_writes(service, lambda: renew(service, lease, host))


@pytest.mark.parametrize("arguments", [
    {"lease_id": "missing"}, {"lease_id": ""}, {"lease_id": None},
    {"lease_id": ["lease"]}, {"lease_id": "x" * 129},
    {"token": "wrong"}, {"token": ""}, {"token": None}, {"token": ["token"]},
    {"token": "x" * 257}, {"token": "é"},
    {"generation": 0}, {"generation": -1}, {"generation": True},
    {"generation": "1"}, {"generation": 1.0}, {"generation": None}, {"generation": 2},
])
def test_invalid_lease_credentials_are_denied_without_mutation(execution, arguments):
    service, _, lease = execution
    assert_denied_without_writes(service, lambda: renew(service, lease, **arguments))


@pytest.mark.parametrize("changes", [
    {"state": "released"}, {"state": "reserved"},
    {"execution_claimed": False}, {"execution_claimed": 1},
    {"request_snapshot": None}, {"request_snapshot": {}},
    {"request_snapshot": {"request_id": "different", "owner_id": "private-operator"}},
    {"request_snapshot": {"request_id": "private-request"}},
    {"host_uid": OTHER_UID}, {"lease_id": "different"},
    {"request_id": "different"}, {"lease_token": "different"}, {"lease_token": None},
])
def test_reservation_must_be_claimed_and_bound_to_the_same_lease(execution, changes):
    service, reservation, lease = execution
    service.repo.save_scheduler_reservation(reservation | changes)
    assert_denied_without_writes(service, lambda: renew(service, lease))


@pytest.mark.parametrize("changes", [
    {"host_uid": OTHER_UID}, {"reservation_id": "missing"}, {"request_id": "different"},
    {"state": "released"}, {"state": "fenced"}, {"token": "different"},
    {"generation": "1"}, {"generation": True},
    {"expires_at": "2000-01-01T00:00:00+00:00"}, {"expires_at": "not-a-date"},
])
def test_authoritative_lease_binding_and_liveness_are_required(execution, changes):
    service, _, lease = execution
    service.repo.save_scheduler_lease(lease | changes)
    assert_denied_without_writes(service, lambda: renew(service, lease))


def test_host_renewal_uses_transaction_rows_instead_of_stale_local_reservation(execution, monkeypatch):
    service, reservation, lease = execution
    admission = service.repo.scheduler_admission

    @contextmanager
    def released_on_another_replica():
        with admission():
            yield [reservation | {"state": "released"}]

    monkeypatch.setattr(service.repo, "scheduler_admission", released_on_another_replica)
    assert_denied_without_writes(service, lambda: renew(service, lease))


@pytest.mark.parametrize("extra", [{"ttl": 3600}, {"host_uid": HOST_UID}, {"owner_id": "private-operator"}])
def test_host_renewal_does_not_accept_caller_authority_or_ttl(execution, extra):
    service, _, lease = execution
    before = service.repo.export_state()
    with pytest.raises(TypeError):
        renew(service, lease, **extra)
    assert service.repo.export_state() == before

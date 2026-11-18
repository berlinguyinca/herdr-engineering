import threading
import http.client
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from herdr_engineering.control_plane.sink import DurableEventSink
from herdr_engineering.fleet_config import FleetConfigError
from tests.control_plane.test_scheduler_http_admission import post, sample
from tests.control_plane.test_scheduler_http_admission import (
    server as scheduler_server,  # noqa: F401
)
from tests.control_plane.test_scheduler_postgres import (
    scheduler_postgres as scheduler_database,  # noqa: F401
)

ROUTE = "/api/v1/fleet/runtime/leases/heartbeat"
HOST_A = "host-" + "a" * 32
HOST_B = "host-" + "b" * 32


@pytest.fixture
def bound(request):
    instance = request.getfixturevalue("scheduler_server")
    return prepare_bound(instance)


def prepare_bound(instance):
    config = SimpleNamespace(section=lambda _: {}, host_for_token=lambda token: {
        "host-a-token": "host-a", "host-b-token": "host-b", "unobserved-token": "unobserved",
    }.get(token))
    instance.fleet_config = SimpleNamespace(current=lambda: config)
    for token, uid in (("host-a-token", HOST_A), ("host-b-token", HOST_B)):
        status, _ = post(instance, "/api/v1/fleet/config/telemetry", {**sample(), "host_uid": uid},
                         {"X-Host-Token": token})
        assert status == 200
    instance.scheduler_admission = True
    status, reservation = post(instance, "/api/v1/scheduler/reservations", {
        "request_id": "bound-runtime", "agent_family": "codex", "explicit_host_uid": HOST_A,
        "resource_profile": {"class": "coding"},
    }, {"X-API-Key": "ops"})
    assert status == 201 and reservation["state"] == "reserved"
    for action in ("commit", "claim"):
        assert post(instance, "/api/v1/scheduler/reservations/" + action,
                    {"reservation_id": reservation["reservation_id"]}, {"X-API-Key": "ops"})[0] == 200
    lease, = instance.repo.list_scheduler_leases()
    instance.scheduler_admission = False
    return instance, lease


def heartbeat(lease, **changes):
    return {"lease_id": lease["lease_id"], "generation": lease["generation"],
            "token": lease["token"], **changes}


def test_host_can_maintain_bound_lease_during_admission_rollback_without_exposing_secret(bound):
    instance, lease = bound
    status, result = post(instance, ROUTE, heartbeat(lease, ttl=999999), {"X-Host-Token": "host-a-token"})
    assert status == 200
    assert set(result) == {"lease_id", "state", "generation", "expires_at"}
    assert result["lease_id"] == lease["lease_id"] and result["state"] == "running"
    assert lease["token"] not in str(result)
    assert 0 < (datetime.fromisoformat(result["expires_at"]) - datetime.now(UTC)).total_seconds() <= 90


@pytest.mark.parametrize("headers", [{}, {"X-API-Key": "ops"}, {"X-Host-Token": "invalid"}])
def test_runtime_lease_callback_requires_host_credential(bound, headers):
    instance, lease = bound
    assert post(instance, ROUTE, heartbeat(lease), headers)[0] == 401


def test_host_cannot_borrow_other_host_identity_even_with_lease_secret(bound):
    instance, lease = bound
    before = instance.repo.list_scheduler_leases()
    assert post(instance, ROUTE, heartbeat(lease, host_uid=HOST_A),
                {"X-Host-Token": "host-b-token"})[0] == 403
    assert instance.repo.list_scheduler_leases() == before


@pytest.mark.parametrize("changes,status", [({"token": "wrong"}, 403),
                                           ({"generation": 2}, 403),
                                           ({"generation": True}, 400)])
def test_runtime_lease_callback_rejects_stale_or_malformed_proof(bound, changes, status):
    instance, lease = bound
    assert post(instance, ROUTE, heartbeat(lease, **changes), {"X-Host-Token": "host-a-token"})[0] == status


def test_unobserved_host_cannot_reuse_raw_host_uid_metadata(bound):
    instance, lease = bound
    instance.repo.upsert_host("unobserved", "unobserved", None, host_uid=HOST_A,
                              scheduler_owned=True, scheduler_trusted=True)
    assert post(instance, ROUTE, heartbeat(lease), {"X-Host-Token": "unobserved-token"})[0] == 403


def test_unclaimed_execution_cannot_be_kept_alive_by_host_callback(bound):
    instance, lease = bound
    reservation, = instance.repo.list_scheduler_reservations()
    instance.repo.save_scheduler_reservation({**reservation, "execution_claimed": False})
    assert post(instance, ROUTE, heartbeat(lease), {"X-Host-Token": "host-a-token"})[0] == 403


def test_unavailable_host_configuration_is_not_a_revocation_or_secret_leak(bound):
    instance, lease = bound

    def unavailable():
        raise FleetConfigError("private configuration detail")

    instance.fleet_config = SimpleNamespace(current=unavailable)
    status, result = post(instance, ROUTE, heartbeat(lease), {"X-Host-Token": "host-a-token"})
    assert status == 503 and "private configuration detail" not in str(result)


def test_host_callback_does_not_acknowledge_or_leak_storage_failure(bound, monkeypatch):
    instance, lease = bound
    before = instance.repo.list_scheduler_leases()

    def unavailable(_event):
        raise OSError("private storage connection detail")

    monkeypatch.setattr(instance.repo._sink, "record_required", unavailable)
    try:
        status, result = post(instance, ROUTE, heartbeat(lease), {"X-Host-Token": "host-a-token"})
    except http.client.RemoteDisconnected:
        status, result = None, {}
    assert status == 503
    assert "private storage connection detail" not in str(result)
    assert instance.repo.list_scheduler_leases() == before


def test_host_callback_refreshes_binding_and_fencing_across_real_database_replicas(request):
    dsn = request.getfixturevalue("scheduler_database")
    running = []

    def start():
        instance = ControlPlaneServer(ControlPlaneRepo(sink=DurableEventSink(dsn=dsn)),
                                     api_keys={"ops": "operator"})
        thread = threading.Thread(target=instance.serve, daemon=True)
        running.append((instance, thread))
        thread.start()
        instance.wait_until_ready()
        return instance

    try:
        first, replica = start(), start()
        _, lease = prepare_bound(first)
        replica.fleet_config = first.fleet_config
        assert replica.repo.list_scheduler_leases() == []
        status, result = post(replica, ROUTE, heartbeat(lease), {"X-Host-Token": "host-a-token"})
        assert status == 200 and result["lease_id"] == lease["lease_id"]
        assert "token" not in result
        first.scheduler.reconcile_leases(datetime.now(UTC) + timedelta(seconds=120))
        assert post(replica, ROUTE, heartbeat(lease), {"X-Host-Token": "host-a-token"})[0] == 403
        assert replica.repo.list_scheduler_leases()[0]["state"] == "fenced"
    finally:
        for instance, thread in running:
            instance.shutdown()
            thread.join(timeout=5)

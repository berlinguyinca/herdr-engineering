import http.client
import json
import threading
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.repo import ControlPlaneRepo


@pytest.fixture
def server(acknowledged_sink):
    config = SimpleNamespace(host_for_token=lambda token: "fleet-linux" if token == "host-secret" else None,
                             section=lambda _: {})
    instance = ControlPlaneServer(ControlPlaneRepo(sink=acknowledged_sink),
                                 fleet_config=SimpleNamespace(current=lambda: config),
                                 api_keys={"ops": "operator"})
    thread = threading.Thread(target=instance.serve, daemon=True)
    thread.start()
    instance.wait_until_ready()
    yield instance
    instance.shutdown()
    thread.join(timeout=5)


def post(server, route, body, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
    try:
        connection.request("POST", route, json.dumps(body),
                           {"Content-Type": "application/json", **(headers or {})})
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


def sample():
    return {"host_uid": "host-" + "a" * 32, "captured_at": datetime.now(UTC).isoformat(),
            "sequence": 1, "platform": "linux", "cpu_count": 16, "cpu_utilization": .1,
            "memory_total_gib": 64, "memory_available_gib": 50,
            "disk_total_gib": 500, "disk_free_gib": 400, "pressure": {}}


def request():
    return {"request_id": "http-placement", "agent_family": "codex",
            "resource_profile": {"class": "coding"}}


def test_resource_observation_requires_host_token_not_operator_key(server):
    route = "/api/v1/fleet/config/telemetry"
    assert post(server, route, sample(), {"X-API-Key": "ops"})[0] == 401
    assert post(server, route, sample(), {"X-Host-Token": "wrong"})[0] == 401
    assert post(server, route, sample(), {"X-Host-Token": "host-secret"})[0] == 200
    assert server.repo.get_host("fleet-linux")["scheduler_trusted"] is True
    assert post(server, route, sample(), {"X-Host-Token": "host-secret"})[0] == 400


def test_new_admission_defaults_off_and_can_be_explicitly_enabled(server):
    route = "/api/v1/scheduler/reservations"
    assert post(server, route, request(), {"X-API-Key": "ops"})[0] == 503
    assert post(server, "/api/v1/scheduler/plan", request(), {"X-API-Key": "ops"})[0] == 200
    post(server, "/api/v1/fleet/config/telemetry", sample(), {"X-Host-Token": "host-secret"})
    server.scheduler_admission = True
    status, row = post(server, route, request(), {"X-API-Key": "ops"})
    assert status == 201 and row["state"] == "reserved"
    server.scheduler_admission = False
    assert post(server, route + "/commit", {"reservation_id": row["reservation_id"]},
                {"X-API-Key": "ops"})[0] == 503
    assert post(server, route + "/cancel", {"reservation_id": row["reservation_id"]},
                {"X-API-Key": "ops"})[0] == 200


def test_scheduler_mutations_enforce_same_origin_with_operator_credentials(server):
    server.scheduler_admission = True
    route = "/api/v1/scheduler/reservations"
    assert post(server, route, request(), {
        "X-API-Key": "ops", "Origin": "https://untrusted.invalid"})[0] == 403
    assert server.repo.list_scheduler_reservations() == []
    status, row = post(server, route, request(), {
        "X-API-Key": "ops", "Origin": f"http://127.0.0.1:{server.port}"})
    assert status == 201 and row["state"] == "queued"

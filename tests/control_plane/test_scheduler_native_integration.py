"""Exercise native scheduler admission over HTTP without launching real processes."""

import hashlib
import http.client
import json
import threading

import pytest

from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from herdr_engineering.herdr_adapter import NativeHerdrAdapter
from herdr_engineering.workspace import WorkspaceManifest, WorkspaceManifestStore

OPERATOR_KEY = "scheduler-integration-operator"
VIEWER_KEY = "scheduler-integration-viewer"
# ControlPlaneServer's request handler _principal binds ownership to key AND role.
OPERATOR_PRINCIPAL = (
    f"api-key:{hashlib.sha256(OPERATOR_KEY.encode()).hexdigest()[:16]}:operator"
)
HOST_ID = "scheduler-host-record"
HOST_UID = "host-0123456789abcdef0123456789abcdef"
MACHINE_LABEL = "saved-native-machine"
WORKSPACE_ID = "prepared-native-workspace"
WORKSPACE_PATH = "/work/prepared task"
LAUNCH_ROUTE = "/api/v1/scheduler/launch"


class RecordedRunner:
    """Only recorded CLI responses are available; no subprocess fallback exists."""

    def __init__(self):
        self.calls = []

    def run(self, args, timeout=30):
        self.calls.append(list(args))
        if args == ["machine", "list", "--json"]:
            return json.dumps([
                {"id": "native-machine-id", "label": MACHINE_LABEL,
                 "target": "unused@example.invalid"},
            ])
        if args[:4] == ["--machine", MACHINE_LABEL, "workspace", "create"]:
            return json.dumps({"ok": True, "result": {"root_pane": {"pane_id": "w7:p3"}}})
        if args[:4] == ["--machine", MACHINE_LABEL, "agent", "start"]:
            return json.dumps({"ok": True, "result": {"ready": True}})
        raise AssertionError(f"Unexpected native command: {args!r}")


class HttpClient:
    def __init__(self, port):
        self.port = port

    def request(self, method, path, *, key=OPERATOR_KEY, body=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            connection.request(
                method, path,
                body=json.dumps(body) if body is not None else None,
                headers={"X-API-Key": key, "Content-Type": "application/json"},
            )
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()


@pytest.fixture
def native_server(acknowledged_sink):
    repo = ControlPlaneRepo(sink=acknowledged_sink)
    repo.upsert_host(
        HOST_ID, "Native scheduler test host", "192.0.2.10",
        host_uid=HOST_UID, aliases=[MACHINE_LABEL], platform="linux",
        scheduler_owned=True, scheduler_trusted=True,
        scheduler_policy={"mode": "automatic", "revision": 1},
        scheduler_capacity={"total_cpu": 16, "total_memory_gib": 64, "total_disk_gib": 100},
    )
    repo.record_telemetry(HOST_ID, {"freshness_seconds": 0, "sequence": 1})
    manifest = WorkspaceManifest(
        workspace_id=WORKSPACE_ID, repo_url="https://example.invalid/repo.git",
        revision="a" * 40, branch="scheduler-test", worktree_id="native-worktree",
        task_id="native-task", artifact_refs=(), environment_digest="test-environment",
        owner=OPERATOR_PRINCIPAL, acl=(OPERATOR_PRINCIPAL,),
    )
    WorkspaceManifestStore(repo).mark_prepared(
        manifest, host_uid=HOST_UID, path=WORKSPACE_PATH, revision=manifest.revision,
    )
    runner = RecordedRunner()
    servers = []

    def start(**options):
        server = ControlPlaneServer(
            repo, api_keys={OPERATOR_KEY: "operator", VIEWER_KEY: "viewer"},
            native_adapter=NativeHerdrAdapter(runner=runner), **options,
        )
        thread = threading.Thread(target=server.serve, daemon=True)
        servers.append((server, thread))
        thread.start()
        server.wait_until_ready()
        assert server.port != 0, "HTTP server did not become ready"
        return HttpClient(server.port), repo, runner

    yield start

    for server, thread in reversed(servers):
        server.shutdown()
        thread.join(timeout=5)
        assert not thread.is_alive(), "HTTP server did not shut down"


def launch_body(family="codex"):
    return {
        "request_id": f"http-native-{family}", "agent_family": family,
        "placement_mode": "automatic", "workspace_id": WORKSPACE_ID,
        "resource_profile": {"class": "coding", "cpu_cores": 2, "memory_gib": 2},
    }


@pytest.mark.parametrize("family,kind", [("pi", "pi"), ("claude-code", "claude"), ("codex", "codex")])
def test_http_native_launch_routes_saved_machine_and_starts_once(native_server, family, kind):
    client, repo, runner = native_server(scheduler_admission=True)
    body = launch_body(family)

    status, result = client.request("POST", LAUNCH_ROUTE, body=body)

    assert status == 201, result
    assert result["request_id"] == body["request_id"]
    assert result["family"] == family
    assert result["host_uid"] == HOST_UID
    assert result["native_result"]["machine"] == MACHINE_LABEL
    assert result["native_result"]["pane_id"] == "w7:p3"
    assert result["native_result"]["start"]["ok"] is True
    name = "scheduler-" + hashlib.sha256(body["request_id"].encode()).hexdigest()[:24]
    expected_calls = [
        ["machine", "list", "--json"],
        ["--machine", MACHINE_LABEL, "workspace", "create", "--cwd", WORKSPACE_PATH,
         "--label", name, "--no-focus"],
        ["--machine", MACHINE_LABEL, "agent", "start", name, "--kind", kind,
         "--pane", "w7:p3"],
    ]
    assert runner.calls == expected_calls
    reservations = repo.list_scheduler_reservations()
    assert len(reservations) == 1
    reservation = reservations[0]
    assert reservation["reservation_id"] == result["reservation_id"]
    assert reservation["state"] == "committed"
    assert reservation["execution_claimed"] is True
    assert reservation["request_snapshot"]["owner_id"] == OPERATOR_PRINCIPAL
    leases = repo.list_scheduler_leases()
    assert len(leases) == 1
    assert leases[0]["lease_id"] == result["lease_id"]
    assert leases[0]["state"] == "running"

    status, repeated = client.request("POST", LAUNCH_ROUTE, body=body)

    assert status == 409, repeated
    assert "reconciliation" in repeated["error"]
    assert runner.calls == expected_calls
    assert repo.list_scheduler_reservations() == reservations
    assert repo.list_scheduler_leases() == leases


def test_http_native_launch_is_disabled_by_default(native_server):
    # Omit scheduler_admission to exercise the actual constructor default.
    client, repo, runner = native_server()
    body = launch_body()
    status, preview = client.request("POST", "/api/v1/scheduler/plan", body=body)
    assert status == 200, preview
    assert preview["selected_host_uid"] == HOST_UID

    status, result = client.request("POST", LAUNCH_ROUTE, body=body)

    assert status == 503, result
    assert "scheduler admission is disabled" in result["error"]
    assert runner.calls == []
    assert repo.list_scheduler_reservations() == []
    assert repo.list_scheduler_leases() == []


def test_http_viewer_cannot_launch_native_agent(native_server):
    client, repo, runner = native_server(scheduler_admission=True)
    status, overview = client.request("GET", "/api/v1/scheduler", key=VIEWER_KEY)
    assert status == 200, overview

    status, result = client.request("POST", LAUNCH_ROUTE, key=VIEWER_KEY, body=launch_body())

    assert status == 403, result
    assert result["error"] == "read-only"
    assert runner.calls == []
    assert repo.list_scheduler_reservations() == []
    assert repo.list_scheduler_leases() == []

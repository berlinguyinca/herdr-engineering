import http.client
import json
import threading
import urllib.error
import urllib.request

import pytest

from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.repo import ControlPlaneRepo


class Client:
    def __init__(self, port):
        self.base = f"http://127.0.0.1:{port}"

    def get(self, path):
        try:
            with urllib.request.urlopen(self.base + path) as r:
                return r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read()

    def post(self, path, body):
        req = urllib.request.Request(
            self.base + path, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()


@pytest.fixture()
def server(acknowledged_sink):
    repo = ControlPlaneRepo(sink=acknowledged_sink)
    svr = ControlPlaneServer(repo)
    thread = threading.Thread(target=svr.serve, daemon=True)
    thread.start()
    svr.wait_until_ready()
    yield Client(svr.port), repo
    svr.shutdown()


def test_health(server):
    c, _ = server
    status, _, body = c.get("/api/v1/health")
    assert status == 200
    assert json.loads(body)["status"] == "ok"


def test_health_reports_deployment_context(server):
    c, _ = server
    _, _, body = c.get("/api/v1/health")
    h = json.loads(body)
    # an in-memory repo reports source=memory + image version/commit
    assert h["source"] == "memory"
    assert h["version"] == "0.1.0"
    assert h["commit"] == "dev"
    assert "missions" in h and "timestamp" in h


def test_missions_roundtrip(server):
    c, repo = server
    mid = repo.create_mission("Dynamic Aliases")
    status, _, body = c.get("/api/v1/missions")
    assert status == 200
    data = json.loads(body)
    assert [m["mission_id"] for m in data["missions"]] == [mid]
    # detail
    status, _, body = c.get(f"/api/v1/missions/{mid}")
    assert json.loads(body)["title"] == "Dynamic Aliases"


def test_mission_not_found_404(server):
    c, _ = server
    status, _, _ = c.get("/api/v1/missions/mission_nope")
    assert status == 404


def test_sessions_and_services(server):
    c, repo = server
    mid = repo.create_mission("m")
    sid = repo.create_session(mission_id=mid, model="gpt-4o")
    repo.register_service("web", "host_h", 8080, session_id=sid)
    status, _, body = c.get("/api/v1/sessions")
    assert len(json.loads(body)["sessions"]) == 1
    status, _, body = c.get("/api/v1/services")
    assert json.loads(body)["services"][0]["port"] == 8080


def test_session_with_colon_id_fetches_percent_encoded(server):
    """Entity ids like ``w9:p5`` are percent-encoded by the web client
    (encodeURIComponent -> ``w9%3Ap5``); the server must URL-decode them so
    session detail resolves instead of 404ing."""
    c, repo = server
    repo.upsert_session("w9:p5", host_id="beast", mission_id="w9",
                        agent_role="pi")
    # literal colon in the path segment also works
    status, _, body = c.get("/api/v1/sessions/w9:p5")
    assert status == 200
    assert json.loads(body)["agent_role"] == "pi"
    # percent-encoded colon (what the web client actually sends)
    status, _, body = c.get("/api/v1/sessions/w9%3Ap5")
    assert status == 200
    assert json.loads(body)["session_id"] == "w9:p5"


def test_api_responses_are_never_cached(server):
    """Dynamic API/static responses carry no-store so stale 404s and stale
    app bundles can never be served from a browser cache (the fix for the
    persistent "session not found" view after a server-side route fix)."""
    c, _ = server
    status, headers, _ = c.get("/api/v1/sessions/w9%3Ap5")
    assert status == 404  # unknown session, but headers still present
    assert headers.get("Cache-Control") == "no-store"
    status, headers, _ = c.get("/api/v1/health")
    assert status == 200
    assert headers.get("Cache-Control") == "no-store"


def test_hosts_health(server):
    c, repo = server
    hid = repo.register_host("bender", "100.104.39.6")
    repo.record_heartbeat(hid)
    status, _, body = c.get("/api/v1/hosts")
    assert json.loads(body)["hosts"][0]["status"] == "healthy"


def test_activity_returns_recent_events(server):
    c, repo = server
    repo.create_mission("a")
    status, _, body = c.get("/api/v1/activity?limit=5")
    events = json.loads(body)["events"]
    assert any(e["event_type"] == "MissionCreated" for e in events)


def test_send_message_and_action(server):
    c, repo = server
    sid = repo.create_session()
    status, body = c.post(f"/api/v1/sessions/{sid}/messages",
                          {"message": "use pytest", "artifact_ids": []})
    assert status == 502
    assert json.loads(body)["ok"] is False
    assert repo.get_session(sid)["messages"] == 0
    status, body = c.post(f"/api/v1/sessions/{sid}/actions", {"action": "approve"})
    assert status == 501 and json.loads(body)["error"] == "unsupported_action"


def test_send_message_unknown_session_404(server):
    c, _ = server
    status, body = c.post("/api/v1/sessions/session_nope/messages",
                          {"message": "hi"})
    assert status == 404


def test_analytics(server):
    c, repo = server
    repo.create_mission("a")
    status, _, body = c.get("/api/v1/analytics/missions")
    assert json.loads(body)["total"] == 1
    status, _, body = c.get("/api/v1/analytics/hosts")
    assert json.loads(body)["total"] == 0


def test_sse_stream_content_type(server):
    c, _ = server
    conn = http.client.HTTPConnection("127.0.0.1", _port(c))
    conn.request("GET", "/api/v1/events/stream")
    resp = conn.getresponse()
    assert resp.status == 200
    assert resp.getheader("Content-Type").startswith("text/event-stream")
    # read the first SSE frame, then close (do NOT block on the infinite stream)
    line = resp.readline()
    assert line.startswith(b"data: ")
    conn.close()


def _port(client):
    return int(client.base.rsplit(":", 1)[1])


def test_host_detail_and_telemetry_routes(server):
    c, repo = server
    hid = repo.register_host("bender", "100.104.39.6")
    repo.record_telemetry(hid, {"cpu_pct": 12, "mem_pct": 38})
    # host detail must still work
    status, _, body = c.get(f"/api/v1/hosts/{hid}")
    assert status == 200 and json.loads(body)["host_name"] == "bender"
    # telemetry sub-route must NOT be swallowed by the detail route
    status, _, body = c.get(f"/api/v1/hosts/{hid}/telemetry")
    assert status == 200
    samples = json.loads(body)["samples"]
    assert samples[0]["cpu_pct"] == 12 and samples[0]["mem_pct"] == 38


def test_entity_event_stream(server):
    c, repo = server
    mid = repo.create_mission("stream me")
    repo.set_mission_stage(mid, "implementation")
    status, _, body = c.get(f"/api/v1/events?entity_type=mission&entity_id={mid}")
    assert status == 200
    events = json.loads(body)["events"]
    assert [e["event_type"] for e in events] == ["MissionCreated", "MissionStageChanged"]
    assert events[0]["sequence"] == 0 and events[1]["sequence"] == 1
    # missing params -> 400
    status, _, _ = c.get("/api/v1/events")
    assert status == 400


def test_unknown_route_404(server):
    c, _ = server
    status, _, _ = c.get("/api/v1/does-not-exist")
    assert status == 404


def test_auth_required_when_key_set():
    repo = ControlPlaneRepo()
    svr = ControlPlaneServer(repo, api_key="sekret")
    thread = threading.Thread(target=svr.serve, daemon=True)
    thread.start()
    svr.wait_until_ready()
    c = Client(svr.port)
    try:
        status, _, _ = c.get("/api/v1/health")
        assert status == 401
        req = urllib.request.Request(
            f"http://127.0.0.1:{svr.port}/api/v1/health",
            headers={"X-API-Key": "sekret"})
        with urllib.request.urlopen(req) as r:
            assert r.status == 200
    finally:
        svr.shutdown()


def test_ingest_endpoint_upserts_host(server):
    c, repo = server
    status, body = c.post("/api/v1/ingest", {
        "entity_type": "host", "entity_id": "bender",
        "event_type": "host.registered",
        "payload": {"host_name": "bender", "tailnet_ip": "100.104.39.6"}})
    assert status == 200
    assert json.loads(body)["ok"] is True
    status, body = c.post("/api/v1/ingest", {
        "entity_type": "host", "entity_id": "bender",
        "event_type": "host.heartbeat",
        "payload": {"cpu": 12, "mem": 40}})
    assert status == 200
    hosts = repo.list_hosts()
    assert len(hosts) == 1  # idempotent — one stable host
    assert hosts[0]["cpu"] == 12
    # the ingested events are visible to a GET on the same stream
    _, _, stream = c.get("/api/v1/events?entity_type=host&entity_id=bender")
    evs = json.loads(stream)["events"]
    assert [e["event_type"] for e in evs] == [
        "HostRegistered", "HostHeartbeat"]


def test_ingest_endpoint_validates_body(server):
    c, _ = server
    status, body = c.post("/api/v1/ingest", {"entity_type": "host"})
    assert status == 400
    status, body = c.post("/api/v1/ingest", {})
    assert status == 400


def test_native_dispatch_requires_identity_and_preserves_observed_status(server):
    c, repo = server
    sid = repo.create_session()
    status, raw = c.post(f"/api/v1/sessions/{sid}/messages", {"message": "hello", "operation_id": "one"})
    assert status == 502
    assert json.loads(raw)["status"] == "failed"
    assert repo.get_session(sid)["messages"] == 0
    status, raw = c.post(f"/api/v1/sessions/{sid}/actions", {"action": "resume"})
    assert status == 501
    assert json.loads(raw)["error"] == "unsupported_action"


def test_api_delivers_original_message_once_and_returns_native_receipt(acknowledged_sink):
    received = []
    def dispatch(session, text, artifacts):
        received.append((session["session_id"], text))
        return {"status": "delivered", "receipt": {"ok": True}}
    repo = ControlPlaneRepo(sink=acknowledged_sink)
    sid = repo.create_session()
    svr = ControlPlaneServer(repo, dispatcher=dispatch)
    thread = threading.Thread(target=svr.serve, daemon=True)
    thread.start()
    svr.wait_until_ready()
    try:
        client = Client(svr.port)
        body = {"message": "use pytest", "operation_id": "request-one"}
        first = client.post(f"/api/v1/sessions/{sid}/messages", body)
        second = client.post(f"/api/v1/sessions/{sid}/messages", body)
        assert first[0] == second[0] == 200
        assert json.loads(first[1])["status"] == "delivered"
        assert received == [(sid, "use pytest")]
    finally:
        svr.shutdown()


def test_default_native_dispatch_checks_conversation_before_prompt(monkeypatch):
    from herdr_engineering.herdr_adapter import NativeHerdrAdapter
    conversation = {"source": "integration", "agent": "pi", "kind": "path", "value": "/tmp/session.jsonl"}
    calls = []
    class Runner:
        def run(self, args, timeout=30):
            calls.append(args)
            if args[-2:] == ["api", "snapshot"]:
                return json.dumps({"ok": True, "result": {"snapshot": {"agents": [{
                    "agent": "pi", "terminal_id": "term-1", "agent_session": conversation,
                    "agent_status": "idle", "interactive_ready": True,
                }]}}})
            return '{"ok": true, "result": {"submitted": true}}'
    adapter = NativeHerdrAdapter(runner=Runner())
    monkeypatch.setattr("herdr_engineering.herdr_adapter.NativeHerdrAdapter", lambda: adapter)
    record = {"host_id": "beast", "machine": "machine-beast", "herdr_session": "eng",
              "terminal_id": "term-1", "agent_session": dict(conversation)}
    receipt = ControlPlaneServer._dispatch_native_message(None, record, "literal hello", [])
    assert receipt["status"] == "delivered"
    assert calls[-1] == ["--machine", "machine-beast", "--session", "eng", "agent", "prompt", "--", "term-1", "literal hello"]
    record["agent_session"] = {**conversation, "value": "/tmp/different-session.jsonl"}
    from herdr_engineering.errors import InvalidError
    with pytest.raises(InvalidError, match="identity changed"):
        ControlPlaneServer._dispatch_native_message(None, record, "do not deliver", [])
    assert len(calls) == 3  # snapshot+prompt, then only the mismatched snapshot


def test_message_without_durable_sink_returns_unavailable_without_dispatch():
    calls = []
    def dispatch(*args):
        calls.append(args)
        return {"status": "delivered"}
    repo = ControlPlaneRepo()
    sid = repo.create_session()
    svr = ControlPlaneServer(repo, dispatcher=dispatch)
    thread = threading.Thread(target=svr.serve, daemon=True)
    thread.start()
    svr.wait_until_ready()
    try:
        client = Client(svr.port)
        status, raw = client.post(f"/api/v1/sessions/{sid}/messages",
                                  {"message": "hello", "operation_id": "requires-durability"})
        assert status == 503
        assert json.loads(raw)["error"] == "receipt_persistence_unavailable"
        assert calls == []
        assert repo.get_session(sid)["messages"] == 0
        assert client.get("/api/v1/health")[0] == 200
    finally:
        svr.shutdown()


def test_services_archive_filter_is_optional_and_detail_history_remains(server):
    c, repo = server
    clock = [1_800_000_000]
    repo._now = lambda: clock[0]
    sid = repo.register_service('old web', 'host', 8000, status='unavailable')
    clock[0] += 8 * 86400
    _, _, body = c.get('/api/v1/services')
    data = json.loads(body)
    assert len(data['services']) == 1
    assert data['summary']['archived'] == 1
    _, _, body = c.get('/api/v1/services?include_archived=false')
    data = json.loads(body)
    assert data['services'] == []
    assert data['summary']['total'] == 1
    status, _, body = c.get(f'/api/v1/services/{sid}')
    assert status == 200
    assert json.loads(body)['lifecycle']['archived'] is True

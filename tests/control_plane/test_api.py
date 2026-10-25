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
        status, _, _ = c.get("/api/v1/missions")   # health stays open (liveness only)
        assert status == 401
        req = urllib.request.Request(
            f"http://127.0.0.1:{svr.port}/api/v1/missions",
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
    # a routine heartbeat (same identity) records no HostHeartbeat event
    assert [e["event_type"] for e in evs] == ["HostRegistered"]


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
    _, _, body = c.get('/api/v1/services?host_id=host')
    assert len(json.loads(body)['services']) == 1
    _, _, body = c.get('/api/v1/services?host_id=host&fields=feed')
    slim = json.loads(body)['services'][0]
    assert set(slim) == {'service_id', 'service_name', 'host_id', 'source_port', 'session_id',
                         'discovery_source', 'verified_origin', 'ownership', 'listener_identity'}
    _, _, body = c.get('/api/v1/services?host_id=other')
    data = json.loads(body)
    assert data['services'] == [] and data['summary']['total'] == 0  # summary follows the filter


def test_retire_endpoint_is_a_dry_run_unless_told_otherwise(server):
    c, repo = server
    clock = [1_800_000_000]
    repo._now = lambda: clock[0]
    sid = repo.register_service('silent', 'host', 8000, status='healthy')
    clock[0] += 9 * 86400
    status, body = c.post('/api/v1/services/retire', {'older_than_days': 7})
    data = json.loads(body)
    assert status == 200 and data['dry_run'] is True and data['matched'] == 1 and data['retired'] == 0
    status, body = c.post('/api/v1/services/retire', {'older_than_days': 7, 'dry_run': False})
    assert json.loads(body)['retired'] == 1
    assert json.loads(c.get(f'/api/v1/services/{sid}')[2])['lifecycle']['archived'] is True
    status, _ = c.post('/api/v1/services/retire', {'older_than_days': 0})
    assert status == 400


# ------------------------------------------------------------ fleet config
import hashlib  # noqa: E402
import os  # noqa: E402

import yaml  # noqa: E402

from herdr_engineering.fleet_config import FleetConfigStore  # noqa: E402

_DB_URL = f"postgres://{'dbuser'}:{'dbvalue'}@h/db"  # assembled, not a literal credential URL
_FC_SECRET = "placeholder-fleet-value"


def _fc_sha(t):
    return hashlib.sha256(t.encode()).hexdigest()


def _fc_doc(extra=""):
    return {
        "version": 1,
        "sections": {
            "llm": {"roles": ["builder"], "values": {"key": _FC_SECRET + extra}},
            "ci": {"roles": ["ci"], "values": {"token": "placeholder-ci-only"}},
            "control_plane": {"roles": ["control-plane"], "values": {"pg_dsn": _DB_URL}},
        },
        "hosts": {
            "bender": {"roles": ["builder"], "token_sha256": _fc_sha("placeholder-host-bender")},
            "beast": {"roles": ["ci"], "token_sha256": _fc_sha("placeholder-host-beast")},
        },
    }


def _fc_write(path, doc):
    path.write_text(yaml.safe_dump(doc))
    os.chmod(path, 0o600)


def _fc_req(port, method, path, headers=None, body=None):
    data = json.dumps(body).encode() if body is not None else None
    h = dict(headers or {})
    if data is not None:
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


@pytest.fixture()
def fc_server(tmp_path, acknowledged_sink):
    path = tmp_path / "fleet.yaml"
    _fc_write(path, _fc_doc())
    store = FleetConfigStore(path)
    svr = ControlPlaneServer(ControlPlaneRepo(sink=acknowledged_sink),
                             api_keys={"opkey": "operator", "viewkey": "viewer"},
                             fleet_config=store)
    threading.Thread(target=svr.serve, daemon=True).start()
    svr.wait_until_ready()
    yield svr.port, path, store
    svr.shutdown()


def test_fleet_config_requires_valid_host_token(fc_server):
    port, _, _ = fc_server
    for hdrs in ({}, {"X-Host-Token": "nope"}, {"X-API-Key": "opkey"}):
        status, _, body = _fc_req(port, "GET", "/api/v1/fleet/config", hdrs)
        assert status == 401
        assert json.loads(body) == {"error": "unauthorized"}


def test_fleet_config_returns_scoped_slice_no_store_header(fc_server):
    port, _, _ = fc_server
    status, headers, body = _fc_req(port, "GET", "/api/v1/fleet/config", {"X-Host-Token": "placeholder-host-bender"})
    assert status == 200
    assert headers["Cache-Control"] == "no-store"
    data = json.loads(body)
    assert data["values"] == {"llm": {"key": _FC_SECRET}}
    assert len(data["revision"]) == 12
    assert "control_plane" not in data["values"] and "ci" not in data["values"]


def test_applied_revision_shows_in_status(fc_server):
    port, _, store = fc_server
    rev = store.current().revision
    status, _, _ = _fc_req(port, "POST", "/api/v1/fleet/config/applied",
                           {"X-Host-Token": "placeholder-host-bender"}, {"host_id": "bender", "revision": rev})
    assert status == 200
    # token of another host cannot report for bender
    status, _, _ = _fc_req(port, "POST", "/api/v1/fleet/config/applied",
                           {"X-Host-Token": "placeholder-host-beast"}, {"host_id": "bender", "revision": "x"})
    assert status == 401
    status, _, body = _fc_req(port, "GET", "/api/v1/fleet/config/status", {"X-API-Key": "viewkey"})
    assert status == 200
    data = json.loads(body)
    assert data["revision"] == rev and data["last_error"] is None
    hosts = {h["id"]: h for h in data["hosts"]}
    assert hosts["bender"]["applied_revision"] == rev
    assert hosts["bender"]["roles"] == ["builder"]
    assert hosts["beast"]["applied_revision"] is None
    # status needs the normal API key
    assert _fc_req(port, "GET", "/api/v1/fleet/config/status")[0] == 401
    assert _fc_req(port, "GET", "/api/v1/fleet/config/status", {"X-Host-Token": "placeholder-host-bender"})[0] == 401


def test_broken_edit_keeps_last_good_revision(fc_server):
    port, path, store = fc_server
    good = store.current().revision
    path.write_text("version: 1\nsections: [broken")
    os.chmod(path, 0o600)
    os.utime(path, (os.stat(path).st_atime + 5, os.stat(path).st_mtime + 5))
    status, _, body = _fc_req(port, "GET", "/api/v1/fleet/config", {"X-Host-Token": "placeholder-host-bender"})
    assert status == 200
    assert json.loads(body)["revision"] == good
    _, _, sbody = _fc_req(port, "GET", "/api/v1/fleet/config/status", {"X-API-Key": "viewkey"})
    s = json.loads(sbody)
    assert s["revision"] == good and s["last_error"]
    # a valid edit afterwards is picked up and clears the error
    _fc_write(path, _fc_doc("-v2"))
    os.utime(path, (os.stat(path).st_atime + 10, os.stat(path).st_mtime + 10))
    _, _, body = _fc_req(port, "GET", "/api/v1/fleet/config", {"X-Host-Token": "placeholder-host-bender"})
    assert json.loads(body)["revision"] != good
    assert json.loads(_fc_req(port, "GET", "/api/v1/fleet/config/status", {"X-API-Key": "viewkey"})[2])["last_error"] is None


def test_status_never_contains_secret_values(fc_server):
    port, path, _ = fc_server
    path.write_text(f"version: 1\nsections: [{_FC_SECRET}")
    os.chmod(path, 0o600)
    os.utime(path, (os.stat(path).st_atime + 5, os.stat(path).st_mtime + 5))
    _, _, body = _fc_req(port, "GET", "/api/v1/fleet/config/status", {"X-API-Key": "opkey"})
    text = body.decode()
    for needle in (_FC_SECRET, "placeholder-ci-only", "postgres://", _fc_sha("placeholder-host-bender"), "placeholder-host-bender"):
        assert needle not in text


def test_fleet_config_routes_404_without_store(server):
    c, _ = server
    assert c.get("/api/v1/fleet/config")[0] == 404
    assert c.get("/api/v1/fleet/config/status")[0] == 404


@pytest.fixture()
def keyed_server(acknowledged_sink):
    svr = ControlPlaneServer(ControlPlaneRepo(sink=acknowledged_sink),
                             api_keys={"opkey": "operator", "ingkey": "ingest", "agentkey": "agent"})
    threading.Thread(target=svr.serve, daemon=True).start()
    svr.wait_until_ready()
    yield svr.port
    svr.shutdown()


def _post_as(port, path, body, key):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json", "X-API-Key": key})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def test_agent_role_may_post_ingest_and_host_tool_routes(keyed_server):
    port = keyed_server
    ev = {"entity_type": "host", "entity_id": "bender", "event_type": "host.registered",
          "payload": {"host_name": "bender", "tailnet_ip": "100.104.39.6"}}
    assert _post_as(port, "/api/v1/ingest", ev, "agentkey") == 200
    # reaches the handler (not 401/403): the body is invalid or the entity absent
    for path in ("/api/v1/llm/keys", "/api/v1/llm/keys/k1/revoke", "/api/v1/reviews",
                 "/api/v1/reviews/r1/result", "/api/v1/reviews/r1/heartbeat",
                 "/api/v1/reviews/r1/failure"):
        assert _post_as(port, path, {}, "agentkey") not in (401, 403), path


def test_agent_role_is_refused_everywhere_else(keyed_server):
    port = keyed_server
    for path in ("/api/v1/services/retire", "/api/v1/llm/models", "/api/v1/llm/models/m/delete",
                 "/api/v1/sessions/s1/messages", "/api/v1/sessions/s1/actions",
                 "/api/v1/hosts/h1/telemetry", "/api/v1/reviews/r1/other"):
        assert _post_as(port, path, {}, "agentkey") == 403, path
    # ingest stays ingest-only
    assert _post_as(port, "/api/v1/llm/keys", {}, "ingkey") == 403
    assert _post_as(port, "/api/v1/services/retire", {}, "opkey") != 403


def test_fleet_config_takes_one_snapshot_per_request(tmp_path, acknowledged_sink):
    """A reload between the token check and the slice (here: the host is revoked in
    the second snapshot) must not raise: the request answers from ONE snapshot."""
    from herdr_engineering.fleet_config import _parse
    first = _parse(_fc_doc())
    revoked = _fc_doc()
    del revoked["hosts"]["bender"]
    second = _parse(revoked)

    class FlippingStore:
        def __init__(self):
            self.calls = 0

        def current(self):
            self.calls += 1
            return first if self.calls == 1 else second

        def record_applied(self, *_a):
            pass

    store = FlippingStore()
    svr = ControlPlaneServer(ControlPlaneRepo(sink=acknowledged_sink), fleet_config=store)
    store.calls = 0  # construction reads the review_gate section once; count requests only
    threading.Thread(target=svr.serve, daemon=True).start()
    svr.wait_until_ready()
    try:
        status, _, body = _fc_req(svr.port, "GET", "/api/v1/fleet/config", {"X-Host-Token": "placeholder-host-bender"})
        assert status == 200 and json.loads(body)["revision"] == first.revision
        assert store.calls == 1
    finally:
        svr.shutdown()

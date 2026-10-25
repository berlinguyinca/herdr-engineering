import json
import threading
import urllib.error
import urllib.request

import pytest

from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.redact import redact_payload, redact_text
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from herdr_engineering.control_plane.retention import apply_retention


# ------------------------------------------------------------------ redaction
def test_redact_text_masks_common_secrets():
    t = "api_key=sk-abcdef123456 and password='hunter2' and token abc.def.ghi"
    out = redact_text(t)
    assert "sk-abcdef123456" not in out
    assert "hunter2" not in out
    assert "abc.def.ghi" not in out
    assert "[REDACTED]" in out


def test_redact_text_masks_extended_secret_forms():
    # connection strings for more schemes + OpenSSH blocks + client_secret
    t = (
        "dsn=postgresql://u:hunter2@db:5432/herdr "
        "client_secret=abc123def456 "
        "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----"
    )
    out = redact_text(t)
    assert "hunter2" not in out
    assert "abc123def456" not in out
    assert "BEGIN OPENSSH PRIVATE KEY" not in out  # whole block redacted
    assert "[REDACTED]" in out


def test_redact_payload_redacts_dsn_and_connection_keys():
    payload = {
        "name": "ok",
        "dsn": "postgresql://herdr:secretpw@postgres:5432/herdr",
        "connection_string": "mysql://root:rootpw@db:3306/x",
    }
    out = redact_payload(payload)
    assert out["dsn"] == "[REDACTED]"
    assert out["connection_string"] == "[REDACTED]"
    assert out["name"] == "ok"


def test_redact_payload_walks_nested_structures():
    payload = {
        "message": "use token sk-xyz",
        "env": {"API_KEY": "AKIA123456", "nested": {"pass": "s3cr3t"}},
        "keep": "safe text",
        "list": [{"secret": "Bearer eyJhbGciOi"}],
    }
    out = redact_payload(payload)
    assert "sk-xyz" not in json.dumps(out)
    assert "AKIA123456" not in json.dumps(out)
    assert "s3cr3t" not in json.dumps(out)
    assert "eyJhbGciOi" not in json.dumps(out)
    assert out["keep"] == "safe text"
    assert out["message"] != payload["message"]


# ---------------------------------------------------------------- retention
def test_retention_expires_old_telemetry_and_events():
    r = ControlPlaneRepo()
    hid = r.register_host("h", "10.0.0.1")
    r.record_telemetry(hid, {"cpu": 1})
    # backdate the sample beyond TTL
    r._telemetry[hid][0]["at"] = "2000-01-01T00:00:00+00:00"
    r.record_event("session", "s1", "OldEvent", {})
    r._events[("session", "s1")][0]["source_timestamp"] = "2000-01-01T00:00:00+00:00"
    r.create_mission("keep")
    removed = apply_retention(
        r, telemetry_ttl_hours=1, events_ttl_days=1, artifact_ttl_days=1,
        now=_iso_of("2030-01-01T00:00:00+00:00"))
    assert removed["telemetry"] == 1
    assert removed["events"] >= 1  # the backdated event (and any other pre-cutoff)
    assert r.list_telemetry(hid) == []
    # structured entity records are durable — never pruned by retention
    assert len(r.list_missions()) == 1


def _iso_of(s):
    import datetime as _dt
    return _dt.datetime.fromisoformat(s).isoformat()


# ------------------------------------------------------------- backup/restore
def test_export_import_roundtrip():
    r = ControlPlaneRepo()
    mid = r.create_mission("backup me")
    r.set_mission_stage(mid, "implementation")
    state = r.export_state()
    r2 = ControlPlaneRepo()
    r2.import_state(state)
    assert r2.get_mission(mid)["stage"] == "implementation"
    assert r2.get_mission(mid)["mission_id"] == mid


def test_export_is_json_serializable():
    r = ControlPlaneRepo()
    r.create_mission("a")
    json.dumps(r.export_state())  # must not raise


# ------------------------------------------------------------- auth roles
def test_viewer_cannot_mutate(acknowledged_sink):
    repo = ControlPlaneRepo(sink=acknowledged_sink)
    svr = ControlPlaneServer(repo, api_keys={"op-1": "operator", "view-1": "viewer"},
                             dispatcher=lambda *args: {"status": "delivered"})
    thread = threading.Thread(target=svr.serve, daemon=True)
    thread.start()
    svr.wait_until_ready()
    base = f"http://127.0.0.1:{svr.port}"
    try:
        sid = repo.create_session()
        # viewer GET is fine
        req = urllib.request.Request(f"{base}/api/v1/health",
                                     headers={"X-API-Key": "view-1"})
        with urllib.request.urlopen(req) as resp:
            assert resp.status == 200
        # viewer POST is forbidden
        req = urllib.request.Request(
            f"{base}/api/v1/sessions/{sid}/messages",
            data=json.dumps({"message": "x"}).encode(),
            headers={"Content-Type": "application/json", "X-API-Key": "view-1"},
            method="POST")
        with pytest.raises(urllib.error.HTTPError) as ei:
            urllib.request.urlopen(req)
        assert ei.value.code == 403
        # operator POST is allowed
        req = urllib.request.Request(
            f"{base}/api/v1/sessions/{sid}/messages",
            data=json.dumps({"message": "x"}).encode(),
            headers={"Content-Type": "application/json", "X-API-Key": "op-1"},
            method="POST")
        with urllib.request.urlopen(req) as resp:
            assert resp.status == 200
    finally:
        svr.shutdown()


def test_unknown_key_unauthorized():
    repo = ControlPlaneRepo()
    svr = ControlPlaneServer(repo, api_keys={"op-1": "operator"})
    thread = threading.Thread(target=svr.serve, daemon=True)
    thread.start()
    svr.wait_until_ready()
    base = f"http://127.0.0.1:{svr.port}"
    try:
        req = urllib.request.Request(f"{base}/api/v1/missions",
                                     headers={"X-API-Key": "wrong"})
        with pytest.raises(urllib.error.HTTPError) as ei:
            urllib.request.urlopen(req)
        assert ei.value.code == 401
    finally:
        svr.shutdown()


def test_fail_closed_on_non_loopback_without_keys():
    repo = ControlPlaneRepo()
    # an unauthenticated server must never bind a non-loopback address
    with pytest.raises(ValueError, match="non-loopback|HERDR_CP_API_KEYS|fail-closed"):
        ControlPlaneServer(repo, host="0.0.0.0")
    with pytest.raises(ValueError):
        ControlPlaneServer(repo, host="10.0.0.5")
    # loopback is still allowed open (private-by-default local dev)
    assert ControlPlaneServer(repo, host="127.0.0.1") is not None


def test_allow_open_opts_into_tailnet_trust_boundary():
    repo = ControlPlaneRepo()
    # --allow-open permits an unauthenticated server on a non-loopback bind
    svr = ControlPlaneServer(repo, host="0.0.0.0", allow_open=True)
    thread = threading.Thread(target=svr.serve, daemon=True)
    thread.start()
    svr.wait_until_ready()
    base = f"http://127.0.0.1:{svr.port}"
    try:
        # auth-free read succeeds (Tailnet is the trust boundary)
        req = urllib.request.Request(f"{base}/api/v1/health")
        body = urllib.request.urlopen(req).read().decode()
        assert '"status": "ok"' in body
    finally:
        svr.shutdown()
    # but it is NOT the default — non-loopback without allow_open still fails
    with pytest.raises(ValueError):
        ControlPlaneServer(repo, host="0.0.0.0")


def test_loopback_host_detection():
    from herdr_engineering.control_plane.api import _is_loopback
    assert _is_loopback("127.0.0.1")
    assert _is_loopback("localhost")
    assert _is_loopback("::1")
    assert not _is_loopback("0.0.0.0")
    assert not _is_loopback("10.0.0.5")
    assert not _is_loopback("100.104.39.6")


# -------------------------------------------------------- rate limiting
def test_rate_limiter_returns_429_when_burst_exceeds():
    from herdr_engineering.control_plane.api import _RateLimiter
    rl = _RateLimiter(max_requests=2, window_seconds=60)
    assert rl.allow("ip")
    assert rl.allow("ip")
    assert not rl.allow("ip")   # third in the window is rejected
    assert rl.allow("other-ip")  # a different client is unaffected


def test_rate_limit_api_returns_429(monkeypatch):
    from herdr_engineering.control_plane.api import _RateLimiter
    repo = ControlPlaneRepo()
    svr = ControlPlaneServer(repo)
    # shrink the limiter so a small burst trips it
    svr._limiter = _RateLimiter(max_requests=3, window_seconds=60)
    thread = threading.Thread(target=svr.serve, daemon=True)
    thread.start()
    svr.wait_until_ready()
    base = f"http://127.0.0.1:{svr.port}"
    try:
        codes = []
        for _ in range(5):
            try:
                with urllib.request.urlopen(f"{base}/api/v1/health") as resp:
                    codes.append(resp.status)
            except urllib.error.HTTPError as e:
                codes.append(e.code)
        assert codes.count(429) >= 1
        assert codes.count(200) >= 1
    finally:
        svr.shutdown()


# ------------------------------------------------- postgres-source health
def test_health_reports_postgres_when_source_present():
    class _EmptySource:
        def missions(self): return []
        def sessions(self): return []
        def services(self): return []
        def hosts(self): return []
        def events(self): return []
    repo = ControlPlaneRepo()
    svr = ControlPlaneServer(repo, source=_EmptySource())
    h = svr._health()
    assert h["source"] == "postgres"
    assert h["version"] == "0.1.0"


@pytest.fixture()
def rate_limited_server(acknowledged_sink):
    from herdr_engineering.control_plane.api import _RateLimiter
    repo = ControlPlaneRepo(sink=acknowledged_sink)
    server = ControlPlaneServer(repo)
    server._limiter = _RateLimiter(max_requests=2, window_seconds=60)
    server._ingest_limiter = _RateLimiter(max_requests=2, window_seconds=60)
    thread = threading.Thread(target=server.serve, daemon=True)
    thread.start()
    server.wait_until_ready()
    yield server, repo
    server.shutdown()


def _rate_request(server, path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(f'http://127.0.0.1:{server.port}{path}', data=data,
                                     headers={'Content-Type': 'application/json'})
    try:
        response = urllib.request.urlopen(request, timeout=5)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        return response.code, response.headers, response.read()


def _rate_service(index):
    return {'entity_type': 'service', 'entity_id': f'web-{index}',
            'event_type': 'service.heartbeat',
            'payload': {'service_name': f'web-{index}', 'host_id': 'host',
                        'port': 8000 + index}}


def test_rate_rejected_ingest_does_not_mutate_or_emit_events(rate_limited_server):
    server, repo = rate_limited_server
    for index in range(2):
        assert _rate_request(server, '/api/v1/ingest', _rate_service(index))[0] == 200
    status, headers, body = _rate_request(server, '/api/v1/ingest', _rate_service(2))
    assert status == 429
    assert headers['Retry-After'] == '60'
    assert json.loads(body) == {'error': 'too many requests'}
    # Shutdown joins request handlers before checking the lack of side effects.
    server.shutdown()
    assert len(repo.list_services()) == 2
    assert len(repo.recent_events(limit=100)) == 2


def test_rate_ingest_burst_does_not_consume_health_or_ui_budget(rate_limited_server):
    server, _ = rate_limited_server
    for index in range(3):
        assert _rate_request(server, '/api/v1/ingest', _rate_service(index))[0] == (
            200 if index < 2 else 429)
    assert _rate_request(server, '/api/v1/health')[0] == 200
    assert _rate_request(server, '/api/v1/services')[0] == 200
    assert _rate_request(server, '/api/v1/health')[0] == 429


def test_rate_ui_burst_does_not_consume_ingest_budget(rate_limited_server):
    server, _ = rate_limited_server
    assert _rate_request(server, '/api/v1/health')[0] == 200
    assert _rate_request(server, '/api/v1/services')[0] == 200
    assert _rate_request(server, '/api/v1/health')[0] == 429
    assert _rate_request(server, '/api/v1/ingest', _rate_service(0))[0] == 200


def test_redact_payload_keeps_author_logins_but_still_redacts_auth_keys():
    """"author" contains the hint "auth": the PR author's login is not a secret and the review quorum needs it."""
    out = redact_payload({"author": "octocat", "authors": ["a"], "auth": "x", "auth_token": "y", "Authorization": "z"})
    assert out["author"] == "octocat" and out["authors"] == ["a"]
    assert out["auth"] == out["auth_token"] == out["Authorization"] == "[REDACTED]"


def test_redact_delivery_preserves_counts_and_urls_but_scans_values():
    from herdr_engineering.control_plane.redact import redact_delivery
    # Delivery records must keep numeric counts, urls and status text, while a
    # leaked secret anywhere in a string value is redacted.
    rec = {
        "test_run_id": "tr1", "passed": 4, "failed": 1, "total": 5,
        "url": "https://example.com/pr/1", "status": "failed",
        "results": [{"summary": "runner leaked token abc123xyz789"}],
        "api_key": "sk-abcDEF123456",
    }
    out = redact_delivery(rec)
    assert out["passed"] == 4 and out["failed"] == 1 and out["total"] == 5
    assert out["url"] == "https://example.com/pr/1" and out["status"] == "failed"
    assert out["test_run_id"] == "tr1"
    assert "abc123xyz789" not in out["results"][0]["summary"]
    # a key whose NAME hints at a secret is still redacted wholesale
    assert out["api_key"] == "[REDACTED]"


def test_redact_delivery_redacts_a_secret_in_a_passed_value():
    from herdr_engineering.control_plane.redact import redact_delivery
    # The 'passed' key must not be blanked merely because it contains 'pass',
    # but a secret string sitting in that value is still scanned and redacted.
    out = redact_delivery({"passed": "sk-abcDEF123456"})
    assert "abcDEF123456" not in out["passed"]


def test_redact_does_not_eat_ids_that_merely_end_in_sk_dash_but_still_redacts_real_keys():
    assert redact_text("task-s6lease") == "task-s6lease"
    assert redact_text("disk-image-1 and risk-profile-high") == "disk-image-1 and risk-profile-high"
    assert redact_payload({"task_id": "task-s6lease"})["task_id"] == "task-s6lease"
    for leaked in ("sk-abcDEF123456", "key=sk-abcDEF123456", "OPENAI_API_KEY_sk-abcDEF123456", '"sk-proj-abcDEF123456"', "(sk-abc123)"):
        assert "abc" not in redact_text(leaked).replace("[REDACTED]", ""), leaked


def test_redact_text_catches_a_key_after_an_ansi_colour_code_or_a_url_encoded_equals_but_not_short_ids():
    key = "sk-" + "A" * 40
    for leaked in ("\x1b[32m" + key, "api_key%3D" + key, "\x1b[1;31m" + key + "\x1b[0m", "x=" + key):
        out = redact_text(leaked)
        assert "AAAA" not in out and "[REDACTED]" in out, leaked
    assert redact_text("task-sk-ab") == "task-sk-ab"
    assert redact_text("\x1b[32mtask-sk-ab\x1b[0m") == "\x1b[32mtask-sk-ab\x1b[0m"
    assert redact_payload({"task_id": "task-sk-ab"})["task_id"] == "task-sk-ab"

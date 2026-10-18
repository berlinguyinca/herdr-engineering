"""Adversarial HTTP-level security tests for the control plane (GitHub #292).

End-to-end checks against the real ``ControlPlaneServer`` (stdlib HTTP
server) with role-scoped API keys:

* RBAC: missing/wrong key -> 401, viewer is read-only (403), ingest is
  ingest-only (403 elsewhere), operator can act.
* Input validation: spoofed ``source`` values and colon-bearing mission ids
  are rejected before they touch the repo (400, state unchanged).
* Source isolation: a pi-sourced session never attaches to a herdr mission
  that shares the raw id.
* Secret redaction: payloads with secret-shaped values/keys are redacted in
  the activity feed; the known gaps (``/api/v1/events`` stream not redacted,
  mission ``title`` field not passed through the redactor) are pinned with
  xfail/leak-pinning tests so a future fix is caught.

Tests only -- no product code is exercised beyond the public HTTP API and the
repo it fronts.
"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.repo import ControlPlaneRepo

# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------

API_KEYS = {
    "viewer-key": "viewer",
    "op-key": "operator",
    "ingest-key": "ingest",
}


@pytest.fixture
def server():
    """ControlPlaneServer on 127.0.0.1:0 with role-scoped API keys."""
    repo = ControlPlaneRepo()
    srv = ControlPlaneServer(repo, host="127.0.0.1", port=0,
                             api_keys=API_KEYS)
    t = threading.Thread(target=srv.serve, daemon=True)
    t.start()
    srv.wait_until_ready()
    yield f"http://127.0.0.1:{srv.port}", repo
    srv.shutdown()
    t.join(timeout=5)


def _request(base_url, method, path, body=None, key=None):
    """Issue one HTTP request.

    Returns ``(status, parsed_json_or_None, raw_bytes)``. ``parsed_json_or_None``
    is ``None`` when the response body is not a JSON object. Request bodies
    are kept small: the server closes denied connections without reading them.
    """
    url = f"{base_url}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    if key is not None:
        req.add_header("X-API-Key", key)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        raw = e.read()
        status = e.code
    else:
        status = resp.status
    try:
        parsed = json.loads(raw.decode())
    except (UnicodeDecodeError, json.JSONDecodeError):
        parsed = None
    return status, parsed, raw


def _get(base_url, path, key=None):
    return _request(base_url, "GET", path, key=key)


def _post(base_url, path, body=None, key=None):
    return _request(base_url, "POST", path, body=body, key=key)


# ---------------------------------------------------------------------------
# (1) missing / wrong key -> 401 unauthorized
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/api/v1/missions"),
        ("GET", "/api/v1/health"),
        ("GET", "/"),
        ("GET", "/api/v1/events?entity_type=x&entity_id=y"),
        ("POST", "/api/v1/ingest"),
        ("POST", "/api/v1/sessions/{sid}/actions"),
    ],
)
def test_missing_key_is_401(server, method, path):
    base_url, repo = server
    path = path.replace("{sid}", repo.create_session())
    body = None
    if method == "POST":
        body = (
            {"action": "stop"}
            if "actions" in path
            else {"entity_type": "host", "entity_id": "h1",
                  "event_type": "heartbeat", "payload": {"name": "h1"}}
        )
    status, parsed, _ = _request(base_url, method, path, body=body)
    assert status == 401
    assert parsed == {"error": "unauthorized"}


def test_wrong_key_is_401(server):
    base_url, _ = server
    status, parsed, _ = _get(base_url, "/api/v1/missions",
                             key="viewer-keyX")
    assert status == 401
    assert parsed == {"error": "unauthorized"}


# ---------------------------------------------------------------------------
# (2) viewer: reads yes, writes no (403 read-only)
# ---------------------------------------------------------------------------

def test_viewer_can_list_missions(server):
    base_url, repo = server
    repo.upsert_mission("M-VIEW", title="t")
    status, parsed, _ = _get(base_url, "/api/v1/missions", key="viewer-key")
    assert status == 200
    assert any(m["mission_id"] == "M-VIEW" for m in parsed["missions"])


def test_viewer_ingest_is_read_only(server):
    base_url, repo = server
    body = {"entity_type": "host", "entity_id": "h1",
            "event_type": "heartbeat", "payload": {"name": "h1"}}
    status, parsed, _ = _post(base_url, "/api/v1/ingest", body=body,
                              key="viewer-key")
    assert status == 403
    assert parsed == {"error": "read-only"}
    assert repo.list_hosts() == []


def test_viewer_session_action_is_read_only(server):
    base_url, repo = server
    sid = repo.create_session()
    status, parsed, _ = _post(base_url, f"/api/v1/sessions/{sid}/actions",
                              body={"action": "stop"}, key="viewer-key")
    assert status == 403
    assert parsed == {"error": "read-only"}


# ---------------------------------------------------------------------------
# (3) ingest key: ingest yes, everything else operator-gated -> 403
# ---------------------------------------------------------------------------

def test_ingest_key_can_read_missions(server):
    base_url, _ = server
    status, parsed, _ = _get(base_url, "/api/v1/missions", key="ingest-key")
    assert status == 200
    assert isinstance(parsed, dict) and "missions" in parsed


def test_ingest_key_can_ingest(server):
    base_url, repo = server
    body = {"entity_type": "host", "entity_id": "h-ing",
            "event_type": "heartbeat", "payload": {"name": "h-ing"}}
    status, parsed, _ = _post(base_url, "/api/v1/ingest", body=body,
                              key="ingest-key")
    assert status == 200
    assert parsed["entity"]["host_id"] == "h-ing"
    assert [h["host_id"] for h in repo.list_hosts()] == ["h-ing"]


def test_ingest_key_cannot_run_session_action(server):
    base_url, repo = server
    sid = repo.create_session()
    status, parsed, _ = _post(base_url, f"/api/v1/sessions/{sid}/actions",
                              body={"action": "stop"}, key="ingest-key")
    assert status == 403
    assert parsed == {"error": "read-only"}


def test_ingest_key_cannot_create_llm_key(server):
    base_url, _ = server
    status, parsed, _ = _post(base_url, "/api/v1/llm/keys",
                              body={"project": "p1"}, key="ingest-key")
    assert status == 403
    assert parsed == {"error": "read-only"}


# ---------------------------------------------------------------------------
# (4) operator: full write access
# ---------------------------------------------------------------------------

def test_operator_can_run_session_action(server):
    base_url, repo = server
    sid = repo.create_session()
    status, parsed, _ = _post(base_url, f"/api/v1/sessions/{sid}/actions",
                              body={"action": "stop"}, key="op-key")
    assert status == 501  # authorized, but native lifecycle command is unsupported
    assert parsed["error"] == "unsupported_action"
    assert repo.get_session(sid)["status"] == "active"


def test_operator_can_send_message(server):
    base_url, repo = server
    sid = repo.create_session()
    status, parsed, _ = _post(base_url, f"/api/v1/sessions/{sid}/messages",
                              body={"message": "hello"}, key="op-key")
    assert status == 502  # authorized, but this synthetic record has no native identity
    assert parsed["status"] == "failed"
    assert repo.get_session(sid)["messages"] == 0


def test_operator_can_ingest(server):
    base_url, repo = server
    body = {"entity_type": "host", "entity_id": "h-op",
            "event_type": "heartbeat", "payload": {"name": "h-op"}}
    status, parsed, _ = _post(base_url, "/api/v1/ingest", body=body,
                              key="op-key")
    assert status == 200
    assert parsed["entity"]["host_id"] == "h-op"
    assert [h["host_id"] for h in repo.list_hosts()] == ["h-op"]


# ---------------------------------------------------------------------------
# (5) spoofed sources are rejected with 400
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "index,source",
    list(enumerate(["Pi:Evil", "pi/evil", "1pi", "a" * 33, 123])),
)
def test_ingest_spoofed_source_rejected(server, index, source):
    base_url, repo = server
    body = {"entity_type": "host", "entity_id": f"h-{index}",
            "event_type": "heartbeat", "payload": {"name": "x"},
            "source": source}
    status, parsed, _ = _post(base_url, "/api/v1/ingest", body=body,
                              key="ingest-key")
    assert status == 400
    assert "invalid source" in parsed["error"].lower()
    # Nothing reached the repo.
    assert repo.list_hosts() == []
    assert repo.recent_events(limit=50) == []


# ---------------------------------------------------------------------------
# (6) colon-bearing mission entity ids are rejected, repo unchanged
# ---------------------------------------------------------------------------

def test_ingest_namespaced_mission_id_rejected(server):
    base_url, repo = server
    pre = repo.upsert_mission("MSN-1", title="original")
    body = {"entity_type": "mission", "entity_id": "pi:MSN-1",
            "event_type": "start", "payload": {"title": "spoofed"}}
    status, parsed, _ = _post(base_url, "/api/v1/ingest", body=body,
                              key="ingest-key")
    assert status == 400
    assert parsed["error"] == "invalid mission id"
    after = repo.get_mission("MSN-1")
    assert after["title"] == pre["title"] == "original"
    assert after["source"] == "herdr"
    assert after["sessions"] == []
    assert repo.get_mission("pi:MSN-1") is None


# ---------------------------------------------------------------------------
# (7) source isolation: pi session does not attach to the herdr mission
# ---------------------------------------------------------------------------

def test_pi_session_does_not_attach_to_herdr_mission(server):
    base_url, _ = server
    status, _, _ = _post(base_url, "/api/v1/ingest", key="ingest-key", body={
        "entity_type": "mission", "entity_id": "MSN-1",
        "event_type": "start", "payload": {"title": "m1"},
    })
    assert status == 200
    status, _, _ = _post(base_url, "/api/v1/ingest", key="ingest-key", body={
        "entity_type": "session", "entity_id": "s1",
        "event_type": "start",
        "payload": {"mission_id": "MSN-1"},
        "source": "pi",
    })
    assert status == 200
    status, parsed, _ = _get(base_url, "/api/v1/missions/MSN-1",
                             key="viewer-key")
    assert status == 200
    assert parsed["sessions"] == []


# ---------------------------------------------------------------------------
# (8) payload redaction on events and activity
# ---------------------------------------------------------------------------

_SECRET_PAYLOAD = {
    "api_key": "sk-live-xyz",
    "Authorization": "Bearer abc",
    "secret": "p",
    "msg": "Bearer abc",
}


def test_ingested_secret_payload_redacted_on_activity_and_events(server):
    base_url, _ = server
    status, _, _ = _post(base_url, "/api/v1/ingest", key="ingest-key", body={
        "entity_type": "note", "entity_id": "n1",
        "event_type": "note.created", "payload": _SECRET_PAYLOAD,
    })
    assert status == 200

    # The activity feed is redacted.
    status, parsed, raw = _get(base_url, "/api/v1/activity",
                               key="viewer-key")
    assert status == 200
    note_events = [e for e in parsed["events"]
                   if e["entity_type"] == "note" and e["entity_id"] == "n1"]
    assert len(note_events) == 1
    payload = note_events[0]["payload"]
    assert payload["api_key"] == "[REDACTED]"
    assert payload["Authorization"] == "[REDACTED]"
    assert payload["secret"] == "[REDACTED]"
    assert payload["msg"] == "[REDACTED]"

    raw_text = raw.decode()
    assert "sk-live-xyz" not in raw_text
    assert "Bearer abc" not in raw_text

    # The per-entity /api/v1/events stream is redacted too.
    status, parsed, raw = _get(
        base_url, "/api/v1/events?entity_type=note&entity_id=n1",
        key="viewer-key")
    assert status == 200
    events = [e for e in parsed["events"]
              if e["entity_type"] == "note" and e["entity_id"] == "n1"]
    assert len(events) == 1
    payload = events[0]["payload"]
    assert payload["api_key"] == "[REDACTED]"
    assert payload["Authorization"] == "[REDACTED]"
    assert payload["secret"] == "[REDACTED]"
    assert "abc" not in str(payload["msg"])

    raw_text = raw.decode()
    assert "sk-live-xyz" not in raw_text
    assert "Bearer abc" not in raw_text


# ---------------------------------------------------------------------------
# (9) mission titles carrying secrets are redacted at the ingest boundary.
# The ingest route runs ``redact_payload`` on the whole envelope before
# storing, so a secret-shaped mission title never reaches the repo and the
# mission record + mission event stream both expose the redacted form.
# ---------------------------------------------------------------------------

def test_mission_title_secret_round_trip(server):
    base_url, _ = server
    status, _, _ = _post(base_url, "/api/v1/ingest", key="ingest-key", body={
        "entity_type": "mission", "entity_id": "M1",
        "event_type": "mission.started",
        "payload": {"title": "deploy sk-live-xyz", "stage": "planning"},
    })
    assert status == 200

    # The mission record must be redacted at the ingest boundary.
    status, parsed, raw = _get(base_url, "/api/v1/missions/M1",
                               key="viewer-key")
    assert status == 200
    assert parsed["title"] == "deploy [REDACTED]"
    raw_text = raw.decode()
    assert "sk-live-xyz" not in raw_text
    assert "sk-live" not in raw_text

    # The mission event stream must be redacted too.
    status, parsed, raw = _get(
        base_url, "/api/v1/events?entity_type=mission&entity_id=M1",
        key="viewer-key")
    assert status == 200
    events = parsed["events"]
    assert len(events) == 1
    assert events[0]["payload"]["title"] == "deploy [REDACTED]"
    raw_text = raw.decode()
    assert "sk-live-xyz" not in raw_text
    assert "sk-live" not in raw_text


# ---------------------------------------------------------------------------
# (10) known gap: /api/v1/events (r.stream) is not redacted, unlike /activity
# ---------------------------------------------------------------------------

@pytest.mark.xfail(strict=True, reason=(
    "api.py returns r.stream() on /api/v1/events without _redact_event "
    "(unlike /activity and SSE); events written directly to the repo "
    "leak secrets"))
def test_events_stream_redaction_gap(server):
    base_url, repo = server
    repo.record_event("note", "n2", "x", {"api_key": "sk-live-xyz"})
    status, parsed, _ = _get(
        base_url, "/api/v1/events?entity_type=note&entity_id=n2",
        key="viewer-key")
    assert status == 200
    assert parsed["events"][0]["payload"]["api_key"] == "[REDACTED]"


# ---------------------------------------------------------------------------
# (11) raw HTML in a title is returned verbatim as JSON (no server-side
# escaping) -- XSS is a UI concern handled by esc() in packages/app.js.
# ---------------------------------------------------------------------------

def test_mission_title_raw_html_returned_as_json(server):
    base_url, repo = server
    repo.upsert_mission("M-HTML", title="<script>alert(1)</script>")
    status, parsed, _ = _get(base_url, "/api/v1/missions/M-HTML",
                             key="viewer-key")
    assert status == 200
    assert parsed["title"] == "<script>alert(1)</script>"
    # The wire content type is JSON, not HTML -- the client (esc()) is
    # responsible for safe rendering.
    resp = urllib.request.urlopen(
        urllib.request.Request(
            f"{base_url}/api/v1/missions/M-HTML",
            headers={"X-API-Key": "viewer-key"},
        ),
        timeout=5,
    )
    assert resp.headers.get("Content-Type") == "application/json"
    body = json.loads(resp.read().decode())
    assert body["title"] == "<script>alert(1)</script>"

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
def test_viewer_cannot_mutate():
    repo = ControlPlaneRepo()
    svr = ControlPlaneServer(repo, api_keys={"op-1": "operator", "view-1": "viewer"})
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
        req = urllib.request.Request(f"{base}/api/v1/health",
                                     headers={"X-API-Key": "wrong"})
        with pytest.raises(urllib.error.HTTPError) as ei:
            urllib.request.urlopen(req)
        assert ei.value.code == 401
    finally:
        svr.shutdown()

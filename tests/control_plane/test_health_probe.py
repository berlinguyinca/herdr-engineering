"""The cheap /api/v1/health?probe=1 shape: O(1) liveness+capability for the deploy
gate and the dashboard nav probe, without the heavy repo/inspector/metrics payload."""
import json
import threading
import urllib.error
import urllib.request

from herdr_engineering.control_plane.api import ControlPlaneServer, _COMMIT
from herdr_engineering.control_plane.repo import ControlPlaneRepo


def _srv(**kw):
    repo = ControlPlaneRepo()
    srv = ControlPlaneServer(repo, **kw)
    threading.Thread(target=srv.serve, daemon=True).start()
    srv.wait_until_ready()
    return srv


def _get(srv, path, key=None):
    headers = {"Content-Type": "application/json"}
    if key is not None:
        headers["X-API-Key"] = key
    req = urllib.request.Request(f"http://127.0.0.1:{srv.port}{path}", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


def test_probe_is_lean_and_gate_compatible():
    srv = _srv(api_keys={"vk": "viewer", "ok": "operator"})
    try:
        # No key at all (the deploy.sh posture): probe answers, cheap shape.
        st, body = _get(srv, "/api/v1/health?probe=1")
        assert st == 200
        assert set(body) == {"status", "version", "commit", "capabilities"}
        assert body["status"] == "ok"
        assert body["commit"] == _COMMIT            # deploy gate: status + commit present
        assert isinstance(body["capabilities"], list)
        # The heavy payload is deliberately absent from the probe.
        for heavy in ("performance", "inspector", "events", "missions", "sessions",
                      "hosts", "services", "by_route", "recent_requests"):
            assert heavy not in body, heavy
    finally:
        srv.shutdown()


def test_probe_lean_even_with_a_key():
    srv = _srv(api_keys={"vk": "viewer"})
    try:
        st, body = _get(srv, "/api/v1/health?probe=1", key="vk")
        assert st == 200 and set(body) == {"status", "version", "commit", "capabilities"}
    finally:
        srv.shutdown()


def test_default_health_contract_unchanged():
    # The default (keyed) health keeps its richer shape: repo counters, inspector
    # detail and the request-metrics snapshot. The probe must not strip these.
    srv = _srv(api_keys={"vk": "viewer"})
    try:
        st, body = _get(srv, "/api/v1/health", key="vk")
        assert st == 200
        assert "events" in body and "inspector" in body and "performance" in body
        assert set(body["performance"]) >= {"requests", "by_route", "recent_requests"}
    finally:
        srv.shutdown()


def test_probe_works_keyless_open_mode():
    # Keyless/open deployments (no api_keys) previously fell through to the heavy
    # health; the probe must serve them cheaply too.
    srv = _srv(allow_open=True)
    try:
        st, body = _get(srv, "/api/v1/health?probe=1")
        assert st == 200 and set(body) == {"status", "version", "commit", "capabilities"}
        assert body["commit"] == _COMMIT
    finally:
        srv.shutdown()

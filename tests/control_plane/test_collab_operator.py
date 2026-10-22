"""Operator (local owner) access: no sign-in on the tailnet-only listener, proxied by the dashboard."""
from __future__ import annotations

import http.client
import json
import threading

import pytest

from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.collab import (
    CollabService,
    FileEventLog,
    StaticTokenAuthenticator,
)
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from tests.control_plane.test_collab import TOKENS, _digest

HOST = "100.64.0.9:8082"


def call(port, method, path, body=None, *, host=HOST, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    h = {"Host": host, **(headers or {})}
    data = None
    if body is not None:
        data = json.dumps(body)
        h.setdefault("Content-Type", "application/json")
    conn.request(method, path, body=data, headers=h)
    resp = conn.getresponse()
    raw = resp.read()
    conn.close()
    try:
        return resp.status, json.loads(raw)
    except ValueError:
        return resp.status, None


def free_port():
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start(srv):
    t = threading.Thread(target=srv.serve, daemon=True)
    t.start()
    srv.wait_until_ready()
    return t


@pytest.fixture
def stack(tmp_path):
    svc = CollabService(StaticTokenAuthenticator({u: _digest(t) for u, t in TOKENS.items()}),
                        sink=FileEventLog(str(tmp_path / "log")), public_config={"public_url": "https://x/"})
    port = free_port()
    only = ControlPlaneServer(ControlPlaneRepo(), host="127.0.0.1", port=0, collab=svc, allow_open=True,
                              public_port=0, public_host="127.0.0.1", collab_only=True,
                              collab_require_https=True, operator_port=port, operator_user="alice",
                              operator_email="alice@example.org",
                              operator_hosts=[HOST, f"127.0.0.1:{port}"])
    t1 = start(only)
    dash = ControlPlaneServer(ControlPlaneRepo(), host="127.0.0.1", port=0, allow_open=True,
                              collab_operator_url=f"http://127.0.0.1:{only.operator_port}")
    t2 = start(dash)
    yield type("S", (), dict(only=only, dash=dash, svc=svc))
    dash.shutdown()
    only.shutdown()
    t1.join(timeout=5)
    t2.join(timeout=5)


def test_operator_listener_needs_no_signin_and_acts_as_the_operator(stack):
    p = stack.only.operator_port
    s, cfg = call(p, "GET", "/api/v1/collab/config")
    assert s == 200 and cfg["operator"] == {"user_id": "alice", "email": "alice@example.org"}
    s, proj = call(p, "POST", "/api/v1/collab/projects", {"name": "p"})
    assert s == 201
    s, room = call(p, "POST", f"/api/v1/collab/projects/{proj['project_id']}/rooms", {"name": "r"})
    assert s == 201
    s, rooms = call(p, "GET", "/api/v1/collab/rooms")
    assert [r["room_id"] for r in rooms["rooms"]] == [room["room_id"]]
    # the same identity the owner has when signing in on the public site
    s, mine = call(stack.only.public_port, "GET", "/api/v1/collab/rooms",
                   headers={"Authorization": f"Bearer {TOKENS['alice']}", "X-Forwarded-Proto": "https"})
    assert [r["room_id"] for r in mine["rooms"]] == [room["room_id"]]


def test_operator_listener_refuses_rebinding_other_paths_and_cross_site_writes(stack):
    p = stack.only.operator_port
    assert call(p, "GET", "/api/v1/collab/rooms", host="evil.example:8082")[0] == 404      # DNS rebinding
    assert call(p, "GET", "/api/v1/collab/rooms", host="localhost")[0] == 404
    for path in ("/", "/api/v1/hosts", "/mcp", "/collab-view.js", "/api/v1/collab/../hosts"):
        assert call(p, "GET", path)[0] == 404
    assert call(p, "POST", "/api/v1/collab/projects", {"name": "x"},
                headers={"Origin": "http://evil.example"})[0] == 403                       # cross-site write
    assert call(p, "POST", "/api/v1/collab/projects", {"name": "x"},
                headers={"Content-Type": "text/plain"})[0] == 415                          # simple-request CSRF


def test_operator_must_still_be_in_the_group(stack):
    stack.svc.auth.disable("alice")
    assert call(stack.only.operator_port, "GET", "/api/v1/collab/rooms")[0] == 403


def test_dashboard_proxies_collab_without_signin(stack):
    d = stack.dash.port
    s, proj = call(d, "POST", "/api/v1/collab/projects", {"name": "p"}, host=f"127.0.0.1:{d}")
    assert s == 201
    s, rooms = call(d, "GET", "/api/v1/collab/rooms", host=f"127.0.0.1:{d}")
    assert s == 200 and rooms == {"rooms": []}
    s, cfg = call(d, "GET", "/api/v1/collab/config", host=f"127.0.0.1:{d}")
    assert cfg["operator"]["email"] == "alice@example.org"


def test_dashboard_proxy_keeps_the_dashboards_own_access_control(stack, tmp_path):
    locked = ControlPlaneServer(ControlPlaneRepo(), host="127.0.0.1", port=0, api_keys={"k": "operator"},
                                collab_operator_url=f"http://127.0.0.1:{stack.only.operator_port}")
    t = start(locked)
    try:
        assert call(locked.port, "GET", "/api/v1/collab/rooms", host="x")[0] in (401, 403)
        assert call(locked.port, "GET", "/api/v1/collab/rooms", host="x",
                    headers={"X-API-Key": "k"})[0] == 200
    finally:
        locked.shutdown()
        t.join(timeout=5)


def test_dashboard_reports_unavailable_service_cleanly():
    dash = ControlPlaneServer(ControlPlaneRepo(), host="127.0.0.1", port=0, allow_open=True,
                              collab_operator_url="http://127.0.0.1:9")
    t = start(dash)
    try:
        s, body = call(dash.port, "GET", "/api/v1/collab/rooms", host="x")
        assert s == 502 and "unavailable" in body["error"]
    finally:
        dash.shutdown()
        t.join(timeout=5)


def test_operator_listener_requires_collab_only_mode_and_hosts():
    with pytest.raises(ValueError):
        ControlPlaneServer(ControlPlaneRepo(), host="127.0.0.1", port=0, operator_port=0,
                           operator_user="a", operator_hosts=["h"])

"""Same-origin app edge tests: path-routing forwarder (devfabric.OriginEdge).

Covers the routing core (path matching, request-line peek, target selection),
the lease data model (Route + routes on DevServiceLease, store round-trip,
backward compatibility), the forwarder dispatch, and a LIVE end-to-end check
that the edge really routes /api/* and / to different backends over loopback.
"""
import os
import socket
import sys
import threading
import uuid

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import pytest

from herdr_engineering.contracts import DevServiceLease, Route
from herdr_engineering.devfabric import (
    DevServiceForwarder,
    DevServiceRegistry,
    OriginEdge,
    _peek_request_path,
    forwarder_for,
    match_target,
    path_matches,
)


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _lease(routes=None, target_port=9000):
    return DevServiceLease(
        id=f"lease_{uuid.uuid4().hex[:16]}", external_port=_free_port(),
        protocol="http", target_machine_id="m", target_host="127.0.0.1",
        target_port=target_port, routes=routes)


# -- path matching ----------------------------------------------------------
def test_path_matches_prefix():
    assert path_matches("/api", "/api")
    assert path_matches("/api/health", "/api")
    assert path_matches("/api/health?x=1", "/api")
    assert not path_matches("/apix", "/api")   # sibling, not a child
    assert not path_matches("/apiary", "/api")
    assert path_matches("/anything", "/")


def test_match_target_longest_prefix_wins():
    routes = [Route("/api", "a", 1), Route("/api/v2", "b", 2), Route("/ui", "c", 3)]
    assert match_target("/api/v2/x", routes, "fb", 0) == ("b", 2)
    assert match_target("/api/v1/x", routes, "fb", 0) == ("a", 1)
    assert match_target("/ui", routes, "fb", 0) == ("c", 3)
    assert match_target("/other", routes, "fb", 0) == ("fb", 0)   # fallback
    assert match_target(None, routes, "fb", 0) == ("fb", 0)       # non-HTTP
    assert match_target("/api", None, "fb", 0) == ("fb", 0)       # no routes


# -- request-line peek ------------------------------------------------------
def test_peek_returns_path_and_buffer():
    a, b = socket.socketpair()
    try:
        b.sendall(b"GET /api/health?x=1 HTTP/1.1\r\nHost: dev\r\n\r\npayload")
        path, buf = _peek_request_path(a, timeout=2.0)
        assert path == "/api/health?x=1"
        assert buf.startswith(b"GET /api/health?x=1 HTTP/1.1\r\n")
    finally:
        a.close(); b.close()


def test_peek_non_http_returns_none():
    a, b = socket.socketpair()
    try:
        b.sendall(b"\x16\x03\x01\r\n")          # not an HTTP request line
        path, buf = _peek_request_path(a, timeout=2.0)
        assert path is None
        assert buf.startswith(b"\x16\x03\x01")
    finally:
        a.close(); b.close()


# -- Route / lease data model ----------------------------------------------
def test_route_normalizes_prefix_and_validates_port():
    assert Route("api", "h", 80).prefix == "/api"
    assert Route("/api/", "h", 80).prefix == "/api"
    assert Route("/", "h", 80).prefix == "/"
    with pytest.raises(ValueError):
        Route("/x", "h", 0)


def test_lease_to_dict_and_roundtrip_preserves_routes():
    lease = _lease(routes=[{"prefix": "/api", "host": "api", "port": 8300}])
    d = lease.to_dict()
    assert d["routes"] == [{"prefix": "/api", "host": "api", "port": 8300}]
    # routes come back as real Route objects (coerced in __post_init__)
    assert all(isinstance(r, Route) for r in lease.routes)


def test_lease_store_roundtrip_rehydrates_routes(tmp_path):
    reg = DevServiceRegistry(store_path=tmp_path / "leases.json")
    reg.register(target_machine_id="m", target_host="127.0.0.1", target_port=3000,
                 owner_process_id="p",
                 routes=[{"prefix": "/api", "host": "api", "port": 8300}])
    # A fresh registry (forces reload from disk) must rehydrate Route objects.
    reg2 = DevServiceRegistry(store_path=tmp_path / "leases.json")
    lease = reg2.active_leases()[0]
    assert lease.routes and isinstance(lease.routes[0], Route)
    assert lease.routes[0].prefix == "/api" and lease.routes[0].port == 8300


def test_no_routes_is_backward_compatible():
    lease = _lease()
    assert lease.routes is None
    assert forwarder_for(lease) is not None
    assert isinstance(forwarder_for(lease), DevServiceForwarder)


def test_forwarder_dispatches_origin_edge_when_routes():
    lease = _lease(routes=[{"prefix": "/api", "host": "api", "port": 8300}])
    assert isinstance(forwarder_for(lease), OriginEdge)


# -- live end-to-end routing over loopback ---------------------------------
def _marker_server(marker: str):
    """A tiny HTTP-ish server: every request gets a 200 with body == marker."""
    resp = (f"HTTP/1.1 200 OK\r\nContent-Length: {len(marker)}\r\n"
            f"Connection: close\r\n\r\n{marker}").encode()
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    port = srv.getsockname()[1]
    stop = threading.Event()

    def loop():
        srv.settimeout(0.3)
        while not stop.is_set():
            try:
                c, _ = srv.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            try:
                c.recv(4096)
                c.sendall(resp)
            except OSError:
                pass
            finally:
                try:
                    c.close()
                except OSError:
                    pass

    t = threading.Thread(target=loop, daemon=True)
    t.start()
    return port, stop, srv


def _http_get(url: str) -> str:
    import urllib.request
    with urllib.request.urlopen(url, timeout=5) as r:
        return r.read().decode()


def test_origin_edge_routes_api_and_ui_to_different_backends():
    api_port, api_stop, api_srv = _marker_server("FROM-API")
    ui_port, ui_stop, ui_srv = _marker_server("FROM-UI")
    lease = _lease(routes=[{"prefix": "/api", "host": "127.0.0.1", "port": api_port}],
                   target_port=ui_port)
    edge = OriginEdge(lease, bind_host="127.0.0.1").start()
    base = f"http://127.0.0.1:{lease.external_port}"
    try:
        assert _http_get(f"{base}/api/health") == "FROM-API"   # route match
        assert _http_get(f"{base}/api") == "FROM-API"          # exact prefix
        assert _http_get(f"{base}/") == "FROM-UI"              # catch-all
        assert _http_get(f"{base}/markets") == "FROM-UI"       # catch-all
        assert _http_get(f"{base}/apix") == "FROM-UI"          # sibling -> catch-all
    finally:
        edge.stop()
        api_stop.set(); ui_stop.set(); api_srv.close(); ui_srv.close()


def test_origin_edge_non_http_falls_back_to_catchall():
    api_port, api_stop, api_srv = _marker_server("FROM-API")
    ui_port, ui_stop, ui_srv = _marker_server("FROM-UI")
    lease = _lease(routes=[{"prefix": "/api", "host": "127.0.0.1", "port": api_port}],
                   target_port=ui_port)
    edge = OriginEdge(lease, bind_host="127.0.0.1").start()
    try:
        s = socket.create_connection(("127.0.0.1", lease.external_port), timeout=5)
        s.sendall(b"\x16\x03\x01\r\n")        # not an HTTP request line
        data = b""
        s.settimeout(5)
        while b"FROM-" not in data:
            chunk = s.recv(4096)
            if not chunk:
                break
            data += chunk
        assert b"FROM-UI" in data             # fell back to the catch-all, no hang
        s.close()
    finally:
        edge.stop()
        api_stop.set(); ui_stop.set(); api_srv.close(); ui_srv.close()

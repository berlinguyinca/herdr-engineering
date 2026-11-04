"""Same-origin app edge tests: path-routing forwarder (devfabric.OriginEdge).

Covers the routing core (path matching, request-line peek, target selection),
the lease data model (Route + routes on DevServiceLease, store round-trip,
backward compatibility), the forwarder dispatch, and a LIVE end-to-end check
that the edge really routes /api/* and / to different backends over loopback.
"""
import json
import os
import socket
import sys
import threading
import time
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


# -- devfabric enroll (the 'sets everything up' entry point) -----------------
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_enroll_registers_routes_from_manifest(tmp_path, monkeypatch, capsys):
    import types

    import herdr_engineering.cli as cli
    from herdr_engineering.devfabric import DevServiceRegistry
    monkeypatch.setenv("HERDR_ENGINEERING_STATE", str(tmp_path / "state"))
    monkeypatch.setattr(cli, "_local_fabric_client", lambda: None)
    manifest = tmp_path / "origin.json"
    manifest.write_text(json.dumps({
        "origin_port": 39411, "machine": "m1", "repo": "augur", "label": "L",
        "catchall": {"host": "127.0.0.1", "port": 3000},
        "routes": [{"prefix": "/api", "host": "127.0.0.1", "port": 8300}]}))
    rc = cli._cmd_dev_enroll(types.SimpleNamespace(
        manifest=str(manifest), origin_port=None, machine=None, repo=None,
        label=None, protocol=None, catchall=None, route=None, serve=False,
        bind="127.0.0.1"))
    assert rc == 0
    reg = DevServiceRegistry()   # same (env) store
    lease = next(cand for cand in reg.active_leases() if cand.external_port == 39411)
    assert [(r.prefix, r.port) for r in lease.routes] == [("/api", 8300)]
    assert lease.target_port == 3000            # catch-all


def test_enroll_serve_end_to_end(tmp_path):
    """The real command, end to end: 'devfabric enroll --manifest --serve' sets
    up a working same-origin edge over loopback."""
    import subprocess
    api_port, api_stop, api_srv = _marker_server("FROM-API")
    ui_port, ui_stop, ui_srv = _marker_server("FROM-UI")
    origin_port = _free_port()
    manifest = tmp_path / "origin.json"
    manifest.write_text(json.dumps({
        "origin_port": origin_port, "machine": "m1", "repo": "augur",
        "catchall": {"host": "127.0.0.1", "port": ui_port},
        "routes": [{"prefix": "/api", "host": "127.0.0.1", "port": api_port}]}))
    env = os.environ.copy()
    env["HERDR_ENGINEERING_STATE"] = str(tmp_path / "state")
    # The subprocess must not inherit host.json and enroll this fixture into
    # the developer's live gateway. In-process host_config mocks do not cross
    # this process boundary.
    env["HOME"] = str(tmp_path / "isolated-home")
    proc = subprocess.Popen(
        [sys.executable, "-m", "herdr_engineering.cli", "devfabric", "enroll",
         "--manifest", str(manifest), "--serve"],
        env=env, cwd=REPO_ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        base = f"http://127.0.0.1:{origin_port}"
        deadline = time.monotonic() + 12
        last = None
        while time.monotonic() < deadline:
            try:
                assert _http_get(base + "/api/health") == "FROM-API"
                break
            except Exception as exc:      # noqa: BLE001 - retry until up
                last = exc
                time.sleep(0.3)
        else:
            raise AssertionError(f"origin never came up: {last}")
        assert _http_get(base + "/") == "FROM-UI"       # catch-all -> UI
        assert _http_get(base + "/apix") == "FROM-UI"   # sibling -> catch-all
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        api_stop.set(); ui_stop.set(); api_srv.close(); ui_srv.close()


# -- devfabric enroll-sync (auto-enroll every manifest in a dir) ------------
def _run_enroll_sync(origins, state_dir, *extra):
    import subprocess
    env = os.environ.copy()
    env["HERDR_ENGINEERING_STATE"] = str(state_dir)
    # Hermetic: force LOCAL enrollment (no live gateway) so the result is
    # written to state_dir/leases.json rather than to a reachable gateway.
    env["HOME"] = str(__import__("pathlib").Path(state_dir).parent / "home")  # no host.json / local config -> no gateway
    return subprocess.run(
        [sys.executable, "-m", "herdr_engineering.cli", "devfabric", "enroll-sync",
         "--dir", str(origins), *extra], env=env, cwd=REPO_ROOT,
        capture_output=True, text=True)


def test_enroll_sync_enrolls_all_and_idempotent(tmp_path):
    from herdr_engineering.devfabric import DevServiceRegistry
    origins = tmp_path / "origins"
    origins.mkdir()
    (origins / "a.json").write_text(json.dumps({
        "origin_port": 39601, "repo": "a", "label": "A",
        "catchall": {"host": "127.0.0.1", "port": 3000},
        "routes": [{"prefix": "/api", "host": "127.0.0.1", "port": 8300}]}))
    (origins / "b.json").write_text(json.dumps({
        "origin_port": 39602, "repo": "b", "label": "B",
        "catchall": {"host": "127.0.0.1", "port": 3001}}))
    st = tmp_path / "state"
    r1 = _run_enroll_sync(origins, st)
    assert r1.returncode == 0
    assert sorted(json.loads(r1.stdout)["enrolled"]) == ["a", "b"]
    reg = DevServiceRegistry(store_path=st / "leases.json")
    assert sorted(lease.label for lease in reg.active_leases()) == ["A", "B"]
    # idempotent: a second run enrolls nothing new
    r2 = _run_enroll_sync(origins, st)
    assert json.loads(r2.stdout)["enrolled"] == []
    assert sorted(json.loads(r2.stdout)["unchanged"]) == ["a", "b"]


def test_enroll_sync_watch_auto_enrolls_new_manifest(tmp_path):
    """The auto-enroll engine: a NEW manifest dropped into the dir is enrolled
    by the watch loop within an interval (no restart, no manual step)."""
    import subprocess

    from herdr_engineering.devfabric import DevServiceRegistry
    origins = tmp_path / "origins"
    origins.mkdir()
    (origins / "a.json").write_text(json.dumps({
        "origin_port": 39701, "repo": "a", "label": "A",
        "catchall": {"host": "127.0.0.1", "port": 3000}}))
    st = tmp_path / "state"
    env = os.environ.copy()
    env["HERDR_ENGINEERING_STATE"] = str(st)
    # Hermetic: force LOCAL enrollment (no live gateway) so the watch loop
    # writes to st/leases.json and the assertions below are deterministic.
    env["HOME"] = str(tmp_path / "home")  # no host.json / local config -> no gateway
    proc = subprocess.Popen(
        [sys.executable, "-m", "herdr_engineering.cli", "devfabric", "enroll-sync",
         "--dir", str(origins), "--watch", "--interval", "1"],
        env=env, cwd=REPO_ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    def reg():
        return DevServiceRegistry(store_path=st / "leases.json")

    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not reg().active_leases():
            time.sleep(0.3)
        assert reg().active_leases(), "initial manifest did not enroll"
        (origins / "b.json").write_text(json.dumps({
            "origin_port": 39702, "repo": "b", "label": "B",
            "catchall": {"host": "127.0.0.1", "port": 3001}}))
        labels = []
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            labels = sorted(lease.label for lease in reg().active_leases())
            if labels == ["A", "B"]:
                break
            time.sleep(0.3)
        assert labels == ["A", "B"], f"new manifest not auto-enrolled: {labels}"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

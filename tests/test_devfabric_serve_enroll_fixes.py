"""Regression tests for issue #76 (devfabric serve per-host forwarder WEDGE)
and issue #77 (enroll-sync self-heal gap + silent local-registry degradation).

#76: the forwarder kept its listener and accepted TCP handshakes but never
     routed requests after an early failure inside route_and_pipe. The fix
     bounds the client peek AND the full pipe, makes per-connection worker
     failures non-fatal (log + close, never hang), and guarantees a dead or
     stuck worker can never starve subsequent requests.

#77a: enroll-sync must re-enroll when the origin's port is absent from the
      gateway's healthz bound_ports even if the local-registry lease entry has
      expired (90s TTL) and/or the sync bookkeeping says "unchanged".

#77b: enrolling via the local registry while a gateway URL is configured or
      reachable must produce a loud warning (and --require-gateway must make
      it a hard failure), instead of silently degrading to
      {"via": "local", "gateway": false}.
"""
import json
import os
import socket
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from herdr_engineering.contracts import DevServiceLease
from herdr_engineering.devfabric import (
    DevServiceRegistry,
    _add_seconds,
    _now_iso,
    forwarder_for,
)


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _http_get(url: str, timeout: float = 5.0) -> str:
    import urllib.request
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return resp.read().decode()


def _marker_server(marker: str):
    class _H(BaseHTTPRequestHandler):
        def do_GET(self):
            body = f"{marker}:{self.path}".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", _free_port()), _H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, srv.server_address[1]


def _lease(routes=None, target_port=9000):
    return DevServiceLease(
        id=f"lease_{uuid.uuid4().hex[:16]}", external_port=_free_port(),
        protocol="http", target_machine_id="m", target_host="127.0.0.1",
        target_port=target_port, routes=routes)


# -- issue #76: forwarder must not wedge -------------------------------------

def test_forwarder_stuck_connection_does_not_wedge_next_requests():
    """A connection whose pipe gets stuck (backend accepts but never reads)
    must not block the accept/route path: the stuck request fails cleanly
    within a bounded time, and the NEXT request is still routed to a live
    backend. Before the fix, request 2 hung indefinitely (the wedge)."""
    # A backend that accepts but never reads or responds: makes the full
    # pipe (client->backend) block forever on the request body.
    stuck = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    stuck.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    stuck.bind(("127.0.0.1", 0))
    stuck_port = stuck.getsockname()[1]
    stuck.listen(8)
    stuck_threads = []

    def _accept_loop():
        while True:
            try:
                c, _ = stuck.accept()
            except OSError:
                return
            stuck_threads.append(c)
            # never read, never respond

    t = threading.Thread(target=_accept_loop, daemon=True)
    t.start()

    # A healthy second backend.
    healthy, healthy_port = _marker_server("OK")

    # The lease's catch-all points at the stuck backend; a /ok route points
    # at the healthy one, so the follow-up request has somewhere to go.
    from herdr_engineering.contracts import Route
    lease = _lease(target_port=stuck_port,
                   routes=[Route("/ok", "127.0.0.1", healthy_port)])
    fwd = forwarder_for(lease, bind_host="127.0.0.1")
    fwd.start()
    try:
        base = f"http://127.0.0.1:{lease.external_port}"

        # Request 1: routed to the stuck backend. Start it in a thread so we
        # can verify request 2 is NOT blocked while request 1 is stuck.
        stuck_req = socket.create_connection(("127.0.0.1", lease.external_port))
        stuck_req.sendall(b"GET /stuck HTTP/1.1\r\nHost: x\r\n\r\n")
        stuck_result = {}

        def _wait_stuck():
            started = time.monotonic()
            try:
                stuck_req.settimeout(60)
                stuck_req.recv(4096)
            except (TimeoutError, ConnectionError, OSError):
                pass
            stuck_result["elapsed"] = time.monotonic() - started

        stuck_thread = threading.Thread(target=_wait_stuck, daemon=True)
        stuck_thread.start()
        time.sleep(0.5)  # let the stuck request settle

        # THE KEY ASSERTION (issue #76): while request 1 is stuck on the
        # unresponsive backend, request 2 to the healthy /ok route must STILL
        # be served within a bounded time. The original bug: the forwarder
        # accepted TCP but never routed new requests.
        ok = _http_get(f"{base}/ok", timeout=10)
        assert "OK" in ok, (
            "subsequent request was not routed while another connection was "
            "stuck: the forwarder is wedged (issue #76)")

        # Request 1 must eventually fail cleanly (bounded inactivity timeout),
        # not hang forever.
        stuck_thread.join(timeout=45)
        assert not stuck_thread.is_alive(), (
            "stuck connection did not time out within 45s: the pipe inactivity "
            "timeout is not bounded (issue #76)")
        elapsed = stuck_result.get("elapsed", 99)
        assert elapsed < 40, (
            f"stuck request sat {elapsed:.1f}s: forwarder pipe inactivity "
            f"timeout is not bounded (issue #76)")
    finally:
        fwd.stop()
        for c in stuck_threads:
            try:
                c.close()
            except OSError:
                pass
        stuck.close()
        healthy.server_close()


def test_forwarder_client_peek_is_bounded():
    """A client that connects but never sends a full request line must not
    hold a worker forever: the peek has a bounded timeout, and other requests
    are still served while it is pending."""
    healthy, healthy_port = _marker_server("OK")
    lease = _lease(target_port=healthy_port)
    fwd = forwarder_for(lease, bind_host="127.0.0.1")
    fwd.start()
    try:
        # Connect and send only a partial request line, then go silent.
        quiet = socket.create_connection(("127.0.0.1", lease.external_port))
        quiet.sendall(b"GET /")  # incomplete: no CRLFCRLF
        time.sleep(0.2)

        # A real request must still be served while the quiet one is pending.
        ok = _http_get(f"http://127.0.0.1:{lease.external_port}/ok", timeout=10)
        assert "OK" in ok, "forwarder wedged on an incomplete request (issue #76)"
        quiet.close()
    finally:
        fwd.stop()
        healthy.server_close()


def test_forwarder_backend_down_fails_cleanly_and_recovers():
    """When the backend is down, requests fail fast with a 5xx (not hang),
    and a forwarder pointed at a live backend routes normally."""
    down_port = _free_port()  # no listener
    lease = _lease(target_port=down_port)
    fwd = forwarder_for(lease, bind_host="127.0.0.1")
    fwd.start()
    base = f"http://127.0.0.1:{lease.external_port}"
    try:
        import urllib.error
        import urllib.request

        # Backend down: request must fail fast with a 5xx, not hang.
        try:
            with urllib.request.urlopen(base + "/x", timeout=10) as resp:
                code = resp.status
        except urllib.error.HTTPError as exc:
            code = exc.code
        assert code >= 500, f"expected a 5xx on a downed backend, got {code}"
    finally:
        fwd.stop()

    # Recovery: a forwarder pointed at a live backend routes.
    healthy, healthy_port = _marker_server("OK")
    lease2 = _lease(target_port=healthy_port)
    fwd2 = forwarder_for(lease2, bind_host="127.0.0.1")
    fwd2.start()
    try:
        ok = _http_get(f"http://127.0.0.1:{lease2.external_port}/ok", timeout=10)
        assert "OK" in ok
    finally:
        fwd2.stop()
        healthy.server_close()


# -- issue #77a: enroll-sync self-heal ---------------------------------------

def _manifest_dict(lease_id, external_port, host, target_port, repo="repo"):
    return {
        "lease_id": lease_id,
        "origin_port": external_port,
        "machine": "machine-A",
        "catchall": {"host": host, "port": target_port},
        "repo": repo,
    }


def _seed_local_lease(reg, lease_id, external_port, target_port, alive: bool):
    now = _now_iso()
    lease = DevServiceLease(
        id=lease_id, external_port=external_port, protocol="http",
        target_machine_id="machine-A", target_host="127.0.0.1",
        target_port=target_port, state="active", created_at=now,
        renewed_at=now,
        expires_at=_add_seconds(now, 300 if alive else -100))
    with reg._lock:
        reg._leases[lease_id] = lease
        reg._persist()


def _fake_forwarder_capture():
    captured = {}

    def fake_forwarder_for(lease, bind_host):
        captured["started"] = captured.get("started", 0) + 1

        class _Fwd:
            def start(self):
                pass

            def stop(self):
                pass
        return _Fwd()

    return fake_forwarder_for, captured


def _make_sync_args(tmp_path):
    return type("A", (), {
        "dir": str(tmp_path), "watch": False, "interval": 0.0,
        "serve": True, "bind": "127.0.0.1", "git_pull": False,
        "require_gateway": False,
    })()


def test_enroll_sync_reenrolls_expired_local_lease_when_gateway_port_missing(
        tmp_path, monkeypatch):
    """#77a: manifest present + origin port absent from gateway bound_ports
    => re-enroll (register on the gateway) even when the local registry lease
    has EXPIRED (TTL) and the sync bookkeeping says "unchanged"."""
    import herdr_engineering.cli as cli

    lease_id = f"lease_{uuid.uuid4().hex[:16]}"
    external_port = _free_port()
    target_port = _free_port()
    manifests_dir = tmp_path / "manifests"
    manifests_dir.mkdir()

    reg = DevServiceRegistry(store_path=tmp_path / "store.json",
                             port_range=(18000, 28999))
    _seed_local_lease(reg, lease_id, external_port, target_port, alive=False)

    register_calls = []

    class FakeGateway:
        via = "gateway"

        def healthz(self):
            return {"ok": True, "bound_ports": [11111]}  # NOT our port

        def register(self, **kwargs):
            register_calls.append(kwargs)
            return {"lease": {"id": kwargs.get("lease_id") or lease_id}}

        def heartbeat(self, lease_id):
            return {"ok": True}

    monkeypatch.setattr(cli, "_local_fabric_client", lambda: FakeGateway())
    monkeypatch.setattr(
        "herdr_engineering.devfabric.DevServiceRegistry",
        lambda **kw: reg)

    (manifests_dir / "app.json").write_text(json.dumps(
        _manifest_dict(lease_id, external_port, "127.0.0.1", target_port)))

    rc = cli._cmd_dev_enroll_sync(_make_sync_args(manifests_dir))
    assert rc == 0
    assert len(register_calls) >= 1, (
        "enroll-sync did NOT re-enroll (register) an origin whose local lease "
        "expired and whose port is missing from the gateway bound_ports "
        "(issue #77a)")


def test_enroll_sync_reenrolls_when_port_missing_even_if_bookkeeping_unchanged(
        tmp_path, monkeypatch):
    """#77a (second half): even when the local registry lease is ALIVE and the
    manifest is unchanged, a missing gateway bound_port forces re-enroll."""
    import herdr_engineering.cli as cli

    lease_id = f"lease_{uuid.uuid4().hex[:16]}"
    external_port = _free_port()
    target_port = _free_port()
    manifests_dir = tmp_path / "manifests"
    manifests_dir.mkdir()

    reg = DevServiceRegistry(store_path=tmp_path / "store.json",
                             port_range=(18000, 28999))
    _seed_local_lease(reg, lease_id, external_port, target_port, alive=True)

    register_calls = []

    class FakeGateway:
        via = "gateway"

        def healthz(self):
            return {"ok": True, "bound_ports": []}  # our port NOT bound

        def register(self, **kwargs):
            register_calls.append(kwargs)
            return {"lease": {"id": lease_id}}

        def heartbeat(self, lease_id):
            return {"ok": True}

    monkeypatch.setattr(cli, "_local_fabric_client", lambda: FakeGateway())
    monkeypatch.setattr(
        "herdr_engineering.devfabric.DevServiceRegistry",
        lambda **kw: reg)

    (manifests_dir / "app.json").write_text(json.dumps(
        _manifest_dict(lease_id, external_port, "127.0.0.1", target_port)))

    args = _make_sync_args(manifests_dir)
    # First run enrolls and establishes bookkeeping ("unchanged" baseline).
    assert cli._cmd_dev_enroll_sync(args) == 0
    register_calls.clear()
    # Second run: manifest unchanged, local lease alive, but gateway port
    # missing => must re-enroll.
    assert cli._cmd_dev_enroll_sync(args) == 0
    assert len(register_calls) >= 1, (
        "enroll-sync did NOT re-enroll (register) an unchanged origin whose "
        "port is missing from the gateway bound_ports (issue #77a)")


# -- issue #77b: loud warning on local-registry degradation ------------------

def test_enroll_via_local_with_gateway_configured_warns(tmp_path, monkeypatch,
                                                        capsys):
    """#77b: a gateway URL is configured/reachable but the CLI fell back to
    the local registry => it must print a loud WARNING, not silently degrade
    to {"via": "local", "gateway": false}."""
    import herdr_engineering.cli as cli

    monkeypatch.setattr(cli, "_local_fabric_client", lambda: None)
    monkeypatch.setattr(cli, "_local_lease_alive", lambda reg, lease_id: True)
    monkeypatch.setattr(cli, "_gateway_configured_or_reachable", lambda: True)
    monkeypatch.setattr(
        "herdr_engineering.devfabric.DevServiceRegistry",
        lambda **kw: DevServiceRegistry(store_path=tmp_path / "store.json"))

    def fake_forwarder_for(lease, bind_host):
        class _Fwd:
            def start(self):
                pass

            def stop(self):
                pass
        return _Fwd()

    monkeypatch.setattr(
        "herdr_engineering.devfabric.forwarder_for", fake_forwarder_for)

    args = type("A", (), {
        "manifest": None, "origin_port": _free_port(), "machine": "m",
        "repo": "repo", "label": None, "protocol": "http",
        "catchall": "127.0.0.1:9", "route": None, "serve": False,
        "bind": "127.0.0.1", "require_gateway": False,
    })()

    rc = cli._cmd_dev_enroll(args)
    assert rc == 0
    captured = capsys.readouterr()
    out = (captured.out + captured.err).lower()
    assert "warning" in out, (
        f"enrolling via the local registry with a gateway configured must "
        f"print a loud warning, got: {out!r}")


def test_enroll_with_require_gateway_fails_when_local(tmp_path, monkeypatch,
                                                      capsys):
    """#77b: --require-gateway makes the local-registry fallback a hard
    failure (nonzero rc) instead of a silent degrade."""
    import herdr_engineering.cli as cli

    monkeypatch.setattr(cli, "_local_fabric_client", lambda: None)
    monkeypatch.setattr(cli, "_local_lease_alive", lambda reg, lease_id: True)
    monkeypatch.setattr(cli, "_gateway_configured_or_reachable", lambda: True)
    monkeypatch.setattr(
        "herdr_engineering.devfabric.DevServiceRegistry",
        lambda **kw: DevServiceRegistry(store_path=tmp_path / "store.json"))

    def fake_forwarder_for(lease, bind_host):
        class _Fwd:
            def start(self):
                pass

            def stop(self):
                pass
        return _Fwd()

    monkeypatch.setattr(
        "herdr_engineering.devfabric.forwarder_for", fake_forwarder_for)

    args = type("A", (), {
        "manifest": None, "origin_port": _free_port(), "machine": "m",
        "repo": "repo", "label": None, "protocol": "http",
        "catchall": "127.0.0.1:9", "route": None, "serve": False,
        "bind": "127.0.0.1", "require_gateway": True,
    })()

    rc = cli._cmd_dev_enroll(args)
    assert rc != 0, (
        "--require-gateway must fail (nonzero) when the gateway is not "
        "reachable, instead of silently using the local registry")

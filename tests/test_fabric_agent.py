"""Fabric auto-registration agent tests (spec 0110).

Hermetic: the OS listener scanner is monkeypatched to return only controlled
listeners, and the gateway control API is faked on a local HTTP server. Real
sockets are used only for the app-under-test and the forwarder, on
OS-allocated ephemeral ports (no fixed port ranges, no machine state).
"""
import http.server
import json
import socket
import threading
import urllib.request

import herdr_engineering.fabric_agent as fa
from herdr_engineering.fabric import FabricClient
from herdr_engineering.fabric_agent import (
    AgentConfig,
    AgentForwarder,
    FabricAgent,
    probe_web,
)


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _http_target(tmp_path):
    """Start a tiny local HTTP server (loopback); returns (server, port)."""
    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), http.server.SimpleHTTPRequestHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


def _raw_tcp_listener():
    """A listener that accepts but speaks no HTTP; returns (sock, port)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", 0))
    s.listen(8)
    return s, s.getsockname()[1]


def _banner_listener(banner: bytes):
    """A listener that greets with a non-HTTP banner (like SSH), then closes.

    Exercises the BadStatusLine path: the HTTP parser chokes on the first
    line, which must be treated as 'not a web service'.
    """
    srv, port = _raw_tcp_listener()

    def _serve():
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            try:
                conn.sendall(banner)
            except OSError:
                pass
            try:
                conn.close()
            except OSError:
                pass

    threading.Thread(target=_serve, daemon=True).start()
    return srv, port


def _listeners(ports: dict[int, str]) -> dict[int, dict]:
    """Shape the scanner output: {port: {"addrs": set, "process": str}}."""
    return {p: {"addrs": {"127.0.0.1"}, "process": proc}
            for p, proc in ports.items()}


class _FakeGateway:
    """Minimal fake of the gateway control API (register/close/healthz)."""

    def __init__(self):
        self.leases: dict[str, dict] = {}
        self.closed: list[str] = []
        # External ports are just opaque numbers here (the fake never binds
        # them); start in the dev window to mirror the real gateway's _LO.
        self._port_counter = 18000
        self._lock = threading.Lock()
        self._httpd = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), _make_fake_handler(self))
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()
        self.port = self._httpd.server_address[1]

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def _next_external_port(self) -> int:
        with self._lock:
            self._port_counter += 1
            return self._port_counter

    def stop(self):
        self._httpd.shutdown()
        self._httpd.server_close()


def _make_fake_handler(gw: _FakeGateway):
    class _Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):  # silence
            pass

        def _send(self, code: int, payload: dict):
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/healthz":
                self._send(200, {"ok": True})
            elif self.path == "/list":
                with gw._lock:
                    self._send(200, {"leases": list(gw.leases.values())})
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            if self.path == "/register":
                ext = gw._next_external_port()
                lease = {
                    "id": f"lease_{len(gw.leases) + 1}",
                    "external_port": ext,
                    "state": "active",
                    "protocol": body.get("protocol", "http"),
                    "label": body.get("label", ""),
                    "target": {
                        "host": body["target_host"],
                        "port": body["target_port"],
                        "machine_id": body["target_machine_id"],
                    },
                }
                gw.leases[lease["id"]] = lease
                # Real gateway nests the lease and adds a stable url.
                self._send(200, {"lease": lease,
                                 "url": f"http://gw.test:{ext}"})
            elif self.path == "/close":
                lease_id = body.get("lease_id") or body.get("id")
                gw.leases.pop(lease_id, None)
                gw.closed.append(lease_id)
                self._send(200, {"closed": lease_id})
            elif self.path == "/heartbeat":
                lease_id = body.get("lease_id") or body.get("id")
                # Match the REAL gateway contract: 200 {"ok": bool}, never 404.
                self._send(200, {"ok": lease_id in gw.leases,
                                 "renewed": lease_id
                                 if lease_id in gw.leases else None})
            else:
                self._send(404, {"error": "not found"})

    return _Handler


def _agent(tmp_path, gw: _FakeGateway, **cfg_over):
    cfg = dict(
        tailnet_ip="127.0.0.1",
        store_path=tmp_path / "fabric-agent.json",
        exclude_ports=frozenset({gw.port}),  # never register the fake itself
        label_prefix="",
        interval=0.1,
        probe_timeout=0.5,
        # Tests bind OS-ephemeral ports; neutralize the noise filter so the
        # hermetic ports stay candidates (the filter itself is unit-tested).
        web_port_min=0,
        ephemeral_min=65536,
        deny_ports=frozenset(),
        heartbeat_ttl=0.0,  # exercise keepalive every cycle
    )
    cfg.update(cfg_over)
    return FabricAgent(FabricClient(gw.url), AgentConfig(**cfg))


def _store(tmp_path) -> dict:
    p = tmp_path / "fabric-agent.json"
    return json.loads(p.read_text()) if p.exists() else {}


# ---------------------------------------------------------------------------
# Parser / probe unit tests (pure, no scanner)
# ---------------------------------------------------------------------------

def test_port_and_host_parsing():
    assert fa._port_of("127.0.0.1:5678") == 5678
    assert fa._port_of("*:8080") == 8080
    assert fa._port_of("[::1]:8080") == 8080
    assert fa._port_of("garbage") == -1
    assert fa._host_of("127.0.0.1:5678") == "127.0.0.1"
    assert fa._host_of("*:8080") == "*"
    assert fa._host_of("[::1]:8080") == "::1"


def test_probe_web_detects_http_and_rejects_raw_tcp(tmp_path):
    server, port = _http_target(tmp_path)
    try:
        assert probe_web(port) is True
    finally:
        server.shutdown()
    raw, raw_port = _raw_tcp_listener()
    try:
        assert probe_web(raw_port, timeout=0.3) is False
    finally:
        raw.close()


def test_probe_web_rejects_banner_first_service(tmp_path):
    """A service that greets before HTTP (e.g. SSH) must not be treated as
    a web service (BadStatusLine must be caught, not leak)."""
    srv, port = _banner_listener(b"SSH-2.0-OpenSSH_9.6\r\n")
    try:
        assert probe_web(port, timeout=1.0) is False
    finally:
        srv.close()


def test_forwarder_proxies_to_loopback_target(tmp_path):
    server, port = _http_target(tmp_path)
    fwd = AgentForwarder("127.0.0.1", port).start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{fwd.port}/",
                                     timeout=2) as resp:
            assert resp.status < 500
    finally:
        fwd.stop()
        server.shutdown()


# ---------------------------------------------------------------------------
# Agent scan-cycle tests (scanner monkeypatched -> hermetic)
# ---------------------------------------------------------------------------

def test_is_dev_web_port_filter(tmp_path):
    """Noise filter: dev ports in, system/ephemeral/denied ports out."""
    gw = _FakeGateway()
    agent = _agent(tmp_path, gw, web_port_min=1024, ephemeral_min=32768,
                   deny_ports=fa.DEFAULT_DENY_PORTS)
    try:
        assert agent._is_dev_web_port(3000) is True      # dev server
        assert agent._is_dev_web_port(8080) is True       # common web
        assert agent._is_dev_web_port(80) is True         # common web (privileged)
        assert agent._is_dev_web_port(631) is False       # privileged system
        assert agent._is_dev_web_port(1933) is False      # deny list (restic)
        assert agent._is_dev_web_port(9090) is False      # deny list (prometheus)
        assert agent._is_dev_web_port(40000) is False     # ephemeral range
        assert agent._is_dev_web_port(gw.port) is False   # excluded
    finally:
        gw.stop()


def test_scan_registers_new_http_service(tmp_path, monkeypatch):
    gw = _FakeGateway()
    server, port = _http_target(tmp_path)
    monkeypatch.setattr(fa, "scan_listening_ports",
                        lambda: _listeners({port: "testapp"}))
    agent = _agent(tmp_path, gw)
    try:
        agent.scan_once()
        targets = [lease["target"]["port"] for lease in gw.leases.values()]
        assert port in targets
        lease = next(lease for lease in gw.leases.values()
                     if lease["target"]["port"] == port)
        assert lease["label"].endswith(f"(port {port})")
        assert str(port) in _store(tmp_path)
    finally:
        server.shutdown()
        agent.stop()
        gw.stop()


def test_scan_skips_raw_tcp_service(tmp_path, monkeypatch):
    gw = _FakeGateway()
    raw, raw_port = _raw_tcp_listener()
    monkeypatch.setattr(fa, "scan_listening_ports",
                        lambda: _listeners({raw_port: "db"}))
    agent = _agent(tmp_path, gw)
    try:
        agent.scan_once()
        assert raw_port not in [
            lease["target"]["port"] for lease in gw.leases.values()]
    finally:
        raw.close()
        agent.stop()
        gw.stop()


def test_vanished_service_is_closed(tmp_path, monkeypatch):
    gw = _FakeGateway()
    server, port = _http_target(tmp_path)
    live = {"ports": {port: "testapp"}}
    monkeypatch.setattr(fa, "scan_listening_ports",
                        lambda: _listeners(live["ports"]))
    agent = _agent(tmp_path, gw)
    try:
        agent.scan_once()
        lease_id = next(lease["id"] for lease in gw.leases.values()
                        if lease["target"]["port"] == port)
        server.shutdown()      # service dies
        live["ports"] = {}     # scanner sees nothing now
        agent.scan_once()
        assert lease_id in gw.closed
        assert lease_id not in gw.leases
        assert str(port) not in _store(tmp_path)
    finally:
        agent.stop()
        gw.stop()


def test_heartbeat_keeps_lease(tmp_path, monkeypatch):
    """A live lease is heartbeated (not re-registered) each cycle."""
    gw = _FakeGateway()
    server, port = _http_target(tmp_path)
    monkeypatch.setattr(fa, "scan_listening_ports",
                        lambda: _listeners({port: "testapp"}))
    agent = _agent(tmp_path, gw)
    try:
        agent.scan_once()
        lease_id = next(lease["id"] for lease in gw.leases.values()
                        if lease["target"]["port"] == port)
        before = len(gw.leases)
        agent.scan_once()  # keepalive heartbeats the existing lease
        assert len(gw.leases) == before  # not re-registered
        assert lease_id in gw.leases     # same lease still live
    finally:
        server.shutdown()
        agent.stop()
        gw.stop()


def test_dropped_lease_is_reregistered(tmp_path, monkeypatch):
    """If the gateway loses a lease (restart / TTL), the agent re-registers
    the same target so the dev:<port> mapping survives."""
    gw = _FakeGateway()
    server, port = _http_target(tmp_path)
    monkeypatch.setattr(fa, "scan_listening_ports",
                        lambda: _listeners({port: "testapp"}))
    agent = _agent(tmp_path, gw)
    try:
        agent.scan_once()
        lease_id = next(lease["id"] for lease in gw.leases.values()
                        if lease["target"]["port"] == port)
        gw.leases.pop(lease_id)  # simulate gateway restart (lease gone)
        agent.scan_once()        # keepalive: heartbeat 404 -> re-register
        assert any(lease["target"]["port"] == port for lease in gw.leases.values())
    finally:
        server.shutdown()
        agent.stop()
        gw.stop()


def test_gateway_front_door_ports_are_not_registered(tmp_path, monkeypatch):
    """On the gateway host the router's own dev:<port> front-door ports are
    live local HTTP listeners; the agent must never register them (feedback
    loop). They are excluded via the gateway's current external-port set."""
    gw = _FakeGateway()
    server, port = _http_target(tmp_path)
    monkeypatch.setattr(fa, "scan_listening_ports",
                        lambda: _listeners({port: "testapp"}))
    agent = _agent(tmp_path, gw)
    try:
        agent.scan_once()  # registers `port` -> gets a gateway external_port
        ext = next(lease["external_port"] for lease in gw.leases.values()
                   if lease["target"]["port"] == port)
        # Now the front-door port `ext` also appears as a local listener
        # (exactly what the gateway host's agent scanner sees).
        monkeypatch.setattr(fa, "scan_listening_ports",
                            lambda: _listeners({port: "testapp", ext: "router"}))
        before = len(gw.leases)
        agent.scan_once()
        assert not any(lease["target"]["port"] == ext for lease in gw.leases.values())
        assert len(gw.leases) == before  # no feedback registration
    finally:
        server.shutdown()
        agent.stop()
        gw.stop()


def test_excluded_ports_are_never_registered(tmp_path, monkeypatch):
    gw = _FakeGateway()
    server, port = _http_target(tmp_path)
    monkeypatch.setattr(fa, "scan_listening_ports",
                        lambda: _listeners({port: "testapp"}))
    agent = _agent(tmp_path, gw, exclude_ports=frozenset({gw.port, port}))
    try:
        agent.scan_once()
        assert port not in [
            lease["target"]["port"] for lease in gw.leases.values()]
    finally:
        server.shutdown()
        agent.stop()
        gw.stop()


def test_restart_does_not_reregister(tmp_path, monkeypatch):
    gw = _FakeGateway()
    server, port = _http_target(tmp_path)
    monkeypatch.setattr(fa, "scan_listening_ports",
                        lambda: _listeners({port: "testapp"}))
    agent1 = _agent(tmp_path, gw)
    agent2 = None
    try:
        agent1.scan_once()
        count_after_first = len(gw.leases)
        assert count_after_first >= 1
        agent2 = _agent(tmp_path, gw)  # restart: store already on disk
        agent2.scan_once()  # same store, must not re-register
        assert len(gw.leases) == count_after_first
    finally:
        server.shutdown()
        agent1.stop()
        if agent2 is not None:
            agent2.stop()
        gw.stop()


def test_forwarder_port_not_adopted_as_new_service(tmp_path, monkeypatch):
    """A forwarder the agent itself started must not be re-adopted as a
    separate service on the next cycle (double-registration guard)."""
    gw = _FakeGateway()
    server, port = _http_target(tmp_path)
    import herdr_engineering.fabric_agent as fa_mod

    real_tcp_open = fa_mod.tcp_open

    def _loopback_only(host, p, timeout=1.0):
        if p == port:            # the app is only on loopback
            return False
        return real_tcp_open(host, p, timeout)

    monkeypatch.setattr(fa_mod, "tcp_open", _loopback_only)
    seen: dict = {"ports": {port: "testapp"}}
    monkeypatch.setattr(fa, "scan_listening_ports",
                        lambda: _listeners(seen["ports"]))
    agent = _agent(tmp_path, gw)
    try:
        agent.scan_once()
        entry = _store(tmp_path)[str(port)]
        assert entry["fwd_port"] is not None
        # Second cycle now also sees the forwarder's own port; it must not
        # be registered as a NEW service (still exactly one lease, the one
        # whose target is the forwarder itself).
        seen["ports"][entry["fwd_port"]] = "python"
        before = len(gw.leases)
        agent.scan_once()
        assert len(gw.leases) == before
    finally:
        server.shutdown()
        agent.stop()
        gw.stop()


def test_loopback_only_service_gets_forwarder(tmp_path, monkeypatch):
    """When the app is NOT reachable via the tailnet IP, the agent puts a
    forwarder in front of it and registers the forwarder's port."""
    gw = _FakeGateway()
    server, port = _http_target(tmp_path)
    import herdr_engineering.fabric_agent as fa_mod

    real_tcp_open = fa_mod.tcp_open

    def _loopback_only(host, p, timeout=1.0):
        if p == port:
            return False
        return real_tcp_open(host, p, timeout)

    monkeypatch.setattr(fa_mod, "tcp_open", _loopback_only)
    monkeypatch.setattr(fa, "scan_listening_ports",
                        lambda: _listeners({port: "testapp"}))
    agent = _agent(tmp_path, gw)
    try:
        agent.scan_once()
        entry = _store(tmp_path)[str(port)]
        assert entry["fwd_port"] is not None
        lease = next(lease for lease in gw.leases.values()
                     if lease["target"]["port"] == entry["fwd_port"])
        assert lease["target"]["port"] != port
    finally:
        server.shutdown()
        agent.stop()
        gw.stop()

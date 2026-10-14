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
import time
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

    def __init__(self, port_range: tuple = (18000, 28999)):
        self.leases: dict[str, dict] = {}
        self.closed: list[str] = []
        # Mirror the real gateway: it binds its WHOLE front-door range, not
        # just the ports that currently hold leases.
        self.port_range = port_range
        self._lo, self._hi = port_range
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
                self._send(200, {"ok": True,
                                 "port_lo": gw._lo, "port_hi": gw._hi})
            elif self.path == "/list":
                with gw._lock:
                    self._send(200, {"leases": list(gw.leases.values())})
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            if self.path == "/register":
                # Mirror the REAL gateway's port-matching: prefer the requested
                # port (default = target port) when not already leased, else
                # fall back to the sequential dev window.
                pref = int(body.get("preferred_port", body["target_port"]))
                taken = {lease["external_port"] for lease in gw.leases.values()}
                ext = pref if pref not in taken else gw._next_external_port()
                lease = {
                    "id": f"lease_{len(gw.leases) + 1}",
                    "external_port": ext, "preferred": pref,
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


def test_scan_skips_gateway_front_door_port(tmp_path, monkeypatch):
    """A listener inside the gateway's reserved front-door range is never
    adopted, even when it looks like a live web service (feedback-loop guard:
    the gateway binds its whole range, not just leased ports)."""
    gw = _FakeGateway()
    front_port = gw._lo + 500  # e.g. 18500, inside the reserved range
    monkeypatch.setattr(fa, "scan_listening_ports",
                        lambda: _listeners({front_port: "web"}))
    agent = _agent(tmp_path, gw)
    try:
        agent.scan_once()
        assert front_port not in [
            lease["target"]["port"] for lease in gw.leases.values()]
        assert str(front_port) not in _store(tmp_path)
    finally:
        agent.stop()
        gw.stop()


def test_scan_skips_container_networking_shim(tmp_path, monkeypatch):
    """Container/VM networking shims (e.g. slirp4netns) are never registered:
    their bindings are meaningless, mostly-stale port forwards."""
    gw = _FakeGateway()
    server, port = _http_target(tmp_path)
    monkeypatch.setattr(fa, "scan_listening_ports",
                        lambda: _listeners({port: "slirp4netns"}))
    agent = _agent(tmp_path, gw)
    try:
        agent.scan_once()
        assert port not in [
            lease["target"]["port"] for lease in gw.leases.values()]
        assert str(port) not in _store(tmp_path)
    finally:
        server.shutdown()
        agent.stop()
        gw.stop()


def test_scan_releases_previously_registered_noise(tmp_path, monkeypatch):
    """Noise that was registered BEFORE the filters existed (gateway
    front-doors, container shims) is actively released on the next scan, not
    silently left in the store/registry."""
    import json as _json
    gw = _FakeGateway()
    front_port = gw._lo + 500  # inside the gateway's front-door range
    shim_port = 28050          # a slirp4netns binding, NOT a front-door
    store = tmp_path / "fabric-agent.json"
    store.write_text(_json.dumps({
        str(front_port): {"lease_id": "lease_front",
                          "label": f"web on bender (port {front_port})",
                          "external_port": gw._lo,
                          "url": f"http://dev.lan:{gw._lo}",
                          "process": "web", "last_hb": 0.0},
        str(shim_port): {"lease_id": "lease_shim",
                         "label": f"slirp4netns on bender (port {shim_port})",
                         "external_port": gw._lo + 1,
                         "url": f"http://dev.lan:{gw._lo + 1}",
                         "process": "slirp4netns", "last_hb": 0.0},
    }))
    monkeypatch.setattr(fa, "scan_listening_ports",
                        lambda: _listeners({front_port: "web",
                                            shim_port: "slirp4netns"}))
    agent = _agent(tmp_path, gw)
    try:
        agent.scan_once()
        assert str(front_port) not in _store(tmp_path)
        assert str(shim_port) not in _store(tmp_path)
        assert "lease_front" in gw.closed
        assert "lease_shim" in gw.closed
    finally:
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
    live local HTTP listeners bound to the tailnet IP; the agent must never
    register them (feedback loop). A front-door is skipped even though it
    answers HTTP, because it is already bound to the tailnet IP."""
    gw = _FakeGateway()
    router_server, ext = _http_target(tmp_path)  # real HTTP front-door
    tailnet = "100.64.0.1"
    # A remote host already owns this external port on the gateway, AND the
    # gateway's router has bound the SAME port on the tailnet IP locally.
    gw.leases["seed"] = {
        "id": "seed", "external_port": ext, "state": "active",
        "protocol": "http", "label": "remote",
        "target": {"host": "100.1.2.3", "port": ext, "machine_id": "other"},
    }
    monkeypatch.setattr(fa, "scan_listening_ports", lambda: {
        ext: {"addrs": {tailnet}, "process": "router"},  # bound to tailnet IP
    })
    agent = _agent(tmp_path, gw, tailnet_ip=tailnet, probe_timeout=0.5)
    try:
        agent.scan_once()
        # front-door never adopted -> exactly the pre-seeded remote lease remains
        assert len(gw.leases) == 1
        assert not any(lease["target"]["port"] == ext
                       for lease in gw.leases.values() if lease["id"] != "seed")
    finally:
        router_server.shutdown()
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


def test_loopback_only_service_matches_port(tmp_path, monkeypatch):
    """Port-matching by default: a loopback-only dev server on port P is
    registered so it appears at dev.lan:P — the forwarder reuses the same
    port on a distinct (tailnet) bind, and the gateway assigns external_port P.
    """
    gw = _FakeGateway()
    server, port = _http_target(tmp_path)
    import herdr_engineering.fabric_agent as fa_mod

    real_tcp_open = fa_mod.tcp_open

    def _loopback_only(host, p, timeout=1.0):
        # app is bound to 127.0.0.1 only; the "tailnet" bind (127.0.0.2) is
        # closed, so a forwarder is required
        if p == port:
            return False
        return real_tcp_open(host, p, timeout)

    monkeypatch.setattr(fa_mod, "tcp_open", _loopback_only)
    monkeypatch.setattr(fa, "scan_listening_ports",
                        lambda: _listeners({port: "testapp"}))
    agent = _agent(tmp_path, gw, tailnet_ip="127.0.0.2")
    try:
        agent.scan_once()
        entry = _store(tmp_path)[str(port)]
        # forwarder reuses the SAME port on the distinct tailnet bind
        assert entry["fwd_port"] == port
        lease = next(lease for lease in gw.leases.values()
                     if lease["target"]["port"] == port)
        # gateway assigns the matching external port => dev.lan:P
        assert lease["external_port"] == port
        assert lease["target"]["port"] == port
    finally:
        server.shutdown()
        agent.stop()
        gw.stop()


def test_forwarder_restored_across_restart_stays_live(tmp_path, monkeypatch):
    """A loopback-only forwarder must keep serving after an agent restart.

    Regression: the restore path called stop() (setting _stopping) and then
    _rebind(), which started a fresh accept loop while the stop flag was still
    set — the loop exited immediately, leaving the rebind listener
    bound-but-dead (connections reset). Docker apps bound to 127.0.0.1 rely on
    this forwarder to be reachable at dev.lan:PORT.
    """
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
        fwd_port = _store(tmp_path)[str(port)]["fwd_port"]
        assert fwd_port is not None
        agent.stop()
        gw.stop()

        # Simulate an agent restart: a fresh agent restores the forwarder
        # from the persisted store (the loopback app is still up).
        gw2 = _FakeGateway()
        agent2 = _agent(tmp_path, gw2)
        try:
            restored = agent2._forwards.get(port)
            assert restored is not None, "forwarder not restored after restart"
            assert restored.port == fwd_port
            import urllib.request
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{fwd_port}/", timeout=5) as resp:
                assert resp.status == 200, "restored forwarder not serving"
        finally:
            agent2.stop()
            gw2.stop()
    finally:
        server.shutdown()


def test_forwarder_falls_back_to_ephemeral_when_same_port_taken(tmp_path, monkeypatch):
    """When the same port is already bound on the tailnet bind (so the
    forwarder cannot reuse it), the agent falls back to an ephemeral port and
    the gateway assigns a fallback external port — no crash, still registered.
    """
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
    # tailnet bind == app bind (127.0.0.1): the same-port forwarder cannot
    # bind, so it must fall back to an ephemeral forwarder port
    agent = _agent(tmp_path, gw, tailnet_ip="127.0.0.1")
    try:
        agent.scan_once()
        entry = _store(tmp_path)[str(port)]
        assert entry["fwd_port"] is not None
        assert entry["fwd_port"] != port  # same port was taken -> ephemeral
        lease = next(lease for lease in gw.leases.values()
                     if lease["target"]["port"] == entry["fwd_port"])
        assert lease["external_port"] != port  # gateway falls back too
    finally:
        server.shutdown()
        agent.stop()
        gw.stop()


def test_watchdog_exits_on_wedged_scan(tmp_path, monkeypatch):
    """If a scan cycle never completes (e.g. an unbounded DNS lookup), the
    watchdog fires and hard-exits so the service manager respawns a healthy
    agent. os._exit is stubbed to a flag so the test itself survives."""
    gw = _FakeGateway()
    agent = _agent(tmp_path, gw, interval=0.1)
    exited: list[int] = []
    monkeypatch.setattr(fa.os, "_exit", lambda code: exited.append(code))
    monkeypatch.setattr(agent, "scan_once", lambda: time.sleep(30))  # wedged
    stop = threading.Event()
    threading.Thread(
        target=agent.run, args=(stop,),
        kwargs={"cycle_timeout": 0.5, "poll": 0.05},
        daemon=True).start()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not exited:
        time.sleep(0.05)
    assert exited == [1]  # watchdog detected the stall and exited
    stop.set()


def test_watchdog_does_not_fire_when_cycles_complete(tmp_path, monkeypatch):
    """A healthy agent (cycles completing) never trips the watchdog."""
    gw = _FakeGateway()
    server, port = _http_target(tmp_path)
    monkeypatch.setattr(fa, "scan_listening_ports",
                        lambda: _listeners({port: "testapp"}))
    agent = _agent(tmp_path, gw, interval=0.1)
    exited: list[int] = []
    monkeypatch.setattr(fa.os, "_exit", lambda code: exited.append(code))
    stop = threading.Event()
    threading.Thread(
        target=agent.run, args=(stop,),
        kwargs={"cycle_timeout": 0.5, "poll": 0.05},
        daemon=True).start()
    time.sleep(1.0)  # several healthy cycles
    assert exited == []
    stop.set()
    try:
        server.shutdown()
        agent.stop()
        gw.stop()
    finally:
        pass


class _FakeControlPlane:
    """Records ``POST /api/v1/ingest`` envelopes; returns 200 {"ok": true}."""

    def __init__(self):
        self.envelopes: list[dict] = []
        self._lock = threading.Lock()
        self._httpd = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), self._handler())
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()

    def _handler(self):
        cp = self

        class _Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
                with cp._lock:
                    cp.envelopes.append(body)
                resp = json.dumps({"ok": True}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(resp)))
                self.end_headers()
                self.wfile.write(resp)

        return _Handler

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._httpd.server_address[1]}"

    def stop(self):
        self._httpd.shutdown()
        self._httpd.server_close()


def test_agent_reports_service_to_control_plane(tmp_path, monkeypatch):
    """A discovered web service is reported as a service.registered envelope,
    and its removal as service.unavailable, mirroring the lease lifecycle."""
    gw = _FakeGateway()
    cp = _FakeControlPlane()
    server, port = _http_target(tmp_path)
    live = {"ports": {port: "testapp"}}
    monkeypatch.setattr(fa, "scan_listening_ports",
                        lambda: _listeners(live["ports"]))
    agent = _agent(tmp_path, gw, control_plane_url=cp.url)
    try:
        agent.scan_once()
        with cp._lock:
            registered = [e for e in cp.envelopes
                          if e["event_type"] == "service.registered"]
        assert len(registered) == 1
        env = registered[0]
        assert env["entity_type"] == "service"
        p = env["payload"]
        assert p["service_name"].endswith(f"(port {port})")
        assert p["host_id"] == socket.gethostname()
        assert p["route"].startswith("http://")
        assert p["port"] > 0
        assert p["status"] == "healthy"
        # every reported service carries a human description: what it is,
        # which system it runs on, and how to reach it
        assert p["description"]
        assert socket.gethostname() in p["description"]
        assert str(p["port"]) in p["description"]
        assert p["route"] in p["description"]

        # service disappears -> unavailable
        live["ports"].clear()
        agent.scan_once()
        with cp._lock:
            states = [e["event_type"] for e in cp.envelopes]
        assert "service.unavailable" in states
    finally:
        server.shutdown()
        agent.stop()
        gw.stop()
        cp.stop()


def test_agent_skips_control_plane_when_url_empty(tmp_path, monkeypatch):
    """Default config (no control-plane URL) must not emit anything."""
    gw = _FakeGateway()
    server, port = _http_target(tmp_path)
    monkeypatch.setattr(fa, "scan_listening_ports",
                        lambda: _listeners({port: "testapp"}))
    agent = _agent(tmp_path, gw)  # control_plane_url defaults to ""
    try:
        agent.scan_once()
        assert len(gw.leases) == 1  # still registers with the gateway
    finally:
        server.shutdown()
        agent.stop()
        gw.stop()


def test_agent_reports_tracked_leases_on_startup(tmp_path):
    """On restart, already-tracked leases are re-heartbeated (not re-adopted),
    so the agent must report them to the control plane at construction time."""
    import json as _json
    gw = _FakeGateway()
    cp = _FakeControlPlane()
    store = tmp_path / "fabric-agent.json"
    store.write_text(_json.dumps({
        "8181": {"lease_id": "lease_a", "label": "web on bender (port 8181)",
                 "external_port": 18000, "url": "http://dev.lan:18000",
                 "process": "node", "last_hb": 0.0},
        "3123": {"lease_id": "lease_b", "label": "api on bender (port 3123)",
                 "external_port": 18001, "url": "http://dev.lan:18001",
                 "process": "python", "last_hb": 0.0},
    }))
    agent = _agent(tmp_path, gw, control_plane_url=cp.url)
    try:
        with cp._lock:
            regs = [e for e in cp.envelopes
                    if e["event_type"] == "service.registered"]
        assert {e["payload"]["service_name"] for e in regs} == {
            "web on bender (port 8181)", "api on bender (port 3123)"}
    finally:
        agent.stop()
        gw.stop()
        cp.stop()

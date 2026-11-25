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

import pytest

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


def test_unchanged_listener_is_not_http_probed_twice(tmp_path, monkeypatch):
    gw = _FakeGateway()
    calls = []
    monkeypatch.setattr(fa, "probe_web",
                        lambda port, timeout=2.0: calls.append(port) or True)
    agent = _agent(tmp_path, gw)
    info = {"addrs": {"127.0.0.1"}, "process": "testapp", "pid": 1}
    try:
        assert agent._probe_web_cached(3210, info) is True
        assert agent._probe_web_cached(3210, info) is True
        assert calls == [3210]
    finally:
        agent.stop()
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
            agent2.scan_once()   # the identity probe passes, then it is restored
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
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not exited:
            time.sleep(0.05)
        # The stub returns, so the stall is still there on the next poll and the watchdog calls it again: assert the
        # code, not the count. And always stop the thread: if it outlived the test it would call the REAL os._exit
        # once monkeypatch restores it, and kill the whole pytest run.
        assert exited and set(exited) == {1}  # watchdog detected the stall and exited
    finally:
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
        kwargs={"cycle_timeout": 2.0, "poll": 0.05},  # 20x the 0.1 s interval: a CPU-starved CI node stalled one cycle past the old 0.5 s
        daemon=True).start()
    try:
        time.sleep(2.6)  # longer than cycle_timeout, so only a refreshed cycle clock keeps the watchdog quiet
        assert exited == []
    finally:
        stop.set()  # a failed assert must not leave the watchdog running past monkeypatch's restore of os._exit
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


def test_unleased_listener_is_surfaced_once_and_cleared(tmp_path, monkeypatch, caplog):
    gw = _FakeGateway()
    raw, raw_port = _raw_tcp_listener()   # accepts, speaks no HTTP -> never leased
    live = {"on": True}
    monkeypatch.setattr(fa, "scan_listening_ports",
                        lambda: _listeners({raw_port: "thing"} if live["on"] else {}))
    agent = _agent(tmp_path, gw)
    try:
        with caplog.at_level("WARNING", logger="herdr_engineering.fabric_agent"):
            first = agent.scan_once()
            second = agent.scan_once()
        assert first["unleased"] == [str(raw_port)] == second["unleased"]
        warned = [r for r in caplog.records if "no fabric lease" in r.getMessage()]
        assert len(warned) == 1                      # once per port, not per cycle
        live["on"] = False
        assert agent.scan_once()["unleased"] == []
        live["on"] = True
        with caplog.at_level("WARNING", logger="herdr_engineering.fabric_agent"):
            agent.scan_once()
        assert len([r for r in caplog.records if "no fabric lease" in r.getMessage()]) == 2
    finally:
        raw.close()
        agent.stop()
        gw.stop()


def test_leased_listener_is_not_reported_unleased(tmp_path, monkeypatch):
    gw = _FakeGateway()
    server, port = _http_target(tmp_path)
    monkeypatch.setattr(fa, "scan_listening_ports", lambda: _listeners({port: "app"}))
    agent = _agent(tmp_path, gw)
    try:
        assert agent.scan_once()["unleased"] == []
    finally:
        server.shutdown()
        agent.stop()
        gw.stop()


# ---------------------------------------------------------------------------
# Host rewriting for loopback apps (the app's own rebinding guard must accept
# the request without anyone configuring the fabric's names into the app)
# ---------------------------------------------------------------------------

def _echo_host_server():
    import socket as _s
    import threading as _t
    srv = _s.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    seen: list[bytes] = []

    def _serve():
        while True:
            try:
                c, _ = srv.accept()
            except OSError:
                return
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = c.recv(4096)
                if not chunk:
                    break
                data += chunk
            seen.append(data)
            c.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n"
                      b"Connection: close\r\n\r\nok")
            c.close()

    _t.Thread(target=_serve, daemon=True).start()
    return srv, seen


def _through_forwarder(names, request: bytes):
    import socket as _s
    srv, seen = _echo_host_server()
    fwd = AgentForwarder("127.0.0.1", srv.getsockname()[1],
                         fabric_names=frozenset(names)).start()
    try:
        c = _s.create_connection(("127.0.0.1", fwd.port), timeout=5)
        c.sendall(request)
        reply = b""
        while True:
            chunk = c.recv(4096)
            if not chunk:
                break
            reply += chunk
        c.close()
        return seen[0], reply, srv.getsockname()[1]
    finally:
        fwd.stop()
        srv.close()


def test_a_fabric_host_reaches_a_loopback_app_as_its_own_address():
    got, reply, port = _through_forwarder(
        {"dev.lan"}, b"GET / HTTP/1.1\r\nHost: DEV.lan:9990\r\n"
                     b"Connection: keep-alive\r\nAccept: */*\r\n\r\n")
    assert f"Host: 127.0.0.1:{port}".encode() in got
    assert b"dev.lan" not in got.lower()
    assert got.lower().count(b"connection:") == 1
    assert b"Connection: close" in got
    assert reply.endswith(b"ok")


def test_a_foreign_host_is_forwarded_untouched_so_the_app_can_refuse_it():
    got, _reply, _port = _through_forwarder(
        {"dev.lan"}, b"GET / HTTP/1.1\r\nHost: evil.example\r\n\r\n")
    assert b"Host: evil.example" in got


def test_a_websocket_upgrade_is_not_rewritten():
    got, _r, _p = _through_forwarder(
        {"dev.lan"}, b"GET /ws HTTP/1.1\r\nHost: dev.lan\r\n"
                     b"Upgrade: websocket\r\nConnection: Upgrade\r\n\r\n")
    assert b"Host: dev.lan" in got


def test_without_fabric_names_nothing_is_rewritten():
    got, _r, _p = _through_forwarder(
        set(), b"GET / HTTP/1.1\r\nHost: dev.lan\r\n\r\n")
    assert b"Host: dev.lan" in got


def test_the_agent_config_names_its_control_plane_and_tailnet_ip():
    from pathlib import Path
    cfg = AgentConfig(tailnet_ip="100.1.2.3", store_path=Path("/tmp/x"),
                      control_plane_url="http://Dev.LAN")
    assert cfg.fabric_names() == frozenset({"100.1.2.3", "dev.lan"})


def test_retarget_moves_the_fabric_names_of_live_forwarders(tmp_path):
    # Central fleet config: follow_config() retargets the agent when a refresh changes the
    # control-plane URL. Forwarders already running must rewrite for the NEW host, not the old.
    gw = _FakeGateway()
    agent = _agent(tmp_path, gw, control_plane_url="http://old.lan")
    fwd = fa.AgentForwarder("127.0.0.1", 1, fabric_names=agent.cfg.fabric_names())
    agent._forwards[1] = fwd
    try:
        assert "old.lan" in fwd.fabric_names
        agent.retarget(FabricClient(gw.url), "http://New.LAN:8080/")
        assert fwd.fabric_names == frozenset({"127.0.0.1", "new.lan"})
        assert agent.cfg.fabric_names() == fwd.fabric_names
    finally:
        gw.stop()


def _untouched(request: bytes) -> bool:
    got, _r, _p = _through_forwarder({"dev.lan"}, request)
    return b"dev.lan" in got.lower() and b"127.0.0.1:" not in got.split(b"\r\n")[1]


def test_only_a_plain_get_is_rewritten():
    # A state-changing method keeps the fabric Host, so the app's own guard
    # still refuses a cross-site POST.
    assert _untouched(b"POST /x HTTP/1.1\r\nHost: dev.lan\r\n\r\n")


def test_pipelined_bytes_after_the_head_stop_the_rewrite():
    assert _untouched(b"GET /a HTTP/1.1\r\nHost: dev.lan\r\n\r\n"
                      b"GET /b HTTP/1.1\r\nHost: dev.lan\r\n\r\n")


def test_ambiguous_heads_are_forwarded_as_received():
    for req in (
        b"GET / HTTP/1.1\r\nHost: dev.lan\r\nHost: dev.lan\r\n\r\n",
        b"GET / HTTP/1.1\r\nX-A: 1\r\n Host: dev.lan\r\n\r\n",
        b"GET / HTTP/1.1\r\nHost : dev.lan\r\n\r\n",
        b"GET / HTTP/1.1\r\nHost: dev.lan\r\nX: a\nHost: evil\r\n\r\n",
        b"GET / HTTP/1.1\r\nHost: dev.lan@evil.example\r\n\r\n",
    ):
        got, _r, _p = _through_forwarder({"dev.lan"}, req)
        assert got.split(b"\r\n\r\n")[0] == req.split(b"\r\n\r\n")[0], req


def test_a_trailing_dot_fabric_name_is_still_a_fabric_name():
    got, _r, port = _through_forwarder(
        {"dev.lan"}, b"GET / HTTP/1.1\r\nHost: dev.lan.:9990\r\n\r\n")
    assert f"Host: 127.0.0.1:{port}".encode() in got


def test_a_client_that_never_speaks_does_not_dial_the_app():
    import socket as _s
    srv, seen = _echo_host_server()
    fwd = AgentForwarder("127.0.0.1", srv.getsockname()[1],
                         fabric_names=frozenset({"dev.lan"}))
    fwd._HEAD_DEADLINE = 0.3  # noqa: SLF001
    fwd.start()
    try:
        c = _s.create_connection(("127.0.0.1", fwd.port), timeout=5)
        time.sleep(0.1)
        assert seen == []          # nothing dialled yet
        c.close()
    finally:
        fwd.stop()
        srv.close()


def test_connections_beyond_the_cap_are_dropped():
    import socket as _s
    srv, _seen = _echo_host_server()
    fwd = AgentForwarder("127.0.0.1", srv.getsockname()[1],
                         fabric_names=frozenset({"dev.lan"}))
    fwd.MAX_CONNECTIONS = 1
    fwd._slots = threading.BoundedSemaphore(1)  # noqa: SLF001
    fwd.start()
    try:
        held = _s.create_connection(("127.0.0.1", fwd.port), timeout=5)
        time.sleep(0.2)            # the first holds the only slot
        second = _s.create_connection(("127.0.0.1", fwd.port), timeout=5)
        second.settimeout(3)
        assert second.recv(10) == b""   # closed unanswered
        held.close()
    finally:
        fwd.stop()
        srv.close()


# --- SigV4: the signed Host must reach the backend untouched ----------------

_SIGV4_AUTH = (b"Authorization: AWS4-HMAC-SHA256 Credential=AKID/20261104/us-east-1/s3/"
               b"aws4_request, SignedHeaders=host;x-amz-date, Signature=abcd\r\n")


def test_a_sigv4_authorization_header_keeps_its_signed_host():
    got, _reply, _port = _through_forwarder(
        {"100.64.8.6"}, b"GET /b?list-type=2 HTTP/1.1\r\nHost: 100.64.8.6:9000\r\n"
                          + _SIGV4_AUTH + b"X-Amz-Date: 20261104T000000Z\r\n\r\n")
    assert b"Host: 100.64.8.6:9000\r\n" in got
    assert b"127.0.0.1" not in got


def test_a_sigv4_query_string_keeps_its_signed_host():
    for q in (b"X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Date=1", b"a=1&X-Amz-Signature=ff"):
        got, _reply, _port = _through_forwarder(
            {"100.64.8.6"}, b"GET /b/k?" + q + b" HTTP/1.1\r\n"
                              b"Host: 100.64.8.6:9000\r\n\r\n")
        assert b"Host: 100.64.8.6:9000\r\n" in got
        assert b"Connection: close" not in got


def test_an_unsigned_request_is_still_rewritten_byte_for_byte():
    got, _reply, port = _through_forwarder(
        {"100.64.8.6"}, b"GET /?x=1 HTTP/1.1\r\nHost: 100.64.8.6:9000\r\n"
                          b"Accept: */*\r\n\r\n")
    assert got == (b"GET /?x=1 HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nAccept: */*\r\n"
                   b"Connection: close\r\n\r\n" % port)


def test_a_non_sigv4_authorization_is_still_rewritten():
    got, _reply, port = _through_forwarder(
        {"dev.lan"}, b"GET / HTTP/1.1\r\nHost: dev.lan\r\n"
                     b"Authorization: Bearer abc\r\n\r\n")
    assert f"Host: 127.0.0.1:{port}".encode() in got


def test_a_signed_request_with_two_hosts_is_forwarded_as_received():
    req = (b"GET / HTTP/1.1\r\nHost: 100.64.8.6\r\nHost: evil\r\n" + _SIGV4_AUTH + b"\r\n")
    got, _reply, _port = _through_forwarder({"100.64.8.6"}, req)
    assert got == req


def test_signed_chunked_put_and_keepalive_framing_survive():
    import socket as _s
    import threading as _t
    srv = _s.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)
    seen: list[bytes] = []

    def _serve():
        c, _ = srv.accept()
        data = b""
        # Two requests on one connection: a chunked PUT, then a GET.
        while not data.endswith(b"\r\n\r\n") or b"GET /two" not in data:
            chunk = c.recv(4096)
            if not chunk:
                break
            data += chunk
            if b"0\r\n\r\n" in data and b"PUT" in data and b"GET /two" not in data:
                c.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
        seen.append(data)
        c.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
        c.close()

    _t.Thread(target=_serve, daemon=True).start()
    fwd = AgentForwarder("127.0.0.1", srv.getsockname()[1],
                         fabric_names=frozenset({"100.64.8.6"})).start()
    try:
        c = _s.create_connection(("127.0.0.1", fwd.port), timeout=5)
        put = (b"PUT /b/k HTTP/1.1\r\nHost: 100.64.8.6:9000\r\n" + _SIGV4_AUTH +
               b"Transfer-Encoding: chunked\r\n\r\n5\r\nhello\r\n0\r\n\r\n")
        get = b"GET /two HTTP/1.1\r\nHost: 100.64.8.6:9000\r\n" + _SIGV4_AUTH + b"\r\n"
        c.sendall(put)
        assert c.recv(4096).startswith(b"HTTP/1.1 200")  # connection still open
        c.sendall(get)
        reply = b""
        while True:
            chunk = c.recv(4096)
            if not chunk:
                break
            reply += chunk
        c.close()
        assert seen[0] == put + get
        assert reply.endswith(b"ok")
    finally:
        fwd.stop()
        srv.close()


# -- lease flood: ephemeral test servers and orphaned forwarders ------------

def _spawn_listener(tmp_path, name: str):
    """A real HTTP listener; the scanner is fed ``name`` as its process (a real
    process title is not portable)."""
    return _http_target(tmp_path)


def test_default_process_denylist_names_test_servers():
    names = fa.DEFAULT_DENY_PROCESSES
    for needed in ("vitest", "jest", "playwright", "vite", "next-server"):
        assert needed in names
    assert fa.process_denied("node (vitest 12", names)
    assert fa.process_denied("next-server (v14.2.3)", names)
    assert fa.process_denied("Playwright", names)
    assert not fa.process_denied("node", names)
    assert not fa.process_denied(None, names)
    assert not fa.process_denied("uvicorn", names)


def test_scan_does_not_lease_denylisted_test_server(tmp_path, monkeypatch):
    gw = _FakeGateway()
    server, port = _spawn_listener(tmp_path, "node (vitest 3")
    monkeypatch.setattr(fa, "scan_listening_ports",
                        lambda: _listeners({port: "node (vitest 3"}))
    agent = _agent(tmp_path, gw)
    try:
        agent.scan_once()
        assert gw.leases == {}
        assert str(port) not in _store(tmp_path)
    finally:
        server.shutdown()
        agent.stop()
        gw.stop()


def test_denylist_is_configurable(tmp_path, monkeypatch):
    gw = _FakeGateway()
    server, port = _spawn_listener(tmp_path, "myrunner")
    monkeypatch.setattr(fa, "scan_listening_ports",
                        lambda: _listeners({port: "myrunner"}))
    agent = _agent(tmp_path, gw, deny_processes=("myrunner",))
    try:
        agent.scan_once()
        assert gw.leases == {}
    finally:
        server.shutdown()
        agent.stop()
        gw.stop()


def test_scan_releases_existing_denylisted_lease(tmp_path, monkeypatch):
    import json as _json
    gw = _FakeGateway()
    server, port = _http_target(tmp_path)
    (tmp_path / "fabric-agent.json").write_text(_json.dumps({
        str(port): {"lease_id": "lease_old", "label": f"x (port {port})",
                    "external_port": 18001, "url": "http://dev.lan:18001",
                    "process": "node (vitest 1", "last_hb": 0.0},
    }))
    monkeypatch.setattr(fa, "scan_listening_ports",
                        lambda: _listeners({port: "node (vitest 1"}))
    agent = _agent(tmp_path, gw)
    try:
        agent.scan_once()
        assert "lease_old" in gw.closed
        assert str(port) not in _store(tmp_path)
    finally:
        server.shutdown()
        agent.stop()
        gw.stop()


def test_scan_releases_forwarded_lease_when_origin_is_gone(tmp_path, monkeypatch):
    """The leak: the agent's own same-port forwarder keeps the port listening
    after the loopback app dies, so 'port not in listeners' never fires. The
    lease must be released once the ORIGIN stops answering."""
    gw = _FakeGateway()
    server, port = _http_target(tmp_path)  # loopback-only app
    # The scan keeps reporting the port: in production the agent's own forwarder
    # (bound on the tailnet IP) holds it open.
    monkeypatch.setattr(fa, "scan_listening_ports",
                        lambda: _listeners({port: "node"}))
    # 127.0.0.2 stands in for the tailnet IP: the app on 127.0.0.1 is not
    # reachable there, so the agent puts a forwarder in front of it.
    agent = _agent(tmp_path, gw, tailnet_ip="127.0.0.2", orphan_grace=0.0)
    try:
        agent.scan_once()
        entry = _store(tmp_path)[str(port)]
        assert entry["fwd_port"] is not None
        agent.scan_once()  # origin alive: lease kept
        assert str(port) in _store(tmp_path)
        server.shutdown()
        server.server_close()
        agent.scan_once()
        assert str(port) not in _store(tmp_path)
        assert entry["lease_id"] in gw.closed
    finally:
        agent.stop()
        gw.stop()


def test_orphan_grace_delays_release(tmp_path, monkeypatch):
    gw = _FakeGateway()
    server, port = _http_target(tmp_path)
    monkeypatch.setattr(fa, "scan_listening_ports",
                        lambda: _listeners({port: "node"}))
    agent = _agent(tmp_path, gw, tailnet_ip="127.0.0.2", orphan_grace=3600.0)
    try:
        agent.scan_once()
        server.shutdown()
        server.server_close()
        agent.scan_once()
        assert str(port) in _store(tmp_path)  # within grace: kept
    finally:
        agent.stop()
        gw.stop()


# -- watcher lease max age (backstop) ---------------------------------------

def _seed_store(tmp_path, port: int, registered_at: float | None) -> None:
    entry = {"lease_id": "lease_old", "label": f"x (port {port})",
             "external_port": 18001, "url": "http://dev.lan:18001",
             "process": "node", "last_hb": 0.0}
    if registered_at is not None:
        entry["registered_at"] = registered_at
    (tmp_path / "fabric-agent.json").write_text(json.dumps({str(port): entry}))


def test_registered_lease_records_registration_time(tmp_path, monkeypatch):
    gw = _FakeGateway()
    server, port = _http_target(tmp_path)
    monkeypatch.setattr(fa, "scan_listening_ports",
                        lambda: _listeners({port: "node"}))
    agent = _agent(tmp_path, gw)
    try:
        before = time.time()
        agent.scan_once()
        assert before <= _store(tmp_path)[str(port)]["registered_at"] <= time.time()
    finally:
        server.shutdown()
        agent.stop()
        gw.stop()


def test_lease_past_max_age_is_refreshed_while_app_is_live(tmp_path, monkeypatch):
    """Backstop: an over-age watcher lease is released; a genuinely live app is
    simply re-registered by the same scan (new lease), not banned."""
    gw = _FakeGateway()
    server, port = _http_target(tmp_path)
    _seed_store(tmp_path, port, registered_at=time.time() - 7200)
    monkeypatch.setattr(fa, "scan_listening_ports",
                        lambda: _listeners({port: "node"}))
    agent = _agent(tmp_path, gw, lease_max_age=3600.0)
    try:
        agent.scan_once()
        assert "lease_old" in gw.closed
        entry = _store(tmp_path)[str(port)]
        assert entry["lease_id"] != "lease_old"
        assert time.time() - entry["registered_at"] < 60
    finally:
        server.shutdown()
        agent.stop()
        gw.stop()


def test_lease_past_max_age_stays_released_when_app_is_gone(tmp_path, monkeypatch):
    gw = _FakeGateway()
    _seed_store(tmp_path, 59999, registered_at=time.time() - 7200)
    monkeypatch.setattr(fa, "scan_listening_ports", lambda: _listeners({}))
    agent = _agent(tmp_path, gw, lease_max_age=3600.0)
    try:
        agent.scan_once()
        assert "lease_old" in gw.closed
        assert _store(tmp_path) == {}
    finally:
        agent.stop()
        gw.stop()


def test_lease_within_max_age_is_kept(tmp_path, monkeypatch):
    gw = _FakeGateway()
    server, port = _http_target(tmp_path)
    _seed_store(tmp_path, port, registered_at=time.time() - 10)
    monkeypatch.setattr(fa, "scan_listening_ports",
                        lambda: _listeners({port: "node"}))
    agent = _agent(tmp_path, gw, lease_max_age=3600.0)
    try:
        agent.scan_once()
        assert "lease_old" not in gw.closed
        assert _store(tmp_path)[str(port)]["lease_id"] == "lease_old"
    finally:
        server.shutdown()
        agent.stop()
        gw.stop()


def test_max_age_zero_disables_the_backstop(tmp_path, monkeypatch):
    gw = _FakeGateway()
    server, port = _http_target(tmp_path)
    _seed_store(tmp_path, port, registered_at=1.0)
    monkeypatch.setattr(fa, "scan_listening_ports",
                        lambda: _listeners({port: "node"}))
    agent = _agent(tmp_path, gw, lease_max_age=0.0)
    try:
        agent.scan_once()
        assert "lease_old" not in gw.closed
    finally:
        server.shutdown()
        agent.stop()
        gw.stop()


def test_legacy_entry_without_registration_time_is_stamped_not_released(
        tmp_path, monkeypatch):
    gw = _FakeGateway()
    server, port = _http_target(tmp_path)
    _seed_store(tmp_path, port, registered_at=None)
    monkeypatch.setattr(fa, "scan_listening_ports",
                        lambda: _listeners({port: "node"}))
    agent = _agent(tmp_path, gw, lease_max_age=3600.0)
    try:
        agent.scan_once()
        assert "lease_old" not in gw.closed
        assert _store(tmp_path)[str(port)]["registered_at"] > 0
    finally:
        server.shutdown()
        agent.stop()
        gw.stop()


# -- persisted heartbeat clock across a reboot -------------------------------

def test_lease_loaded_after_reboot_is_heartbeated_on_first_cycle(tmp_path, monkeypatch):
    """``last_hb`` is a ``time.monotonic()`` reading. A host reboot resets the
    monotonic clock, so a persisted value can sit far in the future: the lease
    was then never "due" again and the gateway's copy expired for good. A
    loaded lease must be heartbeated (and re-registered when gone) at once."""
    gw = _FakeGateway()
    server, port = _http_target(tmp_path)
    entry = {"lease_id": "lease_from_before_reboot", "label": f"x (port {port})",
             "external_port": 18001, "url": "http://dev.lan:18001",
             "target_host": "127.0.0.1", "target_port": port,
             "process": "node", "last_hb": time.monotonic() + 1_000_000.0,
             "registered_at": time.time()}
    (tmp_path / "fabric-agent.json").write_text(json.dumps({str(port): entry}))
    monkeypatch.setattr(fa, "scan_listening_ports",
                        lambda: _listeners({port: "node"}))
    agent = _agent(tmp_path, gw, heartbeat_ttl=90.0)
    try:
        agent.scan_once()
        stored = _store(tmp_path)[str(port)]
        assert stored["lease_id"] in gw.leases  # the gateway holds it again
    finally:
        server.shutdown()
        agent.stop()
        gw.stop()


def test_forwarder_stop_releases_port_immediately():
    """stop() must free the listening port at once, not after the accept
    loop's poll timeout: close() alone leaves the socket listening while the
    accept thread is blocked inside accept(), so an agent restarting right
    after a stop() could not rebind the remembered forward port (flaky
    test_forwarder_restored_across_restart_stays_live under load)."""
    import socket
    import time

    for _ in range(5):
        fwd = fa.AgentForwarder("127.0.0.1", 9).start()
        port = fwd.port
        time.sleep(0.05)  # let the accept thread block in accept()
        fwd.stop()
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(("127.0.0.1", port))
            probe.listen(1)  # fails with EADDRINUSE while the old listener lives
        finally:
            probe.close()


# ---------------------------------------------------------------------------
# Debug / admin endpoints are never adopted (SECURITY.md: CDP/debug endpoints)
# ---------------------------------------------------------------------------

def _json_server(routes: dict[str, object], server_header: str = "",
                 hits: list[str] | None = None, stall: threading.Event | None = None):
    """A real loopback HTTP server answering GET <path> with JSON from routes.

    ``hits`` records every request path; the probe paths wait while ``stall``
    is set, so a probe against it times out.
    """
    class Handler(http.server.BaseHTTPRequestHandler):
        def version_string(self):
            return server_header or super().version_string()

        def do_GET(self):  # noqa: N802
            path = self.path.split("?")[0]
            if hits is not None:
                hits.append(path)
            if stall is not None and stall.is_set() and path != "/":
                time.sleep(1.0)
            body = routes.get(path)
            if body is None:
                self.send_response(404)
                self.end_headers()
                return
            data = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


def _cdp_server():
    return _json_server({
        "/json/version": {
            "Browser": "Chrome/130.0",
            "Protocol-Version": "1.3",
            "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/browser/abc",
        },
        "/": {"ok": True},
    })


def _node_inspector_server():
    return _json_server({"/json/version": {"Browser": "node.js/v20.1.0", "Protocol-Version": "1.1"},
                         "/": {"ok": True}})


def _jupyter_server():
    return _json_server({"/api": {"version": "2.14.0"}, "/": {"ok": True}},
                        server_header="TornadoServer/6.4")


def _jupyter_status_server():
    # No telling Server header, but /api/status has Jupyter's own keys.
    return _json_server({"/api": {"version": "2.14.0"}, "/": {"ok": True},
                         "/api/status": {"started": "t", "last_activity": "t",
                                         "connections": 0, "kernels": 0}})


def _version_only_app():
    # An ordinary app whose /api returns just a version: not Jupyter.
    return _json_server({"/api": {"version": "1.0"}, "/": {"ok": True}})


def _ordinary_app():
    # Has /api and /json/version too, but they are not a debugger's answers.
    return _json_server({"/api": {"version": "1", "name": "shop", "status": "up"},
                         "/json/version": {"build": "7"}, "/": {"ok": True}})


def _leased_ports(gw) -> list[int]:
    return [lease["target"]["port"] for lease in gw.leases.values()]


def test_probe_debug_surface_names_each_debugger():
    cases = [(_cdp_server, "Chrome DevTools"), (_node_inspector_server, "Node inspector"),
             (_jupyter_server, "Jupyter"), (_jupyter_status_server, "Jupyter")]
    for make, needle in cases:
        server, port = make()
        try:
            reason = fa.probe_debug_surface(port, timeout=0.5)
            assert reason is not None and needle in reason
        finally:
            server.shutdown()


def test_probe_debug_surface_passes_ordinary_apps(tmp_path):
    for make in (_ordinary_app, _version_only_app, lambda: _http_target(tmp_path)):
        server, port = make()
        try:
            assert fa.probe_debug_surface(port, timeout=0.5) is None
        finally:
            server.shutdown()


def test_jupyter_reason_says_it_is_a_heuristic():
    server, port = _jupyter_server()
    try:
        assert "heuristic" in fa.probe_debug_surface(port, timeout=0.5)
    finally:
        server.shutdown()


def test_probe_debug_surface_is_inconclusive_when_nothing_answers():
    raw, raw_port = _raw_tcp_listener()   # accepts, never speaks: times out
    try:
        with pytest.raises(fa.ProbeInconclusive):
            fa.probe_debug_surface(raw_port, timeout=0.3)
    finally:
        raw.close()
    with pytest.raises(fa.ProbeInconclusive):
        fa.probe_debug_surface(_free_port(), timeout=0.3)


def test_probe_debug_surface_treats_any_live_answer_as_not_a_debugger():
    banner = _banner_listener(b"SSH-2.0-x\r\n")[0]
    try:
        assert fa.probe_debug_surface(banner.getsockname()[1], timeout=0.3) is None
    finally:
        banner.close()


def test_a_timed_out_probe_is_retried_not_trusted_or_cached(tmp_path, monkeypatch, caplog):
    gw = _FakeGateway()
    stall = threading.Event()
    stall.set()
    server, port = _json_server({"/": {"ok": True}}, stall=stall)
    monkeypatch.setattr(fa, "scan_listening_ports", lambda: _listeners({port: "app"}))
    agent = _agent(tmp_path, gw)
    try:
        with caplog.at_level("INFO", logger="herdr_engineering.fabric_agent"):
            agent.scan_once()
            agent.scan_once()
        assert _leased_ports(gw) == []
        assert port not in agent._debug_cache
        assert len([r for r in caplog.records if f"port {port}" in r.getMessage()
                    and "could not identify" in r.getMessage()]) == 1
        stall.clear()
        agent.scan_once()
        assert _leased_ports(gw) == [port]
    finally:
        server.shutdown()
        agent.stop()
        gw.stop()


def test_a_404_on_the_probe_paths_is_an_answer_so_the_port_is_adopted(tmp_path, monkeypatch):
    gw = _FakeGateway()
    server, port = _json_server({"/": {"ok": True}})   # 404 on /json/version and /api
    monkeypatch.setattr(fa, "scan_listening_ports", lambda: _listeners({port: "app"}))
    agent = _agent(tmp_path, gw)
    try:
        agent.scan_once()
        assert _leased_ports(gw) == [port]
    finally:
        server.shutdown()
        agent.stop()
        gw.stop()


def test_ordinary_api_version_endpoint_is_not_refused(tmp_path, monkeypatch):
    gw = _FakeGateway()
    server, port = _version_only_app()
    monkeypatch.setattr(fa, "scan_listening_ports", lambda: _listeners({port: "app"}))
    agent = _agent(tmp_path, gw)
    try:
        agent.scan_once()
        assert _leased_ports(gw) == [port]
    finally:
        server.shutdown()
        agent.stop()
        gw.stop()


def test_ports_that_were_never_candidates_are_not_logged_as_refused(tmp_path, monkeypatch, caplog):
    gw = _FakeGateway()
    ephemeral, eph_port = _http_target(tmp_path)
    excluded, exc_port = _http_target(tmp_path)
    runner, runner_port = _http_target(tmp_path)
    monkeypatch.setattr(fa, "scan_listening_ports", lambda: _listeners(
        {eph_port: "chrome", exc_port: "thing", runner_port: "node (vitest 3"}))
    agent = _agent(tmp_path, gw, adopt_mode="allowlist", ephemeral_min=1,
                   exclude_ports=frozenset({gw.port, exc_port}))
    try:
        with caplog.at_level("INFO", logger="herdr_engineering.fabric_agent"):
            agent.scan_once()
        assert not [r for r in caplog.records if "refusing" in r.getMessage()]
    finally:
        for s in (ephemeral, excluded, runner):
            s.shutdown()
        agent.stop()
        gw.stop()


def test_test_runner_ports_get_no_identity_probe_and_the_cache_is_pruned(tmp_path, monkeypatch):
    gw = _FakeGateway()
    hits: list[str] = []
    runner, runner_port = _json_server({"/": {"ok": True}}, hits=hits)
    app, app_port = _json_server({"/": {"ok": True}})
    live = {runner_port: "node (vitest 3", app_port: "shop"}
    monkeypatch.setattr(fa, "scan_listening_ports", lambda: _listeners(live))
    agent = _agent(tmp_path, gw)
    try:
        agent.scan_once()
        assert hits == []
        assert app_port in agent._debug_cache
        live.pop(app_port)
        agent.scan_once()
        assert agent._debug_cache == {}
    finally:
        runner.shutdown()
        app.shutdown()
        agent.stop()
        gw.stop()


def test_a_stale_refusal_is_cleared_when_the_port_is_passed_to_adoption(tmp_path, monkeypatch):
    gw = _FakeGateway()
    server, port = _http_target(tmp_path)
    monkeypatch.setattr(fa, "scan_listening_ports", lambda: _listeners({port: "app"}))
    agent = _agent(tmp_path, gw, adopt_mode="allowlist")
    try:
        agent.scan_once()
        assert port in agent._refused
        agent.cfg = fa.replace(agent.cfg, allow_ports=frozenset({port}))
        agent.scan_once()
        assert _leased_ports(gw) == [port]
        assert port not in agent._refused
    finally:
        server.shutdown()
        agent.stop()
        gw.stop()


def test_an_explicitly_allowed_process_overrides_the_default_process_denylist(tmp_path, monkeypatch):
    gw = _FakeGateway()
    vite, vite_port = _http_target(tmp_path)
    jest, jest_port = _http_target(tmp_path)
    cdp, cdp_port = _cdp_server()
    monkeypatch.setattr(fa, "scan_listening_ports", lambda: _listeners(
        {vite_port: "node (vite 5", jest_port: "node (jest 29", cdp_port: "vite"}))
    agent = _agent(tmp_path, gw, allow_processes=("vite",),
                   deny_ports=frozenset())
    try:
        agent.scan_once()
        assert _leased_ports(gw) == [vite_port]   # jest stays denied; a debugger stays refused
    finally:
        for s in (vite, jest, cdp):
            s.shutdown()
        agent.stop()
        gw.stop()


def test_allowed_process_never_overrides_the_port_denylist(tmp_path, monkeypatch):
    gw = _FakeGateway()
    vite, vite_port = _http_target(tmp_path)
    monkeypatch.setattr(fa, "scan_listening_ports", lambda: _listeners({vite_port: "vite"}))
    agent = _agent(tmp_path, gw, allow_processes=("vite",), deny_ports=frozenset({vite_port}))
    try:
        agent.scan_once()
        assert _leased_ports(gw) == []
    finally:
        vite.shutdown()
        agent.stop()
        gw.stop()


def test_default_deny_ports_cover_debuggers_databases_and_consoles():
    for port in (9222, 9229, 5858, 2375, 2376, 5601, 15672, 8500, 8200, 2379, 9515, 4444):
        assert port in fa.DEFAULT_DENY_PORTS, port
    assert 8888 not in fa.DEFAULT_DENY_PORTS  # not unambiguous; Jupyter is caught by its probe


def test_scan_never_adopts_debug_endpoints_and_logs_why(tmp_path, monkeypatch, caplog):
    gw = _FakeGateway()
    cdp, cdp_port = _cdp_server()
    node, node_port = _node_inspector_server()
    jup, jup_port = _jupyter_server()
    app, app_port = _ordinary_app()
    monkeypatch.setattr(fa, "scan_listening_ports", lambda: _listeners(
        {cdp_port: "chrome", node_port: "node", jup_port: "jupyter-lab", app_port: "shop"}))
    agent = _agent(tmp_path, gw)
    try:
        with caplog.at_level("INFO", logger="herdr_engineering.fabric_agent"):
            agent.scan_once()
            agent.scan_once()
        assert _leased_ports(gw) == [app_port]
        for port, needle in ((cdp_port, "Chrome DevTools"), (node_port, "Node inspector"),
                             (jup_port, "Jupyter")):
            lines = [r.getMessage() for r in caplog.records
                     if f"port {port}" in r.getMessage() and "refus" in r.getMessage()]
            assert len(lines) == 1 and needle in lines[0], lines   # once, naming the reason
        assert not any("no fabric lease" in r.getMessage() for r in caplog.records)
    finally:
        for s in (cdp, node, jup, app):
            s.shutdown()
        agent.stop()
        gw.stop()


def test_denylisted_port_is_never_a_candidate_so_it_is_silent(tmp_path, monkeypatch, caplog):
    gw = _FakeGateway()
    server, port = _http_target(tmp_path)
    monkeypatch.setattr(fa, "scan_listening_ports", lambda: _listeners({port: "thing"}))
    agent = _agent(tmp_path, gw, deny_ports=frozenset({port}))
    try:
        with caplog.at_level("INFO", logger="herdr_engineering.fabric_agent"):
            agent.scan_once()
        assert _leased_ports(gw) == []
        assert not [r for r in caplog.records if "refusing" in r.getMessage()]
        with caplog.at_level("INFO", logger="herdr_engineering.fabric_agent"):
            assert agent._adopt(port, {"addrs": {"127.0.0.1"}, "process": "thing"}) is None
        assert any(f"port {port}" in r.getMessage() and "denylist" in r.getMessage()
                   for r in caplog.records)
    finally:
        server.shutdown()
        agent.stop()
        gw.stop()


def test_allowlist_mode_adopts_only_listed_ports_or_processes(tmp_path, monkeypatch):
    gw = _FakeGateway()
    a, a_port = _http_target(tmp_path)
    b, b_port = _http_target(tmp_path)
    c, c_port = _http_target(tmp_path)
    monkeypatch.setattr(fa, "scan_listening_ports", lambda: _listeners(
        {a_port: "alpha", b_port: "beta", c_port: "gamma"}))
    agent = _agent(tmp_path, gw, adopt_mode="allowlist",
                   allow_ports=frozenset({a_port}), allow_processes=("beta",))
    try:
        agent.scan_once()
        assert sorted(_leased_ports(gw)) == sorted([a_port, b_port])
    finally:
        for s in (a, b, c):
            s.shutdown()
        agent.stop()
        gw.stop()


def test_allowlist_mode_with_nothing_listed_adopts_nothing(tmp_path, monkeypatch):
    gw = _FakeGateway()
    server, port = _http_target(tmp_path)
    monkeypatch.setattr(fa, "scan_listening_ports", lambda: _listeners({port: "app"}))
    agent = _agent(tmp_path, gw, adopt_mode="allowlist")
    try:
        agent.scan_once()
        assert _leased_ports(gw) == []
    finally:
        server.shutdown()
        agent.stop()
        gw.stop()


def test_allowlisted_debugger_is_still_refused(tmp_path, monkeypatch):
    gw = _FakeGateway()
    cdp, cdp_port = _cdp_server()
    monkeypatch.setattr(fa, "scan_listening_ports", lambda: _listeners({cdp_port: "chrome"}))
    agent = _agent(tmp_path, gw, adopt_mode="allowlist", allow_ports=frozenset({cdp_port}))
    try:
        agent.scan_once()
        assert _leased_ports(gw) == []
    finally:
        cdp.shutdown()
        agent.stop()
        gw.stop()


def test_a_lease_for_a_port_that_became_denied_is_released(tmp_path, monkeypatch):
    gw = _FakeGateway()
    server, port = _http_target(tmp_path)
    monkeypatch.setattr(fa, "scan_listening_ports", lambda: _listeners({port: "app"}))
    agent = _agent(tmp_path, gw)
    try:
        agent.scan_once()
        assert _leased_ports(gw) == [port]
        agent.cfg = fa.replace(agent.cfg, adopt_mode="allowlist")
        summary = agent.scan_once()
        assert summary["closed"] == [str(port)]
        assert str(port) not in _store(tmp_path)
    finally:
        server.shutdown()
        agent.stop()
        gw.stop()


def test_no_forwarder_and_no_host_rewrite_for_a_refused_service(tmp_path, monkeypatch):
    gw = _FakeGateway()
    cdp, cdp_port = _cdp_server()
    monkeypatch.setattr(fa, "scan_listening_ports", lambda: _listeners({cdp_port: "chrome"}))
    agent = _agent(tmp_path, gw)
    try:
        agent.scan_once()
        assert agent._forwards == {}
        assert agent._adopt(cdp_port, {"addrs": {"127.0.0.1"}, "process": "chrome"}) is None
        assert agent._forwards == {}
    finally:
        cdp.shutdown()
        agent.stop()
        gw.stop()


def test_adoption_config_parsers():
    assert fa.parse_adopt_mode(None) == "denylist"
    assert fa.parse_adopt_mode("allowlist") == "allowlist"
    for bad in ("allow", 1, ""):
        try:
            fa.parse_adopt_mode(bad)
        except ValueError:
            continue
        raise AssertionError(bad)
    assert fa.parse_ports(None, "x") == frozenset()
    assert fa.parse_ports([3000, 4000], "x") == frozenset({3000, 4000})
    for bad in ([0], [70000], ["a"], ["4000"], [3000.5], [3000.0], "3000", [True]):
        try:
            fa.parse_ports(bad, "x")
        except ValueError:
            continue
        raise AssertionError(bad)
    assert fa.parse_allow_processes(None) == ()
    assert fa.parse_allow_processes("vite") == ("vite",)
    assert fa.parse_deny_processes(None) == fa.DEFAULT_DENY_PROCESSES
    assert fa.parse_deny_processes(["a", " b "]) == ("a", "b")
    with pytest.raises(ValueError, match="agent_allow_processes"):
        fa.parse_allow_processes([1])
    with pytest.raises(ValueError, match="agent_deny_processes"):
        fa.parse_deny_processes([""])
    with pytest.raises(ValueError, match="integer"):
        fa.parse_ports([3000.5], "agent_allow_ports")
    with pytest.raises(ValueError, match="duplicate port 3000"):
        fa.parse_ports([3000, 3000], "x")


# ---------------------------------------------------------------------------
# The identity probe also covers leases that are already stored
# ---------------------------------------------------------------------------

def _listeners_pid(port: int, pid: int) -> dict[int, dict]:
    return {port: {"addrs": {"127.0.0.1"}, "process": "app", "pid": pid}}


def _loopback_only(monkeypatch, port: int) -> None:
    real_tcp_open = fa.tcp_open
    monkeypatch.setattr(fa, "tcp_open", lambda host, p, timeout=1.0:
                        False if p == port else real_tcp_open(host, p, timeout))


def _seed_forwarded_lease(tmp_path, port: int, fwd_port: int) -> None:
    entry = {"lease_id": "lease_old", "label": f"x (port {port})",
             "external_port": 18001, "url": "http://dev.lan:18001",
             "target_host": "127.0.0.1", "target_port": fwd_port,
             "fwd_port": fwd_port, "process": "app", "last_hb": 0.0,
             "registered_at": time.time()}
    (tmp_path / "fabric-agent.json").write_text(json.dumps({str(port): entry}))


def _serves(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2) as resp:
            return resp.status == 200
    except OSError:
        return False


def test_a_leased_port_that_becomes_a_debugger_is_released_with_its_forwarder(
        tmp_path, monkeypatch):
    routes: dict[str, object] = {"/": {"ok": True}}
    server, port = _json_server(routes)
    _loopback_only(monkeypatch, port)
    gw = _FakeGateway()
    state = {"pid": 100}
    monkeypatch.setattr(fa, "scan_listening_ports",
                        lambda: _listeners_pid(port, state["pid"]))
    agent = _agent(tmp_path, gw)
    try:
        agent.scan_once()
        fwd_port = _store(tmp_path)[str(port)]["fwd_port"]
        assert _serves(fwd_port)
        # Same port, new process, now a Chrome DevTools endpoint.
        routes["/json/version"] = {"webSocketDebuggerUrl": "ws://x/devtools/browser/a"}
        state["pid"] = 200
        summary = agent.scan_once()
        assert summary["closed"] == [str(port)]
        assert str(port) not in _store(tmp_path)
        assert port not in agent._forwards
        assert _leased_ports(gw) == []
        assert not _serves(fwd_port)
        assert "Chrome DevTools" in agent._refused[port]
    finally:
        server.shutdown()
        agent.stop()
        gw.stop()


def test_a_leased_port_is_reprobed_when_the_cached_answer_expires(tmp_path, monkeypatch):
    routes: dict[str, object] = {"/": {"ok": True}}
    server, port = _json_server(routes)
    gw = _FakeGateway()
    monkeypatch.setattr(fa, "scan_listening_ports", lambda: _listeners_pid(port, 1))
    agent = _agent(tmp_path, gw)
    try:
        agent.scan_once()
        routes["/json/version"] = {"Browser": "node.js/v20.1.0"}
        assert agent.scan_once()["closed"] == []   # same listener, answer still cached
        fingerprint, reason, _ = agent._debug_cache[port]
        agent._debug_cache[port] = (fingerprint, reason, time.monotonic() - 31.0)
        assert agent.scan_once()["closed"] == [str(port)]
        assert _leased_ports(gw) == []
    finally:
        server.shutdown()
        agent.stop()
        gw.stop()


def test_a_leased_plain_http_app_is_kept(tmp_path, monkeypatch):
    server, port = _ordinary_app()
    gw = _FakeGateway()
    state = {"pid": 1}
    monkeypatch.setattr(fa, "scan_listening_ports",
                        lambda: _listeners_pid(port, state["pid"]))
    agent = _agent(tmp_path, gw)
    try:
        agent.scan_once()
        state["pid"] = 2   # a new listener identity is probed again and passes
        assert agent.scan_once()["closed"] == []
        assert _leased_ports(gw) == [port]
        assert str(port) in _store(tmp_path)
    finally:
        server.shutdown()
        agent.stop()
        gw.stop()


def test_an_inconclusive_probe_keeps_a_stored_lease(tmp_path, monkeypatch):
    raw, port = _raw_tcp_listener()   # accepts, never speaks: the probe times out
    gw = _FakeGateway()
    _seed_store(tmp_path, port, time.time())
    monkeypatch.setattr(fa, "scan_listening_ports", lambda: _listeners({port: "node"}))
    agent = _agent(tmp_path, gw, probe_timeout=0.2)
    try:
        assert agent.scan_once()["closed"] == []
        assert str(port) in _store(tmp_path)
    finally:
        raw.close()
        agent.stop()
        gw.stop()


def test_a_restart_does_not_serve_a_stored_debugger_lease(tmp_path, monkeypatch):
    server, port = _cdp_server()
    _loopback_only(monkeypatch, port)
    fwd_port = _free_port()
    _seed_forwarded_lease(tmp_path, port, fwd_port)
    monkeypatch.setattr(fa, "scan_listening_ports", lambda: _listeners({port: "app"}))
    gw = _FakeGateway()
    agent = _agent(tmp_path, gw)
    try:
        assert not _serves(fwd_port)   # nothing is served before the first probe
        summary = agent.scan_once()
        assert summary["closed"] == [str(port)]
        assert port not in agent._forwards
        assert not _serves(fwd_port)
        assert str(port) not in _store(tmp_path)
    finally:
        server.shutdown()
        agent.stop()
        gw.stop()


def test_a_restart_serves_a_stored_plain_app_lease_after_the_first_probe(
        tmp_path, monkeypatch):
    server, port = _ordinary_app()
    _loopback_only(monkeypatch, port)
    fwd_port = _free_port()
    _seed_forwarded_lease(tmp_path, port, fwd_port)
    monkeypatch.setattr(fa, "scan_listening_ports", lambda: _listeners({port: "app"}))
    gw = _FakeGateway()
    agent = _agent(tmp_path, gw)
    try:
        assert not _serves(fwd_port)
        agent.scan_once()
        assert agent._forwards[port].port == fwd_port
        assert _serves(fwd_port)
    finally:
        server.shutdown()
        agent.stop()
        gw.stop()


def test_incomplete_scan_does_not_release_leases(tmp_path, monkeypatch):
    """An empty, incomplete scan (ss timed out) is not evidence services are gone."""
    gw = _FakeGateway()
    server, port = _http_target(tmp_path)
    live: dict = {"inv": fa.ListenerInventory(_listeners({port: "testapp"}), complete=True)}
    monkeypatch.setattr(fa, "scan_listening_ports", lambda: live["inv"])
    agent = _agent(tmp_path, gw)
    try:
        agent.scan_once()
        lease_id = next(lease["id"] for lease in gw.leases.values()
                        if lease["target"]["port"] == port)
        live["inv"] = fa.ListenerInventory(complete=False)  # failed scan
        summary = agent.scan_once()
        assert summary["closed"] == []
        assert lease_id not in gw.closed
        assert str(port) in _store(tmp_path)
        live["inv"] = fa.ListenerInventory(complete=True)  # genuinely gone
        server.shutdown()
        agent.scan_once()
        assert lease_id in gw.closed
    finally:
        agent.stop()
        gw.stop()


def test_restore_failure_drops_entry_so_it_is_readopted(tmp_path, monkeypatch):
    """A remembered forward port that cannot be rebound must not leave a forwarder-less entry."""
    gw = _FakeGateway()
    server, port = _http_target(tmp_path)
    import herdr_engineering.fabric_agent as fa_mod

    real_tcp_open = fa_mod.tcp_open
    monkeypatch.setattr(
        fa_mod, "tcp_open",
        lambda host, p, timeout=1.0: False if p == port else real_tcp_open(host, p, timeout))
    monkeypatch.setattr(fa, "scan_listening_ports",
                        lambda: _listeners({port: "testapp"}))
    agent = _agent(tmp_path, gw)
    blocker = None
    try:
        agent.scan_once()
        old_fwd = _store(tmp_path)[str(port)]["fwd_port"]
        old_lease = _store(tmp_path)[str(port)]["lease_id"]
        agent.stop()
        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        blocker.bind(("127.0.0.1", old_fwd))  # another process took the port
        blocker.listen(1)
        agent2 = _agent(tmp_path, gw)
        try:
            # restore is deferred to the first scan (the port must pass the identity probe first):
            # until then the entry is kept but nothing is served
            assert port not in agent2._forwards
            assert str(port) in agent2._store
            agent2.scan_once()
            assert old_lease in gw.closed
            entry = _store(tmp_path)[str(port)]
            assert entry["fwd_port"] != old_fwd
            assert port in agent2._forwards
        finally:
            agent2.stop()
    finally:
        if blocker:
            blocker.close()
        server.shutdown()
        gw.stop()

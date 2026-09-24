"""Fabric gateway tests (spec 0110 component #3: unified dev:<port> front door)."""
import http.server
import socket
import threading
import time
import urllib.request

import pytest

from herdr_engineering.devfabric import DevServiceRegistry
from herdr_engineering.errors import UnreachableError
from herdr_engineering.fabric import (
    FabricClient,
    FabricGateway,
    GatewayRouter,
)


def _free_port() -> int:
    """Ask the OS for a currently-free loopback port (hermetic ranges)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _port_range() -> tuple[int, int]:
    base = _free_port()
    return base, base + 20


def _http_target(tmp_path):
    """Start a tiny local HTTP server; returns (server, port)."""
    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), http.server.SimpleHTTPRequestHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


def _gateway(tmp_path, *, port=0, token=None, probe_interval=0.5, ttl=5,
             target_host="127.0.0.1", url_base="dev.test",
             port_range: tuple[int, int] | None = None):
    registry = DevServiceRegistry(store_path=tmp_path / "leases.json",
                                  port_range=port_range or _port_range(),
                                  lease_ttl_seconds=ttl)
    router = GatewayRouter([target_host])
    gw = FabricGateway(registry, router, control_host="127.0.0.1", control_port=port,
                       url_base=url_base, probe_interval=probe_interval, token=token)
    gw.start()
    return gw, registry, router


def _client(gw, token=None):
    return FabricClient(f"http://127.0.0.1:{gw.control_port}", token=token)


def _port_open(port, host="127.0.0.1", timeout=1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def test_register_routes_to_target(tmp_path):
    server, target_port = _http_target(tmp_path)
    gw, registry, _ = _gateway(tmp_path)
    try:
        client = _client(gw)
        result = client.register(target_machine_id="beast", target_host="127.0.0.1",
                                 target_port=target_port, label="app")
        lease = result["lease"]
        ext = lease["external_port"]
        # stable dev:<port> address advertised
        assert result["url"] == f"http://dev.test:{ext}"
        # data path: connect through the gateway's leased port
        with urllib.request.urlopen(f"http://127.0.0.1:{ext}/", timeout=5) as resp:
            assert resp.status == 200
        # registry agrees
        assert registry.resolve_port(ext).id == lease["id"]
    finally:
        gw.stop()
        server.shutdown()


def test_close_unbinds_port(tmp_path):
    server, target_port = _http_target(tmp_path)
    gw, _, _ = _gateway(tmp_path)
    try:
        client = _client(gw)
        lease = client.register(target_machine_id="m", target_host="127.0.0.1",
                                target_port=target_port)["lease"]
        ext = lease["external_port"]
        assert _port_open(ext)
        assert client.close(lease["id"])["ok"] is True
        assert not _port_open(ext)  # router released the port
    finally:
        gw.stop()
        server.shutdown()


def test_dead_target_expires_and_unbinds(tmp_path):
    server, target_port = _http_target(tmp_path)
    gw, registry, _ = _gateway(tmp_path, ttl=2, probe_interval=0.4)
    try:
        client = _client(gw)
        lease = client.register(target_machine_id="m", target_host="127.0.0.1",
                                target_port=target_port)["lease"]
        ext = lease["external_port"]
        assert _port_open(ext)
        server.shutdown()  # kill the app
        server.server_close()
        # probe loop: unhealthy -> no renewal -> TTL expiry -> unbind
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if not _port_open(ext):
                break
            time.sleep(0.2)
        assert not _port_open(ext)
        assert registry.get(lease["id"]) is None or \
            registry.get(lease["id"]).state == "expired"
    finally:
        gw.stop()


def test_healthy_target_stays_active_past_ttl(tmp_path):
    server, target_port = _http_target(tmp_path)
    gw, registry, _ = _gateway(tmp_path, ttl=1, probe_interval=0.4)
    try:
        client = _client(gw)
        lease = client.register(target_machine_id="m", target_host="127.0.0.1",
                                target_port=target_port)["lease"]
        time.sleep(2.5)  # several TTL periods
        assert registry.get(lease["id"]).state == "active"  # renewed by probes
    finally:
        gw.stop()
        server.shutdown()


def test_token_required(tmp_path):
    gw, _, _ = _gateway(tmp_path, token="sekrit")
    try:
        no_token = FabricClient(f"http://127.0.0.1:{gw.control_port}", token="wrong")
        with pytest.raises(UnreachableError):
            no_token.list()
        ok = _client(gw, token="sekrit")
        assert "leases" in ok.list()
    finally:
        gw.stop()


def test_list_and_resolve(tmp_path):
    server, target_port = _http_target(tmp_path)
    gw, _, _ = _gateway(tmp_path)
    try:
        client = _client(gw)
        lease = client.register(target_machine_id="m", target_host="127.0.0.1",
                                target_port=target_port, label="x")["lease"]
        listed = client.list()["leases"]
        assert any(e["id"] == lease["id"] and
                   e["url"] == f"http://dev.test:{lease['external_port']}"
                   for e in listed)
        resolved = client.resolve(lease["external_port"])
        assert resolved["lease"]["id"] == lease["id"]
        assert resolved["url"] == f"http://dev.test:{lease['external_port']}"
    finally:
        gw.stop()
        server.shutdown()


def test_router_restart_rebinds_active_leases(tmp_path):
    """A gateway restart must re-bind leases that survived in the registry."""
    store = tmp_path / "leases.json"
    server, target_port = _http_target(tmp_path)
    gw, registry, _ = _gateway(tmp_path)
    try:
        lease = _client(gw).register(target_machine_id="m",
                                     target_host="127.0.0.1",
                                     target_port=target_port)["lease"]
        ext = lease["external_port"]
        gw.stop()
        # new gateway over the same store: must rebind the active lease
        registry2 = DevServiceRegistry(store_path=store, port_range=_port_range())
        router2 = GatewayRouter(["127.0.0.1"])
        gw2 = FabricGateway(registry2, router2, control_host="127.0.0.1",
                            control_port=0, url_base="dev.test",
                            probe_interval=0.5)
        gw2.start()
        assert _port_open(ext)
        with urllib.request.urlopen(f"http://127.0.0.1:{ext}/", timeout=5) as resp:
            assert resp.status == 200
        gw2.stop()
    finally:
        server.shutdown()


def test_gateway_url_from_config_env_wins(monkeypatch):
    from herdr_engineering.fabric import gateway_url_from_config
    config = {"fabric": {"gateway_url": "http://cfg-host:29999"}}
    assert gateway_url_from_config(config) == "http://cfg-host:29999"
    monkeypatch.setenv("HERDR_ENGINEERING_FABRIC_URL", "http://env-host:29999")
    assert gateway_url_from_config(config) == "http://env-host:29999"


def test_gateway_url_bare_base_gets_default_port(monkeypatch):
    from herdr_engineering.fabric import gateway_url_from_config
    monkeypatch.delenv("HERDR_ENGINEERING_FABRIC_URL", raising=False)
    assert gateway_url_from_config({"fabric": {"gateway_url": "bender.ts.net"}}) \
        == "http://bender.ts.net:29999"
    assert gateway_url_from_config({"fabric": {"gateway_url": ""}}) is None
    assert gateway_url_from_config({}) is None


def test_client_register_returns_nested_lease_with_url(tmp_path):
    """Lock the API shape: {"lease": {...}, "url": ...} (the CLI depends on it)."""
    server, target_port = _http_target(tmp_path)
    gw, _, _ = _gateway(tmp_path)
    try:
        result = _client(gw).register(target_machine_id="m", target_host="127.0.0.1",
                                      target_port=target_port)
        assert "lease" in result and "url" in result
        ext = result["lease"]["external_port"]
        assert result["url"] == f"http://dev.test:{ext}"
    finally:
        gw.stop()
        server.shutdown()


def test_second_gateway_refuses_same_store(tmp_path):
    gw, _, _ = _gateway(tmp_path)
    try:
        registry2 = DevServiceRegistry(store_path=tmp_path / "leases.json")
        router2 = GatewayRouter(["127.0.0.1"])
        gw2 = FabricGateway(registry2, router2, control_host="127.0.0.1",
                            control_port=0, url_base="dev.test")
        from herdr_engineering.errors import ConflictError
        with pytest.raises(ConflictError):
            gw2.start()
    finally:
        gw.stop()


def test_bind_refused_when_port_taken(tmp_path):
    taken = _free_port()
    other = _free_port()
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    blocker.bind(("127.0.0.1", taken))
    blocker.listen(1)
    try:
        router = GatewayRouter(["127.0.0.1"])
        assert router.bind(taken, "127.0.0.1", 1) is False  # OS owns it
        assert router.bind(other, "127.0.0.1", 1) is True
        router.unbind(other)
    finally:
        blocker.close()

"""Transparent dev fabric tests (spec 0110)."""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import pytest

from herdr_engineering.devfabric import (
    DevServiceForwarder,
    DevServiceRegistry,
    _add_seconds,
)
from herdr_engineering.errors import ConflictError


def _free_port() -> int:
    """Ask the OS for a currently-free loopback port."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_collision_free_port_allocation(tmp_path):
    r = DevServiceRegistry(store_path=tmp_path / "leases.json")
    l1 = r.register(target_machine_id="fry", target_host="127.0.0.1", target_port=8080,
                    owner_process_id="p1")
    l2 = r.register(target_machine_id="fry", target_host="127.0.0.1", target_port=8081,
                    owner_process_id="p2")
    assert l1.external_port != l2.external_port


def test_idempotent_registration(tmp_path):
    r = DevServiceRegistry(store_path=tmp_path / "leases.json")
    l1 = r.register(target_machine_id="fry", target_host="127.0.0.1", target_port=9000,
                    owner_herdr_session_id="s1", owner_process_id="p1")
    l2 = r.register(target_machine_id="fry", target_host="127.0.0.1", target_port=9000,
                    owner_herdr_session_id="s1", owner_process_id="p1")
    assert l1.id == l2.id  # same lease renewed, not duplicated


def test_ttl_heartbeat_reconcile(tmp_path):
    r = DevServiceRegistry(store_path=tmp_path / "leases.json", lease_ttl_seconds=60)
    lease = r.register(target_machine_id="fry", target_host="127.0.0.1", target_port=7000)
    # simulate expiry by setting expires_at in the past
    lease.expires_at = _add_seconds(lease.expires_at, -120)
    expired = r.reconcile()
    assert lease.id in expired
    assert r.active_leases() == []
    # heartbeat revives
    hb = r.heartbeat(lease.id)
    assert hb is not None and hb.state == "active"


def test_persistence_round_trip_across_instances(tmp_path):
    """A lease registered in one registry instance must be visible to a new
    instance loading the same store (i.e. another process)."""
    store = tmp_path / "leases.json"
    r1 = DevServiceRegistry(store_path=store)
    lease = r1.register(target_machine_id="fry", target_host="127.0.0.1",
                        target_port=8080, label="rt", owner_process_id="p1")
    r2 = DevServiceRegistry(store_path=store)
    loaded = r2.get(lease.id)
    assert loaded is not None
    assert loaded.target_host == "127.0.0.1"
    assert loaded.target_port == 8080
    assert loaded.label == "rt"
    assert r2.resolve_port(lease.external_port).id == lease.id


def test_resolve_port(tmp_path):
    r = DevServiceRegistry(store_path=tmp_path / "leases.json")
    lease = r.register(target_machine_id="fry", target_host="127.0.0.1", target_port=5000)
    assert r.resolve_port(lease.external_port).id == lease.id


def test_close(tmp_path):
    r = DevServiceRegistry(store_path=tmp_path / "leases.json")
    lease = r.register(target_machine_id="fry", target_host="127.0.0.1", target_port=5001)
    assert r.close(lease.id) is True
    assert r.active_leases() == []


def test_port_exhaustion_raises_conflict(tmp_path):
    # Pick a pair of ports the OS has free so the test is hermetic (the
    # allocator skips ports with live OS bindings, e.g. a running gateway).
    base = _free_port()
    r = DevServiceRegistry(store_path=tmp_path / "leases.json",
                           port_range=(base, base + 1))
    r.register(target_machine_id="a", target_host="h", target_port=1, owner_process_id="p1")
    r.register(target_machine_id="a", target_host="h", target_port=2, owner_process_id="p2")
    with pytest.raises(ConflictError):
        r.register(target_machine_id="a", target_host="h", target_port=3,
                   owner_process_id="p3")


def test_allocation_skips_os_bound_port(tmp_path):
    import socket
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    blocker.bind(("127.0.0.1", 18500))
    blocker.listen(1)
    try:
        r = DevServiceRegistry(store_path=tmp_path / "leases.json",
                               port_range=(18500, 18501))
        lease = r.register(target_machine_id="a", target_host="h", target_port=1,
                           owner_process_id="p1")
        assert lease.external_port == 18501  # 18500 is bound by the blocker
    finally:
        blocker.close()


def test_forwarder_proxies_http(tmp_path):
    import http.server
    import threading
    import urllib.request

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), http.server.SimpleHTTPRequestHandler)
    target_port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    r = DevServiceRegistry(store_path=tmp_path / "leases.json")
    lease = r.register(target_machine_id="local", target_host="127.0.0.1",
                       target_port=target_port, label="test-app")
    fwd = DevServiceForwarder(lease, bind_host="127.0.0.1").start()
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{lease.external_port}/", timeout=5) as resp:
            assert resp.status == 200
    finally:
        fwd.stop()
        server.shutdown()


def test_forwarder_refuses_public_bind(tmp_path):
    r = DevServiceRegistry(store_path=tmp_path / "leases.json")
    lease = r.register(target_machine_id="local", target_host="127.0.0.1",
                       target_port=8080)
    with pytest.raises(ConflictError):
        DevServiceForwarder(lease, bind_host="203.0.113.10").start()

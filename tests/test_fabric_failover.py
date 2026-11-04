"""Active/standby gateway failover (spec D8). Steps are driven by hand (_ha_step) so nothing races a timer."""
import http.server
import socket
import threading

import pytest

from herdr_engineering.devfabric import DevServiceRegistry
from herdr_engineering.errors import UnreachableError
from herdr_engineering.fabric import FabricClient, FabricGateway, GatewayRouter
from tests.test_fabric import _port_range


def _node(tmp_path, name, peer, node_id, port_range, promote_after=2):
    registry = DevServiceRegistry(store_path=tmp_path / name / "leases.json", port_range=port_range, lease_ttl_seconds=60)
    gw = FabricGateway(registry, GatewayRouter(["127.0.0.1"]), control_host="127.0.0.1", control_port=0,
                       url_base="dev.test", probe_interval=3600, token=None, peer_url=peer, node_id=node_id,
                       promote_after=promote_after, auto_ha=False)
    return gw


@pytest.fixture
def origin():
    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(200); self.send_header("Content-Length", "2"); self.end_headers(); self.wfile.write(b"ok")
    s = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    yield s.server_address[1]
    s.shutdown()


def _get(port):
    with socket.create_connection(("127.0.0.1", port), timeout=3) as c:
        c.sendall(b"GET / HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
        out = b""
        while chunk := c.recv(4096):
            out += chunk
    return out


def _refused(port):
    try:
        socket.create_connection(("127.0.0.1", port), timeout=1).close()
        return False
    except OSError:
        return True


def _pair(tmp_path):
    rng = _port_range()
    a = _node(tmp_path, "a", None, "node-a", rng)
    a.start()
    a.peer_url = None
    b = _node(tmp_path, "b", f"http://127.0.0.1:{a.control_port}", "node-b", rng)
    b.start()
    a.peer_url = f"http://127.0.0.1:{b.control_port}"
    return a, b


def test_second_gateway_starts_as_standby_and_mirrors_leases(tmp_path, origin):
    a, b = _pair(tmp_path)
    try:
        assert (a.role, b.role) == ("active", "standby")
        lease = FabricClient(f"http://127.0.0.1:{a.control_port}").register(
            target_machine_id="bender", target_host="127.0.0.1", target_port=origin, label="web")["lease"]
        b._ha_step()
        assert [lz.id for lz in b.registry.active_leases()] == [lease["id"]]
        assert _refused(lease["external_port"]) is False and b.router.bound_ports() == []   # only a serves it
    finally:
        a.stop(); b.stop()


def test_standby_refuses_writes_with_a_hint(tmp_path, origin):
    a, b = _pair(tmp_path)
    try:
        with pytest.raises(UnreachableError) as exc:
            FabricClient(f"http://127.0.0.1:{b.control_port}").register(
                target_machine_id="x", target_host="127.0.0.1", target_port=origin)
        assert "503" in str(exc.value)
        assert FabricClient(f"http://127.0.0.1:{b.control_port}").healthz()["role"] == "standby"
    finally:
        a.stop(); b.stop()


def test_standby_promotes_after_misses_and_serves_the_mirrored_lease(tmp_path, origin):
    a, b = _pair(tmp_path)
    lease = FabricClient(f"http://127.0.0.1:{a.control_port}").register(
        target_machine_id="bender", target_host="127.0.0.1", target_port=origin)["lease"]
    b._ha_step()
    a.stop()
    try:
        b._ha_step()
        assert b.role == "standby"            # one miss is not enough (flapping peer)
        b._ha_step()
        assert b.role == "active" and b.epoch >= 1
        assert _get(lease["external_port"]).endswith(b"ok")
        out = FabricClient(f"http://127.0.0.1:{b.control_port}").register(
            target_machine_id="beast", target_host="127.0.0.1", target_port=origin, label="new")
        assert out["lease"]["id"]                                  # writes work again
    finally:
        b.stop()


def test_a_flapping_peer_resets_the_miss_counter(tmp_path, origin):
    a, b = _pair(tmp_path)
    try:
        b._peer_failures = 1
        b._ha_step()                           # peer healthy
        assert b._peer_failures == 0 and b.role == "standby"
    finally:
        a.stop(); b.stop()


def test_restarted_primary_rejoins_as_standby_instead_of_splitting_the_brain(tmp_path, origin):
    a, b = _pair(tmp_path)
    a.stop()
    b._ha_step(); b._ha_step()
    assert b.role == "active"
    a2 = _node(tmp_path, "a", f"http://127.0.0.1:{b.control_port}", "node-a", _port_range())
    try:
        a2.start()
        assert a2.role == "standby" and a2.epoch == b.epoch
        assert a2.router.bound_ports() == []
    finally:
        a2.stop(); b.stop()


def test_double_active_is_resolved_by_epoch_then_node_id(tmp_path):
    rng = _port_range()
    x = _node(tmp_path, "x", None, "node-a", rng); x.start()
    y = _node(tmp_path, "y", None, "node-b", rng); y.start()    # both believe they are active (started apart)
    try:
        x.peer_url = f"http://127.0.0.1:{y.control_port}"
        y.peer_url = f"http://127.0.0.1:{x.control_port}"
        x.epoch = y.epoch = 1
        x._ha_step(); y._ha_step()
        assert (x.role, y.role) == ("active", "standby")        # equal epoch: smaller node id wins
        y.epoch = 5
        y.promote()
        x._ha_step()
        assert x.role == "standby" and y.role == "active"       # higher epoch beats node id
    finally:
        x.stop(); y.stop()


def test_no_peer_means_always_active_as_before(tmp_path):
    g = _node(tmp_path, "solo", None, "solo", _port_range())
    g.start()
    try:
        assert g.role == "active" and FabricClient(f"http://127.0.0.1:{g.control_port}").healthz()["role"] == "active"
    finally:
        g.stop()



def test_background_loop_mirrors_and_promotes_without_any_manual_step(tmp_path, origin):
    """The real probe thread (auto_ha=True), not hand-driven steps: standby notices the active is gone and takes over."""
    import time
    rng = _port_range()
    a = _node(tmp_path, "a", None, "node-a", rng)
    a.start()
    registry = DevServiceRegistry(store_path=tmp_path / "b" / "leases.json", port_range=rng, lease_ttl_seconds=60)
    b = FabricGateway(registry, GatewayRouter(["127.0.0.1"]), control_host="127.0.0.1", control_port=0, url_base="dev.test",
                      probe_interval=0.2, token=None, peer_url=f"http://127.0.0.1:{a.control_port}", node_id="node-b",
                      promote_after=2)
    b.start()
    try:
        lease = FabricClient(f"http://127.0.0.1:{a.control_port}").register(
            target_machine_id="bender", target_host="127.0.0.1", target_port=origin)["lease"]
        deadline = time.time() + 15
        while time.time() < deadline and not any(lz.id == lease["id"] for lz in b.registry.active_leases()):
            time.sleep(0.1)
        assert b.role == "standby" and any(lz.id == lease["id"] for lz in b.registry.active_leases())   # mirrored by the loop
        a.stop()
        while time.time() < deadline and b.role != "active":
            time.sleep(0.1)
        assert b.role == "active"
        while time.time() < deadline and lease["external_port"] not in b.router.bound_ports():
            time.sleep(0.1)
        assert _get(lease["external_port"]).endswith(b"ok")
    finally:
        b.stop()

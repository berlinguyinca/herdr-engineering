"""origin_port (where the app listens, vs. the forwarder port) and network_namespace (which loopback)."""
import json
import socket

import pytest

from herdr_engineering import fabric_agent as fa
from herdr_engineering.contracts import DevServiceLease
from herdr_engineering.devfabric import DevServiceRegistry, _parse_leases
from herdr_engineering.fabric_urls import resolve_url
from tests.test_fabric import _client, _gateway, _http_target
from tests.test_fabric_agent import _agent, _FakeGateway

NS_A, NS_B = "net:[4026531840]", "net:[4026532999]"


def _lease(**kw):
    base = dict(id="L1", external_port=3000, protocol="http", target_machine_id="bender", target_host="100.64.0.9", target_port=3000)
    base.update(kw)
    return DevServiceLease(**base)


def test_contract_validates_the_new_fields():
    assert _lease(origin_port=3000, network_namespace=NS_A).to_dict()["origin_port"] == 3000
    for bad in (dict(origin_port=0), dict(origin_port=70000), dict(network_namespace="net:[1]; rm -rf"),
                dict(network_namespace="x" * 65)):
        with pytest.raises(ValueError):
            _lease(**bad)


def test_registry_round_trips_and_older_leases_still_load(tmp_path):
    reg = DevServiceRegistry(store_path=tmp_path / "l.json", port_range=(41000, 41050))
    lease = reg.register(target_machine_id="bender", target_host="100.64.0.9", target_port=40111,
                         origin_port=3000, network_namespace=NS_A)
    again = DevServiceRegistry(store_path=tmp_path / "l.json", port_range=(41000, 41050))._leases.get(lease.id)
    stored = json.loads((tmp_path / "l.json").read_text())
    text = json.dumps(stored)
    assert "origin_port" in text and NS_A in text
    old = {"id": "L0", "external_port": 3001, "protocol": "http", "target_machine_id": "m", "target_host": "100.64.0.9", "target_port": 3001}
    parsed = _parse_leases({"leases": [old]})
    lz = parsed["L0"]
    assert lz.origin_port is None and lz.network_namespace is None
    assert again.origin_port == 3000 and again.network_namespace == NS_A


def _l(port, tport, machine="bender", ns=None, origin=None, lid="L1"):
    d = {"id": lid, "external_port": 3000 + port % 7, "label": "web", "state": "active",
         "target": {"machine_id": machine, "host": "100.64.0.9", "port": tport}}
    if origin:
        d["origin_port"] = origin
    if ns:
        d["network_namespace"] = ns
    return d


def test_loopback_url_matches_the_origin_port_not_the_forwarder_port():
    leases = [_l(3000, 40111, origin=3000)]
    assert resolve_url("http://localhost:3000/x", leases, url_base="f.test", source_machine="bender").status == "resolved"
    # the ephemeral forwarder port is not something an app ever prints as its own URL
    assert resolve_url("http://localhost:40111/x", leases, url_base="f.test", source_machine="bender").status == "unmapped"
    # leases without origin_port keep working on the target port
    assert resolve_url("http://localhost:3000/x", [_l(3000, 3000)], url_base="f.test", source_machine="bender").status == "resolved"


def test_namespace_separates_two_loopbacks_on_one_machine():
    leases = [_l(3000, 3000, ns=NS_A, lid="A"), _l(3001, 3000, ns=NS_B, lid="B")]
    a = resolve_url("http://localhost:3000/", leases, url_base="f.test", source_machine="bender", source_namespace=NS_A)
    b = resolve_url("http://localhost:3000/", leases, url_base="f.test", source_machine="bender", source_namespace=NS_B)
    assert (a.status, a.lease_id, b.status, b.lease_id) == ("resolved", "A", "resolved", "B")
    # unknown namespace on the source: both eligible, so honestly ambiguous rather than a guess
    none = resolve_url("http://localhost:3000/", leases, url_base="f.test", source_machine="bender")
    assert none.status == "ambiguous"
    # a third namespace sees neither
    assert resolve_url("http://localhost:3000/", leases, url_base="f.test", source_machine="bender",
                       source_namespace="net:[1]").status == "unmapped"
    # a lease that never recorded a namespace stays eligible for everyone
    legacy = [_l(3000, 3000)]
    assert resolve_url("http://localhost:3000/", legacy, url_base="f.test", source_machine="bender",
                       source_namespace=NS_A).status == "resolved"


def test_gateway_accepts_the_fields_and_resolves_through_them(tmp_path):
    server, tport = _http_target(tmp_path)
    gw, _, _ = _gateway(tmp_path)
    try:
        res = _client(gw).register(target_machine_id="bender", target_host="127.0.0.1", target_port=tport,
                                   origin_port=4321, network_namespace=NS_A)
        assert res["lease"]["origin_port"] == 4321 and res["lease"]["network_namespace"] == NS_A
        got = _client(gw).resolve_urls(["http://localhost:4321/p"], "bender", NS_A)["resolutions"][0]
        assert got["status"] == "resolved" and got["canonical"].endswith(f":{res['lease']['external_port']}/p")
        other = _client(gw).resolve_urls(["http://localhost:4321/p"], "bender", NS_B)["resolutions"][0]
        assert other["status"] == "unmapped"
    finally:
        gw.stop()
        server.shutdown()


def test_agent_reports_origin_port_when_the_forwarder_could_not_reuse_it(tmp_path):
    # app on 127.0.0.1:P only; agent's tailnet ip is 127.0.0.2, whose port P is already taken => ephemeral forwarder
    app = socket.socket(); app.bind(("127.0.0.1", 0)); app.listen(5)
    port = app.getsockname()[1]
    squatter = socket.socket(); squatter.bind(("127.0.0.2", port)); squatter.listen(5)
    # tcp_open(127.0.0.2, port) must be False for the forwarder path: close the squatter's accept side by not listening
    squatter.close()
    block = socket.socket(); block.bind(("127.0.0.2", port))      # bound, not listening: connect refused, bind still blocked
    gw = _FakeGateway()
    agent = _agent(tmp_path, gw, tailnet_ip="127.0.0.2")
    seen = {}
    real = agent._client.register

    def spy(**kw):
        seen.update(kw)
        return real(**kw)
    agent._client.register = spy
    try:
        entry = agent._adopt(port, {"process": "node"})
        assert entry and seen["target_port"] != port
        assert seen["origin_port"] == port and seen["network_namespace"] == fa._own_net_namespace()
    finally:
        for f in list(agent._forwards.values()):
            f.stop()
        gw.stop(); app.close(); block.close()


def test_agent_omits_origin_port_when_ports_match(tmp_path):
    app = socket.socket(); app.bind(("127.0.0.1", 0)); app.listen(5)
    port = app.getsockname()[1]
    gw = _FakeGateway()
    agent = _agent(tmp_path, gw)                     # tailnet ip 127.0.0.1: reachable directly, no forwarder
    seen = {}
    real = agent._client.register
    agent._client.register = lambda **kw: (seen.update(kw), real(**kw))[1]
    try:
        assert agent._adopt(port, {"process": "node"})
        assert seen["origin_port"] is None and seen["target_port"] == port
    finally:
        gw.stop(); app.close()


def test_own_net_namespace_is_a_valid_identifier():
    ns = fa._own_net_namespace()
    assert ns is None or _lease(network_namespace=ns).network_namespace == ns


# ---- a lease must never be handed out for a port the gateway could not bind ----------------------
def _squat(host, port):
    s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); s.bind((host, port)); s.listen(1); return s


def test_allocation_skips_a_port_held_on_the_routers_own_address(tmp_path):
    # 127.0.0.2 stands in for the tailnet IP: loopback (127.0.0.1) is free, the bind address is not
    probe = socket.socket(); probe.bind(("127.0.0.2", 0)); lo = probe.getsockname()[1]; probe.close()
    held = _squat("127.0.0.2", lo)
    server, tport = _http_target(tmp_path)
    from herdr_engineering.devfabric import DevServiceRegistry
    from herdr_engineering.fabric import FabricGateway, GatewayRouter
    reg = DevServiceRegistry(store_path=tmp_path / "l.json", port_range=(lo, lo + 40))
    gw = FabricGateway(reg, GatewayRouter(["127.0.0.2"]), control_host="127.0.0.1", control_port=0,
                       url_base="x.test", probe_interval=30, token="")
    gw.start()
    try:
        res = _client(gw).register(target_machine_id="m", target_host="127.0.0.1", target_port=tport, preferred_port=lo)
        assert res["lease"]["external_port"] != lo and res["lease"]["external_port"] in gw.router.bound_ports()
    finally:
        gw.stop(); server.shutdown(); held.close()


def test_a_bind_failure_is_a_409_and_leaves_no_lease(tmp_path):
    from herdr_engineering.errors import UnreachableError
    from herdr_engineering.devfabric import DevServiceRegistry
    from herdr_engineering.fabric import FabricGateway, GatewayRouter
    reg = DevServiceRegistry(store_path=tmp_path / "l.json", port_range=(44000, 44010))
    router = GatewayRouter(["127.0.0.1"])
    router.bind = lambda *a, **k: False                      # the race: free at allocation, taken at bind
    gw = FabricGateway(reg, router, control_host="127.0.0.1", control_port=0, url_base="x.test", probe_interval=30, token="")
    gw.start()
    try:
        with pytest.raises(UnreachableError, match="409"):
            _client(gw).register(target_machine_id="m", target_host="127.0.0.1", target_port=3000)
        assert reg.active_leases() == []
    finally:
        gw.stop()

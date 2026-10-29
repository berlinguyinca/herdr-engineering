"""Stable service-name URLs: name assignment, SNI/Host parsing, and real routing through the named front."""
import socket
import ssl
import threading

import pytest

from herdr_engineering import fabric_named as fn
from herdr_engineering.fabric import FabricGateway, GatewayRouter
from herdr_engineering.devfabric import DevServiceRegistry
from tests.test_fabric import _http_target
from tests.test_fabric_certs import _self_signed


def L(id, machine="bender", port=3000, label=None, created="2026-10-28T00:00:00Z", origin=None, state="active"):
    d = {"id": id, "label": label, "created_at": created, "state": state, "external_port": 20000 + port % 1000,
         "target": {"machine_id": machine, "host": "127.0.0.1", "port": port}}
    if origin:
        d["origin_port"] = origin
    return d


def test_automatic_name_is_machine_and_listening_port():
    assert fn.automatic_name(L("a", machine="Bender_01", port=40111, origin=3000)) == "bender-01-3000"
    assert fn.automatic_name(L("a", machine="", port=80)) == "svc-80"


def test_valid_label_is_the_name_and_junk_labels_fall_back():
    names = fn.assign_names([L("a", label="myapp"), L("b", port=3001, label="node on bender (port 3001)"),
                             L("c", port=3002, label="api"), L("d", port=3003, label="UPPER")])
    assert names == {"a": "myapp", "b": "bender-3001", "c": "bender-3002", "d": "bender-3003"}


def test_collisions_are_deterministic_oldest_wins():
    ls = [L("new", port=3001, label="web", created="2026-10-28T02:00:00Z"), L("old", port=3002, label="web", created="2026-10-28T01:00:00Z")]
    assert fn.assign_names(ls) == {"old": "web", "new": "bender-3001"}
    assert fn.assign_names(list(reversed(ls))) == fn.assign_names(ls)
    twins = fn.assign_names([L("x", created="1"), L("y", created="2")])         # same machine+port, two namespaces
    assert len(set(twins.values())) == 2


def _client_hello(name):
    cctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    cctx.check_hostname, cctx.verify_mode = False, ssl.CERT_NONE
    inc, out = ssl.MemoryBIO(), ssl.MemoryBIO()
    obj = cctx.wrap_bio(inc, out, server_hostname=name)
    try:
        obj.do_handshake()
    except ssl.SSLWantReadError:
        pass
    return out.read()


def test_parse_sni_reads_real_client_hellos_and_survives_garbage():
    assert fn.parse_sni(_client_hello("Svc.Local.Example.Test")) == "svc.local.example.test"
    assert fn.parse_sni(_client_hello("a.b")) == "a.b"
    hello = _client_hello("x.local.example.test")
    for cut in range(len(hello)):                       # every truncation is rejected without raising
        fn.parse_sni(hello[:cut])
    assert fn.parse_sni(b"") is None and fn.parse_sni(b"GET / HTTP/1.1\r\n\r\n") is None
    assert fn.parse_sni(b"\x16\x03\x01\x00\x05\x01" + b"\xff" * 40) is None


def test_parse_host():
    assert fn.parse_host(b"GET / HTTP/1.1\r\nAccept: */*\r\nHost: Svc.Local.Example.Test:8443\r\n\r\n") == "svc.local.example.test"
    assert fn.parse_host(b"GET / HTTP/1.1\r\nHost: [::1]:80\r\n\r\n") == "::1"
    assert fn.parse_host(b"GET / HTTP/1.1\r\nAccept: x\r\n\r\n") is None


# ---- end to end through a real gateway ---------------------------------------------------------
def _free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


@pytest.fixture
def stack(tmp_path):
    server, tport = _http_target(tmp_path)
    cert, key = _self_signed(tmp_path / "pub", "local.example.test", 30)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(cert), str(key))
    registry = DevServiceRegistry(store_path=tmp_path / "l.json", port_range=(42000, 42200))
    router = GatewayRouter(["127.0.0.1"])
    named_port = _free_port()
    gw = FabricGateway(registry, router, control_host="127.0.0.1", control_port=0, url_base="local.example.test",
                       probe_interval=30, token="", named_domain="local.example.test", named_port=named_port)
    gw.start()
    gw.enable_tls(ctx)
    lease = registry.register(target_machine_id="bender", target_host="127.0.0.1", target_port=tport, label="docs")
    router.bind(lease.external_port, lease.target_host, lease.target_port)
    yield gw, lease, named_port
    gw.stop()
    server.shutdown()


def _get(port, host, tls=True):
    raw = socket.create_connection(("127.0.0.1", port), timeout=5)
    sock = raw
    if tls:
        c = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        c.check_hostname, c.verify_mode = False, ssl.CERT_NONE
        try:
            sock = c.wrap_socket(raw, server_hostname=host)
        except (OSError, ssl.SSLError):
            raw.close()
            return b""                                   # refused at the handshake: nothing was served
    try:
        sock.sendall(f"GET / HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode())
    except OSError:
        return b""
    data = b""
    try:
        while chunk := sock.recv(4096):
            data += chunk
    except (OSError, ssl.SSLError):
        pass
    sock.close()
    return data


def test_named_host_routes_to_its_lease_over_tls_and_plain(stack):
    gw, lease, port = stack
    assert gw.named_url(lease.to_dict()) == f"https://docs.local.example.test:{port}"
    assert _get(port, "docs.local.example.test").startswith(b"HTTP/1.")
    assert _get(port, "docs.local.example.test", tls=False).startswith(b"HTTP/1.")


def test_unknown_names_get_nothing(stack):
    gw, lease, port = stack
    for host in ("nope.local.example.test", "local.example.test", "a.b.local.example.test", "docs.evil.example", "bender-3000.local.example.test"):
        assert _get(port, host) == b"", host
        assert _get(port, host, tls=False) == b"", host


def test_resolution_prefers_the_stable_name_and_keeps_the_port_url(stack):
    gw, lease, port = stack
    r = gw.op_resolve_urls([f"http://localhost:{lease.target_port}/x?y=1"], "bender")["resolutions"][0]
    assert r["status"] == "resolved"
    assert r["canonical"] == f"https://docs.local.example.test:{port}/x?y=1"
    assert r["port_url"].startswith(f"https://local.example.test:{lease.external_port}/x")
    listed = gw.op_list()["leases"][0]
    assert listed["named_url"] == listed["url"] and listed["port_url"].endswith(str(lease.external_port))


def test_no_named_url_when_the_front_could_not_bind(tmp_path):
    busy = socket.socket(); busy.bind(("127.0.0.1", 0)); busy.listen(1)
    registry = DevServiceRegistry(store_path=tmp_path / "l.json", port_range=(42300, 42400))
    gw = FabricGateway(registry, GatewayRouter(["127.0.0.1"]), control_host="127.0.0.1", control_port=0,
                       url_base="local.example.test", probe_interval=30, token="",
                       named_domain="local.example.test", named_port=busy.getsockname()[1])
    gw.start()
    try:
        lease = registry.register(target_machine_id="m", target_host="127.0.0.1", target_port=3000)
        assert gw.named_front is None and gw.named_url(lease.to_dict()) is None
        r = gw.op_resolve_urls(["http://localhost:3000/"], "m")["resolutions"][0]
        assert r["canonical"].startswith("http://local.example.test:") and r["port_url"] is None
    finally:
        gw.stop()
        busy.close()


def test_access_policy_still_applies_on_the_named_front(stack):
    gw, lease, port = stack

    class Deny:
        enforcing = True

        def decide(self, peer):
            return False, "test denies everyone"
    assert _get(port, "docs.local.example.test").startswith(b"HTTP/1.")
    gw.router.access = Deny()
    assert _get(port, "docs.local.example.test") == b""
    assert _get(port, "docs.local.example.test", tls=False) == b""


def test_bare_fleet_names_reach_the_apex_upstream_only(tmp_path):
    dash, dport = _http_target(tmp_path)
    cert, key = _self_signed(tmp_path / "pub", "local.example.test", 30)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(cert), str(key))
    registry = DevServiceRegistry(store_path=tmp_path / "l.json", port_range=(42500, 42600))
    named = _free_port()
    gw = FabricGateway(registry, GatewayRouter(["127.0.0.1"]), control_host="127.0.0.1", control_port=0,
                       url_base="local.example.test", probe_interval=30, token="", named_domain="local.example.test",
                       named_port=named, url_aliases=("dev.lan",), apex_upstream=("127.0.0.1", dport))
    gw.start()
    gw.enable_tls(ctx)
    try:
        for host in ("dev.lan", "local.example.test"):
            assert _get(named, host).startswith(b"HTTP/1."), host
            assert _get(named, host, tls=False).startswith(b"HTTP/1."), host
        assert _get(named, "evil.example") == b""
        assert _get(named, "unknown.local.example.test") == b""          # a subdomain is a lease name, not the apex
    finally:
        gw.stop()
        dash.shutdown()
    # no upstream configured: bare names are dropped like any other unknown name
    gw2 = FabricGateway(DevServiceRegistry(store_path=tmp_path / "m.json", port_range=(42700, 42800)), GatewayRouter(["127.0.0.1"]),
                        control_host="127.0.0.1", control_port=0, url_base="local.example.test", probe_interval=30, token="",
                        named_domain="local.example.test", named_port=_free_port())
    gw2.start()
    try:
        assert _get(gw2.named_front.port, "local.example.test", tls=False) == b""
    finally:
        gw2.stop()


def test_exact_host_routes_take_over_a_name_without_opening_its_subdomains(tmp_path):
    a, aport = _http_target(tmp_path)
    cert, key = _self_signed(tmp_path / "pub", "bender.tail.ts.net", 30)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(cert), str(key))
    named = _free_port()
    gw = FabricGateway(DevServiceRegistry(store_path=tmp_path / "l.json", port_range=(43000, 43100)), GatewayRouter(["127.0.0.1"]),
                       control_host="127.0.0.1", control_port=0, url_base="local.example.test", probe_interval=30, token="",
                       named_domain="local.example.test", named_port=named, host_routes={"Bender.Tail.ts.net": ("127.0.0.1", aport)})
    gw.start()
    gw.enable_tls(ctx)
    try:
        assert _get(named, "bender.tail.ts.net").startswith(b"HTTP/1.")
        assert _get(named, "x.bender.tail.ts.net") == b""
        assert _get(named, "local.example.test") == b""
    finally:
        gw.stop()
        a.shutdown()

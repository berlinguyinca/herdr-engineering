"""Fleet names: the public name is canonical, legacy names are accepted, and the agent/forwarder know them."""
import json
import socket
import urllib.request

from herdr_engineering import fabric_agent as fa
from herdr_engineering.control_plane.api import ControlPlaneServer  # noqa: F401  (import check for the kwarg)
from herdr_engineering.fabric_urls import resolve_url
from tests.test_fabric import _gateway

LEASES = [{"id": "L1", "external_port": 3188, "label": "web", "state": "active",
           "target": {"machine_id": "bender", "host": "100.64.0.9", "port": 3188}}]


def test_alias_input_canonicalises_to_the_public_name():
    r = resolve_url("https://dev.lan:3188/x?y=1", LEASES, url_base="local.example.test", aliases=("dev.lan",))
    assert r.status == "resolved" and r.canonical == "https://local.example.test:3188/x?y=1"
    sub = resolve_url("https://svc.dev.lan:3188/", LEASES, url_base="local.example.test", aliases=("dev.lan",))
    assert sub.canonical == "https://local.example.test:3188/"


def test_unknown_or_lookalike_hosts_are_not_fabric_names():
    for host in ("evil.example", "notdev.lan", "dev.lan.evil.example"):
        r = resolve_url(f"https://{host}:3188/", LEASES, url_base="local.example.test", aliases=("dev.lan",))
        assert r.status == "not_loopback", host
    # without the alias dev.lan is just another external host
    assert resolve_url("https://dev.lan:3188/", LEASES, url_base="local.example.test").status == "not_loopback"


def test_gateway_reports_names_and_resolves_aliases(tmp_path):
    gw, _, _ = _gateway(tmp_path, url_base="local.example.test")
    gw.url_aliases = ("dev.lan",)
    try:
        base = f"http://127.0.0.1:{gw.control_port}"
        names = json.loads(urllib.request.urlopen(f"{base}/names", timeout=5).read())
        assert names == {"url_base": "local.example.test", "aliases": ["dev.lan"], "scheme": "http"}
        assert json.loads(urllib.request.urlopen(f"{base}/healthz", timeout=5).read())["url_aliases"] == ["dev.lan"]
        assert gw.op_names()["url_base"] == "local.example.test"
    finally:
        gw.stop()


def test_agent_forwarder_rewrites_host_for_fleet_domains_and_subdomains_only():
    def through(host):
        from tests.test_fabric_agent import _echo_host_server
        import socket as _s
        srv, seen = _echo_host_server()
        fwd = fa.AgentForwarder("127.0.0.1", srv.getsockname()[1], fabric_domains=frozenset({"local.example.test"})).start()
        try:
            c = _s.create_connection(("127.0.0.1", fwd.port), timeout=5)
            c.sendall(f"GET / HTTP/1.1\r\nHost: {host}:9990\r\nAccept: */*\r\n\r\n".encode())
            while c.recv(4096):
                pass
            c.close()
            return seen[0], srv.getsockname()[1]
        finally:
            fwd.stop()
            srv.close()
    got, port = through("Svc.Local.Example.Test")
    assert f"Host: 127.0.0.1:{port}".encode() in got
    got, port = through("local.example.test")
    assert f"Host: 127.0.0.1:{port}".encode() in got
    for other in ("evil.example", "notlocal.example.test", "local.example.test.evil.example"):
        got, port = through(other)
        assert other.encode() in got and f"127.0.0.1:{port}".encode() not in got, other


def test_agent_config_exposes_domains_normalised():
    cfg = fa.AgentConfig(tailnet_ip="100.64.0.1", store_path=None, fabric_domains=("Local.Example.Test.", "", "dev.lan"))
    assert cfg.domains() == frozenset({"local.example.test", "dev.lan"})

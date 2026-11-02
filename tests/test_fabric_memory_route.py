"""The memory.<domain> / memory.dev.lan routes through the existing fabric gateway: off by default, switched by
scripts/fabric-stack.sh memory-route, parsed by the gateway's own --route parser, HTTPS-only in practice (the gateway
terminates TLS with its existing certificates; unknown names stay silent). Local sockets only."""
import shutil
import ssl
import subprocess
from pathlib import Path

import pytest
import yaml

from herdr_engineering import cli
from herdr_engineering.devfabric import DevServiceRegistry
from herdr_engineering.fabric import FabricGateway, GatewayRouter
from tests.test_fabric import _http_target
from tests.test_fabric_certs import _self_signed
from tests.test_fabric_named import _free_port, _get

ROOT = Path(__file__).resolve().parents[1]
GNU = "/opt/homebrew/opt/coreutils/libexec/gnubin"


@pytest.fixture
def tree(tmp_path):
    """A copy of the pieces fabric-stack.sh resolves relative to itself, with stub docker/tailscale."""
    (tmp_path / "scripts").mkdir()
    shutil.copy(ROOT / "scripts/fabric-stack.sh", tmp_path / "scripts/fabric-stack.sh")
    (tmp_path / "deploy/fabric-stack").mkdir(parents=True)
    shutil.copy(ROOT / "deploy/fabric-stack/config.yaml", tmp_path / "deploy/fabric-stack/config.yaml")
    (tmp_path / "deploy/control-plane").mkdir(parents=True)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for n in ("docker", "tailscale"):
        (bindir / n).write_text("#!/bin/sh\nexit 0\n")
        (bindir / n).chmod(0o755)
    return tmp_path, bindir


def routes(tmp_path):
    return yaml.safe_load((tmp_path / "deploy/fabric-stack/config.yaml").read_text())["fabric"]["routes"]


def script(tree, *args):
    tmp, bindir = tree
    import os
    return subprocess.run(["bash", str(tmp / "scripts/fabric-stack.sh"), "memory-route", *args], capture_output=True,
                          text=True, env=dict(os.environ, PATH=f"{bindir}:{GNU}:{os.environ['PATH']}"), timeout=30)


def test_routes_are_off_by_default_in_the_shipped_config():
    cfg = yaml.safe_load((ROOT / "deploy/fabric-stack/config.yaml").read_text())["fabric"]
    assert cfg["routes"] == ["bender.tail0c50da.ts.net=127.0.0.1:8787"]
    assert cfg["acme_domain"] and "dev.lan" in cfg["url_aliases"]
    text = (ROOT / "deploy/fabric-stack/config.yaml").read_text()
    assert text.count("# herdr-memory-route") == 2


def test_enable_and_disable_toggle_exactly_the_two_marked_lines(tree):
    tmp, _ = tree
    original = (tmp / "deploy/fabric-stack/config.yaml").read_text()
    r = script(tree, "enable")
    assert r.returncode == 0, r.stderr
    assert routes(tmp) == ["bender.tail0c50da.ts.net=127.0.0.1:8787", "memory.local.metabolomics.us=127.0.0.1:8888",
                           "memory.dev.lan=127.0.0.1:8888"]
    assert "recreate the fabric-gateway container" in r.stdout
    assert script(tree, "status").stdout.count("herdr-memory-route") == 2
    assert script(tree, "disable").returncode == 0
    assert (tmp / "deploy/fabric-stack/config.yaml").read_text() == original
    assert not list((tmp / "deploy/fabric-stack").glob("*.bak"))
    assert script(tree, "bogus").returncode == 2


def test_gateway_parses_the_enabled_routes_and_serves_only_those_names_over_tls(tree, tmp_path):
    tmp, _ = tree
    script(tree, "enable")
    parsed = dict(cli._route(r) for r in routes(tmp))
    assert parsed["memory.dev.lan"] == ("127.0.0.1", 8888) == parsed["memory.local.metabolomics.us"]
    target, tport = _http_target(tmp_path)                       # stands in for the loopback Hindsight API
    cert, key = _self_signed(tmp_path / "ca", "memory.dev.lan", 30)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(cert), str(key))
    named = _free_port()
    host_routes = {h: ("127.0.0.1", tport) for h in parsed if h.startswith("memory.")}
    gw = FabricGateway(DevServiceRegistry(store_path=tmp_path / "l.json", port_range=(43200, 43300)),
                       GatewayRouter(["127.0.0.1"]), control_host="127.0.0.1", control_port=0,
                       url_base="local.example.test", probe_interval=30, token="",
                       named_domain="local.example.test", named_port=named, host_routes=host_routes)
    gw.start()
    gw.enable_tls(ctx)
    try:
        assert _get(named, "memory.dev.lan").startswith(b"HTTP/1.")
        assert _get(named, "memory.local.metabolomics.us").startswith(b"HTTP/1.")
        assert _get(named, "other.dev.lan") == b""                # unknown names are dropped silently
        assert _get(named, "x.memory.dev.lan") == b""             # the route opens no subdomains
        # Existing gateway behaviour for EVERY named route: the listener sniffs the first byte, so a plain-HTTP request to
        # :443 is also proxied. Documented in memory-hindsight.md; the herdr-memory client refuses http:// off loopback,
        # so nothing configures it, and the tailnet is WireGuard-encrypted regardless. Pinned here so a change shows up.
        assert _get(named, "memory.dev.lan", tls=False).startswith(b"HTTP/1.")
    finally:
        gw.stop()
        target.shutdown()


def test_coredns_wildcard_already_answers_memory_dev_lan():
    zone = (ROOT / "deploy/fabric-stack/coredns/dev.lan.zone.tmpl").read_text()
    assert "\n*   IN A   {{GATEWAY_TAILNET_IP}}" in zone       # *.dev.lan -> the gateway: no per-service record needed

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
    """Run the real script with HOME in the temp tree: the host-local route file lives under $HOME/.config."""
    tmp, bindir = tree
    import os
    return subprocess.run(["bash", str(tmp / "scripts/fabric-stack.sh"), "memory-route", *args], capture_output=True,
                          text=True, env=dict(os.environ, HOME=str(tmp / "home"), PATH=f"{bindir}:{GNU}:{os.environ['PATH']}"),
                          timeout=30)


def local_file(tmp_path):
    return tmp_path / "home/.config/herdr-engineering/fabric/memory-route.yaml"


def local_routes(tmp_path):
    return yaml.safe_load(local_file(tmp_path).read_text())["fabric"]["local_routes"]


def test_routes_are_off_by_default_in_the_shipped_config():
    cfg = yaml.safe_load((ROOT / "deploy/fabric-stack/config.yaml").read_text())["fabric"]
    assert cfg["routes"] == ["bender.tail0c50da.ts.net=127.0.0.1:8787"]
    assert cfg["acme_domain"] and "dev.lan" in cfg["url_aliases"]
    text = (ROOT / "deploy/fabric-stack/config.yaml").read_text()
    assert text.count("# herdr-memory-route") == 2


def test_enable_writes_a_host_local_route_file_and_leaves_the_shipped_config_alone(tree):
    tmp, _ = tree
    original = (tmp / "deploy/fabric-stack/config.yaml").read_text()
    r = script(tree, "enable", "8897")
    assert r.returncode == 0, r.stderr
    assert local_routes(tmp) == ["memory.local.metabolomics.us=127.0.0.1:8897", "memory.dev.lan=127.0.0.1:8897"]
    assert (tmp / "deploy/fabric-stack/config.yaml").read_text() == original      # the checkout is never edited
    assert "docker restart herdr-fabric-gateway" in r.stdout
    assert "8897" in script(tree, "status").stdout
    assert script(tree, "enable").returncode == 0                                   # default port, idempotent rewrite
    assert local_routes(tmp)[-1] == "memory.dev.lan=127.0.0.1:8888"
    assert script(tree, "disable").returncode == 0
    assert not local_file(tmp).exists()
    assert "off" in script(tree, "status").stdout
    assert script(tree, "bogus").returncode == 2
    assert script(tree, "enable", "not-a-port").returncode == 2


def test_the_memory_route_file_adds_to_the_baked_routes_and_a_missing_one_is_skipped(tree, monkeypatch):
    """What the gateway container does: HERDR_ENGINEERING_CONFIG lists the baked file then the host-local one."""
    from herdr_engineering.config import load_config
    tmp, _ = tree
    baked = tmp / "deploy/fabric-stack/config.yaml"
    local = local_file(tmp)
    monkeypatch.setenv("HOME", str(tmp / "empty-home"))
    monkeypatch.chdir(tmp)
    monkeypatch.setenv("HERDR_ENGINEERING_CONFIG", f"{baked}:{local}")
    assert load_config()["fabric"]["routes"] == ["bender.tail0c50da.ts.net=127.0.0.1:8787"]      # no local file yet
    script(tree, "enable", "8897")
    cfg = load_config()["fabric"]
    assert cfg["routes"] == ["bender.tail0c50da.ts.net=127.0.0.1:8787"]            # the baked list is never replaced
    assert cfg["local_routes"] == ["memory.local.metabolomics.us=127.0.0.1:8897", "memory.dev.lan=127.0.0.1:8897"]
    assert cfg["acme_domain"] == "local.metabolomics.us"                          # the rest still comes from the baked file


def test_the_gateway_service_mounts_the_host_local_dir_and_lists_it_after_the_baked_config():
    svc = yaml.safe_load((ROOT / "deploy/control-plane/compose.yaml").read_text())["services"]["fabric-gateway"]
    assert "${HOME}/.config/herdr-engineering/fabric:/fabric-local:ro" in svc["volumes"]
    assert svc["environment"]["HERDR_ENGINEERING_CONFIG"] == "/app/config.yaml:/fabric-local/memory-route.yaml"


def test_gateway_parses_the_enabled_routes_and_serves_only_those_names_over_tls(tree, tmp_path):
    tmp, _ = tree
    script(tree, "enable")
    parsed = dict(cli._route(r) for r in local_routes(tmp))
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


def test_enable_and_disable_touch_only_the_memory_route_file(tree):
    tmp, _ = tree
    other = tmp / "home/.config/herdr-engineering/fabric/config.yaml"
    other.parent.mkdir(parents=True)
    other.write_text("fabric:\n  node_id: n1\n  peer_url: http://peer\n")
    assert script(tree, "enable", "8897").returncode == 0
    assert script(tree, "disable").returncode == 0
    assert other.read_text() == "fabric:\n  node_id: n1\n  peer_url: http://peer\n"


def test_enable_fails_loudly_and_leaves_no_file_when_the_dir_is_unwritable(tree):
    tmp, _ = tree
    d = tmp / "home/.config/herdr-engineering/fabric"
    d.mkdir(parents=True)
    d.chmod(0o555)
    try:
        r = script(tree, "enable", "8897")
    finally:
        d.chmod(0o755)
    assert r.returncode != 0 and "enabled" not in r.stdout
    assert not local_file(tmp).exists() and not (d / "memory-route.yaml.new").exists()


def test_enable_fails_when_the_shipped_config_has_no_memory_route_lines(tree):
    tmp, _ = tree
    (tmp / "deploy/fabric-stack/config.yaml").write_text("fabric:\n  routes:\n    - \"a=127.0.0.1:1\"\n")
    r = script(tree, "enable")
    assert r.returncode != 0 and "enabled" not in r.stdout and not local_file(tmp).exists()


def test_fabric_stack_up_creates_the_local_dir_and_refuses_a_file_in_its_place(tree):
    tmp, _ = tree
    import os
    env = dict(os.environ, HOME=str(tmp / "home"), PATH=f"{tree[1]}:{GNU}:{os.environ['PATH']}")
    d = tmp / "home/.config/herdr-engineering/fabric"
    d.parent.mkdir(parents=True)
    d.write_text("not a dir")
    r = subprocess.run(["bash", str(tmp / "scripts/fabric-stack.sh"), "up"], capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode != 0 and "must be a real directory" in r.stderr
    d.unlink()
    subprocess.run(["bash", str(tmp / "scripts/fabric-stack.sh"), "up"], capture_output=True, text=True, env=env, timeout=60)
    assert d.is_dir() and d.stat().st_uid == os.getuid()


def test_a_single_config_path_containing_a_colon_is_used_whole_and_a_missing_listed_part_warns(tmp_path, monkeypatch, caplog):
    from herdr_engineering.config import load_config
    odd = tmp_path / "a:b" / "c.yaml"
    odd.parent.mkdir()
    odd.write_text("fabric:\n  probe_interval_seconds: 77\n")
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HERDR_ENGINEERING_CONFIG", str(odd))
    assert load_config()["fabric"]["probe_interval_seconds"] == 77
    monkeypatch.setenv("HERDR_ENGINEERING_CONFIG", f"{odd.parent}/x.yaml:{tmp_path}/gone.yaml")
    with caplog.at_level("WARNING"):
        load_config()
    assert "gone.yaml" in caplog.text

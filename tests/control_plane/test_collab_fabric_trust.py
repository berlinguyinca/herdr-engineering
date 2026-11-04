"""Fabric trust slice 1: peer identity. Real unix-socket LocalAPI server, real TCP peer addresses, no mocks."""
from __future__ import annotations

import json
import os
import socketserver
import threading
import time
from http.server import BaseHTTPRequestHandler

import pytest

from herdr_engineering.control_plane.collab import CollabService, StaticTokenAuthenticator
from herdr_engineering.control_plane.collab_fabric_trust import (
    FabricPeerResolver,
    normalize_fabric_trust,
)
from herdr_engineering.fabric_access import TailscaleWhois

NODES = {"100.64.0.1": "bender.tail1234.ts.net.", "100.64.0.2": "beast.tail1234.ts.net.",
         "100.64.0.9": "stranger.tail1234.ts.net."}


class _Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


@pytest.fixture
def localapi(tmp_path):
    """A real tailscaled-LocalAPI-shaped server on a unix socket; counts requests."""
    state = {"hits": 0, "nodes": dict(NODES)}

    class H(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            state["hits"] += 1
            ip = self.path.split("addr=")[1].split("%3A")[0].split(":")[0]
            name = state["nodes"].get(ip)
            if name is None:
                self.send_response(404); self.end_headers(); return
            body = json.dumps({"Node": {"Name": name, "Tags": ["tag:fabric"]}, "UserProfile": {"LoginName": "tagged-devices"}}).encode()
            self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

        def log_message(self, *a):
            pass

        def address_string(self):
            return "unix"
    sock = str(tmp_path / "tailscaled.sock")
    srv = _Server(sock, H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    state["sock"] = sock
    yield state
    srv.shutdown(); srv.server_close()


def cfg(**kw):
    return normalize_fabric_trust({"enabled": True, "machines": ["bender", "beast"], **kw})


def resolver(localapi, config, now=time.monotonic):
    return FabricPeerResolver(lambda: config() if callable(config) else config,
                              whois=TailscaleWhois(sockets=(localapi["sock"],), timeout=2.0), now=now)


# ---------------------------------------------------------------- resolver
def test_allowed_machine_resolves_to_short_name(localapi):
    r = resolver(localapi, cfg())
    assert r.resolve("100.64.0.1") == "bender" and r.resolve("100.64.0.2") == "beast"


def test_real_tcp_peer_address_is_what_the_resolver_takes(localapi):
    import socket
    srv = socket.socket(); srv.bind(("127.0.0.1", 0)); srv.listen(1)
    c = socket.create_connection(srv.getsockname()); conn, peer = srv.accept()
    try:
        assert resolver(localapi, cfg()).resolve(peer[0]) is None       # loopback is not a tailnet node
    finally:
        c.close(); conn.close(); srv.close()


def test_non_allow_listed_unknown_and_malformed_peers_are_none(localapi):
    r = resolver(localapi, cfg())
    assert r.resolve("100.64.0.9") is None          # on the tailnet, not allow-listed
    assert r.resolve("100.64.0.77") is None         # tailscaled does not know it
    assert r.resolve("not-an-ip") is None and r.resolve("") is None and r.resolve(None) is None


def test_unreachable_tailscaled_fails_closed(tmp_path):
    r = FabricPeerResolver(lambda: cfg(), whois=TailscaleWhois(sockets=(str(tmp_path / "nope.sock"),), timeout=0.5))
    assert r.resolve("100.64.0.1") is None


def test_flag_off_and_kill_switch_resolve_to_none_without_asking_tailscaled(localapi):
    assert resolver(localapi, normalize_fabric_trust(None)).resolve("100.64.0.1") is None
    assert resolver(localapi, cfg(enabled=False)).resolve("100.64.0.1") is None
    assert resolver(localapi, cfg(kill_switch=True)).resolve("100.64.0.1") is None
    assert localapi["hits"] == 0


def test_config_source_failure_fails_closed(localapi):
    def boom():
        raise ValueError("unusable")
    assert resolver(localapi, boom).resolve("100.64.0.1") is None


def test_flag_flip_takes_effect_on_next_call_despite_cache(localapi):
    box = {"c": cfg()}
    r = resolver(localapi, lambda: box["c"])
    assert r.resolve("100.64.0.1") == "bender"
    box["c"] = cfg(kill_switch=True)
    assert r.resolve("100.64.0.1") is None


def test_removing_a_machine_from_the_list_takes_effect_despite_cache(localapi):
    box = {"c": cfg()}
    r = resolver(localapi, lambda: box["c"])
    assert r.resolve("100.64.0.2") == "beast"
    box["c"] = cfg(machines=["bender"])
    assert r.resolve("100.64.0.2") is None


def test_cache_ttl_is_honoured(localapi):
    t = [100.0]
    r = resolver(localapi, cfg(whois_cache_seconds=30), now=lambda: t[0])
    r.resolve("100.64.0.1"); r.resolve("100.64.0.1")
    assert localapi["hits"] == 1
    t[0] += 29
    r.resolve("100.64.0.1")
    assert localapi["hits"] == 1
    t[0] += 2
    r.resolve("100.64.0.1")
    assert localapi["hits"] == 2


def test_errors_are_not_cached(localapi, tmp_path):
    r = FabricPeerResolver(lambda: cfg(), whois=TailscaleWhois(sockets=(str(tmp_path / "late.sock"),), timeout=0.5))
    assert r.resolve("100.64.0.1") is None
    r.whois = TailscaleWhois(sockets=(localapi["sock"],))
    assert r.resolve("100.64.0.1") == "bender"


def test_spoofed_forwarded_for_cannot_even_be_passed(localapi):
    import inspect
    r = resolver(localapi, cfg())
    assert list(inspect.signature(r.resolve).parameters) == ["peer_ip"]
    with pytest.raises(TypeError):
        r.resolve("100.64.0.9", headers={"X-Forwarded-For": "100.64.0.1"})  # type: ignore[call-arg]
    assert r.resolve("100.64.0.9") is None


# ---------------------------------------------------------------- config
def test_absent_config_is_off():
    assert normalize_fabric_trust(None) == {"enabled": False, "kill_switch": False, "machines": [], "whois_cache_seconds": 30}


def test_valid_config_is_normalized():
    c = normalize_fabric_trust({"enabled": True, "machines": ["Bender", "beast"], "whois_cache_seconds": 5})
    assert c["machines"] == ["bender", "beast"] and c["whois_cache_seconds"] == 5 and c["enabled"] is True


@pytest.mark.parametrize("raw", [
    "yes", ["x"], {"bogus": 1}, {"enabled": "yes"}, {"kill_switch": 1}, {"machines": "bender"}, {"machines": [1]},
    {"machines": [""]}, {"machines": ["bender.tail.ts.net"]}, {"machines": ["a b"]}, {"whois_cache_seconds": True},
    {"whois_cache_seconds": "30"}, {"whois_cache_seconds": -1}, {"whois_cache_seconds": 99999},
])
def test_invalid_config_raises(raw):
    with pytest.raises(ValueError):
        normalize_fabric_trust(raw)


def test_enabled_without_machines_is_rejected():
    with pytest.raises(ValueError):
        normalize_fabric_trust({"enabled": True})


# ---------------------------------------------------------------- collab wiring
def test_collab_service_reads_fabric_trust_per_request():
    svc = CollabService(StaticTokenAuthenticator({"a": "0" * 64}))
    assert svc.fabric_trust_active() is False
    box = {"v": {"enabled": True, "machines": ["bender"]}}
    svc.fabric_trust_source = lambda: box["v"]
    assert svc.fabric_trust_active() is True
    box["v"] = {"enabled": True, "machines": ["bender"], "kill_switch": True}
    assert svc.fabric_trust_active() is False
    box["v"] = {"enabled": True, "kill_switch": "yes"}
    assert svc.fabric_trust_active() is False


def test_unix_socket_env_is_not_consulted(monkeypatch, localapi):
    monkeypatch.setenv("TS_SOCKET", "/nonexistent")
    assert os.environ["TS_SOCKET"] and resolver(localapi, cfg()).resolve("100.64.0.1") == "bender"

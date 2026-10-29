import json
import os
import socketserver
import threading

import pytest

from herdr_engineering.fabric_access import AccessPolicy, Identity, TailscaleWhois
from tests.test_fabric import _client, _gateway
from tests.test_fabric_tls_gateway import Origin  # noqa: F401  (handler reuse)

ALICE = Identity("alice@example.com", (), "alice-mac")
TAGGED = Identity(None, ("tag:dev",), "ci-box")
GUEST = Identity("guest@other.org", (), "guest-phone")


def policy(table, **kw):
    calls = []

    def whois(ip):
        calls.append(ip)
        v = table[ip]
        if isinstance(v, Exception):
            raise v
        return v
    p = AccessPolicy(users=kw.pop("users", {"alice@example.com"}), tags=kw.pop("tags", {"tag:dev"}), whois=whois, **kw)
    p.calls = calls
    return p


def test_no_policy_allows_everyone():
    assert AccessPolicy().decide("100.64.0.9")[0]


def test_user_tag_and_guest_decisions():
    p = policy({"100.64.0.1": ALICE, "100.64.0.2": TAGGED, "100.64.0.3": GUEST, "100.64.0.4": None})
    assert p.decide("100.64.0.1")[0] and p.decide("100.64.0.2")[0]
    ok, why = p.decide("100.64.0.3")
    assert not ok and "allow-list" in why
    assert not p.decide("100.64.0.4")[0]


def test_user_match_is_case_insensitive():
    assert policy({"100.64.0.1": Identity("ALICE@Example.com")}).decide("100.64.0.1")[0]


def test_lookup_failure_fails_closed_and_is_not_cached():
    p = policy({"100.64.0.1": OSError("tailscaled down")})
    assert not p.decide("100.64.0.1")[0]
    p.whois = lambda ip: ALICE
    assert p.decide("100.64.0.1")[0]


def test_verdicts_are_cached_then_expire():
    t = [0.0]
    p = policy({"100.64.0.1": ALICE}, now=lambda: t[0], cache_seconds=30)
    p.decide("100.64.0.1"); p.decide("100.64.0.1")
    assert len(p.calls) == 1
    t[0] = 31
    p.decide("100.64.0.1")
    assert len(p.calls) == 2


def test_loopback_trusted_by_default_not_when_disabled():
    p = policy({})
    assert p.decide("127.0.0.1")[0] and p.decide("::1")[0]
    assert not policy({}, trust_loopback=False).decide("127.0.0.1")[0]


@pytest.mark.parametrize("peer", [None, "", "not-an-ip", "999.1.1.1"])
def test_garbage_peers_denied(peer):
    assert not policy({}).decide(peer)[0]


def test_local_api_client_parses_whois(tmp_path):
    sock = str(tmp_path / "ts.sock")

    class H(socketserver.StreamRequestHandler):
        def handle(self):
            line = self.rfile.readline().decode()
            while self.rfile.readline() not in (b"\r\n", b""):
                pass
            if "100.64.0.9" in line:
                self.wfile.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")
                return
            body = json.dumps({"Node": {"Name": "ci-box.tail.ts.net.", "Tags": ["tag:dev"]},
                               "UserProfile": {"LoginName": "tagged-devices"}}).encode()
            self.wfile.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n" % len(body) + body)

    srv = socketserver.UnixStreamServer(sock, H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        w = TailscaleWhois([sock])
        ident = w.lookup("100.64.0.5")
        assert ident == Identity(None, ("tag:dev",), "ci-box.tail.ts.net")
        assert w.lookup("100.64.0.9") is None
        with pytest.raises(OSError):
            TailscaleWhois([str(tmp_path / "missing.sock")]).lookup("100.64.0.5")
    finally:
        srv.shutdown(); srv.server_close(); os.unlink(sock) if os.path.exists(sock) else None


def test_router_drops_denied_peer_before_speaking_http(tmp_path):
    import http.server
    import socket
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Origin)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    gw, _, router = _gateway(tmp_path)
    # the test peer is loopback; disable the loopback trust so the policy must say no
    router.access = policy({}, trust_loopback=False)
    try:
        lease = _client(gw).register(target_machine_id="bender", target_host="127.0.0.1",
                                     target_port=server.server_address[1])["lease"]
        before = len(Origin.seen)
        with socket.create_connection(("127.0.0.1", lease["external_port"]), timeout=5) as s:
            s.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
            s.settimeout(5)
            try:
                data = s.recv(100)
            except ConnectionResetError:
                data = b""
            assert data == b""
        assert len(Origin.seen) == before and router.access.denied >= 1
        router.access = policy({}, users={"alice@example.com"})   # loopback trusted again
        with socket.create_connection(("127.0.0.1", lease["external_port"]), timeout=5) as s:
            s.sendall(b"GET / HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
            out = b""
            while chunk := s.recv(65536):
                out += chunk
        assert out.endswith(b"hello")
    finally:
        gw.stop(); server.shutdown()

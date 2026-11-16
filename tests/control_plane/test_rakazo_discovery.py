"""Zero-typing onboarding: GET /.well-known/herdr-shepherd and the whois-verified tailnet owner session.
Real RakazoBridge, a real unix-socket LocalAPI-shaped whois server and real TCP for the HTTP route; no mocks."""
import json
import socketserver
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler

import pytest

from herdr_engineering.control_plane import rakazo_bridge as rb
from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.assistant import AssistantService, assistant_config
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from herdr_engineering.fabric_access import TailscaleWhois
from tests.io_timeouts import HTTP_TIMEOUT

ACCOUNTS = [{"email": "alice@example.com", "password_scrypt": rb.hash_password("pw pw pw pw pw", n=2 ** 14), "role": "operator"}]
NODES = {"100.64.0.5": "phone.tail1234.ts.net.", "fd7a:115c:a1e0::5": "phone.tail1234.ts.net."}
WK = "/.well-known/herdr-shepherd"


class _Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


@pytest.fixture
def localapi(tmp_path):
    state = {"hits": 0}

    class H(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            from urllib.parse import unquote
            state["hits"] += 1
            ip = unquote(self.path.split("addr=")[1]).rsplit(":", 1)[0].strip("[]")
            name = NODES.get(ip)
            if name is None:
                self.send_response(404); self.end_headers(); return
            body = json.dumps({"Node": {"Name": name}, "UserProfile": {"LoginName": "me@example.com"}}).encode()
            self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

        def log_message(self, *a):
            pass

        def address_string(self):
            return "unix"
    sock = str(tmp_path / "ts.sock")
    srv = _Server(sock, H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    state["sock"] = sock
    yield state
    srv.shutdown(); srv.server_close()


def bridge(localapi, **over):
    cfg = rb.rakazo_bridge_config({"enabled": True, "accounts": ACCOUNTS, **over})
    svc = AssistantService(config=assistant_config({"personal_assistant": {"enabled": True}}))
    return rb.RakazoBridge(cfg, svc, whois=TailscaleWhois(sockets=(localapi["sock"],), timeout=2.0))


def get(b, peer, xff=""):
    return b.handle("GET", WK, {"x-forwarded-for": xff} if xff else {}, b"", peer)


def body(resp):
    return json.loads(resp.body)


def test_well_known_identifies_a_tailnet_peer_without_credentials(localapi):
    b = bridge(localapi, public_url="https://local.example.ts.net", name="Home", tailnet_open_owner=True)
    r = get(b, "100.64.0.5")
    assert r.status == 200
    assert body(r) == {"name": "Home", "bridge_url": "https://local.example.ts.net", "api_version": 1,
                       "auth_modes": ["tailnet", "password"], "tailnet": True}


def test_well_known_says_tailnet_false_for_strangers_loopback_and_public(localapi):
    b = bridge(localapi, tailnet_open_owner=True)
    for peer in ("100.64.0.99", "127.0.0.1", "8.8.8.8", "192.168.1.4", "not-an-ip"):
        d = body(get(b, peer))
        assert d["tailnet"] is False and d["auth_modes"] == ["password"], peer
    assert localapi["hits"] == 1          # only the tailnet-range stranger cost a whois


def test_well_known_ignores_a_spoofed_forwarded_for_from_an_untrusted_peer(localapi):
    b = bridge(localapi, tailnet_open_owner=True)
    assert body(get(b, "8.8.8.8", xff="100.64.0.5"))["tailnet"] is False
    assert body(get(b, "127.0.0.1", xff="100.64.0.5"))["tailnet"] is False


def test_well_known_believes_forwarded_for_only_from_a_trusted_proxy(localapi):
    b = bridge(localapi, tailnet_open_owner=True, trusted_proxies=["10.0.0.2"])
    assert body(get(b, "10.0.0.2", xff="100.64.0.5"))["tailnet"] is True
    assert body(get(b, "10.0.0.2", xff="100.64.0.99"))["tailnet"] is False
    assert body(get(b, "10.0.0.2"))["tailnet"] is False


def test_well_known_fails_closed_when_whois_is_unreachable(tmp_path, localapi):
    cfg = rb.rakazo_bridge_config({"enabled": True, "accounts": ACCOUNTS, "tailnet_open_owner": True})
    b = rb.RakazoBridge(cfg, AssistantService(config=assistant_config({"personal_assistant": {"enabled": True}})),
                        whois=TailscaleWhois(sockets=(str(tmp_path / "none.sock"),), timeout=0.3))
    assert body(get(b, "100.64.0.5"))["tailnet"] is False


def test_well_known_leaks_no_secret_or_identity(localapi):
    b = bridge(localapi, tailnet_open_owner=True)
    raw = get(b, "100.64.0.5").body.decode()
    for needle in ("alice", "example.com", "scrypt", "token", "room", "me@"):
        assert needle not in raw, needle
    assert set(json.loads(raw)) == {"name", "bridge_url", "api_version", "auth_modes", "tailnet"}


def test_well_known_only_answers_get(localapi):
    b = bridge(localapi)
    assert b.handle("POST", WK, {}, b"", "100.64.0.5").status == 405


def test_well_known_verdict_is_cached(localapi):
    b = bridge(localapi)
    get(b, "100.64.0.5"); get(b, "100.64.0.5")
    assert localapi["hits"] == 1


# ------------------------------------------------------------ session
def open_session(b, peer, origin="rakazo://", xff=""):
    h = {"origin": origin}
    if xff:
        h["x-forwarded-for"] = xff
    return b.handle("POST", "/api/auth/open", h, b"{}", peer)


def test_tailnet_open_owner_gives_the_operator_session_to_an_identified_peer(localapi):
    b = bridge(localapi, tailnet_open_owner=True)
    r = open_session(b, "100.64.0.5")
    assert r.status == 200 and json.loads(r.body)["token"]


def test_tailnet_open_owner_is_off_by_default(localapi):
    b = bridge(localapi)
    assert open_session(b, "100.64.0.5").status == 404


def test_tailnet_open_owner_refuses_unidentified_and_loopback_peers(localapi):
    b = bridge(localapi, tailnet_open_owner=True)
    for peer in ("100.64.0.99", "127.0.0.1", "8.8.8.8", "192.168.1.4"):
        assert open_session(b, peer).status == 403, peer


def test_tailnet_open_owner_still_wants_the_app_origin(localapi):
    b = bridge(localapi, tailnet_open_owner=True)
    for origin in ("https://evil.example", ""):
        assert open_session(b, "100.64.0.5", origin).status == 403


def test_tailnet_open_owner_does_not_trust_a_forwarded_header_from_an_untrusted_peer(localapi):
    b = bridge(localapi, tailnet_open_owner=True)
    assert open_session(b, "8.8.8.8", xff="100.64.0.5").status == 403


def test_plain_open_owner_keeps_its_network_rule_without_whois(localapi):
    b = bridge(localapi, open_owner=True)
    assert open_session(b, "100.64.0.99").status == 200      # range only, as before
    assert open_session(b, "8.8.8.8").status == 403


# ------------------------------------------------------------ config
@pytest.mark.parametrize("bad", [{"tailnet_open_owner": "yes"}, {"public_url": "http://x.example"}, {"public_url": "https://a b"},
                                 {"name": ""}, {"name": "x" * 70}, {"whois_socket": "relative.sock"}])
def test_bad_discovery_config_is_refused(bad):
    with pytest.raises(ValueError, match="rakazo_bridge"):
        rb.rakazo_bridge_config({"enabled": True, "accounts": ACCOUNTS, **bad})


def test_tailnet_open_owner_needs_an_operator():
    fam = [{**ACCOUNTS[0], "role": "family"}]
    with pytest.raises(ValueError, match="tailnet_open_owner"):
        rb.rakazo_bridge_config({"enabled": True, "accounts": fam, "tailnet_open_owner": True})


def test_defaults():
    cfg = rb.rakazo_bridge_config({"enabled": True, "accounts": ACCOUNTS})
    assert cfg["tailnet_open_owner"] is False and cfg["public_url"] is None and cfg["name"] == "herdr-shepherd"


# ------------------------------------------------------------ over real HTTP
def test_the_route_is_served_unauthenticated_by_the_control_plane():
    svc = AssistantService(config=assistant_config({"personal_assistant": {"enabled": True}}))
    svr = ControlPlaneServer(ControlPlaneRepo(), assistant_service=svc, rakazo_bridge={"enabled": True, "accounts": ACCOUNTS,
                                                                                       "public_url": "https://h.example.ts.net"})
    threading.Thread(target=svr.serve, daemon=True).start()
    svr.wait_until_ready()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{svr.port}{WK}", timeout=HTTP_TIMEOUT) as r:
            d = json.loads(r.read())
        assert r.status == 200 and d["bridge_url"] == "https://h.example.ts.net" and d["tailnet"] is False
    finally:
        svr.shutdown()

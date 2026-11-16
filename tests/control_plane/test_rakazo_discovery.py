"""Zero-typing onboarding: GET /.well-known/herdr-shepherd and the whois-verified tailnet owner session.
Real RakazoBridge, a real unix-socket LocalAPI-shaped whois server and real TCP for the HTTP route; no mocks."""
import json
import socketserver
import threading
import time
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
    state = {"hits": 0, "users": {}, "mode": None, "delay": 0.0, "paths": [], "nodes": dict(NODES)}

    class H(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            from urllib.parse import unquote
            state["hits"] += 1
            state["paths"].append(self.path)
            time.sleep(state["delay"])
            if state["mode"] == "500":
                self.send_response(500); self.end_headers(); return
            ip = unquote(self.path.split("addr=")[1]).rsplit(":", 1)[0].strip("[]")
            if state["mode"] == "garbage":
                body = b"<<not json"
                self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
                return
            name = state["nodes"].get(ip)
            if name is None:
                self.send_response(404); self.end_headers(); return
            body = json.dumps({"Node": {"Name": name}, "UserProfile": {"LoginName": state["users"].get(ip, "me@example.com")}}).encode()
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


def bridge(localapi, clock=None, is_local_address=None, **over):
    cfg = rb.rakazo_bridge_config({"enabled": True, "accounts": ACCOUNTS, **over})
    svc = AssistantService(config=assistant_config({"personal_assistant": {"enabled": True}}))
    extra = {"monotonic": clock} if clock else {}
    if is_local_address:
        extra["is_local_address"] = is_local_address
    return rb.RakazoBridge(cfg, svc, whois=TailscaleWhois(sockets=(localapi["sock"],), timeout=2.0), **extra)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


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
    b = bridge(localapi, tailnet_open_owner=True, public_url="https://local.example.ts.net")
    for peer in ("100.64.0.99", "127.0.0.1", "8.8.8.8", "192.168.1.4", "not-an-ip"):
        d = body(get(b, peer))
        assert d["tailnet"] is False and d["auth_modes"] == ["password"], peer
        assert d["bridge_url"] is None, peer            # the URL is only disclosed to identified tailnet callers
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
        assert r.status == 200 and d["bridge_url"] is None and d["tailnet"] is False     # loopback caller: no URL disclosed
    finally:
        svr.shutdown()


# ------------------------------------------------------------ security review fixes
def test_a_tagged_device_gets_no_session_and_is_not_a_member(localapi):
    localapi["users"]["100.64.0.5"] = "tagged-devices"
    b = bridge(localapi, tailnet_open_owner=True, public_url="https://h.example.ts.net")
    assert open_session(b, "100.64.0.5").status == 403
    d = body(get(b, "100.64.0.5"))
    assert d["tailnet"] is False and d["bridge_url"] is None and d["auth_modes"] == ["password"]


def test_the_hosts_own_tailnet_address_is_never_a_caller(localapi):
    b = bridge(localapi, tailnet_open_owner=True, is_local_address=lambda ip: ip == "100.64.0.5")
    assert open_session(b, "100.64.0.5").status == 403
    assert body(get(b, "100.64.0.5"))["tailnet"] is False


def test_the_default_local_address_check_uses_real_interfaces():
    assert rb._is_local_address("127.0.0.1") is True
    assert rb._is_local_address("203.0.113.77") is False       # documentation range: never configured on a host


def test_tailnet_open_owner_users_limits_the_session_to_named_users(localapi):
    b = bridge(localapi, tailnet_open_owner=True, tailnet_open_owner_users=["ME@example.com"])
    assert open_session(b, "100.64.0.5").status == 200           # case-insensitive match
    localapi["users"]["100.64.0.5"] = "other@example.com"
    b2 = bridge(localapi, tailnet_open_owner=True, tailnet_open_owner_users=["me@example.com"])
    assert open_session(b2, "100.64.0.5").status == 403
    d = body(get(b2, "100.64.0.5"))
    assert d["tailnet"] is True and d["auth_modes"] == ["password"]


def test_an_empty_or_absent_user_list_allows_any_tailnet_user(localapi):
    localapi["users"]["100.64.0.5"] = "anyone@else.example"
    for over in ({}, {"tailnet_open_owner_users": []}):
        assert open_session(bridge(localapi, tailnet_open_owner=True, **over), "100.64.0.5").status == 200


@pytest.mark.parametrize("bad", [["a"], "me@example.com", [1], [""], ["x@y"] * 60])
def test_bad_tailnet_open_owner_users_is_refused(bad):
    with pytest.raises(ValueError, match="tailnet_open_owner_users"):
        rb.rakazo_bridge_config({"enabled": True, "accounts": ACCOUNTS, "tailnet_open_owner": True, "tailnet_open_owner_users": bad})


def test_an_ipv4_mapped_trusted_proxy_is_still_a_trusted_proxy(localapi):
    b = bridge(localapi, tailnet_open_owner=True, trusted_proxies=["10.0.0.2"])
    assert body(get(b, "::ffff:10.0.0.2", xff="100.64.0.5"))["tailnet"] is True
    assert b.client_address("::ffff:10.0.0.2", "100.64.0.5") == "100.64.0.5"
    assert open_session(b, "::ffff:10.0.0.2", xff="100.64.0.5").status == 200


def test_a_mapped_trusted_proxy_without_a_client_address_is_refused_even_if_whois_knows_it(localapi):
    localapi["nodes"]["100.80.0.1"] = "relay.tail1234.ts.net."
    b = bridge(localapi, tailnet_open_owner=True, trusted_proxies=["100.80.0.1"])
    assert open_session(b, "::ffff:100.80.0.1").status == 403
    assert open_session(b, "100.80.0.1").status == 403
    assert body(get(b, "::ffff:100.80.0.1", xff="8.8.8.8"))["tailnet"] is False


def test_a_trusted_proxy_with_a_present_but_empty_forwarded_for_is_refused(localapi):
    b = bridge(localapi, tailnet_open_owner=True, trusted_proxies=["10.0.0.2"])
    assert open_session(b, "10.0.0.2", xff=" ").status == 403
    h = {"origin": "rakazo://", "x-forwarded-for": ""}
    assert b.handle("POST", "/api/auth/open", h, b"{}", "10.0.0.2").status == 403
    assert open_session(b, "10.0.0.2").status == 403
    assert open_session(b, "10.0.0.2", xff="100.64.0.5").status == 200


def test_with_both_flags_on_whois_still_applies(localapi):
    b = bridge(localapi, open_owner=True, tailnet_open_owner=True)
    for peer in ("100.64.0.99", "127.0.0.1"):                    # the plain range rule would have admitted both
        assert open_session(b, peer).status == 403, peer
    assert open_session(b, "100.64.0.5").status == 200


def test_ipv6_and_mapped_peers_are_looked_up_unmapped_and_share_one_rate_key(localapi):
    b = bridge(localapi, tailnet_open_owner=True)
    assert body(get(b, "fd7a:115c:a1e0::5"))["tailnet"] is True
    assert body(get(b, "::ffff:100.64.0.5"))["tailnet"] is True
    assert any("addr=100.64.0.5%3A1" in p for p in localapi["paths"])
    assert open_session(b, "::ffff:100.64.0.5").status == 200
    again = open_session(b, "100.64.0.5")
    assert again.status == 429 and again.headers["Retry-After"] == "30"


def test_the_tailnet_path_is_rate_limited_per_caller(localapi):
    clock = Clock()
    b = bridge(localapi, clock=clock, tailnet_open_owner=True)
    assert open_session(b, "100.64.0.5").status == 200
    assert open_session(b, "100.64.0.5").status == 429
    clock.t += 3600
    assert open_session(b, "100.64.0.5").status == 200


def test_whois_errors_fail_closed_and_are_cached_only_briefly(localapi):
    clock = Clock()
    b = bridge(localapi, clock=clock)
    for mode in ("500", "garbage"):
        localapi["mode"], localapi["hits"] = mode, 0
        clock.t += 100
        assert body(get(b, "100.64.0.5"))["tailnet"] is False
        assert body(get(b, "100.64.0.5"))["tailnet"] is False
        assert localapi["hits"] == 1                              # a burst of retries does not hammer whois
        localapi["mode"] = None
        clock.t += 1
        assert body(get(b, "100.64.0.5"))["tailnet"] is False     # still inside the short error cache
        clock.t += 2
        assert body(get(b, "100.64.0.5"))["tailnet"] is True      # recovered after ~2 s, not 30


def test_a_positive_verdict_expires_after_thirty_seconds(localapi):
    clock = Clock()
    b = bridge(localapi, clock=clock)
    assert body(get(b, "100.64.0.5"))["tailnet"] is True
    del localapi["nodes"]["100.64.0.5"]
    clock.t += 29
    assert body(get(b, "100.64.0.5"))["tailnet"] is True
    clock.t += 2
    assert body(get(b, "100.64.0.5"))["tailnet"] is False


def test_concurrent_first_lookups_for_one_address_share_a_single_whois_call(localapi):
    localapi["delay"] = 0.4
    b = bridge(localapi)
    out = []
    ts = [threading.Thread(target=lambda: out.append(body(get(b, "100.64.0.5"))["tailnet"])) for _ in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert out == [True] * 8 and localapi["hits"] == 1


def over_http(localapi, **over):
    svc = AssistantService(config=assistant_config({"personal_assistant": {"enabled": True}}))
    cfg = {"enabled": True, "accounts": ACCOUNTS, "tailnet_open_owner": True, "trusted_proxies": ["127.0.0.1"],
           "whois_socket": localapi["sock"], **over}
    svr = ControlPlaneServer(ControlPlaneRepo(), assistant_service=svc, rakazo_bridge=cfg)
    threading.Thread(target=svr.serve, daemon=True).start()
    svr.wait_until_ready()
    return svr


def http_call(svr, method, path, headers):
    import http.client
    conn = http.client.HTTPConnection("127.0.0.1", svr.port, timeout=HTTP_TIMEOUT)
    conn.putrequest(method, path)
    for k, v in headers:
        conn.putheader(k, v)
    conn.putheader("Content-Length", "2" if method == "POST" else "0")
    conn.endheaders(b"{}" if method == "POST" else b"")
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    return resp.status, data


def test_whois_socket_is_spoken_to_as_a_sidecar_and_one_forwarded_for_header_works(localapi):
    svr = over_http(localapi)
    try:
        status, data = http_call(svr, "POST", "/api/auth/open", [("Origin", "rakazo://"), ("X-Forwarded-For", "100.64.0.5")])
        assert status == 200 and json.loads(data)["token"]
        assert localapi["paths"] and all(p.startswith("/whois?addr=") for p in localapi["paths"])   # proxy=True wiring
    finally:
        svr.shutdown()


def test_more_than_one_forwarded_for_header_is_refused_over_real_http(localapi):
    svr = over_http(localapi)
    try:
        two = [("Origin", "rakazo://"), ("X-Forwarded-For", "8.8.8.8"), ("X-Forwarded-For", "100.64.0.5")]
        assert http_call(svr, "POST", "/api/auth/open", two)[0] == 403
        flipped = [("Origin", "rakazo://"), ("X-Forwarded-For", "100.64.0.5"), ("X-Forwarded-For", "8.8.8.8")]
        assert http_call(svr, "POST", "/api/auth/open", flipped)[0] == 403
        status, data = http_call(svr, "GET", WK, two[1:])
        assert json.loads(data)["tailnet"] is False
    finally:
        svr.shutdown()

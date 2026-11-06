# ruff: noqa: F811  (pytest fixtures are imported by name from the sibling test modules)
"""Fabric trust slice 6a: the dedicated fabric listener and the whois sidecar.

Real CollabService, real HTTP servers, a real sidecar PROCESS, real unix sockets (the tailscaled-LocalAPI fixture), no mocks.
Deployment shape under test: a collab-only process (public handler, internet side) PLUS a listener bound on a tailnet address
that serves the collab API and nothing else, accepts only ``Authorization: Fabric`` and asks the sidecar, not tailscaled."""
from __future__ import annotations

import http.client
import ipaddress
import json
import os
import socket
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from herdr_engineering.control_plane import collab_fabric_trust as fabric_trust_module
from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.collab_fabric_trust import (
    FabricPeerResolver,
    normalize_fabric_trust,
)
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from herdr_engineering.fabric_access import TailscaleWhois
from tests.control_plane.test_collab import BASE
from tests.control_plane.test_collab_fabric_auth import (  # noqa: F401
    SUFFIX,
    TAILNET,
    auth,
    fabric_agent,
    fw,
    lan_ip,
)
from tests.control_plane.test_collab_fabric_trust import localapi  # noqa: F401
from tests.control_plane.test_collab_tasks import USERS, _digest, code, svc  # noqa: F401

ROOT = Path(__file__).resolve().parents[2]


def start_sidecar(tmp_path, localapi_sock, name="whois.sock"):
    sock = str(tmp_path / name)
    code_ = "import sys; from herdr_engineering.cli import main; sys.exit(main(sys.argv[1:]))"
    proc = subprocess.Popen([sys.executable, "-c", code_, "fabric", "whois-proxy", "--listen-socket", sock,
                             "--tailscaled-socket", localapi_sock], cwd=ROOT, env={**os.environ, "PYTHONPATH": str(ROOT)},
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    deadline = time.time() + 15
    while time.time() < deadline and not os.path.exists(sock):
        if proc.poll() is not None:
            raise AssertionError("sidecar exited: " + proc.stderr.read().decode())
        time.sleep(0.05)
    assert os.path.exists(sock), "sidecar socket never appeared"
    return proc, sock


def unix_get(sock, path, method="GET"):
    class C(http.client.HTTPConnection):
        def connect(self):
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.settimeout(5)
            self.sock.connect(sock)
    c = C("sidecar")
    c.request(method, path)
    r = c.getresponse()
    body = r.read()
    c.close()
    return r.status, body


@pytest.fixture
def sidecar(tmp_path, localapi):
    proc, sock = start_sidecar(tmp_path, localapi["sock"])
    yield sock
    proc.terminate()
    proc.wait(timeout=10)
    if proc.stderr:
        proc.stderr.close()


# ------------------------------------------------------------------ config
def test_listener_keys_are_validated_together_and_must_be_tailnet_addresses():
    cfg = normalize_fabric_trust({"listen_host": "100.64.0.5", "listen_port": 9483})
    assert (cfg["listen_host"], cfg["listen_port"]) == ("100.64.0.5", 9483)
    assert normalize_fabric_trust({"listen_host": "fd7a:115c:a1e0::5", "listen_port": 9483})["listen_host"] == "fd7a:115c:a1e0::5"
    assert normalize_fabric_trust(None)["listen_host"] is None and normalize_fabric_trust(None)["whois_socket"] is None
    for host in ("192.168.1.5", "127.0.0.1", "0.0.0.0", "::", "::1", "8.8.8.8", "bender", "100.128.0.1", "fd7b::1", "100.64.0.5/32", ""):
        with pytest.raises(ValueError):
            normalize_fabric_trust({"listen_host": host, "listen_port": 9483})
    with pytest.raises(ValueError):
        normalize_fabric_trust({"listen_host": "100.64.0.5"})            # both or neither
    with pytest.raises(ValueError):
        normalize_fabric_trust({"listen_port": 9483})
    for port in (0, 70000, "9483", True):
        with pytest.raises(ValueError):
            normalize_fabric_trust({"listen_host": "100.64.0.5", "listen_port": port})
    for sock in ("relative/path.sock", "", 5, "/a/../b.sock"):
        with pytest.raises(ValueError):
            normalize_fabric_trust({"whois_socket": sock})
    assert normalize_fabric_trust({"whois_socket": "/run/herdr-whois/whois.sock"})["whois_socket"] == "/run/herdr-whois/whois.sock"


def test_the_server_constructor_refuses_a_non_tailnet_bind_unless_the_test_override_is_given():
    kw = {"collab": None}
    with pytest.raises(ValueError):
        ControlPlaneServer(ControlPlaneRepo(), fabric_listen=("192.168.1.5", 9483), **kw)
    with pytest.raises(ValueError):
        ControlPlaneServer(ControlPlaneRepo(), fabric_listen=("100.64.0.5", 9483), **kw)       # needs a collab service


def test_the_fleet_config_validator_and_slice_carry_the_listener_keys():
    from herdr_engineering.fleet_config_cli import COLLAB_KEYS
    assert "fabric_trust" in COLLAB_KEYS
    for bad in ({"listen_host": "192.168.1.5", "listen_port": 9483}, {"listen_host": "100.64.0.5"}):
        with pytest.raises(ValueError):
            normalize_fabric_trust(bad)


# ------------------------------------------------------------------ the sidecar
def test_sidecar_serves_only_whois_and_allow_lists_everything_else(sidecar, localapi):
    mode = stat.S_IMODE(os.stat(sidecar).st_mode)
    assert mode == 0o660
    status, body = unix_get(sidecar, "/whois?addr=100.64.0.1")
    data = json.loads(body)
    assert status == 200 and data["Node"]["Name"] == "bender.tail1234.ts.net" and data["UserProfile"]["LoginName"] == "tagged-devices"
    assert set(data) == {"Node", "UserProfile"} and set(data["Node"]) == {"Name", "Tags"}      # nothing else is passed through
    assert unix_get(sidecar, "/whois?addr=fd7a:115c:a1e0::1")[0] == 200
    hits = localapi["hits"]
    for path in ("/localapi/v0/status", "/localapi/v0/whois?addr=100.64.0.1%3A1", "/", "/whois/", "/whois2?addr=100.64.0.1",
                 "/v0/prefs", "/%77hois/x", "/whois/../localapi/v0/status"):
        assert unix_get(sidecar, path)[0] == 404, path
    for path in ("/whois", "/whois?addr=", "/whois?addr=abc", "/whois?addr=100.64.0.1:1", "/whois?addr=100.64.0.1&x=1",
                 "/whois?addr=100.64.0.1&addr=100.64.0.2", "/whois?x=100.64.0.1", "/whois?addr=100.64.0.1%00",
                 "/whois?addr=999.1.1.1", "/whois?addr=../etc"):
        assert unix_get(sidecar, path)[0] == 400, path
    assert unix_get(sidecar, "/whois?addr=100.64.0.77")[0] == 404          # tailscaled does not know it
    before = localapi["hits"]
    assert unix_get(sidecar, "/whois?addr=8.8.8.8")[0] == 404              # never even asked: not a tailnet address
    assert unix_get(sidecar, "/whois?addr=127.0.0.1")[0] == 404
    assert localapi["hits"] == before
    for method in ("POST", "PUT", "DELETE"):
        assert unix_get(sidecar, "/whois?addr=100.64.0.1", method=method)[0] in (404, 405, 501)
    assert localapi["hits"] > hits


def test_sidecar_reports_an_unreachable_tailscaled_as_a_failure_not_an_identity(tmp_path):
    proc, sock = start_sidecar(tmp_path, str(tmp_path / "no-such-tailscaled.sock"))
    try:
        assert unix_get(sock, "/whois?addr=100.64.0.1")[0] == 502
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        proc.stderr.close()


def test_whois_through_the_sidecar_has_the_same_shape_as_tailscaled(sidecar, localapi):
    direct = TailscaleWhois(sockets=(localapi["sock"],)).lookup("100.64.0.2")
    proxied = TailscaleWhois(sockets=(sidecar,), proxy=True).lookup("100.64.0.2")
    assert proxied == direct and proxied.node == "beast.tail1234.ts.net"
    assert TailscaleWhois(sockets=(sidecar,), proxy=True).lookup("100.64.0.77") is None
    with pytest.raises(OSError):
        TailscaleWhois(sockets=(sidecar + ".gone",), proxy=True).lookup("100.64.0.1")


# ------------------------------------------------------------------ the listener
@pytest.fixture
def lw(fw, sidecar, request, monkeypatch):
    """collab-only process (public handler) plus the dedicated fabric listener, whois via the sidecar."""
    ip = lan_ip()
    if ip is None:
        pytest.fail("no non-loopback address: the fabric listener tests cannot run")
    on_tailnet = ipaddress.ip_address(ip) in TAILNET
    if not on_tailnet:       # no tailnet address on this host: widen the check to this one address (and the constructor override)
        monkeypatch.setattr(fabric_trust_module, "FABRIC_PEER_NETS", (*fabric_trust_module.FABRIC_PEER_NETS, ipaddress.ip_network(ip + "/32")))
    fw.s.fabric_trust = normalize_fabric_trust({"enabled": True, "tailnet_suffix": SUFFIX, "machines": ["bender", "beast"],
                                                "whois_cache_seconds": 0, "whois_socket": sidecar})
    fw.s.fabric_resolver = FabricPeerResolver(fw.s.fabric_trust_cfg)           # NO injected whois: config picks the sidecar
    srv = ControlPlaneServer(ControlPlaneRepo(), host="127.0.0.1", port=0, collab=fw.s, collab_only=True, public_port=0,
                             public_host=ip, fabric_listen=(ip, 0), fabric_allow_any_host=not on_tailnet)
    t = threading.Thread(target=srv.serve, daemon=True)
    t.start()
    srv.wait_until_ready()
    fw.lan, fw.public_port, fw.fabric_port, fw.srv = ip, srv.public_port, srv.fabric_port, srv
    fw.sidecar = sidecar
    yield fw
    srv.shutdown()
    t.join(timeout=5)


def freq(w, method, path, *, headers=None, body=None, port=None, host=None):
    c = http.client.HTTPConnection(host or w.lan, port or w.fabric_port, timeout=5)
    h = dict(headers or {})
    data = None
    if body is not None:
        data = json.dumps(body)
        h.setdefault("Content-Type", "application/json")
    c.request(method, path, body=data, headers=h)
    r = c.getresponse()
    raw = r.read()
    c.close()
    try:
        return r.status, json.loads(raw)
    except ValueError:
        return r.status, None


def test_fabric_request_over_the_dedicated_listener_works_end_to_end_through_the_sidecar(lw):
    w = lw
    info = fabric_agent(w)
    w.localapi["nodes"][w.lan] = "bender.tail1234.ts.net."
    before = w.localapi["hits"]
    s, b = freq(w, "GET", f"{BASE}/rooms/{w.room}/events?cursor=0", headers=auth(info, **{"X-Herdr-Machine": "bender"}))
    assert s == 200 and "events" in b
    assert w.localapi["hits"] > before                                  # collab -> sidecar -> fixture really happened
    s, _ = freq(w, "POST", f"{BASE}/rooms/{w.room}/messages", headers=auth(info), body={"text": "yo", "idempotency_key": "l1"})
    assert s in (200, 201)
    row = [e for e in w.s._log if e["event_type"] == "message"][-1]["payload"]
    assert row["via"] == "fabric" and row["machine"] == "bender"
    # the real socket peer is what whois saw (not a proxy, not a header)
    assert w.lan in {q.rsplit(":", 1)[0].strip("[]") for q in w.localapi["queries"]}


def test_a_peer_whois_does_not_know_or_that_is_not_listed_is_401_on_the_listener(lw):
    w = lw
    info = fabric_agent(w)
    path = f"{BASE}/rooms/{w.room}/events?cursor=0"
    assert freq(w, "GET", path, headers=auth(info))[0] == 401                              # unknown to tailscaled
    w.localapi["nodes"][w.lan] = "stranger.tail1234.ts.net."
    assert freq(w, "GET", path, headers=auth(info))[0] == 401                              # known, not allow-listed
    w.localapi["nodes"][w.lan] = "bender.tail1234.ts.net."
    assert freq(w, "GET", path, headers=auth(info))[0] == 200


def test_sidecar_down_fails_closed_on_the_listener(lw):
    w = lw
    info = fabric_agent(w)
    w.localapi["nodes"][w.lan] = "bender.tail1234.ts.net."
    w.s.fabric_trust = normalize_fabric_trust({**{k: w.s.fabric_trust[k] for k in ("tailnet_suffix", "machines")}, "enabled": True,
                                               "whois_cache_seconds": 0, "whois_socket": w.sidecar + ".gone"})
    assert freq(w, "GET", f"{BASE}/rooms/{w.room}/events?cursor=0", headers=auth(info))[0] == 401


def test_bearer_and_agent_headers_are_401_on_the_listener_without_being_evaluated(lw):
    w = lw
    info = fabric_agent(w)
    path = f"{BASE}/rooms/{w.room}/events?cursor=0"
    cred = {"Authorization": f"Agent {info['credential']}", "X-Herdr-Device": info["device_id"]}
    assert freq(w, "GET", path, headers=cred, port=w.public_port)[0] == 200                # the credential IS valid elsewhere
    failures = w.s.stats["auth_failures"]
    assert freq(w, "GET", path, headers=cred)[0] == 401
    assert freq(w, "GET", path, headers={"Authorization": "Bearer abc.def.ghi"})[0] == 401
    assert freq(w, "GET", path, headers={"Authorization": f"bearer {info['credential']}"})[0] == 401
    assert freq(w, "GET", path)[0] == 401                                                   # no header at all
    assert freq(w, "GET", f"{BASE}/config")[0] == 401                                       # not even the public config
    assert w.s.stats["auth_failures"] == failures                                           # authenticate() never ran


def test_the_public_handler_still_refuses_fabric_from_a_valid_tailnet_peer(lw):
    w = lw
    info = fabric_agent(w)
    w.localapi["nodes"][w.lan] = "bender.tail1234.ts.net."
    path = f"{BASE}/rooms/{w.room}/events?cursor=0"
    assert freq(w, "GET", path, headers=auth(info), port=w.public_port)[0] == 401
    assert freq(w, "GET", path, headers=auth(info))[0] == 200


def test_only_the_collab_api_exists_on_the_listener(lw):
    w = lw
    info = fabric_agent(w)
    w.localapi["nodes"][w.lan] = "bender.tail1234.ts.net."
    h = auth(info)
    for method, path in (("GET", "/"), ("GET", "/index.html"), ("GET", "/mcp"), ("POST", "/mcp"), ("POST", "/a2a"),
                         ("GET", "/.well-known/agent-card.json"), ("GET", "/api/v1/health"), ("GET", "/api/v1/hosts"),
                         ("GET", "/api/v1/events/stream"), ("POST", "/api/v1/ingest"), ("GET", "/api/v1/collabx"),
                         ("GET", "/api/v1/collab-evil/rooms"), ("GET", "/collab.html"), ("GET", "/api/v1/fleet-config"),
                         ("GET", "/s/anything"), ("GET", "/api/v1/assistants"), ("GET", "/%2e%2e/etc/passwd")):
        status, _ = freq(w, method, path, headers=h, body={} if method == "POST" else None)
        assert status == 404, (method, path, status)
        assert freq(w, method, path, body={} if method == "POST" else None)[0] == 404, (method, path)     # also without a header
    assert freq(w, "PUT", "/api/v1/assistant-apps/x", headers=h, body={})[0] in (404, 501)


def test_a_forwarding_header_refuses_fabric_on_the_listener(lw):
    w = lw
    info = fabric_agent(w)
    w.localapi["nodes"][w.lan] = "bender.tail1234.ts.net."
    path = f"{BASE}/rooms/{w.room}/events?cursor=0"
    for hdr in ("X-Forwarded-For", "Forwarded", "X-Real-IP", "Via", "Tailscale-User-Login"):
        assert freq(w, "GET", path, headers=auth(info, **{hdr: "100.64.0.1"}))[0] == 401, hdr
    assert freq(w, "GET", path, headers=auth(info))[0] == 200


def test_kill_switch_and_flag_off_apply_on_the_listener(lw):
    w = lw
    info = fabric_agent(w)
    w.localapi["nodes"][w.lan] = "bender.tail1234.ts.net."
    path = f"{BASE}/rooms/{w.room}/events?cursor=0"
    assert freq(w, "GET", path, headers=auth(info))[0] == 200
    w.s.fabric_trust = normalize_fabric_trust({"enabled": True, "tailnet_suffix": SUFFIX, "kill_switch": True, "machines": ["bender"]})
    assert freq(w, "GET", path, headers=auth(info))[0] == 401
    w.s.fabric_trust = normalize_fabric_trust(None)
    assert freq(w, "GET", path, headers=auth(info))[0] == 401


def test_a_listener_that_cannot_bind_is_a_loud_startup_error(fw, monkeypatch):
    ip = lan_ip()
    on_tailnet = ipaddress.ip_address(ip) in TAILNET
    taken = socket.socket()
    taken.bind((ip, 0))
    taken.listen(1)
    try:
        srv = ControlPlaneServer(ControlPlaneRepo(), host="127.0.0.1", port=0, collab=fw.s, collab_only=True, public_port=0,
                                 public_host=ip, fabric_listen=(ip, taken.getsockname()[1]), fabric_allow_any_host=not on_tailnet)
        with pytest.raises(OSError):
            srv.serve()
    finally:
        taken.close()


def test_without_listener_config_nothing_extra_is_bound(fw):
    srv = ControlPlaneServer(ControlPlaneRepo(), host="127.0.0.1", port=0, collab=fw.s, collab_only=True, public_port=0,
                             public_host="127.0.0.1")
    t = threading.Thread(target=srv.serve, daemon=True)
    t.start()
    srv.wait_until_ready()
    try:
        assert srv.fabric_port is None
    finally:
        srv.shutdown()
        t.join(timeout=5)


# ---- review findings: Host allow-list and pre-authentication routes -------------------------------------------------------------------

def test_a_foreign_host_header_is_refused_even_with_valid_fabric_auth(lw):
    w = lw
    info = fabric_agent(w)
    w.localapi["nodes"][w.lan] = "bender.tail1234.ts.net."
    path = f"{BASE}/rooms/{w.room}/events?cursor=0"
    good = f"{w.lan}:{w.fabric_port}"
    assert freq(w, "GET", path, headers={**auth(info), "Host": good})[0] == 200
    assert freq(w, "GET", path, headers=auth(info))[0] == 200                          # http.client's own Host is the right one
    for bad in ("evil.example", f"evil.example:{w.fabric_port}", f"{w.lan}", f"{w.lan}:{w.fabric_port + 1}",
                f"localhost:{w.fabric_port}", f"127.0.0.1:{w.fabric_port}", ""):
        before = w.s.stats["auth_failures"]
        s, _ = freq(w, "GET", path, headers={**auth(info), "Host": bad})
        assert s == 421, (bad, s)
        assert w.s.stats["auth_failures"] == before                                    # refused before authentication
    # the case of the name does not matter
    assert freq(w, "GET", path, headers={**auth(info), "Host": good.upper()})[0] == 200


def test_a_rebound_browser_origin_equal_to_its_foreign_host_is_refused(lw):
    w = lw
    info = fabric_agent(w)
    w.localapi["nodes"][w.lan] = "bender.tail1234.ts.net."
    rebound = f"rebind.attacker.example:{w.fabric_port}"
    h = {**auth(info), "Host": rebound, "Origin": f"http://{rebound}"}
    assert freq(w, "POST", f"{BASE}/rooms/{w.room}/messages", headers=h, body={"text": "x", "idempotency_key": "rb1"})[0] == 421
    assert freq(w, "GET", f"{BASE}/rooms/{w.room}/events?cursor=0", headers=h)[0] == 421
    assert not [e for e in w.s._log if e["event_type"] == "message" and e["payload"].get("text") == "x"]
    ok = {**auth(info), "Origin": f"http://{w.lan}:{w.fabric_port}"}                  # Origin == the allowed Host still passes
    assert freq(w, "POST", f"{BASE}/rooms/{w.room}/messages", headers=ok, body={"text": "y", "idempotency_key": "rb2"})[0] in (200, 201)


def test_pre_auth_routes_do_not_exist_on_the_fabric_listener_but_work_on_the_public_one(lw):
    w = lw
    info = fabric_agent(w)
    w.localapi["nodes"][w.lan] = "bender.tail1234.ts.net."
    h = auth(info)
    routes = (("POST", f"{BASE}/agents/enrollment/complete"), ("POST", f"{BASE}/agents/join"),
              ("POST", f"{BASE}/agents/join-link"), ("GET", f"{BASE}/config"))
    for method, path in routes:
        body = {} if method == "POST" else None
        assert freq(w, method, path, headers=h, body=body)[0] == 404, (method, path)       # with a valid Fabric header
        assert freq(w, method, path, body=body)[0] == 401, (method, path)                   # without one: the usual 401
    # still served by the public handler (the listener's 404 is not what they answer there)
    s, _ = freq(w, "GET", f"{BASE}/config", port=w.public_port)
    assert s == 200
    for _, path in routes[:3]:
        s, _ = freq(w, "POST", path, port=w.public_port, body={})
        assert s not in (404, 421), (path, s)                                               # reaches the service
    assert freq(w, "GET", f"{BASE}/rooms/{w.room}/events?cursor=0", headers=h)[0] == 200  # authenticated routes unaffected

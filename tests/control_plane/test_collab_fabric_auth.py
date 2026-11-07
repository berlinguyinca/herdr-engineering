# ruff: noqa: F811  (pytest fixtures are imported by name from the sibling test modules)
"""Fabric trust slice 2: the ``Authorization: Fabric <agent_id>`` scheme.

Real CollabService, real HTTP server, real unix-socket tailscaled-LocalAPI fixture, no mocks. The service never sees headers:
it is given the real socket peer address (``peer_ip``) and the informational ``X-Herdr-Machine`` value, nothing else."""
from __future__ import annotations

import http.client
import ipaddress
import json
import socket
import threading

import pytest

from herdr_engineering.control_plane import collab_fabric_trust as fabric_trust_module
from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.collab import CollabService, StaticTokenAuthenticator
from herdr_engineering.control_plane.collab_fabric_trust import (
    FabricPeerResolver,
    normalize_fabric_trust,
)
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from herdr_engineering.fabric_access import TailscaleWhois
from tests.control_plane.test_collab import BASE, read_frame
from tests.control_plane.test_collab_fabric_trust import localapi  # noqa: F401
from tests.control_plane.test_collab_tasks import USERS, W, _digest, code, svc  # noqa: F401

BENDER, BEAST, STRANGER = "100.64.0.1", "100.64.0.2", "100.64.0.9"
SUFFIX = "tail1234.ts.net"


def _local_ips() -> list[str]:
    """Every non-loopback IPv4 address of this host, tailnet (100.64.0.0/10) addresses first."""
    found: set[str] = set()
    for probe in ("10.255.255.254", "100.100.100.100"):          # a UDP connect sends nothing
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect((probe, 9))
            found.add(s.getsockname()[0])
        except OSError:
            pass
        finally:
            s.close()
    try:
        found.update(i[4][0] for i in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET))
    except OSError:
        pass
    ips = [ip for ip in found if not ip.startswith("127.")]
    return sorted(ips, key=lambda ip: (ipaddress.ip_address(ip) not in TAILNET, ip))


TAILNET = ipaddress.ip_network("100.64.0.0/10")


def lan_ip() -> str | None:
    ips = _local_ips()
    return ips[0] if ips else None


@pytest.fixture
def fw(svc, localapi):
    """A room world with fabric trust ON for bender and beast, resolved through the real LocalAPI socket fixture."""
    w = W(svc)
    svc.fabric_trust = normalize_fabric_trust({"enabled": True, "tailnet_suffix": SUFFIX, "machines": ["bender", "beast"], "whois_cache_seconds": 0})
    svc.fabric_resolver = FabricPeerResolver(svc.fabric_trust_cfg, whois=TailscaleWhois(sockets=(localapi["sock"],), timeout=2.0))
    w.localapi = localapi
    return w


def fabric_agent(w, owner="alice", machine="bender", bit=True, actions=("read", "post", "execute")):
    pr = getattr(w, owner)
    body = {"harness_type": "claude-code", **({"machine": machine} if machine else {})}
    info = w.s.connect_agent(pr, body)
    out = w.s.grant_agent(pr, w.room, {"agent_id": info["agent_id"], "actions": list(actions)})
    if out["status"] == "pending":
        w.s.decide_agent_request(w.alice, w.room, out["request_id"], {"decision": "approve"})
    if bit:
        w.s.set_agent_fabric(pr, info["agent_id"], {"fabric": True})
    return info


def fabric(w, info, peer=BENDER, machine=None):
    return w.s.authenticate(f"Fabric {info['agent_id']}", None, peer_ip=peer, machine=machine)


def denied(w, info, peer=BENDER, machine=None, scheme="Fabric", aid=None):
    before = w.s.stats["auth_failures"]
    assert code(w.s.authenticate, f"{scheme} {info['agent_id'] if aid is None else aid}", None, peer_ip=peer, machine=machine) == 401
    assert w.s.stats["auth_failures"] == before + 1


# ------------------------------------------------------------------ the happy path
def test_allowed_machine_bound_agent_with_bit_is_authenticated(fw):
    info = fabric_agent(fw)
    pr = fabric(fw, info, machine="bender")
    assert (pr.kind, pr.id, pr.owner, pr.via, pr.machine) == ("agent", info["agent_id"], "alice", "fabric", "bender")
    cred = fw.s.authenticate(f"Agent {info['credential']}", info["device_id"])
    assert (cred.kind, cred.id, cred.owner) == (pr.kind, pr.id, pr.owner) and cred.via is None
    assert fw.s.read_events(pr, fw.room, 0, 10)["events"] is not None            # same grants as the credential
    assert fabric(fw, info).machine == "bender"                                  # the header is optional


def test_audit_rows_of_a_fabric_actor_say_via_fabric_and_machine(fw):
    info = fabric_agent(fw)
    pr = fabric(fw, info)
    fw.s.post_message(pr, fw.room, {"text": "hi", "idempotency_key": "k1"})
    row = [e for e in fw.s._log if e["event_type"] == "message"][-1]["payload"]
    assert row["actor_agent_id"] == info["agent_id"] and row["via"] == "fabric" and row["machine"] == "bender"
    cred = fw.s.authenticate(f"Agent {info['credential']}", info["device_id"])
    fw.s.post_message(cred, fw.room, {"text": "hi", "idempotency_key": "k2"})
    assert "via" not in [e for e in fw.s._log if e["event_type"] == "message"][-1]["payload"]


# ------------------------------------------------------------------ every refusal is the same generic 401
def test_unknown_or_unlisted_machine_is_401(fw):
    info = fabric_agent(fw)
    denied(fw, info, peer=STRANGER)                        # on the tailnet, not allow-listed
    denied(fw, info, peer="100.64.0.77")                   # tailscaled does not know it
    denied(fw, info, peer="not-an-ip")


def test_agent_bound_to_another_machine_is_401_and_audited(fw):
    info = fabric_agent(fw, machine="beast")
    denied(fw, info, peer=BENDER)
    assert fabric(fw, info, peer=BEAST).machine == "beast"
    reasons = [e["payload"]["reason"] for e in fw.s._log if e["event_type"] == "audit_denied"]
    assert "fabric_machine_unbound" in reasons


def test_agent_without_a_recorded_machine_is_401(fw):
    denied(fw, fabric_agent(fw, machine=None, bit=False))
    info = fabric_agent(fw, machine=None, bit=False)
    fw.s._commit("agent_fabric_set", {"actor_user_id": "alice", "actor_agent_id": None, "agent_id": info["agent_id"],
                                      "fabric": True, "action": "agent.fabric.set", "outcome": "allowed",
                                      "project_ids": [], "resource_id": info["agent_id"]})
    denied(fw, info)


def test_agent_without_the_fabric_bit_is_401(fw):
    info = fabric_agent(fw, bit=False)
    denied(fw, info)
    assert "fabric_not_enabled" in [e["payload"]["reason"] for e in fw.s._log if e["event_type"] == "audit_denied"]


def test_x_herdr_machine_mismatch_is_401(fw):
    info = fabric_agent(fw)
    denied(fw, info, machine="beast")
    denied(fw, info, machine="")
    assert fabric(fw, info, machine="BENDER").machine == "bender"


def test_external_and_helper_agents_never_use_fabric(fw):
    ext = fabric_agent(fw)
    fw.s._agents[ext["agent_id"]]["external"] = {"room_id": fw.room, "hard_expiry": 1e18}    # as an invite would have bound it
    denied(fw, ext)
    hlp = fabric_agent(fw)
    fw.s._helper_minted.add(hlp["agent_id"])
    denied(fw, hlp)


def test_revoked_agent_and_disabled_owner_are_401(fw):
    info = fabric_agent(fw)
    fw.s.revoke_agent(fw.alice, info["agent_id"])
    denied(fw, info)
    info2 = fabric_agent(fw)
    fw.s.auth = StaticTokenAuthenticator({u: _digest(t) for u, t in USERS.items() if u != "alice"})
    denied(fw, info2)


def test_unknown_agent_and_other_schemes_stay_generic(fw):
    info = fabric_agent(fw)
    denied(fw, info, aid="agent_nope")
    denied(fw, info, aid="")
    denied(fw, info, scheme="Fabrik")


def test_missing_peer_loopback_and_mapped_loopback_are_never_fabric(fw):
    info = fabric_agent(fw)
    for ip in ("127.0.0.1", "::1", "::ffff:127.0.0.1", "0.0.0.0", "127.0.0.2"):
        fw.localapi["nodes"][ip] = "bender.tail1234.ts.net."            # even if tailscaled claimed it were bender
        denied(fw, info, peer=ip)
    denied(fw, info, peer=None)
    denied(fw, info, peer="")


def test_flag_off_kill_switch_and_degraded_service_are_401(fw):
    info = fabric_agent(fw)
    assert fabric(fw, info)
    fw.s.fabric_trust = normalize_fabric_trust({"enabled": True, "tailnet_suffix": SUFFIX, "kill_switch": True, "machines": ["bender"]})
    denied(fw, info)
    fw.s.fabric_trust = normalize_fabric_trust(None)
    denied(fw, info)
    fw.s.fabric_trust = normalize_fabric_trust({"enabled": True, "tailnet_suffix": SUFFIX, "machines": ["bender"], "whois_cache_seconds": 0})
    assert fabric(fw, info)
    fw.s.degraded = True
    denied(fw, info)


def test_tailscaled_unreachable_fails_closed(fw, tmp_path):
    info = fabric_agent(fw)
    fw.s.fabric_resolver.whois = TailscaleWhois(sockets=(str(tmp_path / "gone.sock"),), timeout=0.5)
    denied(fw, info)


# ------------------------------------------------------------------ the owner-only bit
def test_only_the_owner_sets_or_clears_the_bit(fw):
    info = fabric_agent(fw, bit=False)
    aid = info["agent_id"]
    agent = fw.s.authenticate(f"Agent {info['credential']}", info["device_id"])
    assert code(fw.s.set_agent_fabric, agent, aid, {"fabric": True}) == 403                 # an agent can never
    assert code(fw.s.set_agent_fabric, fw.bob, aid, {"fabric": True}) == 404                # nor another person
    assert code(fw.s.set_agent_fabric, fw.mallory, aid, {"fabric": True}) == 404
    assert code(fw.s.set_agent_fabric, fw.alice, "agent_nope", {"fabric": True}) == 404
    assert code(fw.s.set_agent_fabric, fw.alice, aid, {"fabric": "yes"}) == 400
    assert code(fw.s.set_agent_fabric, fw.alice, aid, {}) == 400
    denied(fw, info)
    first = fw.s.set_agent_fabric(fw.alice, aid, {"fabric": True})
    assert first == {"agent_id": aid, "fabric": True, "machine": "bender", "changed": True,
                     "fabric_expires_at": first["fabric_expires_at"]}
    assert fabric(fw, info)
    assert fw.s.set_agent_fabric(fw.alice, aid, {"fabric": True})["changed"] is False       # a renewal, not a state change
    assert fw.s.set_agent_fabric(fw.alice, aid, {"fabric": False})["changed"] is True
    denied(fw, info)
    kinds = [e["event_type"] for e in fw.s._log if e["event_type"] == "agent_fabric_set"]
    assert len(kinds) == 3                                               # set, renew, clear


def test_a_fabric_principal_cannot_set_the_bit_or_rotate_a_credential(fw):
    info = fabric_agent(fw)
    pr = fabric(fw, info)
    assert code(fw.s.set_agent_fabric, pr, info["agent_id"], {"fabric": False}) == 403
    assert code(fw.s.rotate_credential, pr) == 403                      # no secret can be minted out of a fabric identity


def test_the_bit_needs_a_machine_and_never_applies_to_external_or_revoked(fw):
    nomachine = fabric_agent(fw, machine=None, bit=False)
    assert code(fw.s.set_agent_fabric, fw.alice, nomachine["agent_id"], {"fabric": True}) == 400
    assert fw.s.set_agent_fabric(fw.alice, nomachine["agent_id"], {"fabric": True, "machine": "beast"})["machine"] == "beast"
    assert fabric(fw, nomachine, peer=BEAST)
    assert code(fw.s.set_agent_fabric, fw.alice, nomachine["agent_id"], {"fabric": True, "machine": "Bad Name!"}) == 400
    ext = fabric_agent(fw, bit=False)
    fw.s._agents[ext["agent_id"]]["external"] = {"room_id": fw.room, "hard_expiry": 1e18}
    assert code(fw.s.set_agent_fabric, fw.alice, ext["agent_id"], {"fabric": True}) == 404
    gone = fabric_agent(fw, bit=False)
    fw.s.revoke_agent(fw.alice, gone["agent_id"])
    assert code(fw.s.set_agent_fabric, fw.alice, gone["agent_id"], {"fabric": True}) == 404


def test_clearing_the_bit_is_a_safety_write_and_listing_shows_the_fields(fw):
    info = fabric_agent(fw)
    row = [a for a in fw.s.list_my_agents(fw.alice)["agents"] if a["agent_id"] == info["agent_id"]][0]
    assert row["machine"] == "bender" and row["fabric"] is True
    assert fw.s._is_safety("agent_fabric_set", {"fabric": False}) and not fw.s._is_safety("agent_fabric_set", {"fabric": True})


def test_state_survives_a_restart_from_the_log(fw):
    info = fabric_agent(fw)
    fresh = CollabService(StaticTokenAuthenticator({u: _digest(t) for u, t in USERS.items()}), now=fw.s._now)
    fresh.hydrate(list(fw.s._log))
    fresh.fabric_trust = fw.s.fabric_trust
    fresh.fabric_resolver = fw.s.fabric_resolver
    assert fresh.authenticate(f"Fabric {info['agent_id']}", None, peer_ip=BENDER).via == "fabric"


# ------------------------------------------------------------------ enrollment records the machine
def test_every_enrollment_path_records_an_optional_machine(fw):
    s = fw.s
    a = s.connect_agent(fw.alice, {"harness_type": "pi", "machine": "Beast"})
    assert s._agents[a["agent_id"]]["machine"] == "beast" and s._agents[a["agent_id"]]["fabric"] is False
    assert s._agents[s.connect_agent(fw.alice, {})["agent_id"]]["machine"] is None
    c = s.start_enrollment(fw.alice, {"device_id": "d1", "harness_type": "pi"})["code"]
    b = s.complete_enrollment({"code": c, "device_id": "d1", "machine": "bender"})
    assert s._agents[b["agent_id"]]["machine"] == "bender"
    tok = s.create_join_token(fw.alice, {"harness_type": "pi", "name_prefix": "w", "count": 5})["token"]
    j = s.redeem_join_token({"token": tok, "machine": "zapp"})
    assert s._agents[j["agent_id"]]["machine"] == "zapp"
    for bad in ("a b", "x.y", 5, "-", "A" * 80):
        assert code(s.connect_agent, fw.alice, {"machine": bad}) == 400
    # an enroll payload can never switch the bit on, even if a client tries
    j2 = s.redeem_join_token({"token": tok, "machine": "zapp", "fabric": True})
    assert s._agents[j2["agent_id"]]["fabric"] is False


# ------------------------------------------------------------------ long streams
def test_still_valid_rechecks_every_fabric_condition(fw):
    info = fabric_agent(fw)
    pr = fabric(fw, info)
    assert fw.s.still_valid(pr)
    nodes = fw.localapi["nodes"]
    saved = nodes.pop(BENDER)
    assert not fw.s.still_valid(pr)                                      # tailscaled no longer vouches for the peer
    nodes[BENDER] = saved
    assert fw.s.still_valid(pr)
    fw.s.set_agent_fabric(fw.alice, info["agent_id"], {"fabric": False})
    assert not fw.s.still_valid(pr)
    fw.s.set_agent_fabric(fw.alice, info["agent_id"], {"fabric": True})
    assert fw.s.still_valid(pr)
    fw.s.fabric_trust = normalize_fabric_trust({"enabled": True, "tailnet_suffix": SUFFIX, "machines": ["beast"], "whois_cache_seconds": 0})
    assert not fw.s.still_valid(pr)                                      # bender removed from the list
    fw.s.fabric_trust = normalize_fabric_trust({"enabled": True, "tailnet_suffix": SUFFIX, "kill_switch": True, "machines": ["bender"]})
    assert not fw.s.still_valid(pr)
    fw.s.fabric_trust = normalize_fabric_trust({"enabled": True, "tailnet_suffix": SUFFIX, "machines": ["bender"], "whois_cache_seconds": 0})
    fw.s.revoke_agent(fw.alice, info["agent_id"])
    assert not fw.s.still_valid(pr)


def test_a_credential_principal_is_judged_exactly_as_before(fw):
    info = fabric_agent(fw)
    cred = fw.s.authenticate(f"Agent {info['credential']}", info["device_id"])
    fw.s.set_agent_fabric(fw.alice, info["agent_id"], {"fabric": False})
    assert fw.s.still_valid(cred)                                        # the bit has nothing to do with a secret
    fw.s.revoke_agent(fw.alice, info["agent_id"])
    assert not fw.s.still_valid(cred)


# ------------------------------------------------------------------ real HTTP
@pytest.fixture
def http_world(fw, request, monkeypatch):
    """The real server bound on every interface, so a request to the host's own LAN address has a non-loopback peer."""
    ip = lan_ip()
    if ip is None:
        # These tests are the only proof the HTTP layer hands the real socket peer to Fabric auth, so a host with no
        # non-loopback address is a failure, not a skip. Pass --allow-fabric-http-skip to accept that explicitly.
        if request.config.getoption("--allow-fabric-http-skip"):
            pytest.skip("no non-loopback address on this host (--allow-fabric-http-skip)")
        pytest.fail("no non-loopback address: the Fabric HTTP tests cannot run (use --allow-fabric-http-skip to skip)")
    if ipaddress.ip_address(ip) not in TAILNET:
        # No tailnet address here: widen the tailnet check to this one address for the test, exactly as a tailnet peer would pass.
        monkeypatch.setattr(fabric_trust_module, "FABRIC_PEER_NETS", (*fabric_trust_module.FABRIC_PEER_NETS, ipaddress.ip_network(ip + "/32")))
    srv = ControlPlaneServer(ControlPlaneRepo(), host="0.0.0.0", port=0, collab=fw.s, collab_tls_terminated=True, public_port=0,
                             api_keys={"op": "operator"})
    t = threading.Thread(target=srv.serve, daemon=True)
    t.start()
    srv.wait_until_ready()
    fw.lan, fw.port, fw.public_port, fw.srv = ip, srv.port, srv.public_port, srv
    yield fw
    srv.shutdown()
    t.join(timeout=5)


def req(w, method, path, *, host=None, headers=None, body=None, public=False):
    c = http.client.HTTPConnection(host or w.lan, w.public_port if public else w.port, timeout=5)
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


def auth(info, **extra):
    return {"Authorization": f"Fabric {info['agent_id']}", **extra}


def test_http_real_peer_in_allow_list_gets_200(http_world):
    w = http_world
    info = fabric_agent(w)
    w.localapi["nodes"][w.lan] = "bender.tail1234.ts.net."
    s, b = req(w, "GET", f"{BASE}/rooms/{w.room}/events?cursor=0", headers=auth(info, **{"X-Herdr-Machine": "bender"}))
    assert s == 200 and "events" in b
    s, _ = req(w, "POST", f"{BASE}/rooms/{w.room}/messages", headers=auth(info), body={"text": "yo", "idempotency_key": "h1"})
    assert s in (200, 201)
    assert [e for e in w.s._log if e["event_type"] == "message"][-1]["payload"]["via"] == "fabric"


def test_http_unknown_machine_wrong_binding_and_header_mismatch_are_401(http_world):
    w = http_world
    info = fabric_agent(w)
    path = f"{BASE}/rooms/{w.room}/events?cursor=0"
    assert req(w, "GET", path, headers=auth(info))[0] == 401                       # lan ip not known to tailscaled
    w.localapi["nodes"][w.lan] = "stranger.tail1234.ts.net."
    assert req(w, "GET", path, headers=auth(info))[0] == 401                       # known, not allow-listed
    w.localapi["nodes"][w.lan] = "beast.tail1234.ts.net."
    assert req(w, "GET", path, headers=auth(info))[0] == 401                       # agent is bound to bender
    w.localapi["nodes"][w.lan] = "bender.tail1234.ts.net."
    assert req(w, "GET", path, headers=auth(info, **{"X-Herdr-Machine": "beast"}))[0] == 401
    assert req(w, "GET", path, headers=auth(info))[0] == 200


def test_http_loopback_peer_is_never_fabric_even_if_whois_would_say_so(http_world):
    """Behind `tailscale serve` every peer is 127.0.0.1; whatever the proxy forwards, that must not be trusted."""
    w = http_world
    info = fabric_agent(w)
    for ip in ("127.0.0.1", w.lan):
        w.localapi["nodes"][ip] = "bender.tail1234.ts.net."
    path = f"{BASE}/rooms/{w.room}/events?cursor=0"
    assert req(w, "GET", path, host="127.0.0.1", headers=auth(info))[0] == 401
    spoof = auth(info, **{"X-Forwarded-For": BENDER, "Forwarded": f"for={BENDER}", "X-Real-IP": BENDER,
                          "Tailscale-User-Login": "x@y"})
    assert req(w, "GET", path, host="127.0.0.1", headers=spoof)[0] == 401
    assert req(w, "GET", path, headers=auth(info))[0] == 200                       # the same request from a real peer


def test_http_forwarded_for_spoof_never_grants_and_proxied_requests_are_refused(http_world):
    w = http_world
    info = fabric_agent(w)
    path = f"{BASE}/rooms/{w.room}/events?cursor=0"
    w.localapi["nodes"][w.lan] = "stranger.tail1234.ts.net."
    assert req(w, "GET", path, headers=auth(info, **{"X-Forwarded-For": BENDER}))[0] == 401
    w.localapi["nodes"][w.lan] = "bender.tail1234.ts.net."
    assert req(w, "GET", path, headers=auth(info, **{"X-Forwarded-For": STRANGER}))[0] == 401   # a forwarding header means a proxy


def test_http_flag_off_and_kill_switch_are_401(http_world):
    w = http_world
    info = fabric_agent(w)
    w.localapi["nodes"][w.lan] = "bender.tail1234.ts.net."
    path = f"{BASE}/rooms/{w.room}/events?cursor=0"
    assert req(w, "GET", path, headers=auth(info))[0] == 200
    w.s.fabric_trust = normalize_fabric_trust({"enabled": True, "tailnet_suffix": SUFFIX, "kill_switch": True, "machines": ["bender"]})
    assert req(w, "GET", path, headers=auth(info))[0] == 401
    w.s.fabric_trust = normalize_fabric_trust(None)
    assert req(w, "GET", path, headers=auth(info))[0] == 401


def test_http_public_handler_never_accepts_fabric(http_world):
    w = http_world
    info = fabric_agent(w)
    msg = {"jsonrpc": "2.0", "id": 1, "method": "initialize"}
    a2a = {"jsonrpc": "2.0", "id": 1, "method": "agent/getAuthenticatedExtendedCard"}
    assert req(w, "POST", "/mcp", public=True, headers=auth(info), body=msg)[0] == 401
    assert req(w, "POST", "/a2a", public=True, headers=auth(info), body=a2a)[0] == 401
    w.localapi["nodes"][w.lan] = "bender.tail1234.ts.net."
    # the public (internet-facing) handler NEVER accepts Fabric, even from a perfectly valid tailnet peer
    assert req(w, "POST", "/mcp", public=True, headers=auth(info), body=msg)[0] == 401
    assert req(w, "POST", "/a2a", public=True, headers=auth(info), body=a2a)[0] == 401
    assert req(w, "GET", f"{BASE}/rooms/{w.room}/events?cursor=0", public=True, headers=auth(info))[0] == 401
    # ...while the same request on the main collab listener is fine
    assert req(w, "GET", f"{BASE}/rooms/{w.room}/events?cursor=0", headers=auth(info))[0] == 200


def test_http_set_bit_route_is_owner_only(http_world):
    w = http_world
    info = fabric_agent(w, bit=False)
    path = f"{BASE}/agents/{info['agent_id']}/fabric"
    w.localapi["nodes"][w.lan] = "bender.tail1234.ts.net."
    cred = {"Authorization": f"Agent {info['credential']}", "X-Herdr-Device": info["device_id"]}
    assert req(w, "POST", path, headers=cred, body={"fabric": True})[0] == 403
    from tests.control_plane.test_collab import TOKENS
    assert req(w, "POST", path, headers={"Authorization": f"Bearer {TOKENS['bob']}"}, body={"fabric": True})[0] == 404
    s, b = req(w, "POST", path, headers={"Authorization": f"Bearer {TOKENS['alice']}"}, body={"fabric": True})
    assert s == 200 and b["fabric"] is True
    assert req(w, "GET", f"{BASE}/rooms/{w.room}/events?cursor=0", headers=auth(info))[0] == 200


def test_http_stream_opened_via_fabric_ends_when_the_agent_is_revoked(http_world):
    w = http_world
    info = fabric_agent(w)
    w.localapi["nodes"][w.lan] = "bender.tail1234.ts.net."
    c = http.client.HTTPConnection(w.lan, w.port, timeout=10)
    c.request("GET", f"{BASE}/rooms/{w.room}/events/stream?cursor=0", headers=auth(info))
    r = c.getresponse()
    assert r.status == 200
    assert read_frame(r)["type"] == "connected"
    w.s.revoke_agent(w.alice, info["agent_id"])
    assert read_frame(r) == {"type": "closed", "reason": "access_revoked"}
    c.close()


# ------------------------------------------------------------------ peer must be a tailnet address
def test_peer_outside_the_tailnet_ranges_is_never_fabric(fw):
    info = fabric_agent(fw)
    for ip in ("192.168.1.5", "10.0.0.1", "172.16.0.1", "8.8.8.8", "100.128.0.1", "100.63.255.255", "fd00::1", "2001:db8::1"):
        fw.localapi["nodes"][ip] = "bender.tail1234.ts.net."
        denied(fw, info, peer=ip)
    for ip in ("100.127.255.254", "fd7a:115c:a1e0::5"):
        fw.localapi["nodes"][ip] = "bender.tail1234.ts.net."
        assert fabric(fw, info, peer=ip).machine == "bender"


# ------------------------------------------------------------------ full-name identity
def test_machine_is_the_full_fqdn_not_the_first_label(fw):
    info = fabric_agent(fw)
    fw.localapi["nodes"]["100.64.0.30"] = "bender.attacker.example."
    denied(fw, info, peer="100.64.0.30")
    fw.localapi["nodes"]["100.64.0.31"] = "BENDER.TAIL1234.ts.net"
    assert fabric(fw, info, peer="100.64.0.31").machine == "bender"
    assert code(fw.s.authenticate, f"Fabric {info['agent_id']}", None, peer_ip="100.64.0.30") == 401


# ------------------------------------------------------------------ expiry
def test_fabric_bit_expires_and_setting_it_again_renews(fw):
    s = fw.s
    clock = {"t": 1000.0}
    s._now = lambda: clock["t"]
    info = fabric_agent(fw)
    aid = info["agent_id"]
    ttl = s.fabric_trust_cfg()["agent_ttl_seconds"]
    assert ttl == 2592000
    row = [a for a in s.list_my_agents(fw.alice)["agents"] if a["agent_id"] == aid][0]
    assert row["fabric_expires_at"] == 1000.0 + ttl
    pr = fabric(fw, info)
    clock["t"] = 1000.0 + ttl - 1
    assert s.still_valid(pr) and fabric(fw, info)
    clock["t"] = 1000.0 + ttl
    assert not s.still_valid(pr)
    denied(fw, info)
    assert "fabric_expired" in [e["payload"]["reason"] for e in s._log if e["event_type"] == "audit_denied"]
    out = s.set_agent_fabric(fw.alice, aid, {"fabric": True})                  # renew
    assert out["fabric_expires_at"] == clock["t"] + ttl and fabric(fw, info)
    s.set_agent_fabric(fw.alice, aid, {"fabric": False})
    assert [a for a in s.list_my_agents(fw.alice)["agents"] if a["agent_id"] == aid][0]["fabric_expires_at"] is None


def test_ttl_comes_from_config_and_old_events_without_expiry_fail_closed(fw):
    fw.s.fabric_trust = normalize_fabric_trust({"enabled": True, "machines": ["bender"], "tailnet_suffix": SUFFIX,
                                                "agent_ttl_seconds": 300, "whois_cache_seconds": 0})
    info = fabric_agent(fw)
    assert fw.s._agents[info["agent_id"]]["fabric_expires_at"] == fw.s._now() + 300
    old = fabric_agent(fw, bit=False)                   # an event from before this field existed
    fw.s._commit("agent_fabric_set", {"actor_user_id": "alice", "actor_agent_id": None, "agent_id": old["agent_id"],
                                      "fabric": True, "machine": "bender", "action": "agent.fabric.set", "outcome": "allowed",
                                      "project_ids": [], "resource_id": old["agent_id"]})
    assert fw.s._agents[old["agent_id"]]["fabric"] is True
    denied(fw, old)


# ------------------------------------------------------------------ audit flood + actor routing
def test_a_flood_of_refusals_writes_one_audit_row_per_agent_and_reason(fw):
    info = fabric_agent(fw, bit=False)
    before = len(fw.s._log)
    for _ in range(50):
        denied(fw, info)
    rows = [e for e in list(fw.s._log)[before:] if e["event_type"] == "audit_denied"]
    assert len(rows) == 1 and rows[0]["payload"]["reason"] == "fabric_not_enabled"
    assert rows[0]["payload"]["via"] == "fabric" and rows[0]["payload"]["machine"] == "bender"


def test_helper_task_event_carries_the_actor_via_fields(fw):
    info = fabric_agent(fw, actions=("read", "post", "spawn_helpers"))
    fw.s.set_room_profile(fw.alice, fw.room, {"allowed_families": ["claude-code"]})
    pr = fabric(fw, info)
    fw.s.request_helper(pr, {"room_id": fw.room, "role": "worker", "task": "look at this"})
    row = [e for e in fw.s._log if e["event_type"] == "helper_task"][-1]["payload"]
    assert row["via"] == "fabric" and row["machine"] == "bender" and row["actor_agent_id"] == info["agent_id"]

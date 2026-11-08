# ruff: noqa: F811  (pytest fixtures are imported by name from the sibling test modules)
"""Fabric auto-join: an agent on an allow-listed tailnet machine joins with NO token and NO owner click.

Real CollabService over a real file event log, real HTTP servers, real unix-socket tailscaled-LocalAPI fixture. No mocks."""
from __future__ import annotations

import http.client
import ipaddress
import json
import threading

import pytest

from herdr_engineering.control_plane import collab_fabric_trust as fabric_trust_module
from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.collab import CollabService, StaticTokenAuthenticator
from herdr_engineering.control_plane.collab_fabric_trust import (
    FABRIC_TRUST_KEYS,
    FabricPeerResolver,
    normalize_fabric_trust,
)
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from herdr_engineering.fabric_access import TailscaleWhois
from tests.control_plane.test_collab import BASE
from tests.control_plane.test_collab_fabric_auth import (  # noqa: F401
    BEAST,
    BENDER,
    SUFFIX,
    STRANGER,
    TAILNET,
    fabric_agent,
    fw,
    lan_ip,
)
from tests.control_plane.test_collab_fabric_trust import localapi  # noqa: F401
from tests.control_plane.test_collab_tasks import USERS, _digest, code, svc  # noqa: F401

OWNER = "alice"


def trust(**kw):
    return {"enabled": True, "tailnet_suffix": SUFFIX, "machines": ["bender", "beast"], "whois_cache_seconds": 0, **kw}


def set_trust(w, **kw):
    w.s.fabric_trust = normalize_fabric_trust(trust(**kw))


@pytest.fixture
def aw(fw):
    """fw with auto_join switched on."""
    set_trust(fw, auto_join=True)
    return fw


def join(w, name="bender-claude", peer=BENDER, body=None, authorization="", fabric_listener=True, owner=OWNER, machine=None):
    return w.s.dispatch("POST", ["agents", "fabric-join"], {}, {"name": name, **(body or {})}, authorization, None,
                        peer_ip=peer, machine=machine, fabric_listener=fabric_listener, fabric_owner=owner)


def agent_auth(w, aid, peer=BENDER):
    return w.s.authenticate(f"Fabric {aid}", None, peer_ip=peer)


def enrolled_events(w):
    return [e for e in w.s._log if e["event_type"] == "agent_enrolled"]


def fabric_set_events(w):
    return [e for e in w.s._log if e["event_type"] == "agent_fabric_set"]


# ------------------------------------------------------------------ the config switch
def test_auto_join_is_a_known_bool_key_default_off_and_needs_enabled():
    assert "auto_join" in FABRIC_TRUST_KEYS
    assert normalize_fabric_trust(None)["auto_join"] is False
    assert normalize_fabric_trust(trust())["auto_join"] is False
    assert normalize_fabric_trust(trust(auto_join=True))["auto_join"] is True
    for bad in ("yes", 1, None, [], {}):
        with pytest.raises(ValueError, match="auto_join"):
            normalize_fabric_trust(trust(auto_join=bad))
    with pytest.raises(ValueError, match="auto_join"):
        normalize_fabric_trust({"auto_join": True})                      # needs enabled
    with pytest.raises(ValueError, match="auto_join"):
        normalize_fabric_trust({**trust(auto_join=True), "enabled": False})


def test_auto_join_is_hot_reloaded_with_the_other_keys(fw):
    raw = {"v": trust()}
    fw.s.fabric_trust_source = lambda: raw["v"]
    assert fw.s.fabric_trust_cfg()["auto_join"] is False
    assert join(fw)[0] == 404
    raw["v"] = trust(auto_join=True)
    assert fw.s.fabric_trust_cfg()["auto_join"] is True
    assert join(fw)[0] == 201
    raw["v"] = {"auto_join": "maybe"}                                     # an invalid edit means OFF
    assert join(fw, name="other")[0] == 404


# ------------------------------------------------------------------ the happy path
def test_join_creates_a_normal_operator_owned_agent_with_the_fabric_bit(aw):
    status, out = join(aw)
    assert status == 201
    assert set(out) == {"agent_id", "name", "machine", "fabric_expires_at"}
    assert (out["name"], out["machine"]) == ("bender-claude", "bender")
    a = aw.s._agents[out["agent_id"]]
    assert a["owner"] == OWNER and a["status"] == "active" and not a.get("external")
    assert a["machine"] == "bender" and a["fabric"] is True and a["origin"] == "fabric_join"
    assert out["fabric_expires_at"] == a["fabric_expires_at"] == aw.s._now() + aw.s.fabric_trust_cfg()["agent_ttl_seconds"]
    assert out["agent_id"] not in aw.s._helper_minted and out["agent_id"] not in aw.s._helper_by_agent
    pr = agent_auth(aw, out["agent_id"])                                  # usable at once, no token, no owner click
    assert (pr.kind, pr.owner, pr.via, pr.machine) == ("agent", OWNER, "fabric", "bender")


def test_the_event_is_a_normal_audited_enrolment_via_fabric(aw):
    _, out = join(aw)
    ev = enrolled_events(aw)[-1]["payload"]
    assert ev["agent_id"] == out["agent_id"] and ev["origin"] == "fabric_join"
    assert (ev["via"], ev["machine"], ev["actor_user_id"], ev["actor_agent_id"]) == ("fabric", "bender", OWNER, None)
    assert ev["action"] == "agent.fabric_join" and ev["outcome"] == "allowed"
    assert "credential" not in json.dumps(out)


def test_the_authorization_header_is_ignored_whatever_it_says(aw):
    assert join(aw, name="a-1")[0] == 201
    assert join(aw, name="a-2", authorization="Fabric agent_whatever")[0] == 201
    assert join(aw, name="a-3", authorization="Bearer " + USERS["alice"])[0] == 201
    assert join(aw, name="a-4", authorization="Garbage")[0] == 201


def test_the_header_machine_is_informational_and_must_match_whois(aw):
    assert join(aw, name="m-1", machine="bender", body={"machine": "bender"})[0] == 201
    assert join(aw, name="m-2", body={"machine": "beast"})[0] == 401      # claims another machine than whois found
    assert join(aw, name="m-3", machine="beast")[0] == 401
    assert join(aw, name="m-4", body={"machine": "Bad Name!"})[0] == 400


# ------------------------------------------------------------------ idempotence
def test_a_repeat_join_returns_the_same_agent_and_renews_the_expiry(aw):
    _, first = join(aw)
    n = len(enrolled_events(aw))
    aw.s.clock.t += 3600
    status, again = join(aw)
    assert status == 200 and again["agent_id"] == first["agent_id"]
    assert len(enrolled_events(aw)) == n                                  # no duplicate
    assert again["fabric_expires_at"] == first["fabric_expires_at"] + 3600
    assert sum(1 for a in aw.s._agents.values() if a.get("origin") == "fabric_join") == 1


def test_the_same_name_on_two_machines_is_two_agents(aw):
    _, a = join(aw, peer=BENDER)
    _, b = join(aw, peer=BEAST)
    assert a["agent_id"] != b["agent_id"] and (a["machine"], b["machine"]) == ("bender", "beast")
    assert agent_auth(aw, b["agent_id"], BEAST).machine == "beast"
    assert code(agent_auth, aw, b["agent_id"], BENDER) == 401              # bound to its own machine


def test_a_join_never_adopts_an_existing_agent_of_the_same_name(aw):
    plain = fabric_agent(aw, machine="bender", bit=False)
    _, out = join(aw, name=aw.s._agents[plain["agent_id"]]["name"])
    assert out["agent_id"] != plain["agent_id"]
    assert aw.s._agents[plain["agent_id"]]["fabric"] is False             # the owner's agent was not touched


@pytest.mark.parametrize("bad", ["", " ", "a" * 65, "../etc", "a/b", "has space", "UPPER", ".hidden", "-lead", "x\n", 5, None, ["a"]])
def test_names_are_validated(aw, bad):
    status, _ = join(aw, name=bad)
    assert status == 400
    assert not [a for a in aw.s._agents.values() if a.get("origin") == "fabric_join"]


def test_a_missing_name_is_400(aw):
    status, _ = aw.s.dispatch("POST", ["agents", "fabric-join"], {}, {}, "", None, peer_ip=BENDER, fabric_listener=True, fabric_owner=OWNER)
    assert status == 400


# ------------------------------------------------------------------ who may join
def test_off_kill_switch_and_disabled_are_a_generic_404(fw):
    assert join(fw)[0] == 404                                             # auto_join defaults off
    set_trust(fw, auto_join=True, kill_switch=True)
    assert join(fw)[0] == 404
    set_trust(fw)
    assert join(fw)[0] == 404
    assert not enrolled_events(fw)[-1:] or enrolled_events(fw)[-1]["payload"].get("origin") != "fabric_join"


def test_only_a_listed_tailnet_machine_joins(aw):
    assert join(aw, peer=STRANGER)[0] == 401                             # on the tailnet, not allow-listed
    assert join(aw, peer="100.64.0.77")[0] == 401                        # whois does not know it
    for peer in (None, "", "127.0.0.1", "::ffff:127.0.0.1", "10.0.0.5", "not-an-ip"):
        assert join(aw, peer=peer)[0] == 401
    assert not [a for a in aw.s._agents.values() if a.get("origin") == "fabric_join"]


def test_the_route_only_exists_on_the_fabric_listener(aw):
    assert join(aw, fabric_listener=False)[0] == 404
    assert join(aw, owner=None)[0] == 404                                # no operator user configured: nobody to own the agent
    assert aw.s.dispatch("POST", ["agents", "fabric-join"], {}, {"name": "x-1"}, "", None, operator="alice")[0] != 201


def test_a_degraded_service_does_not_join(aw):
    aw.s.degraded = True
    assert join(aw)[0] in (401, 404, 503)
    aw.s.degraded = False
    assert not [a for a in aw.s._agents.values() if a.get("origin") == "fabric_join"]


def test_a_disabled_owner_cannot_own_new_agents(aw):
    assert join(aw, owner="nobody-here")[0] in (401, 404)


# ------------------------------------------------------------------ what an auto-joined agent can do
def test_an_auto_joined_agent_has_no_room_permissions_and_no_coordinate(aw):
    _, out = join(aw)
    aid = out["agent_id"]
    pr = agent_auth(aw, aid)
    listed = aw.s.list_my_agents(aw.alice)["agents"]
    mine = next(a for a in listed if a["agent_id"] == aid)
    assert mine["rooms"] == [] and mine["origin"] == "fabric_join" and mine["fabric"] is True
    assert code(aw.s.read_events, pr, aw.room, 0, 10) in (403, 404)
    assert code(aw.s.post_message, pr, aw.room, {"text": "hi", "idempotency_key": "k"}) in (403, 404)
    assert aw.s.agent_rooms(pr)["rooms"] == []
    # nothing can be self-granted: grants are the owner's, an agent principal is refused
    assert code(aw.s.grant_agent, pr, aw.room, {"agent_id": aid, "actions": ["read", "post"]}) in (403, 404)
    assert code(aw.s.grant_agent, pr, aw.room, {"agent_id": aid, "actions": ["coordinate"], "team_id": "t", "roles": ["reviewer"]}) in (403, 404)
    assert code(aw.s.set_agent_fabric, pr, aid, {"fabric": True}) == 403
    assert code(aw.s.rotate_credential, pr) == 403
    for room in aw.s._rooms.values():
        assert aid not in room["grants"]


def test_the_owner_grants_as_before_and_the_agent_then_acts_through_fabric(aw):
    _, out = join(aw)
    aid = out["agent_id"]
    res = aw.s.grant_agent(aw.alice, aw.room, {"agent_id": aid, "actions": ["read", "post"]})
    if res["status"] == "pending":
        aw.s.decide_agent_request(aw.alice, aw.room, res["request_id"], {"decision": "approve"})
    pr = agent_auth(aw, aid)
    assert aw.s.read_events(pr, aw.room, 0, 10)["events"] is not None
    aw.s.post_message(pr, aw.room, {"text": "from the fabric", "idempotency_key": "j1"})
    row = [e for e in aw.s._log if e["event_type"] == "message"][-1]["payload"]
    assert row["via"] == "fabric" and row["machine"] == "bender" and row["actor_agent_id"] == aid


# ------------------------------------------------------------------ the switch also governs the agents it made
def test_switching_auto_join_off_or_the_kill_switch_ends_fabric_auth_of_auto_joined_agents(aw):
    _, out = join(aw)
    agent_auth(aw, out["agent_id"])
    set_trust(aw)                                                         # auto_join off, fabric on
    assert code(agent_auth, aw, out["agent_id"]) == 401
    set_trust(aw, auto_join=True)
    agent_auth(aw, out["agent_id"])                                       # back on: it works again, nothing was deleted
    set_trust(aw, auto_join=True, kill_switch=True)
    assert code(agent_auth, aw, out["agent_id"]) == 401
    set_trust(aw, auto_join=True)
    other = fabric_agent(aw)                                              # an owner-enabled agent is NOT governed by auto_join
    set_trust(aw)
    assert agent_auth(aw, other["agent_id"]).via == "fabric"


# ------------------------------------------------------------------ revocation
def test_clearing_the_bit_revokes_at_once_and_a_rejoin_does_not_resurrect(aw):
    _, out = join(aw)
    aid = out["agent_id"]
    aw.s.set_agent_fabric(aw.alice, aid, {"fabric": False})
    assert code(agent_auth, aw, aid) == 401
    n = len(enrolled_events(aw))
    status, body = join(aw)
    assert status == 403 and body == {"error": "forbidden"}
    assert aw.s._agents[aid]["fabric"] is False and len(enrolled_events(aw)) == n
    assert code(agent_auth, aw, aid) == 401
    denied = [e["payload"] for e in aw.s._log if e["event_type"] == "audit_denied" and e["payload"]["action"] == "agent.fabric_join"]
    assert denied and denied[-1]["reason"] == "fabric_join_revoked" and denied[-1]["machine"] == "bender"
    # another name on the same machine is a different agent and still joins
    assert join(aw, name="bender-other")[0] == 201


def test_revoking_the_agent_revokes_at_once_and_a_rejoin_does_not_resurrect(aw):
    _, out = join(aw)
    aid = out["agent_id"]
    aw.s.revoke_agent(aw.alice, aid)
    assert code(agent_auth, aw, aid) == 401
    status, _ = join(aw)
    assert status == 403
    assert aw.s._agents[aid]["status"] == "revoked"
    assert sum(1 for a in aw.s._agents.values() if a.get("origin") == "fabric_join") == 1


def test_the_owner_can_switch_a_revoked_bit_back_on_deliberately(aw):
    _, out = join(aw)
    aw.s.set_agent_fabric(aw.alice, out["agent_id"], {"fabric": False})
    aw.s.set_agent_fabric(aw.alice, out["agent_id"], {"fabric": True})
    assert agent_auth(aw, out["agent_id"]).via == "fabric"
    assert join(aw)[0] == 200                                             # and then the same agent renews as usual


# ------------------------------------------------------------------ limits
def test_joins_are_rate_limited_per_machine_and_refusals_are_audited(aw):
    aw.s.fabric_join_per_hour = 5
    for i in range(5):
        assert join(aw, name=f"r-{i}")[0] == 201
    status, _ = join(aw, name="r-5")
    assert status == 429
    assert join(aw, name="r-b", peer=BEAST)[0] == 201                    # another machine has its own budget
    reasons = [e["payload"]["reason"] for e in aw.s._log if e["event_type"] == "audit_denied" and e["payload"]["action"] == "agent.fabric_join"]
    assert "fabric_join_rate_limited" in reasons
    aw.s.clock.t += 3601
    assert join(aw, name="r-5")[0] == 201


def test_a_machine_has_a_hard_cap_of_auto_joined_agents(aw):
    aw.s.fabric_join_max_agents = 3
    for i in range(3):
        assert join(aw, name=f"c-{i}")[0] == 201
    assert join(aw, name="c-3")[0] == 429
    assert join(aw, name="c-1")[0] == 200                                # an existing one still renews
    assert join(aw, name="c-z", peer=BEAST)[0] == 201
    reasons = [e["payload"]["reason"] for e in aw.s._log if e["event_type"] == "audit_denied" and e["payload"]["action"] == "agent.fabric_join"]
    assert "fabric_join_machine_cap" in reasons
    aw.s.revoke_agent(aw.alice, next(a for a, v in aw.s._agents.items() if v["name"] == "c-0"))
    assert join(aw, name="c-3")[0] == 201                                # revoked ones do not count against the cap


# ------------------------------------------------------------------ sliding renewal
def test_a_fabric_request_renews_an_auto_joined_agent_at_most_every_ten_minutes(aw):
    _, out = join(aw)
    aid = out["agent_id"]
    ttl = aw.s.fabric_trust_cfg()["agent_ttl_seconds"]
    n = len(fabric_set_events(aw))
    aw.s.clock.t += 120
    agent_auth(aw, aid)
    assert len(fabric_set_events(aw)) == n                                # inside the window: no write
    aw.s.clock.t += 600
    agent_auth(aw, aid)
    assert len(fabric_set_events(aw)) == n + 1
    ev = fabric_set_events(aw)[-1]["payload"]
    assert ev["origin"] == "fabric_join" and ev["via"] == "fabric" and ev["fabric"] is True
    assert aw.s._agents[aid]["fabric_expires_at"] == aw.s._now() + ttl
    agent_auth(aw, aid)
    agent_auth(aw, aid)
    assert len(fabric_set_events(aw)) == n + 1


def test_an_active_auto_joined_agent_outlives_the_fixed_ttl(aw):
    _, out = join(aw)
    aid = out["agent_id"]
    ttl = aw.s.fabric_trust_cfg()["agent_ttl_seconds"]
    for _ in range(4):
        aw.s.clock.t += ttl * 0.6
        agent_auth(aw, aid)                                              # each use slides the window forward
    assert aw.s._now() > out["fabric_expires_at"]
    aw.s.clock.t += ttl + 1                                              # idle for a whole window: expired, the agent re-joins
    assert code(agent_auth, aw, aid) == 401
    status, again = join(aw)
    assert status == 200 and again["agent_id"] == aid
    agent_auth(aw, aid)


def test_owner_set_bits_keep_the_fixed_ttl(aw):
    info = fabric_agent(aw)
    fixed = aw.s._agents[info["agent_id"]]["fabric_expires_at"]
    n = len(fabric_set_events(aw))
    aw.s.clock.t += 5000
    agent_auth(aw, info["agent_id"])
    assert len(fabric_set_events(aw)) == n
    assert aw.s._agents[info["agent_id"]]["fabric_expires_at"] == fixed


def test_a_full_log_does_not_break_authentication_when_renewal_cannot_be_written(aw):
    _, out = join(aw)
    aw.s.clock.t += 700
    aw.s.max_log_events = len(aw.s._log)                                  # the log is full: renewal is best effort
    n = len(aw.s._log)
    assert agent_auth(aw, out["agent_id"]).via == "fabric"
    assert len(aw.s._log) == n


# ------------------------------------------------------------------ restart
def test_state_and_the_index_survive_a_restart_from_the_log(aw):
    _, out = join(aw)
    aw.s.clock.t += 700
    agent_auth(aw, out["agent_id"])
    fresh = CollabService(StaticTokenAuthenticator({u: _digest(t) for u, t in USERS.items()}), now=aw.s._now)
    fresh.hydrate(list(aw.s._log))
    fresh.fabric_trust = aw.s.fabric_trust
    fresh.fabric_resolver = aw.s.fabric_resolver
    assert fresh._agents[out["agent_id"]]["origin"] == "fabric_join"
    assert fresh.authenticate(f"Fabric {out['agent_id']}", None, peer_ip=BENDER).via == "fabric"
    status, again = fresh.dispatch("POST", ["agents", "fabric-join"], {}, {"name": "bender-claude"}, "", None,
                                   peer_ip=BENDER, fabric_listener=True, fabric_owner=OWNER)
    assert status == 200 and again["agent_id"] == out["agent_id"]
    aw.s.set_agent_fabric(aw.alice, out["agent_id"], {"fabric": False})
    fresh2 = CollabService(StaticTokenAuthenticator({u: _digest(t) for u, t in USERS.items()}), now=aw.s._now)
    fresh2.hydrate(list(aw.s._log))
    fresh2.fabric_trust, fresh2.fabric_resolver = aw.s.fabric_trust, aw.s.fabric_resolver
    assert fresh2.dispatch("POST", ["agents", "fabric-join"], {}, {"name": "bender-claude"}, "", None,
                           peer_ip=BENDER, fabric_listener=True, fabric_owner=OWNER)[0] == 403


# ------------------------------------------------------------------ over real HTTP
@pytest.fixture
def lw(aw, request, monkeypatch):
    """collab-only process (public handler) plus the dedicated fabric listener; the operator user is alice."""
    ip = lan_ip()
    if ip is None:
        pytest.fail("no non-loopback address: the fabric listener tests cannot run")
    on_tailnet = ipaddress.ip_address(ip) in TAILNET
    if not on_tailnet:
        monkeypatch.setattr(fabric_trust_module, "FABRIC_PEER_NETS", (*fabric_trust_module.FABRIC_PEER_NETS, ipaddress.ip_network(ip + "/32")))
    aw.s.fabric_resolver = FabricPeerResolver(aw.s.fabric_trust_cfg, whois=TailscaleWhois(sockets=(aw.localapi["sock"],), timeout=2.0))
    srv = ControlPlaneServer(ControlPlaneRepo(), host="127.0.0.1", port=0, collab=aw.s, collab_only=True, public_port=0,
                             public_host=ip, fabric_listen=(ip, 0), fabric_allow_any_host=not on_tailnet, operator_user=OWNER)
    t = threading.Thread(target=srv.serve, daemon=True)
    t.start()
    srv.wait_until_ready()
    aw.lan, aw.public_port, aw.fabric_port, aw.srv = ip, srv.public_port, srv.fabric_port, srv
    aw.localapi["nodes"][ip] = "bender.tail1234.ts.net."
    yield aw
    srv.shutdown()
    t.join(timeout=5)


def hreq(w, method, path, *, headers=None, body=None, port=None, host=None):
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


def test_http_join_on_the_fabric_listener_needs_no_authorization_header(lw):
    status, out = hreq(lw, "POST", f"{BASE}/agents/fabric-join", body={"name": "bender-claude"})
    assert status == 201 and out["machine"] == "bender" and out["name"] == "bender-claude"
    status, again = hreq(lw, "POST", f"{BASE}/agents/fabric-join", body={"name": "bender-claude"},
                         headers={"Authorization": "Fabric whatever"})
    assert status == 200 and again["agent_id"] == out["agent_id"]
    status, _ = hreq(lw, "GET", f"{BASE}/agents", headers={"Authorization": f"Fabric {out['agent_id']}"})
    assert status in (200, 403)                                          # authenticated as the agent (a person route refuses agents)


def test_http_other_routes_on_the_listener_still_need_the_fabric_scheme(lw):
    status, _ = hreq(lw, "GET", f"{BASE}/agents")
    assert status == 401
    status, _ = hreq(lw, "POST", f"{BASE}/agents/fabric-join", body={"name": "x-1"}, headers={"Authorization": "Bearer " + USERS["alice"]})
    assert status == 201                                                 # ignored, like any header on this route


def test_http_the_join_route_is_404_on_the_public_listener(lw):
    status, _ = hreq(lw, "POST", f"{BASE}/agents/fabric-join", body={"name": "bender-claude"}, port=lw.public_port)
    assert status in (404, 401)
    assert status == 404 or not [a for a in lw.s._agents.values() if a.get("origin") == "fabric_join"]
    assert not [a for a in lw.s._agents.values() if a.get("origin") == "fabric_join"]


def test_http_a_relayed_request_cannot_join(lw):
    status, _ = hreq(lw, "POST", f"{BASE}/agents/fabric-join", body={"name": "bender-claude"}, headers={"X-Forwarded-For": "100.64.0.1"})
    assert status == 401
    assert not [a for a in lw.s._agents.values() if a.get("origin") == "fabric_join"]


def test_http_a_foreign_host_header_is_refused(lw):
    status, _ = hreq(lw, "POST", f"{BASE}/agents/fabric-join", body={"name": "bender-claude"}, headers={"Host": "evil.example"})
    assert status == 421


def test_http_a_peer_that_is_not_listed_cannot_join(lw):
    lw.localapi["nodes"][lw.lan] = "stranger.tail1234.ts.net."
    status, _ = hreq(lw, "POST", f"{BASE}/agents/fabric-join", body={"name": "bender-claude"})
    assert status == 401
    lw.localapi["nodes"][lw.lan] = "bender.evil.ts.net."                 # wrong tailnet suffix
    assert hreq(lw, "POST", f"{BASE}/agents/fabric-join", body={"name": "bender-claude"})[0] == 401


def test_http_off_is_404_on_the_listener(lw):
    set_trust(lw)
    status, _ = hreq(lw, "POST", f"{BASE}/agents/fabric-join", body={"name": "bender-claude"})
    assert status == 404

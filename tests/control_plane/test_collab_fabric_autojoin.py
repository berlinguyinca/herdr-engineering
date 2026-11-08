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
    STRANGER,
    SUFFIX,
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
    aw.s.clock.t += 90_000
    status, again = join(aw)
    assert status == 200 and again["agent_id"] == first["agent_id"]
    assert len(enrolled_events(aw)) == n                                  # no duplicate
    assert again["fabric_expires_at"] == first["fabric_expires_at"] + 90_000
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
    assert join(aw, name="c-3")[0] == 429                                # revoked names still count: the index is bounded


# ------------------------------------------------------------------ sliding renewal
def test_a_polling_agent_costs_about_one_renewal_event_a_day(aw):
    _, out = join(aw)
    aid = out["agent_id"]
    ttl = aw.s.fabric_trust_cfg()["agent_ttl_seconds"]
    n = len(fabric_set_events(aw))
    for _ in range(3 * 144):                                              # every ten minutes for three days
        aw.s.clock.t += 600
        agent_auth(aw, aid)
    written = len(fabric_set_events(aw)) - n
    assert 2 <= written <= 4
    ev = fabric_set_events(aw)[-1]["payload"]
    assert ev["origin"] == "fabric_join" and ev["via"] == "fabric" and ev["fabric"] is True
    assert aw.s._agents[aid]["fabric_expires_at"] > aw.s._now() + ttl - 2 * 86400     # the window is still sliding


def test_the_join_route_cannot_be_used_to_spam_renewal_events(aw):
    _, out = join(aw)
    n = len(aw.s._log)
    for _ in range(100):
        aw.s.clock.t += 5
        status, again = join(aw)
        assert status == 200 and again["agent_id"] == out["agent_id"]
    assert len(aw.s._log) == n                                            # nothing durable inside the renewal interval
    assert again["fabric_expires_at"] == aw.s._agents[out["agent_id"]]["fabric_expires_at"]


def test_a_short_ttl_still_renews_before_it_runs_out(aw):
    set_trust(aw, auto_join=True, agent_ttl_seconds=300)
    _, out = join(aw)
    for _ in range(20):                                                   # 20 minutes, far past one 300 s window
        aw.s.clock.t += 60
        assert agent_auth(aw, out["agent_id"]).via == "fabric"


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
    aw.s.clock.t += 100_000
    agent_auth(aw, info["agent_id"])
    assert len(fabric_set_events(aw)) == n
    assert aw.s._agents[info["agent_id"]]["fabric_expires_at"] == fixed


def test_a_full_log_does_not_break_authentication_when_renewal_cannot_be_written(aw):
    _, out = join(aw)
    aw.s.clock.t += 100_000
    aw.s.max_log_events = len(aw.s._log)                                  # the log is full: renewal is best effort
    n = len(aw.s._log)
    assert agent_auth(aw, out["agent_id"]).via == "fabric"
    assert len(aw.s._log) == n


# ------------------------------------------------------------------ review findings
def denied_rows(w, reason=None):
    rows = [e["payload"] for e in w.s._log if e["event_type"] == "audit_denied" and e["payload"]["action"] == "agent.fabric_join"]
    return [r for r in rows if reason is None or r["reason"] == reason]


def test_refusal_audits_are_throttled_per_machine_and_reason_not_per_name(aw):
    aw.s.fabric_join_max_agents = 1
    assert join(aw, name="first")[0] == 201
    for i in range(60):                                                   # a varying-name loop
        assert join(aw, name=f"loop-{i}")[0] == 429
    assert len(denied_rows(aw, "fabric_join_machine_cap")) == 1
    assert aw.s.stats["audit_throttled"] >= 59
    aw.s.clock.t += 3600
    assert join(aw, name="loop-x")[0] == 429
    assert len(denied_rows(aw, "fabric_join_machine_cap")) == 2


def test_total_refusal_audits_are_capped_so_the_log_cannot_be_filled(aw):
    aw.s.fabric_join_max_agents = 1
    aw.s.fabric_join_audit_max = 4
    join(aw, name="first")
    for i in range(30):
        aw.s.clock.t += 3600                                              # every gate is open again
        assert join(aw, name=f"loop-{i}")[0] == 429                       # still refused, just no longer written
    assert len(denied_rows(aw)) == 4
    fresh = CollabService(StaticTokenAuthenticator({u: _digest(t) for u, t in USERS.items()}), now=aw.s._now)
    fresh.hydrate(list(aw.s._log))
    assert fresh._fabric_join_denied_total == 4                          # the budget survives a restart


def test_auto_joined_agents_do_not_use_the_operators_own_agent_quota(aw):
    aw.s.max_agents_per_user = 2
    for i in range(5):
        assert join(aw, name=f"q-{i}")[0] == 201
    aw.s._agent_slots(OWNER)                                              # the operator can still enrol their own agents
    aw.s._agent_slots(OWNER, 2)


def test_a_separate_total_cap_bounds_auto_joined_agents_across_machines(aw):
    aw.s.fabric_join_max_total = 2
    assert join(aw, name="t-1")[0] == 201
    assert join(aw, name="t-2", peer=BEAST)[0] == 201
    assert join(aw, name="t-3")[0] == 429
    assert join(aw, name="t-1")[0] == 200                                # renewals are not creations
    assert len(denied_rows(aw, "fabric_join_total_cap")) == 1


def test_a_rejoin_after_the_owner_rebound_the_machine_is_refused_and_changes_nothing(aw):
    _, out = join(aw)
    aid = out["agent_id"]
    aw.s.set_agent_fabric(aw.alice, aid, {"fabric": True, "machine": "beast"})
    n = len(aw.s._log)
    status, _ = join(aw)                                                  # still from bender
    assert status == 403
    assert aw.s._agents[aid]["machine"] == "beast"
    assert len(denied_rows(aw, "fabric_join_machine_rebound")) == 1
    assert [e for e in aw.s._log[n:] if e["event_type"] != "audit_denied"] == []


def test_a_renewal_event_never_rebinds_the_machine_even_on_replay(aw):
    _, out = join(aw)
    aid = out["agent_id"]
    aw.s.clock.t += 100_000
    join(aw)
    ev = fabric_set_events(aw)[-1]
    assert ev["payload"]["action"] == "agent.fabric.renew"
    aw.s.set_agent_fabric(aw.alice, aid, {"fabric": True, "machine": "beast"})
    aw.s.clock.t += 100_000
    agent_auth(aw, aid, peer=BEAST)                                       # sliding renewal from the new machine
    fresh = CollabService(StaticTokenAuthenticator({u: _digest(t) for u, t in USERS.items()}), now=aw.s._now)
    fresh.hydrate(list(aw.s._log))
    assert fresh._agents[aid]["machine"] == "beast"


def test_renewals_and_revoked_or_capped_refusals_do_not_drain_the_creation_budget(aw):
    aw.s.fabric_join_per_hour = 4
    aw.s.fabric_join_max_agents = 4
    assert join(aw, name="a-1")[0] == 201                                 # budget 1 of 4
    for _ in range(25):
        assert join(aw, name="a-1")[0] == 200                             # renewals are free
    _, gone = join(aw, name="a-gone")                                     # budget 2 of 4
    aw.s.revoke_agent(aw.alice, gone["agent_id"])
    for _ in range(25):
        assert join(aw, name="a-gone")[0] == 403                          # a revoked name costs nothing
    assert join(aw, name="a-3")[0] == 201                                 # budget 3 of 4
    assert join(aw, name="a-4")[0] == 201                                 # budget 4 of 4; the cap of 4 is reached (the revoked name counts)
    for i in range(25):
        assert join(aw, name=f"over-{i}")[0] == 429
    assert join(aw, name="a-9")[0] == 429
    aw.s.fabric_join_max_agents = 50
    aw.s.clock.t += 3601                                                  # a new hour: the budget is whole again
    for i in range(4):
        assert join(aw, name=f"b-{i}")[0] == 201


def test_cap_refusals_leave_the_budget_for_legitimate_callers(aw):
    aw.s.fabric_join_per_hour = 2
    aw.s.fabric_join_max_agents = 1
    assert join(aw, name="only")[0] == 201                                # budget 1 of 2
    for i in range(30):
        assert join(aw, name=f"x-{i}")[0] == 429
    aw.s.fabric_join_max_agents = 5
    assert join(aw, name="legit")[0] == 201                               # budget 2 of 2 is still there


def test_auto_joined_agents_are_addressable_like_normal_agents(aw):
    aw.s._commit("identity_linked", {"actor_user_id": OWNER, "actor_agent_id": None, "user_id": OWNER,
                                     "email": "alice@example.org"})      # the operator signed in once, so has a handle
    _, a = join(aw, name="bender-claude")
    _, b = join(aw, name="bender-codex")
    assert aw.s._agents[a["agent_id"]]["harness_type"] != "fabric"
    addr_a, addr_b = aw.s._address(a["agent_id"]), aw.s._address(b["agent_id"])
    assert addr_a and addr_b and addr_a != addr_b                        # operator.agent and operator.agent-2
    assert addr_a.split(".", 1)[0] == addr_b.split(".", 1)[0]
    res = aw.s.grant_agent(aw.alice, aw.room, {"agent_id": a["agent_id"], "actions": ["read", "post"]})
    if res["status"] == "pending":
        aw.s.decide_agent_request(aw.alice, aw.room, res["request_id"], {"decision": "approve"})
    book = aw.s._address_book(aw.room)
    assert addr_a in book and book[addr_a]["id"] == a["agent_id"]        # an @mention of its address resolves to it
    assert "bender-claude" in book and book["bender-claude"]["id"] == a["agent_id"]    # and so does its name


def test_a_revoked_name_stays_blocked_and_counts_toward_the_machine_cap(aw):
    aw.s.fabric_join_max_agents = 3
    ids_ = [join(aw, name=f"n-{i}")[1]["agent_id"] for i in range(3)]
    aw.s.revoke_agent(aw.alice, ids_[0])
    assert join(aw, name="n-0")[0] == 403                                # anti-resurrection
    assert join(aw, name="n-new")[0] == 429                              # the index cannot grow past the cap by churning names
    assert join(aw, name="n-1")[0] == 200


# ------------------------------------------------------------------ restart
def test_state_and_the_index_survive_a_restart_from_the_log(aw):
    _, out = join(aw)
    aw.s.clock.t += 100_000
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

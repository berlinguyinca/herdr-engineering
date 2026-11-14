# ruff: noqa: F811  (pytest fixtures are imported by name from the sibling test modules)
"""Owner renew of an (expired) agent (with its lapsed room grants), and the capped sliding extension on use.
Real CollabService, injected clock."""
from __future__ import annotations

import pytest

from herdr_engineering.control_plane.collab import (
    AGENT_MAX_LIFE,
    CollabError,
    CollabService,
    FileEventLog,
    StaticTokenAuthenticator,
)
from tests.control_plane.test_collab import _digest
from tests.control_plane.test_collab_external import enroll
from tests.control_plane.test_collab_helpers import ask, run
from tests.control_plane.test_collab_helpers import w as hw  # noqa: F401
from tests.control_plane.test_collab_intents import Clock
from tests.control_plane.test_collab_tasks import USERS, W, code, svc, user  # noqa: F401

DAY = 24 * 3600.0


@pytest.fixture
def w(svc):
    return W(svc)


@pytest.fixture
def lw(tmp_path):
    """Like w, but room grants outlive the 30-day credential, so a renewed agent can be seen to keep them."""
    clock = Clock()
    s = CollabService(StaticTokenAuthenticator({u: _digest(t) for u, t in USERS.items()}),
                      sink=FileEventLog(str(tmp_path / "log")), now=clock, max_agents_per_user=20,
                      default_grant_ttl=90 * DAY)
    s.clock = clock
    return W(s)


@pytest.fixture
def gw(tmp_path):
    """Room grants lapse (10 days) well before the 30-day credential does."""
    clock = Clock()
    s = CollabService(StaticTokenAuthenticator({u: _digest(t) for u, t in USERS.items()}),
                      sink=FileEventLog(str(tmp_path / "log")), now=clock, max_agents_per_user=20,
                      default_grant_ttl=10 * DAY)
    s.clock = clock
    return W(s)


def replayed(s, **kw):
    fresh = CollabService(StaticTokenAuthenticator({u: _digest(t) for u, t in USERS.items()}),
                          sink=None, now=s.clock, max_agents_per_user=20, **kw)
    fresh.hydrate(list(s._log))
    return fresh


def auth(s, info):
    return s.authenticate(f"Agent {info["credential"]}", info["device_id"])


def test_renew_keeps_identity_and_grants_and_replaces_the_credential(lw):
    w = lw
    agent, info = w.agent("alice", name="keeper")
    old_cred = info["credential"]
    w.s.clock.t += w.s.remote_credential_ttl + 10                     # expired
    assert code(auth, w.s, info) == 401
    out = w.s.renew_agent(w.alice, info["agent_id"])
    assert out["agent_id"] == info["agent_id"] and out["device_id"] == info["device_id"] and out["name"] == "keeper"
    assert out["credential"].startswith("hca_") and out["credential"] != old_cred
    assert out["expires_at"] == w.s.clock.t + w.s.remote_credential_ttl
    assert code(w.s.authenticate, f"Agent {old_cred}", info["device_id"]) == 401     # the old secret is dead at once
    new = auth(w.s, out)
    assert new.id == info["agent_id"]
    assert w.s.agent_rooms(new)["rooms"][0]["room_id"] == w.room      # the grant survived
    row = next(a for a in w.s.list_my_agents(w.alice)["agents"] if a["agent_id"] == info["agent_id"])
    assert row["name"] == "keeper" and row["status"] == "active" and row["expires_at"] == out["expires_at"]
    ev = [e for e in w.s._log if e["event_type"] == "credential_rotated"][-1]["payload"]
    assert ev["action"] == "agent.renew" and ev["outcome"] == "allowed" and "credential" not in str(ev)
    assert out["grants_restored"] == []                              # the grant had not lapsed: nothing to restore


def test_renew_survives_a_restart(lw):
    w = lw
    _, info = w.agent("alice")
    w.s.clock.t += w.s.remote_credential_ttl + 10
    out = w.s.renew_agent(w.alice, info["agent_id"])
    fresh = CollabService(StaticTokenAuthenticator({u: _digest(t) for u, t in USERS.items()}),
                          sink=None, now=w.s.clock, max_agents_per_user=20)
    fresh.hydrate(list(w.s._log))
    assert auth(fresh, out).id == info["agent_id"]
    assert code(fresh.authenticate, f"Agent {info["credential"]}", info["device_id"]) == 401


def test_only_the_owner_can_renew_others_get_404_agents_get_403(w):
    agent, info = w.agent("alice")
    for who in (w.bob, w.mallory, w.carol):
        assert code(w.s.renew_agent, who, info["agent_id"]) == 404
    assert code(w.s.renew_agent, agent, info["agent_id"]) == 403      # an agent can never renew (it has rotate)
    assert code(w.s.renew_agent, w.alice, "agent_nope") == 404


def test_a_revoked_agent_is_refused(w):
    _, info = w.agent("alice")
    w.s.revoke_agent(w.alice, info["agent_id"])
    assert code(w.s.renew_agent, w.alice, info["agent_id"]) == 409
    assert code(auth, w.s, info) == 401


def test_the_agent_list_says_which_agents_are_renewable(w, hw):
    _, info = w.agent("alice")
    _, fab = w.agent("alice")
    w.s.set_agent_fabric(w.alice, fab["agent_id"], {"fabric": True, "machine": "bender"})
    pr, out, _ = enroll(w)
    rows = {a["agent_id"]: a for a in w.s.list_my_agents(w.alice)["agents"]}
    assert rows[info["agent_id"]]["renewable"] is True
    assert rows[fab["agent_id"]]["renewable"] is False and rows[pr.id]["renewable"] is False
    h, _c = run(hw, ask(hw)["helper_id"])
    assert next(a for a in hw.s.list_my_agents(hw.alice)["agents"] if a["agent_id"] == h.id)["renewable"] is False


def test_a_helper_agent_is_refused(hw):
    h, c = run(hw, ask(hw)["helper_id"])
    assert code(hw.s.renew_agent, hw.alice, h.id) == 403


def test_an_external_agent_is_refused_and_its_hard_expiry_is_untouched(w):
    pr, out, _ = enroll(w)
    before = w.s._agents[pr.id]["expires_at"]
    assert code(w.s.renew_agent, w.alice, pr.id) == 403
    assert w.s._agents[pr.id]["expires_at"] == before


def test_a_fabric_bit_agent_is_refused(w):
    _, info = w.agent("alice")
    w.s.set_agent_fabric(w.alice, info["agent_id"], {"fabric": True, "machine": "bender"})
    assert code(w.s.renew_agent, w.alice, info["agent_id"]) == 403


def test_http_route_renews_for_the_owner_only(w):
    _, info = w.agent("alice")
    w.s.clock.t += w.s.remote_credential_ttl + 10
    path = ["agents", info["agent_id"], "renew"]
    tok = lambda u: f"Bearer {USERS[u]}"                                    # noqa: E731
    assert w.s.dispatch("POST", path, {}, {}, tok("mallory"), None)[0] == 404
    status, body = w.s.dispatch("POST", path, {}, {}, tok("alice"), None)
    assert status == 200 and body["credential"].startswith("hca_") and body["agent_id"] == info["agent_id"]


def test_the_agent_slot_cap_still_applies_to_renewing_an_expired_agent():
    s = CollabService(StaticTokenAuthenticator({u: _digest(t) for u, t in USERS.items()}),
                      sink=None, now=(clock := Clock()), max_agents_per_user=1)
    alice = s.authenticate(f"Bearer {USERS["alice"]}", None)
    old = s.connect_agent(alice, {"name": "old"})
    clock.t += s.remote_credential_ttl + 10
    s.connect_agent(alice, {"name": "new"})                               # the expired one no longer holds the slot
    assert code(s.renew_agent, alice, old["agent_id"]) == 429


# ------------------------------------------------------------------ room grants lapse on their own clock
def test_renew_restores_a_grant_that_lapsed_with_the_same_actions(gw):
    w = gw
    _, info = w.agent("alice", actions=("read", "post"))
    w.s.clock.t += 11 * DAY                                             # the 10-day grant lapsed; the credential did not
    new = auth(w.s, info)
    assert w.s.agent_rooms(new)["rooms"] == []                          # housekeeping dropped it from state
    assert any(e["event_type"] == "agent_grant_expired" for e in w.s._log)
    out = w.s.renew_agent(w.alice, info["agent_id"])
    assert out["grants_restored"] == [w.room]
    rooms = w.s.agent_rooms(auth(w.s, out))["rooms"]
    assert [(r["room_id"], r["actions"]) for r in rooms] == [(w.room, ["post", "read"])]
    assert w.s._rooms[w.room]["grants"][info["agent_id"]]["expires_at"] == out["expires_at"]


def test_renew_restores_after_the_credential_itself_expired(gw):
    w = gw
    _, info = w.agent("alice", actions=("read", "post", "execute"))
    w.s.clock.t += w.s.remote_credential_ttl + 10
    w.s._housekeeping()                                                 # housekeeping runs on any hot path
    out = w.s.renew_agent(w.alice, info["agent_id"])
    assert out["grants_restored"] == [w.room]
    assert w.s.agent_rooms(auth(w.s, out))["rooms"][0]["actions"] == ["execute", "post", "read"]


def test_renew_extends_a_live_grant_that_would_lapse_before_the_new_credential(gw):
    w = gw
    _, info = w.agent("alice")
    w.s.clock.t += 8 * DAY                                              # grant still listed, 2 days left
    out = w.s.renew_agent(w.alice, info["agent_id"])
    assert out["grants_restored"] == [w.room]
    assert w.s._rooms[w.room]["grants"][info["agent_id"]]["expires_at"] == out["expires_at"]
    w.s.clock.t += 25 * DAY
    assert w.s.agent_rooms(auth(w.s, out))["rooms"][0]["room_id"] == w.room


def test_a_restored_grant_survives_a_restart(gw):
    w = gw
    _, info = w.agent("alice", actions=("read",))
    w.s.clock.t += 11 * DAY
    w.s.agent_rooms(auth(w.s, info))
    out = w.s.renew_agent(w.alice, info["agent_id"])
    fresh = replayed(w.s)
    assert fresh.agent_rooms(auth(fresh, out))["rooms"][0]["actions"] == ["read"]
    # and a lapse that was never followed by a renew is still restorable after a restart
    _, info2 = w.agent("alice", actions=("read", "post"), room=w.room2)
    w.s.clock.t += 11 * DAY
    w.s.agent_rooms(w.alice)
    w.s._housekeeping()
    fresh = replayed(w.s)
    got = fresh.renew_agent(user(fresh, "alice"), info2["agent_id"])
    assert got["grants_restored"] == [w.room2]


def test_only_rooms_the_owner_owns_are_restored(gw):
    w = gw
    _, info = w.agent("bob", actions=("read", "post"))                 # bob is a contributor: alice approved the grant
    assert w.s._rooms[w.room]["grants"][info["agent_id"]]
    w.s.clock.t += 11 * DAY
    w.s.agent_rooms(w.alice)
    out = w.s.renew_agent(w.bob, info["agent_id"])
    assert out["grants_restored"] == []                                 # bob cannot grant here; alice must approve again
    assert w.s.agent_rooms(auth(w.s, out))["rooms"] == []


def test_a_grant_the_owner_removed_is_not_restored(gw):
    w = gw
    _, info = w.agent("alice")
    w.s.remove_agent_from_room(w.alice, w.room, info["agent_id"])
    w.s.clock.t += 11 * DAY
    w.s.agent_rooms(w.alice)
    assert w.s.renew_agent(w.alice, info["agent_id"])["grants_restored"] == []


# ------------------------------------------------------------------ sliding extension on use
def test_use_extends_the_window_only_when_under_half_is_left(w):
    _, info = w.agent("alice")
    aid = info["agent_id"]
    start = w.s._agents[aid]["expires_at"]
    half = w.s.remote_credential_ttl / 2
    w.s.clock.t += half - DAY
    auth(w.s, info)
    assert w.s._agents[aid]["expires_at"] == start                      # more than half left: nothing written
    events = len(w.s._log)
    w.s.clock.t += DAY + 1
    auth(w.s, info)
    assert w.s._agents[aid]["expires_at"] == w.s.clock.t + w.s.remote_credential_ttl
    assert len(w.s._log) == events + 1
    ev = w.s._log[-1]["payload"]
    assert ev["action"] == "agent.slide" and ev["secret_digest"] == w.s._agents[aid]["cred_digest"]
    auth(w.s, info)
    auth(w.s, info)
    assert len(w.s._log) == events + 1                                  # still one event for the window
    assert auth(w.s, info).id == aid                                    # the same credential keeps working


def test_a_slid_expiry_survives_a_restart(w):
    _, info = w.agent("alice")
    w.s.clock.t += w.s.remote_credential_ttl * 0.6
    auth(w.s, info)
    slid = w.s._agents[info["agent_id"]]["expires_at"]
    fresh = replayed(w.s)
    assert fresh._agents[info["agent_id"]]["expires_at"] == slid
    w.s.clock.t += w.s.remote_credential_ttl * 0.6                      # past the ORIGINAL expiry, inside the slid one
    assert auth(fresh, info).id == info["agent_id"]


def test_sliding_stops_at_the_lifetime_cap_and_a_year_costs_few_events(w):
    _, info = w.agent("alice")
    aid = info["agent_id"]
    created = w.s.clock.t
    day = 0
    while day < 400:
        day += 1
        w.s.clock.t = created + day * DAY
        if code_of(auth, w.s, info) == 401:
            break
    slides = [e for e in w.s._log if e["payload"].get("action") == "agent.slide"]
    assert day == int(AGENT_MAX_LIFE / DAY)                             # dead on the cap day
    assert w.s._agents[aid]["expires_at"] == created + AGENT_MAX_LIFE
    assert len(slides) <= 12                                            # about two writes per 30-day window, not one a day


def test_an_owner_renew_restarts_the_lifetime_cap(w):
    _, info = w.agent("alice")
    aid = info["agent_id"]
    created = w.s.clock.t
    for day in range(1, int(AGENT_MAX_LIFE / DAY) - 4):                 # used daily up to five days before the cap
        w.s.clock.t = created + day * DAY
        auth(w.s, info)
    assert w.s._agents[aid]["expires_at"] == created + AGENT_MAX_LIFE
    out = w.s.renew_agent(w.alice, aid)
    assert out["expires_at"] == w.s.clock.t + w.s.remote_credential_ttl
    w.s.clock.t += 20 * DAY
    auth(w.s, out)
    assert w.s._agents[aid]["expires_at"] > created + AGENT_MAX_LIFE


def test_an_agent_made_before_the_cap_existed_gets_one_at_its_first_slide(w):
    _, info = w.agent("alice")
    aid = info["agent_id"]
    rows = [e for e in w.s._log]
    for e in rows:                                                      # a log written before life_end_epoch existed
        e["payload"].pop("life_end_epoch", None)
    old = replayed(w.s)
    assert old._agents[aid]["life_end"] is None
    w.s.clock.t += old.remote_credential_ttl * 0.6
    auth(old, info)
    assert old._agents[aid]["life_end"] == w.s.clock.t + AGENT_MAX_LIFE


def test_use_never_revives_an_expired_or_revoked_agent(w):
    _, info = w.agent("alice")
    aid = info["agent_id"]
    w.s.clock.t += w.s.remote_credential_ttl + 1
    expires = w.s._agents[aid]["expires_at"]
    assert code(auth, w.s, info) == 401
    assert w.s._agents[aid]["expires_at"] == expires
    _, info2 = w.agent("alice")
    w.s.revoke_agent(w.alice, info2["agent_id"])
    w.s.clock.t += DAY + 1
    assert code(auth, w.s, info2) == 401
    assert w.s._agents[info2["agent_id"]]["status"] == "revoked"


def test_use_never_extends_external_helper_or_fabric_agents(w, hw):
    _, info = w.agent("alice")
    w.s.set_agent_fabric(w.alice, info["agent_id"], {"fabric": True, "machine": "bender"})
    before = w.s._agents[info["agent_id"]]["expires_at"]
    pr, out, _ = enroll(w)
    xbefore = w.s._agents[pr.id]["expires_at"]
    w.s.clock.t = min(before, xbefore) - 10                             # both still valid, nearly out of life
    auth(w.s, info)
    assert w.s._agents[info["agent_id"]]["expires_at"] == before
    w.s.authenticate(f"Agent {out["credential"]}", out["device_id"])
    assert w.s._agents[pr.id]["expires_at"] == xbefore
    h, c = run(hw, ask(hw)["helper_id"])
    before = hw.s._agents[h.id]["expires_at"]
    hw.s.clock.t = before - 10
    hw.s.authenticate(f"Agent {c["credential"]}", c["device_id"])
    assert hw.s._agents[h.id]["expires_at"] == before


def test_a_full_log_does_not_fail_the_request_and_does_not_slide(w):
    _, info = w.agent("alice")
    aid = info["agent_id"]
    w.s.clock.t += w.s.remote_credential_ttl * 0.6
    tight = replayed(w.s, max_log_events=len(w.s._log))                 # a real service whose log is already at its limit
    expires = tight._agents[aid]["expires_at"]
    assert auth(tight, info).id == aid
    assert tight._agents[aid]["expires_at"] == expires
    assert tight.stats["log_full"] >= 1


def code_of(fn, *a):
    try:
        fn(*a)
    except CollabError as e:
        return e.status
    return None

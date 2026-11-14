# ruff: noqa: F811  (pytest fixtures are imported by name from the sibling test modules)
"""Owner renew of an (expired) agent, and the once-a-day sliding extension on use. Real CollabService, injected clock."""
from __future__ import annotations

import pytest

from herdr_engineering.control_plane.collab import (
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


# ------------------------------------------------------------------ sliding extension on use
def test_use_extends_the_window_at_most_once_a_day(w):
    _, info = w.agent("alice")
    aid = info["agent_id"]
    start = w.s._agents[aid]["expires_at"]
    w.s.clock.t += DAY / 2
    auth(w.s, info)
    assert w.s._agents[aid]["expires_at"] == start                      # under a day: nothing written
    events = len(w.s._log)
    w.s.clock.t += DAY / 2 + 1
    auth(w.s, info)
    assert w.s._agents[aid]["expires_at"] == w.s.clock.t + w.s.remote_credential_ttl
    assert len(w.s._log) == events + 1
    ev = w.s._log[-1]["payload"]
    assert ev["action"] == "agent.renew" and ev["secret_digest"] == w.s._agents[aid]["cred_digest"]
    auth(w.s, info)
    auth(w.s, info)
    assert len(w.s._log) == events + 1                                  # still one event for the day
    assert auth(w.s, info).id == aid                                    # the same credential keeps working


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
    pr, out, _ = enroll(w)
    before = w.s._agents[pr.id]["expires_at"]
    w.s.clock.t = before - 10                                           # still valid, long past a day of life
    w.s.authenticate(f"Agent {out["credential"]}", out["device_id"])
    assert w.s._agents[pr.id]["expires_at"] == before
    _, info = w.agent("alice")
    w.s.set_agent_fabric(w.alice, info["agent_id"], {"fabric": True, "machine": "bender"})
    before = w.s._agents[info["agent_id"]]["expires_at"]
    w.s.clock.t += DAY + 1
    auth(w.s, info)
    assert w.s._agents[info["agent_id"]]["expires_at"] == before
    h, c = run(hw, ask(hw)["helper_id"])
    before = hw.s._agents[h.id]["expires_at"]
    hw.s.clock.t = before - 10
    hw.s.authenticate(f"Agent {c["credential"]}", c["device_id"])
    assert hw.s._agents[h.id]["expires_at"] == before


def test_a_full_log_does_not_fail_the_request(w):
    _, info = w.agent("alice")
    w.s.clock.t += DAY + 1
    w.s._log_limit = lambda *a, **k: 0                                   # the log is full for ordinary writes
    assert auth(w.s, info).id == info["agent_id"]

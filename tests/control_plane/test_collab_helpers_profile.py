# ruff: noqa: F811  (pytest fixtures are imported by name from the sibling test modules)
"""Room profile: policy for agent helpers. Names of secrets only, never their values."""
from __future__ import annotations

import json

import pytest

from herdr_engineering.control_plane.collab import CollabService
from tests.control_plane.test_collab_tasks import (  # noqa: F401
    USERS,
    StaticTokenAuthenticator,
    W,
    _digest,
    code,
    svc,
)

GOOD = {
    "allowed_families": ["claude-code", "codex"],
    "allowed_models": ["sonnet", "haiku"],
    "tools_allowed": True,
    "permanent_hosts": ["bender.example.org:22", "10.0.0.5:2222"],
    "services": [{"name": "search", "upstream_url": "https://api.example.org/v1",
                  "header": "X-Api-Key", "secret_name": "SEARCH_KEY"}],
    "ssh_secret_names": ["bender_ssh"],
    "spend_tokens_per_day": 100000,
    "limits": {"live_per_agent": 2, "depth": 1, "per_hour": 10},
}


@pytest.fixture
def w(svc):
    return W(svc)


def test_defaults_when_unset(w):
    p = w.s._profile(w.room)
    assert p["limits"] == {"live_per_agent": 3, "depth": 2, "per_hour": 20}
    assert p["allowed_families"] == [] and p["permanent_hosts"] == [] and p["services"] == []
    assert p["tools_allowed"] is False and p["allowed_models"] is None and p["spend_tokens_per_day"] is None
    assert w.s.get_room_profile(w.carol, w.room) == p


def test_owner_sets_and_gets(w):
    out = w.s.set_room_profile(w.alice, w.room, GOOD)
    assert out == w.s._profile(w.room)
    assert out["allowed_families"] == ["claude-code", "codex"] and out["limits"]["depth"] == 1
    assert w.s.get_room_profile(w.bob, w.room) == out
    assert w.s._profile(w.room2)["allowed_families"] == []          # per room


def test_partial_body_takes_defaults_and_limits_may_equal_defaults(w):
    out = w.s.set_room_profile(w.alice, w.room, {"limits": {"depth": 2}})
    assert out["limits"] == {"live_per_agent": 3, "depth": 2, "per_hour": 20}


def test_non_owner_cannot_set_and_non_member_cannot_see(w):
    for who in (w.bob, w.carol, w.dave, w.mallory):
        assert code(w.s.set_room_profile, who, w.room, GOOD) == 404
    assert code(w.s.get_room_profile, w.mallory, w.room) == 404
    assert code(w.s.set_room_profile, w.alice, "room_nope", GOOD) == 404
    assert w.s._profile(w.room)["allowed_families"] == []


def test_an_agent_cannot_set_the_profile(w):
    agent, _ = w.agent("alice", actions=("read", "post"))
    assert code(w.s.set_room_profile, agent, w.room, GOOD) == 403


@pytest.mark.parametrize("patch", [
    {"services": [{**GOOD["services"][0], "upstream_url": "http://api.example.org/v1"}]},
    {"services": [{**GOOD["services"][0], "upstream_url": "https://u:p@api.example.org/"}]},
    {"services": [{**GOOD["services"][0], "upstream_url": "ftp://x"}]},
    {"services": [{**GOOD["services"][0], "value": "hunter2"}]},            # no value field exists
    {"services": [{**GOOD["services"][0], "secret_name": "has space"}]},
    {"services": [{**GOOD["services"][0], "header": "Bad Header"}]},
    {"services": [GOOD["services"][0], GOOD["services"][0]]},               # duplicate name
    {"services": [{**GOOD["services"][0], "name": f"s{i}"} for i in range(21)]},
    {"limits": {"live_per_agent": 4}}, {"limits": {"depth": 3}}, {"limits": {"per_hour": 21}},
    {"limits": {"depth": 0}}, {"limits": {"depth": True}}, {"limits": {"bogus": 1}},
    {"permanent_hosts": ["Bender:22"]}, {"permanent_hosts": ["bender"]},
    {"permanent_hosts": ["bender:123456"]}, {"permanent_hosts": ["a b:22"]},
    {"permanent_hosts": ["a.b:22"] * 51}, {"permanent_hosts": "bender:22"},
    {"allowed_families": ["gemini"]}, {"allowed_families": "codex"},
    {"allowed_models": "x"}, {"tools_allowed": "yes"},
    {"ssh_secret_names": ["pass word"]}, {"ssh_secret_names": [5]},
    {"spend_tokens_per_day": -1}, {"spend_tokens_per_day": True},
    {"password": "hunter2"},                                                 # unknown top-level key
])
def test_invalid_profiles_rejected(w, patch):
    assert code(w.s.set_room_profile, w.alice, w.room, {**GOOD, **patch}) == 400
    assert w.s._profile(w.room)["allowed_families"] == []                   # nothing was stored


def test_non_dict_body_rejected(w):
    assert code(w.s.set_room_profile, w.alice, w.room, ["x"]) == 400


def test_profile_survives_hydrate(w):
    w.s.set_room_profile(w.alice, w.room, GOOD)
    again = CollabService(StaticTokenAuthenticator({u: _digest(t) for u, t in USERS.items()}), now=w.s.clock)
    again.hydrate(w.s._log)
    assert again._profile(w.room) == w.s._profile(w.room)
    assert again._profile(w.room2) == w.s._profile(w.room2)


def test_later_set_replaces_earlier(w):
    w.s.set_room_profile(w.alice, w.room, GOOD)
    out = w.s.set_room_profile(w.alice, w.room, {"allowed_families": ["pi"]})
    assert out["allowed_families"] == ["pi"] and out["services"] == []


def test_events_and_responses_hold_names_only(w):
    w.s.set_room_profile(w.alice, w.room, GOOD)
    evs = [e for e in w.s._log if e["event_type"] == "room_profile_set"]
    assert len(evs) == 1
    prof = evs[0]["payload"]["profile"]
    assert prof["services"][0]["secret_name"] == "SEARCH_KEY" and prof["ssh_secret_names"] == ["bender_ssh"]
    for svc_ in prof["services"]:
        assert set(svc_) == {"name", "upstream_url", "header", "secret_name"}
    blob = json.dumps(evs[0]["payload"])
    assert "hunter2" not in blob
    feed = w.s.read_events(w.alice, w.room, 0, 200)["events"]
    assert all(e["event_type"] != "room_profile_set" for e in feed)


def test_http_routes(w):
    st, out = w.s.dispatch("PUT", ["rooms", w.room, "profile"], {}, GOOD, f"Bearer {USERS['alice']}", None)
    assert st == 200 and out["limits"]["per_hour"] == 10
    st, got = w.s.dispatch("GET", ["rooms", w.room, "profile"], {}, {}, f"Bearer {USERS['carol']}", None)
    assert st == 200 and got == out
    st, _ = w.s.dispatch("PUT", ["rooms", w.room, "profile"], {}, GOOD, f"Bearer {USERS['bob']}", None)
    assert st == 404
    st, _ = w.s.dispatch("PUT", ["rooms", w.room, "profile"], {}, {"limits": {"depth": 9}},
                         f"Bearer {USERS['alice']}", None)
    assert st == 400

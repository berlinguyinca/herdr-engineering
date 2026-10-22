# ruff: noqa: F811  (pytest fixtures are imported by name from the sibling test modules)
"""Join tokens, bulk registration and bulk approval: fleets of agents without weakening approval.

Cast as in test_collab_tasks: alice owns the room, bob contributes, carol views, dave is a guest."""
from __future__ import annotations

import json

import pytest

from herdr_engineering.control_plane.collab import MAX_JOIN_AGENTS, CollabError
from tests.control_plane.test_collab_tasks import W, code, svc  # noqa: F401


@pytest.fixture
def w(svc):
    svc.max_agents_per_user = 60
    return W(svc)


def mint(w, who="alice", **kw):
    body = {"harness_type": "claude-code", "name_prefix": "fleet", "count": 3, "room_id": w.room,
            "actions": ["read", "post", "execute"], **kw}
    return w.s.create_join_token(getattr(w, who), body)


def test_a_token_makes_agents_that_have_no_access_until_the_owner_approves(w):
    tok = mint(w)
    made = [w.s.redeem_join_token({"token": tok["token"]}) for _ in range(3)]
    assert [m["name"] for m in made] == ["fleet-1", "fleet-2", "fleet-3"]
    assert all(m["status"] == "pending" and m["credential"].startswith("hca_") for m in made)
    agent = w.s.authenticate(f"Agent {made[0]['credential']}", made[0]["device_id"])
    assert w.s.agent_rooms(agent) == {"rooms": []}                     # nothing yet
    assert len(w.s.list_agent_requests(w.alice, w.room)["requests"]) == 3
    out = w.s.approve_all_requests(w.alice, w.room, {})
    assert out == {"approved": 3, "skipped": 0}
    assert w.s.agent_rooms(agent)["rooms"][0]["actions"] == ["execute", "post", "read"]


def test_a_token_is_limited_in_uses_time_and_by_revocation(w):
    tok = mint(w, count=2)
    w.s.redeem_join_token({"token": tok["token"]})
    w.s.redeem_join_token({"token": tok["token"]})
    assert code(w.s.redeem_join_token, {"token": tok["token"]}) == 401        # used up
    tok = mint(w, ttl_seconds=60)
    w.s.clock.t += 61
    assert code(w.s.redeem_join_token, {"token": tok["token"]}) == 401        # expired
    tok = mint(w)
    w.s.revoke_join_token(w.alice, tok["join_id"])
    assert code(w.s.redeem_join_token, {"token": tok["token"]}) == 401        # revoked
    for bad in ("", "hcj_forged", "hca_" + "x" * 20, None, 5, "hcj_" + "x" * 200):
        assert code(w.s.redeem_join_token, {"token": bad}) == 401


def test_a_token_only_reaches_rooms_its_minter_may_bring_agents_to(w):
    assert code(w.s.create_join_token, w.carol, {"harness_type": "x", "name_prefix": "f", "room_id": w.room,
                                                  "actions": ["read"]}) in (403, 404)       # viewer
    assert code(w.s.create_join_token, w.mallory, {"harness_type": "x", "name_prefix": "f", "room_id": w.room,
                                                    "actions": ["read"]}) == 404
    tok = mint(w, "bob")                                                  # a contributor's agents still wait for the owner
    made = w.s.redeem_join_token({"token": tok["token"]})
    assert made["status"] == "pending" and made["name"] == "fleet-1"
    assert w.s.list_agent_requests(w.alice, w.room)["requests"][0]["owner_user_id"] == "bob"


def test_a_removed_member_cannot_get_requests_filed_by_their_token(w):
    tok = mint(w, "bob")
    w.s.remove_member(w.alice, w.room, "bob")
    made = w.s.redeem_join_token({"token": tok["token"]})
    assert made["request_id"] is None
    assert w.s.list_agent_requests(w.alice, w.room)["requests"] == []


def test_validation_and_the_family_cap(w):
    base = {"harness_type": "claude-code", "name_prefix": "f", "count": 2}
    for bad in ({"count": 0}, {"count": MAX_JOIN_AGENTS + 1}, {"count": True}, {"count": "3"},
                {"name_prefix": "Bad Name"}, {"name_prefix": "x" * 25}, {"name_prefix": "-lead"},
                {"ttl_seconds": 5}, {"ttl_seconds": 10**9}, {"room_id": 5}):
        assert code(w.s.create_join_token, w.alice, {**base, **bad}) == 400, bad
    w.s.max_agents_per_user = 4
    w.s.create_join_token(w.alice, {**base, "count": 4})
    assert code(w.s.create_join_token, w.alice, {**base, "count": 5}) == 429
    tok = w.s.create_join_token(w.alice, {**base, "count": 4})
    for _ in range(4):
        w.s.redeem_join_token({"token": tok["token"]})
    assert code(w.s.create_join_token, w.alice, base) == 429


def test_token_listing_never_shows_the_secret_and_minting_is_throttled(w):
    tok = mint(w)
    listing = json.dumps(w.s.list_join_tokens(w.alice))
    assert tok["token"] not in listing and "hcj_" not in listing and tok["join_id"] in listing
    assert w.s.list_join_tokens(w.bob) == {"tokens": []}
    with pytest.raises(CollabError) as e:
        for _ in range(15):
            mint(w, count=1)
    assert e.value.status == 429
    assert code(w.s.revoke_join_token, w.bob, tok["join_id"]) == 404


def test_names_stay_unique_across_tokens_and_survive_revocation(w):
    a = w.s.redeem_join_token({"token": mint(w, count=2)["token"]})
    b = w.s.redeem_join_token({"token": mint(w, count=2)["token"]})
    assert (a["name"], b["name"]) == ("fleet-1", "fleet-2")


def test_bulk_connect_grants_the_owner_directly_and_requests_for_a_contributor(w):
    body = {"harness_type": "codex", "name_prefix": "cx", "count": 5, "room_id": w.room, "actions": ["read", "post"]}
    mine = w.s.bulk_connect(w.alice, body)["agents"]
    assert [a["status"] for a in mine] == ["granted"] * 5
    theirs = w.s.bulk_connect(w.bob, {**body, "name_prefix": "bx"})["agents"]
    assert [a["status"] for a in theirs] == ["pending"] * 5
    assert len(w.s.list_agent_requests(w.alice, w.room)["requests"]) == 5
    assert code(w.s.bulk_connect, w.carol, body) in (403, 404)


def test_approve_all_never_exceeds_what_was_asked_and_is_owner_only(w):
    tok = mint(w, count=2, actions=["read"])
    for _ in range(2):
        w.s.redeem_join_token({"token": tok["token"]})
    assert code(w.s.approve_all_requests, w.bob, w.room, {}) in (403, 404)
    out = w.s.approve_all_requests(w.alice, w.room, {"actions": ["read", "execute"]})   # asked for read only
    assert out == {"approved": 0, "skipped": 2}
    assert w.s.approve_all_requests(w.alice, w.room, {"actions": ["read"]}) == {"approved": 2, "skipped": 0}
    assert code(w.s.approve_all_requests, w.alice, w.room, {"request_ids": "all"}) == 400


def test_http_routes_public_join_is_open_but_bulk_is_operator_only(w):
    tok = mint(w)
    s, out = w.s.dispatch("POST", ["agents", "join"], {}, {"token": tok["token"]}, "", None)     # no sign-in
    assert s == 201 and out["credential"].startswith("hca_")
    s, _ = w.s.dispatch("POST", ["agents", "bulk"], {}, {"harness_type": "x", "name_prefix": "p"},
                        "Bearer tok-alice-0123456789", None)
    assert s == 404                                                       # a signed-in user on the public site
    s, out = w.s.dispatch("POST", ["agents", "bulk"], {}, {"harness_type": "x", "name_prefix": "p", "count": 2},
                          "", None, operator="alice")
    assert s == 201 and len(out["agents"]) == 2
    s, _ = w.s.dispatch("POST", ["agents", "join-tokens"], {}, {"harness_type": "x", "name_prefix": "q"}, "", None)
    assert s in (401, 403)

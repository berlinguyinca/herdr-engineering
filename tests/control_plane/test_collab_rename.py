# ruff: noqa: F811  (pytest fixtures are imported by name from the sibling test modules)
"""Renaming an agent: who may, what must stay unique, and what the herdr runner can read."""
from __future__ import annotations

import pytest

from herdr_engineering.control_plane.collab import CollabService
from tests.control_plane.test_collab_tasks import (  # noqa: F401
    USERS,
    StaticTokenAuthenticator,
    W,
    _digest,
    code,
    svc,
    user,
)


@pytest.fixture
def w(svc):
    svc.max_agents_per_user = 60
    return W(svc)


def names(w, who="alice"):
    return {a["agent_id"]: a["name"] for a in w.s.list_room_agents(getattr(w, who), w.room)["agents"]}


def test_the_owner_renames_an_agent_and_the_room_sees_it(w):
    a1, i1 = w.agent("alice", name="lead")
    out = w.s.rename_agent(w.alice, i1["agent_id"], {"name": "architect"})
    assert out == {"agent_id": i1["agent_id"], "name": "architect", "changed": True}
    assert names(w)[i1["agent_id"]] == "architect" and names(w, "carol")[i1["agent_id"]] == "architect"
    assert w.s.list_my_agents(w.alice)["agents"][0]["name"] == "architect"
    feed = [e for e in w.s.read_events(w.carol, w.room, 0, 200)["events"] if e["event_type"] == "agent_renamed"]
    assert feed and feed[-1]["name"] == "architect" and feed[-1]["old_name"] == "lead"
    assert w.s.rename_agent(w.alice, i1["agent_id"], {"name": "architect"})["changed"] is False     # no-op, no event


def test_the_old_name_is_free_again_and_new_names_must_be_valid(w):
    a1, i1 = w.agent("alice", name="lead")
    w.s.rename_agent(w.alice, i1["agent_id"], {"name": "architect"})
    a2, i2 = w.agent("bob", name="lead")                                    # the freed handle
    assert names(w)[i2["agent_id"]] == "lead"
    for bad in ("Has Space", "UPPER", "-lead", "x" * 33, "", 5, None, "a/b", "a\nb"):
        assert code(w.s.rename_agent, w.alice, i1["agent_id"], {"name": bad}) == 400, bad


def test_two_agents_in_one_room_never_share_a_name(w):
    a1, i1 = w.agent("alice", name="lead")
    a2, i2 = w.agent("bob", name="coder")
    assert code(w.s.rename_agent, w.alice, i1["agent_id"], {"name": "coder"}) == 409
    assert names(w)[i1["agent_id"]] == "lead"
    mine = w.s.connect_agent(w.alice, {"name": "scratch"})                    # in no room, but still alice's
    assert code(w.s.rename_agent, w.alice, mine["agent_id"], {"name": "lead"}) == 409


def test_only_the_owner_or_the_operator_may_rename_and_never_an_agent(w):
    a1, i1 = w.agent("alice", name="lead")
    assert code(w.s.rename_agent, w.bob, i1["agent_id"], {"name": "x1"}) == 404        # someone else's agent
    assert code(w.s.rename_agent, w.mallory, i1["agent_id"], {"name": "x1"}) == 404
    assert code(w.s.rename_agent, a1, i1["agent_id"], {"name": "x1"}) == 403           # an agent cannot
    w.alice.admin = False
    op = user(w.s, "carol")
    op.admin = True                                                                   # the dashboard operator
    assert w.s.rename_agent(op, i1["agent_id"], {"name": "x1", "source": "herdr"})["changed"] is True
    ev = [e for e in w.s.read_events(w.alice, w.room, 0, 200)["events"] if e["event_type"] == "agent_renamed"][-1]
    assert ev["source"] == "herdr"
    w.s.rename_agent(w.alice, i1["agent_id"], {"name": "x2", "source": "herdr"})       # a person cannot claim "herdr"
    ev = [e for e in w.s.read_events(w.alice, w.room, 0, 200)["events"] if e["event_type"] == "agent_renamed"][-1]
    assert ev["source"] == "web"


def test_a_revoked_agent_cannot_be_renamed(w):
    a1, i1 = w.agent("alice", name="lead")
    w.s.revoke_agent(w.alice, i1["agent_id"])
    assert code(w.s.rename_agent, w.alice, i1["agent_id"], {"name": "x1"}) == 404


def test_rename_is_throttled(w):
    a1, i1 = w.agent("alice", name="lead")
    with pytest.raises(Exception) as exc:
        for i in range(130):
            w.s.rename_agent(w.alice, i1["agent_id"], {"name": f"n{i}"})
    assert getattr(exc.value, "status", None) == 429 or "rate limit" in str(exc.value)


def test_a_rename_survives_a_restart_and_links_list_current_names(w):
    a1, i1 = w.agent("alice", name="lead")
    w.s.link_agent(w.alice, i1["agent_id"], {"instance_id": "pane:bender:w1:p1"})
    w.s.rename_agent(w.alice, i1["agent_id"], {"name": "architect"})
    assert w.s.agent_links(w.alice)["agents"] == [{"agent_id": i1["agent_id"], "name": "architect",
                                                    "harness_type": "claude-code", "instance_id": "pane:bender:w1:p1", "parent_agent_id": None}]
    again = CollabService(StaticTokenAuthenticator({u: _digest(t) for u, t in USERS.items()}), now=w.s.clock)
    again.hydrate(w.s._log)
    assert again.agent_links(user(again, "alice"))["agents"][0]["name"] == "architect"

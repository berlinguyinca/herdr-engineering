# ruff: noqa: F811  (pytest fixtures are imported by name from the sibling test modules)
"""P05: a review task must be independent of the work it reviews (enforced where tasks are created)."""
from __future__ import annotations

import pytest

from tests.control_plane.test_collab import BASE
from tests.control_plane.test_collab_public import H, connect, main_api, stack  # noqa: F401


@pytest.fixture
def team(stack):
    api = stack.main
    s, p = api.call("POST", f"{BASE}/projects", {"name": "p"}, user="alice", headers=H)
    s, r = api.call("POST", f"{BASE}/projects/{p['project_id']}/rooms", {"name": "r"}, user="alice", headers=H)
    room = r["room_id"]
    agents = []
    for _ in range(3):
        a = connect(api, "alice")
        s, _ = api.call("POST", f"{BASE}/rooms/{room}/agents",
                        {"agent_id": a["agent_id"], "actions": ["read", "post", "execute", "instruct"]},
                        user="alice", headers=H)
        assert s == 201
        agents.append(a)
    return type("T", (), dict(api=api, room=room, impl=agents[0], rev=agents[1], other=agents[2]))


def assign(team, agent, text, **extra):
    return team.api.call("POST", f"{BASE}/rooms/{team.room}/tasks",
                         {"target_agent_id": agent["agent_id"], "text": text, **extra}, user="alice", headers=H)


def test_a_review_by_a_different_agent_is_created_and_remembers_what_it_reviews(team):
    s, work = assign(team, team.impl, "build it")
    assert s == 201
    s, rev = assign(team, team.rev, "review it", reviews_task_id=work["task_id"])
    assert s == 201, rev
    s, state = team.api.call("GET", f"{BASE}/rooms/{team.room}/state", user="alice", headers=H)
    by_id = {t["task_id"]: t for t in state["tasks"]}
    assert by_id[rev["task_id"]]["status"] == "pending" and work["task_id"] in by_id


def test_the_agent_that_did_the_work_cannot_be_its_reviewer(team):
    s, work = assign(team, team.impl, "build it")
    s, out = assign(team, team.impl, "review my own work", reviews_task_id=work["task_id"])
    assert s == 400 and "did the work" in out["error"]
    s, ok = assign(team, team.rev, "independent review", reviews_task_id=work["task_id"])     # positive control
    assert s == 201


def test_a_review_must_name_a_task_in_this_room(team):
    s, out = assign(team, team.rev, "review", reviews_task_id="no-such-task")
    assert s == 404
    s, out = assign(team, team.rev, "review", reviews_task_id=["not", "a", "string"])
    assert s == 404


def test_tasks_without_the_field_behave_exactly_as_before(team):
    s, a = assign(team, team.impl, "plain", idempotency_key="k-plain")
    s, again = assign(team, team.impl, "plain", idempotency_key="k-plain")
    assert s == 201 or s == 200
    assert again["task_id"] == a["task_id"] and again.get("replayed") is True
    s, clash = assign(team, team.impl, "plain but different", idempotency_key="k-plain")
    assert s == 409


def test_the_same_key_for_a_review_and_a_plain_task_is_a_different_request(team):
    s, work = assign(team, team.impl, "build it")
    s, rev = assign(team, team.rev, "check", idempotency_key="k-rev", reviews_task_id=work["task_id"])
    assert s == 201
    s, replay = assign(team, team.rev, "check", idempotency_key="k-rev", reviews_task_id=work["task_id"])
    assert replay["task_id"] == rev["task_id"]
    s, clash = assign(team, team.rev, "check", idempotency_key="k-rev")                       # no reviews: different task
    assert s == 409


def as_agent(team, agent, method, path, body=None):
    return team.api.call(method, path, body, agent=agent, headers=H)


def test_an_agent_cannot_choose_the_reviewer_of_its_own_work(team):
    s, work = assign(team, team.impl, "build it")
    s, _ = as_agent(team, team.impl, "POST", f"{BASE}/tasks/{work['task_id']}/update", {"status": "accepted"})
    assert s == 200
    body = {"target_agent_id": team.other["agent_id"], "text": "review me please", "parent_task_id": work["task_id"]}
    s, plain = as_agent(team, team.impl, "POST", f"{BASE}/rooms/{team.room}/tasks", body)
    assert s == 201, plain                                                           # positive control: delegating is fine
    s, out = as_agent(team, team.impl, "POST", f"{BASE}/rooms/{team.room}/tasks",
                      {**body, "text": "rubber-stamp my work", "reviews_task_id": work["task_id"]})
    assert s == 400 and "choose the reviewer of its own work" in out["error"]


def test_a_reviewer_cannot_be_the_agent_that_handed_the_work_out(team):
    s, root = assign(team, team.impl, "lead the work")
    as_agent(team, team.impl, "POST", f"{BASE}/tasks/{root['task_id']}/update", {"status": "accepted"})
    s, sub = as_agent(team, team.impl, "POST", f"{BASE}/rooms/{team.room}/tasks",
                      {"target_agent_id": team.other["agent_id"], "text": "do the part", "parent_task_id": root["task_id"]})
    assert s == 201
    s, out = assign(team, team.impl, "review the part", reviews_task_id=sub["task_id"])      # by alice, handed out by impl
    assert s == 400 and "handed out the work" in out["error"]
    s, ok = assign(team, team.rev, "independent review of the part", reviews_task_id=sub["task_id"])
    assert s == 201

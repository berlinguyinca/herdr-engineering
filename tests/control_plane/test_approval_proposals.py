"""Task-scoped approval proposals are durable requests, never authority grants."""
from __future__ import annotations

import threading

import pytest

from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.collab import CollabService, StaticTokenAuthenticator
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from tests.control_plane.test_collab import (
    BASE,
    TOKENS,
    Api,
    Clock,
    _digest,
    enroll,
    grant,
    invite,
    make_room,
)


@pytest.fixture
def env(acknowledged_sink):
    clock = Clock()
    repo = ControlPlaneRepo(sink=acknowledged_sink)
    auth = StaticTokenAuthenticator({user: _digest(token) for user, token in TOKENS.items()})
    service = CollabService(auth, sink=acknowledged_sink, now=clock,
                            stream_poll_seconds=0.05)
    server = ControlPlaneServer(repo, host="127.0.0.1", port=0, collab=service,
                                api_keys={"op-key": "operator", "view-key": "viewer"})
    thread = threading.Thread(target=server.serve, daemon=True)
    thread.start()
    server.wait_until_ready()
    yield type("Env", (), {"api": Api(server.port), "svc": service, "repo": repo,
                           "srv": server, "sink": acknowledged_sink, "clock": clock})
    server.shutdown()
    thread.join(timeout=5)


def _task(env):
    _, room = make_room(env.api)
    agent = enroll(env.api, "alice")
    assert grant(env.api, room, agent, actions=("read", "execute"))[0] == 201
    status, task = env.api.call(
        "POST",
        f"{BASE}/rooms/{room}/tasks",
        {"target_agent_id": agent["agent_id"], "text": "prepare venue email"},
        user="alice",
    )
    assert status == 201
    return room, agent, task["task_id"]


def _body(task_id, key="proposal-1", **changes):
    body = {
        "task_id": task_id,
        "kind": "email.send",
        "summary": "Send the approved venue inquiry",
        "details": {"recipient_count": 2, "followups": 1},
        "idempotency_key": key,
    }
    body.update(changes)
    return body


def test_assigned_agent_creates_pending_proposal_and_owner_lists_it(env):
    room, agent, task_id = _task(env)
    status, made = env.api.call(
        "POST", f"{BASE}/rooms/{room}/approval-proposals", _body(task_id), agent=agent
    )
    assert status == 201
    assert made["proposal"]["state"] == "pending"
    assert made["proposal"]["task_id"] == task_id
    assert made["proposal"]["requester"] == {
        "type": "agent", "id": agent["agent_id"], "owner_user_id": "alice"
    }
    assert "granted" not in made["proposal"] and "approved" not in made["proposal"]

    status, listed = env.api.call(
        "GET", f"{BASE}/rooms/{room}/approval-proposals", user="alice"
    )
    assert status == 200 and listed["proposals"] == [made["proposal"]]


def test_proposal_requires_task_relationship_and_never_trusts_authority_fields(env):
    room, agent, task_id = _task(env)
    other = enroll(env.api, "alice", device="dev-2")
    assert grant(env.api, room, other, actions=("read", "execute"))[0] == 201

    assert env.api.call(
        "POST", f"{BASE}/rooms/{room}/approval-proposals", _body(task_id), agent=other
    )[0] == 404
    assert env.api.call(
        "POST", f"{BASE}/rooms/{room}/approval-proposals",
        _body(task_id, approved=True), agent=agent,
    )[0] == 400
    assert env.api.call(
        "POST", f"{BASE}/rooms/{room}/approval-proposals",
        _body(task_id, authority={"send": True}), agent=agent,
    )[0] == 400


def test_idempotency_replays_same_request_and_conflicts_on_changed_payload(env):
    room, agent, task_id = _task(env)
    path = f"{BASE}/rooms/{room}/approval-proposals"
    status, first = env.api.call("POST", path, _body(task_id), agent=agent)
    assert status == 201
    status, replay = env.api.call("POST", path, _body(task_id), agent=agent)
    assert status == 200 and replay["replayed"] is True
    assert replay["proposal"] == first["proposal"]
    assert env.api.call(
        "POST", path, _body(task_id, summary="Different request"), agent=agent
    )[0] == 409


def test_visibility_is_owner_scoped_and_room_membership_is_required(env):
    room, agent, task_id = _task(env)
    invite(env.api, room, "bob", role="contributor")
    env.api.call("POST", f"{BASE}/rooms/{room}/approval-proposals", _body(task_id), agent=agent)

    status, bob = env.api.call("GET", f"{BASE}/rooms/{room}/approval-proposals", user="bob")
    assert status == 200 and bob["proposals"] == []
    assert env.api.call(
        "GET", f"{BASE}/rooms/{room}/approval-proposals", user="mallory"
    )[0] == 404


def test_room_owner_cannot_read_another_users_proposal_or_details_from_events(env):
    _, room = make_room(env.api)
    invite(env.api, room, "bob", role="contributor")
    agent = enroll(env.api, "bob", device="bob-device")
    assert grant(env.api, room, agent, actions=("read", "execute"), owner="bob",
                 approver="alice")[0] == 201
    status, task = env.api.call(
        "POST", f"{BASE}/rooms/{room}/tasks",
        {"target_agent_id": agent["agent_id"], "text": "prepare private request"},
        user="alice",
    )
    assert status == 201
    status, before = env.api.call("GET", f"{BASE}/rooms/{room}/state", user="alice")
    assert status == 200
    details = {"private_note": "bob-only-detail"}
    status, _ = env.api.call(
        "POST", f"{BASE}/rooms/{room}/approval-proposals",
        _body(task["task_id"], details=details), agent=agent,
    )
    assert status == 201
    assert env.api.call("GET", f"{BASE}/rooms/{room}/approval-proposals", user="alice")[1] == {
        "proposals": []
    }
    status, feed = env.api.call("GET", f"{BASE}/rooms/{room}/events", user="alice")
    assert status == 200 and "bob-only-detail" not in str(feed)
    status, state = env.api.call("GET", f"{BASE}/rooms/{room}/state", user="alice")
    assert status == 200 and "bob-only-detail" not in str(state)
    status, unchanged = env.api.call(
        "GET", f"{BASE}/rooms/{room}/state?since={before['seq']}", user="alice"
    )
    assert status == 200 and unchanged == {"seq": before["seq"], "changed": False}
    status, own = env.api.call("GET", f"{BASE}/rooms/{room}/approval-proposals", user="bob")
    assert status == 200 and own["proposals"][0]["details"] == details


def test_revoked_agent_grant_blocks_list_and_idempotent_replay(env):
    room, agent, task_id = _task(env)
    path = f"{BASE}/rooms/{room}/approval-proposals"
    assert env.api.call("POST", path, _body(task_id), agent=agent)[0] == 201
    assert env.api.call("GET", path, agent=agent)[0] == 200
    assert env.api.call(
        "POST", f"{BASE}/rooms/{room}/agents/{agent['agent_id']}/remove", {}, user="alice"
    )[0] == 200
    assert env.api.call("GET", path, agent=agent)[0] == 404
    assert env.api.call("POST", path, _body(task_id), agent=agent)[0] == 404


def test_read_only_agent_cannot_list_prior_execution_proposals(env):
    room, agent, task_id = _task(env)
    path = f"{BASE}/rooms/{room}/approval-proposals"
    assert env.api.call("POST", path, _body(task_id), agent=agent)[0] == 201
    assert grant(env.api, room, agent, actions=("read",))[0] == 201
    assert env.api.call("GET", path, agent=agent)[0] == 404
    assert env.api.call("GET", path, user="alice")[1] == {"proposals": []}
    assert env.api.call("POST", path, _body(task_id, key="owner-after-narrow"), user="alice")[0] == 404


def test_late_execute_regrant_does_not_revive_task_for_proposals(env):
    room, agent, task_id = _task(env)
    path = f"{BASE}/rooms/{room}/approval-proposals"
    assert env.api.call("POST", path, _body(task_id), agent=agent)[0] == 201
    assert env.api.call(
        "POST", f"{BASE}/rooms/{room}/agents/{agent['agent_id']}/remove", {}, user="alice"
    )[0] == 200
    assert grant(env.api, room, agent, actions=("read", "execute"))[0] == 201
    assert env.api.call("GET", path, user="alice")[1] == {"proposals": []}
    assert env.api.call("GET", path, agent=agent)[0] == 200
    assert env.api.call("POST", path, _body(task_id), agent=agent)[0] == 404
    assert env.api.call("POST", path, _body(task_id, key="owner-regrant"), user="alice")[0] == 404


def test_human_task_starter_and_room_owner_can_request_but_unrelated_member_cannot(env):
    room, agent, task_id = _task(env)
    invite(env.api, room, "bob", role="contributor")
    path = f"{BASE}/rooms/{room}/approval-proposals"
    assert env.api.call("POST", path, _body(task_id, key="owner"), user="alice")[0] == 201
    assert env.api.call("POST", path, _body(task_id, key="unrelated"), user="bob")[0] == 404


def test_proposals_survive_hydration_with_idempotency(env):
    room, agent, task_id = _task(env)
    path = f"{BASE}/rooms/{room}/approval-proposals"
    status, made = env.api.call("POST", path, _body(task_id), agent=agent)
    assert status == 201

    auth = StaticTokenAuthenticator({u: _digest(t) for u, t in TOKENS.items()})
    restored = CollabService(auth, now=env.clock)
    restored.hydrate(list(env.svc._log))
    status, listed = restored.dispatch(
        "GET", ["rooms", room, "approval-proposals"], {}, {},
        f"Bearer {TOKENS['alice']}", None,
    )
    assert status == 200 and listed["proposals"][0]["proposal_id"] == made["proposal"]["proposal_id"]
    status, replay = restored.dispatch(
        "POST", ["rooms", room, "approval-proposals"], {}, _body(task_id),
        f"Agent {agent['credential']}", agent["device_id"],
    )
    assert status == 200 and replay["replayed"] is True


def test_validation_is_bounded_and_errors_do_not_echo_details(env):
    room, agent, task_id = _task(env)
    path = f"{BASE}/rooms/{room}/approval-proposals"
    secret = "never-echo-this-secret"
    bad_cases = [
        _body(task_id, idempotency_key=None),
        _body(task_id, kind=""),
        _body(task_id, summary="x" * 501),
        _body(task_id, details={"note": secret * 1000}),
        _body(task_id, details=["not", "an", "object"]),
    ]
    for body in bad_cases:
        status, out = env.api.call("POST", path, body, agent=agent)
        assert status == 400
        assert secret not in str(out)


def test_pending_proposal_count_is_bounded_per_user_and_room(env, monkeypatch):
    from herdr_engineering.control_plane import collab

    monkeypatch.setattr(collab, "MAX_APPROVAL_PROPOSALS_PER_OWNER", 1)
    room, agent, task_id = _task(env)
    path = f"{BASE}/rooms/{room}/approval-proposals"
    assert env.api.call("POST", path, _body(task_id), agent=agent)[0] == 201
    status, out = env.api.call("POST", path, _body(task_id, key="proposal-2"), agent=agent)
    assert status == 429 and "limit" in out["error"]


def test_task_can_be_read_directly_with_room_scope(env):
    room, agent, task_id = _task(env)
    status, out = env.api.call("GET", f"{BASE}/rooms/{room}/tasks/{task_id}", user="alice")
    assert status == 200 and out["task"]["task_id"] == task_id
    assert out["task"]["target_agent_id"] == agent["agent_id"]

    _, other_room = make_room(env.api, owner="bob")
    assert env.api.call(
        "GET", f"{BASE}/rooms/{other_room}/tasks/{task_id}", user="bob"
    )[0] == 404
    assert env.api.call(
        "GET", f"{BASE}/rooms/{room}/tasks/{task_id}", user="mallory"
    )[0] == 404

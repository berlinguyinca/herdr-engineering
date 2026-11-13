# ruff: noqa: F811  (pytest fixtures are imported by name from the sibling test modules)
"""Rakazo rooms when collaboration is a separate service: HttpCollab talks to its real operator listener (no mocks).

A real CollabService is served by a collab-only ControlPlaneServer with an operator listener on an ephemeral port; the
dashboard-side server has no in-process collab and only ``collab_operator_url``, as in production."""
import socket
import threading

import pytest

from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.assistant import AssistantService, assistant_config
from herdr_engineering.control_plane.collab import CollabError, Principal
from herdr_engineering.control_plane.collab_http import HttpCollab
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from tests.control_plane.test_collab_tasks import W, svc  # noqa: F401
from tests.control_plane.test_rakazo_bridge import ACCOUNTS, PW_A, PW_B, Api, bridge_section
from tests.control_plane.test_rakazo_spaces import ok, rpc


def _start(**kw):
    server = ControlPlaneServer(ControlPlaneRepo(), **kw)
    threading.Thread(target=server.serve, daemon=True).start()
    server.wait_until_ready()
    return server


@pytest.fixture()
def operator(svc):
    """The collab service as its own process would run it: alice is the operator."""
    server = _start(collab=svc, collab_only=True, public_port=0, public_host="127.0.0.1", operator_port=0,
                    operator_user="alice", operator_hosts=["placeholder"])
    server.operator_hosts = frozenset({f"127.0.0.1:{server.operator_port}"})      # the port is known only now
    yield server
    server.shutdown()


@pytest.fixture()
def remote(svc, operator):
    w = W(svc)
    w.http = HttpCollab(f"http://127.0.0.1:{operator.operator_port}")
    w.operator = operator
    return w


@pytest.fixture()
def world(svc, operator):
    """The dashboard process: no collab object, only the operator url. alice@ is linked to collab user alice, bob@ to bob."""
    w = W(svc)
    accounts = [{**ACCOUNTS[0], "collab_user": "alice"}, {**ACCOUNTS[1], "collab_user": "bob"}]
    server = _start(collab_operator_url=f"http://127.0.0.1:{operator.operator_port}",
                    rakazo_bridge=bridge_section(accounts=accounts),
                    assistant_service=AssistantService(config=assistant_config({"personal_assistant": {"enabled": True}})))
    w.api, w.server = Api(server.port), server
    w.alice_t = w.api.login("alice@example.com", PW_A)
    w.bob_t = w.api.login("bob@example.com", PW_B)
    yield w
    server.shutdown()


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ------------------------------------------------------------------------------------------------ through the bridge
def test_groups_create_and_list_through_the_http_collab(world):
    a1 = world.s.connect_agent(world.alice, {"name": "atlas"})["agent_id"]
    a2 = world.s.connect_agent(world.alice, {"name": "boreas"})["agent_id"]
    created = ok(world.api, "groups/create", {"name": "tui-smoke", "botIds": [a1, a2]}, world.alice_t)
    groups = ok(world.api, "groups/list", {}, world.alice_t)
    room = next(g for g in groups if g["name"] == "tui-smoke")
    assert room["id"] == created["id"]
    assert {a1, a2} <= {m["botId"] for m in room["members"]}
    # the room exists in the real service with both agents granted
    assert {a["agent_id"] for a in world.s.list_room_agents(world.alice, room["id"])["agents"]} == {a1, a2}


def test_post_then_read_through_the_http_collab(world):
    sent = ok(world.api, "threads/send", {"groupId": world.room, "text": "hello <b>team</b>", "clientNonce": "n1"}, world.alice_t)
    again = ok(world.api, "threads/send", {"groupId": world.room, "text": "hello <b>team</b>", "clientNonce": "n1"}, world.alice_t)
    assert again["seq"] == sent["seq"]
    snap = ok(world.api, "threads/get", {"groupId": world.room}, world.alice_t)
    assert [m["blocks"][0]["text"] for m in snap["messages"] if m["role"] == "user"] == ["hello <b>team</b>"]
    assert any(e.get("text") == "hello <b>team</b>" for e in world.s.read_events(world.alice, world.room, 0, 50)["events"])


def test_a_room_that_is_not_the_callers_is_a_404_through_the_bridge(world):
    status = rpc(world.api, "groups/get", {"groupId": "room_nope"}, world.alice_t)[0]
    assert status == 404


def test_only_the_operators_account_is_linked_and_any_other_collab_user_is_refused(world):
    ok(world.api, "groups/list", {}, world.alice_t)                                # learns the operator
    assert ok(world.api, "groups/list", {}, world.bob_t) == []                     # bob@ is linked to collab user bob: not served
    status, body, _ = rpc(world.api, "groups/create", {"name": "x", "botIds": ["a", "b"]}, world.bob_t)
    assert status == 400 and "not linked" in str(body)
    assert ok(world.api, "messaging/status", {}, world.bob_t)["enabled"] is False


def test_a_collab_service_that_is_down_is_a_clear_unavailable_answer(world, operator):
    operator.shutdown()
    status, body, _ = rpc(world.api, "groups/list", {}, world.alice_t)
    assert status == 503 and "unavailable" in str(body)


# ------------------------------------------------------------------------------------------------ HttpCollab itself
def test_the_client_refuses_a_principal_that_is_not_the_operator(remote):
    assert remote.http.still_valid(remote.alice) is True
    assert remote.http.still_valid(remote.bob) is False
    with pytest.raises(CollabError) as e:
        remote.http.list_rooms(remote.bob)
    assert e.value.status == 403
    with pytest.raises(CollabError):
        remote.http.post_message(Principal("user", "bob", "bob"), remote.room, {"text": "x", "idempotency_key": "k"})


def test_service_down_is_503_and_a_wrong_url_is_never_retried(remote):
    dead = HttpCollab(f"http://127.0.0.1:{_free_port()}", timeout=2)
    with pytest.raises(CollabError) as e:
        dead.list_rooms(remote.alice)
    assert e.value.status == 503
    assert dead.acts_for("anyone") is True            # operator unknown: not linked away, every call still refuses


def test_unknown_room_keeps_the_services_404(remote):
    with pytest.raises(CollabError) as e:
        remote.http.read_events(remote.alice, "room_nope", 0, 10)
    assert e.value.status == 404


def test_read_tail_pages_and_trims(remote, monkeypatch):
    monkeypatch.setattr("herdr_engineering.control_plane.collab_http.PAGE", 7)       # force several pages
    for i in range(25):
        remote.s.post_message(remote.alice, remote.room, {"text": f"m{i}", "idempotency_key": f"k{i}"})
    tail = remote.http.read_tail(remote.alice, remote.room, 5)
    assert tail == remote.s.read_tail(remote.alice, remote.room, 5)
    assert [e["text"] for e in tail["events"]][-1] == "m24"


# ------------------------------------------------------------------------------------------------ contract
def _norm(x):
    """JSON round trip (tuples to lists) so in-process and HTTP shapes compare."""
    import json
    return json.loads(json.dumps(x, sort_keys=True, default=list))


def _skeleton(x):
    """Keys and value types, not values: ids and clocks differ between two services."""
    if isinstance(x, dict):
        return {k: _skeleton(v) for k, v in sorted(x.items())}
    if isinstance(x, list):
        return [_skeleton(x[0])] if x else []
    return type(x).__name__ if x is not None else "NoneType"


def test_http_collab_answers_every_method_in_the_shape_the_in_process_service_does(remote):
    s, h, alice, room = remote.s, remote.http, remote.alice, remote.room
    agent, info = remote.agent("alice", name="atlas")
    s.post_message(alice, room, {"text": "hi", "idempotency_key": "c1"})
    pid = s.list_rooms(alice)["rooms"][0]["project_id"]
    ext = s.connect_agent(alice, {"name": "extra"})["agent_id"]

    def same(name, *args):
        try:
            mine = _norm(getattr(s, name)(alice, *args))
        except CollabError as exc:               # a feature this service has off: HTTP must refuse the same way
            with pytest.raises(CollabError) as e:
                getattr(h, name)(alice, *args)
            assert e.value.status == exc.status, name
            return
        assert _skeleton(mine) == _skeleton(_norm(getattr(h, name)(alice, *args))), name

    same("list_my_agents")
    same("list_rooms")
    same("list_room_agents", room)
    same("read_events", room, 0, 50)
    same("read_tail", room, 5)
    same("room_people", room)
    same("list_steering", room, {})
    same("list_proposals", room)
    assert h.still_valid(alice) is s.still_valid(alice) is True
    # writes: each side does it once to its own fresh object, the answers have the same shape
    assert _skeleton(_norm(h.create_project(alice, {"name": "p2"}))) == _skeleton(_norm(s.create_project(alice, {"name": "p3"})))
    assert _skeleton(_norm(h.create_room(alice, pid, {"name": "via-http"}))) == _skeleton(_norm(s.create_room(alice, pid, {"name": "direct"})))
    got = h.grant_agent(alice, room, {"agent_id": ext, "actions": ["read", "post"]})
    assert _skeleton(_norm(got)) == _skeleton(_norm(s.grant_agent(alice, remote.room2, {"agent_id": ext, "actions": ["read", "post"]})))
    status, out = h.post_message(alice, room, {"text": "via http", "idempotency_key": "c2"})
    assert (status, _skeleton(_norm(out))) == (s.post_message(alice, room, {"text": "direct", "idempotency_key": "c3"})[0],
                                               _skeleton(_norm(s.post_message(alice, room, {"text": "direct", "idempotency_key": "c4"})[1])))
    assert _skeleton(_norm(h.remove_agent_from_room(alice, room, ext))) == _skeleton(_norm(s.remove_agent_from_room(alice, remote.room2, ext)))
    # steering and proposals need a real target / proposal: both refuse with the service's own 404, over HTTP and in process
    for call in (lambda c: c.get_proposal(alice, room, "prop_none"),
                 lambda c: c.decide_proposal(alice, room, "prop_none", {"decision": "accept"}),
                 lambda c: c.confirm_steering(alice, room, "steer_none", {"digest": "d", "expected_fence": 1}),
                 lambda c: c.reject_steering(alice, room, "steer_none", {"reason": "no"}),
                 lambda c: c.revoke_external(alice, room, "agent_none")):
        statuses = []
        for c in (s, h):
            with pytest.raises(CollabError) as e:
                call(c)
            statuses.append(e.value.status)
        assert statuses[0] == statuses[1], statuses

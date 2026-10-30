# ruff: noqa: F811  (pytest fixtures are imported by name from the sibling test modules)
"""Rakazo groups and agent bots over collaboration rooms: the bridge acts as the account's configured collab user, no more."""
import http.client
import json
import threading

import pytest

from herdr_engineering.control_plane import rakazo_rooms as rr
from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.assistant import AssistantService, assistant_config
from herdr_engineering.control_plane.repo import ControlPlaneRepo
from tests.control_plane.test_collab_tasks import W, svc  # noqa: F401
from tests.control_plane.test_rakazo_bridge import (
    ACCOUNTS,
    PW_A,
    PW_B,
    Api,
    bridge_section,
    read_frames,
)
from tests.control_plane.test_rakazo_contract import validate
from tests.control_plane.test_rakazo_spaces import SPACE, ok, rpc


@pytest.fixture()
def world(svc):
    """A real collaboration service with alice's room (bob, carol, dave in it), served by a bridge that links the phone
    accounts alice@ and bob@ to the collab users alice and bob. mallory's account is linked to nobody."""
    w = W(svc)
    accounts = [{**ACCOUNTS[0], "collab_user": "alice"}, {**ACCOUNTS[1], "collab_user": "bob"}]
    server = ControlPlaneServer(ControlPlaneRepo(), collab=svc, rakazo_bridge=bridge_section(accounts=accounts),
                                assistant_service=AssistantService(config=assistant_config(
                                    {"personal_assistant": {"enabled": True}})))
    threading.Thread(target=server.serve, daemon=True).start()
    server.wait_until_ready()
    api = Api(server.port)
    w.api, w.server = api, server
    w.alice_t = api.login("alice@example.com", PW_A)
    w.bob_t = api.login("bob@example.com", PW_B)
    yield w
    server.shutdown()


def test_groups_list_shows_the_rooms_the_collab_user_belongs_to(world):
    agent, info = world.agent("alice", name="atlas")
    groups = ok(world.api, "groups/list", {}, world.alice_t)
    validate("groups/list", groups)
    assert {g["name"] for g in groups} == {"r", "r2"}
    room = next(g for g in groups if g["id"] == world.room)
    assert room["threadId"] == "thr_" + world.room and room["spaceId"] == "herdr"
    names = {m["name"] for m in room["members"]}
    assert "atlas" in names and {"bob", "carol"} <= {n.split("@")[0] for n in names}
    assert info["agent_id"] in {m["botId"] for m in room["members"]}
    # bob is in only the first room, and sees only what his collab user can see
    assert [g["id"] for g in ok(world.api, "groups/list", {}, world.bob_t)] == [world.room]


def test_a_room_the_user_is_not_in_is_not_found_through_every_group_call(world):
    for proc in ("groups/get", "threads/get", "threads/head", "threads/messages"):
        assert rpc(world.api, proc, {"groupId": world.room2}, world.bob_t)[0] == 404, proc
    assert rpc(world.api, "threads/send", {"groupId": world.room2, "text": "hi", "clientNonce": "x"}, world.bob_t)[0] == 404
    assert rpc(world.api, "groups/get", {"groupId": "room_nope"}, world.alice_t)[0] == 404


def test_the_group_thread_reads_and_sends_room_messages(world):
    world.s.post_message(world.bob, world.room, {"text": "morning from bob", "idempotency_key": "b1"})
    snap = ok(world.api, "threads/get", {"groupId": world.room}, world.alice_t)
    validate("threads/get", snap)
    assert snap["groupId"] == world.room and snap["groupName"] == "r" and snap["run"] is None
    [msg] = snap["messages"]
    assert msg["role"] == "bot" and msg["blocks"][0]["text"] == "morning from bob" and msg["botId"].startswith("usr_")
    assert msg["botId"] in {m["botId"] for m in snap["members"]}
    sent = ok(world.api, "threads/send", {"groupId": world.room, "text": "hello team", "clientNonce": "c1"}, world.alice_t)
    validate("threads/send", sent)
    again = ok(world.api, "threads/send", {"groupId": world.room, "text": "hello team", "clientNonce": "c1"}, world.alice_t)
    assert again["seq"] == sent["seq"]                                       # a retry is the same message
    snap = ok(world.api, "threads/get", {"groupId": world.room}, world.alice_t)
    mine = [m for m in snap["messages"] if m["role"] == "user"]
    assert [m["blocks"][0]["text"] for m in mine] == ["hello team"] and mine[0]["seq"] == sent["seq"]
    assert snap["cursor"] >= sent["seq"]
    head = ok(world.api, "threads/head", {"groupId": world.room}, world.alice_t)
    validate("threads/head", head)
    assert head["cursor"] == snap["cursor"]
    page = ok(world.api, "threads/messages", {"groupId": world.room}, world.alice_t)
    validate("threads/messages", page)
    assert [m["seq"] for m in page["messages"]] == sorted(m["seq"] for m in page["messages"])
    # the message is in the room for the others too
    assert any(e.get("text") == "hello team" for e in world.s.read_events(world.bob, world.room, 0, 50)["events"])


def test_validation_and_limits_on_sending_to_a_group(world):
    for bad in ("", "   ", None, 5):
        assert rpc(world.api, "threads/send", {"groupId": world.room, "text": bad, "clientNonce": "n"}, world.alice_t)[0] == 400
    assert rpc(world.api, "threads/send", {"groupId": world.room, "text": "x", "artifactIds": ["a"]}, world.alice_t)[0] == 400
    # a viewer cannot post: the collaboration service's own role check answers
    carol_cannot = world.s.list_rooms(world.carol)["rooms"][0]["role"]
    assert carol_cannot == "viewer"


def test_paging_older_group_messages(world):
    for i in range(60):
        world.s.post_message(world.alice, world.room, {"text": f"m{i}", "idempotency_key": f"k{i}"})
    snap = ok(world.api, "threads/get", {"groupId": world.room}, world.alice_t)
    assert len(snap["messages"]) == rr.MAX_PAGE and snap["olderCursor"] == snap["messages"][0]["seq"]
    older = ok(world.api, "threads/messages", {"groupId": world.room, "before": snap["olderCursor"]}, world.alice_t)
    validate("threads/messages", older)
    assert len(older["messages"]) == 10 and older["messages"][-1]["seq"] < snap["olderCursor"] and older["olderCursor"] is None


def test_agent_bots_are_listed_and_a_message_to_one_goes_to_its_room(world):
    agent, info = world.agent("alice", name="atlas")
    bots = ok(world.api, "bots/list", {}, world.alice_t)
    validate("bots/list", bots)
    [bot] = [b for b in bots if b["id"] == info["agent_id"]]
    assert bot["name"] == "atlas" and bot["spawnKey"] == info["agent_id"] and bot["threadId"] == "thr_" + world.room
    assert bot["computerMode"] == "dedicated"
    validate("bots/get", ok(world.api, "bots/get", {"botId": info["agent_id"]}, world.alice_t))
    ok(world.api, "threads/send", {"botId": info["agent_id"], "text": "status?", "clientNonce": "a1"}, world.alice_t)
    texts = [e["text"] for e in world.s.read_events(world.bob, world.room, 0, 50)["events"] if e.get("text")]
    assert texts and texts[-1].startswith("@") and texts[-1].endswith("status?")
    snap = ok(world.api, "threads/get", {"botId": info["agent_id"]}, world.alice_t)
    assert snap["groupId"] == world.room
    # bob does not see alice's agent as a bot of his own
    assert info["agent_id"] not in {b["id"] for b in ok(world.api, "bots/list", {}, world.bob_t)}
    assert rpc(world.api, "bots/get", {"botId": info["agent_id"]}, world.bob_t)[0] == 404


def test_an_agent_that_is_in_no_room_cannot_be_messaged(world):
    info = world.s.connect_agent(world.alice, {"harness_type": "claude-code", "name": "loner"})
    status, body, _ = rpc(world.api, "threads/send", {"botId": info["agent_id"], "text": "hi", "clientNonce": "z"},
                          world.alice_t)
    assert status == 400 and "not in a group" in body["json"]["message"]


def test_group_create_update_archive_restore_remove(world):
    a1 = world.s.connect_agent(world.alice, {"harness_type": "claude-code", "name": "one"})["agent_id"]
    a2 = world.s.connect_agent(world.alice, {"harness_type": "codex", "name": "two"})["agent_id"]
    a3 = world.s.connect_agent(world.alice, {"harness_type": "pi", "name": "three"})["agent_id"]
    created = ok(world.api, "groups/create", {"name": "Crew", "botIds": [a1, a2]}, world.alice_t)
    validate("groups/create", created)
    assert {m["botId"] for m in created["members"]} == {a1, a2}
    gid = created["id"]
    assert rpc(world.api, "groups/create", {"name": "x", "botIds": [a1]}, world.alice_t)[0] == 400       # too few
    assert rpc(world.api, "groups/create", {"name": "x", "botIds": [a1, "asst_nope"]}, world.alice_t)[0] == 400
    updated = ok(world.api, "groups/update", {"groupId": gid, "name": "Crew 2", "botIds": [a2, a3], "pinned": True},
                 world.alice_t)
    validate("groups/update", updated)
    assert updated["name"] == "Crew 2" and updated["pinned"] is True
    assert {m["botId"] for m in updated["members"]} == {a2, a3}
    stale = rpc(world.api, "groups/update", {"groupId": gid, "botIds": [a1, a2], "expectedBotIds": [a1]}, world.alice_t)
    assert stale[0] == 409
    validate("groups/get", ok(world.api, "groups/get", {"groupId": gid}, world.alice_t))
    out = ok(world.api, "groups/archive", {"groupId": gid}, world.alice_t)
    validate("groups/archive", out)
    assert out == {"ok": True}
    assert gid not in {g["id"] for g in ok(world.api, "groups/list", {}, world.alice_t)}
    archived = ok(world.api, "groups/listArchived", {}, world.alice_t)
    validate("groups/listArchived", archived)
    assert [g["id"] for g in archived] == [gid] and archived[0]["archivedAt"]
    validate("groups/restore", ok(world.api, "groups/restore", {"groupId": gid}, world.alice_t))
    assert gid in {g["id"] for g in ok(world.api, "groups/list", {}, world.alice_t)}
    copy = ok(world.api, "groups/duplicate", {"groupId": gid}, world.alice_t)
    validate("groups/duplicate", copy)
    assert copy["name"] == "Crew 2 copy" and copy["id"] != gid
    removed = ok(world.api, "groups/remove", {"groupId": gid}, world.alice_t)
    validate("groups/remove", removed)
    assert removed == {"ok": True}
    assert rpc(world.api, "groups/get", {"groupId": gid}, world.alice_t)[0] == 404
    # the room itself is untouched for everyone else: removing hides it from this phone only
    assert any(r["room_id"] == gid for r in world.s.list_rooms(world.alice)["rooms"])


def test_only_the_own_agents_can_be_added(world):
    theirs = world.s.connect_agent(world.bob, {"harness_type": "claude-code", "name": "bobs"})["agent_id"]
    mine = world.s.connect_agent(world.alice, {"harness_type": "claude-code", "name": "mine"})["agent_id"]
    other = world.s.connect_agent(world.alice, {"harness_type": "codex", "name": "other"})["agent_id"]
    assert rpc(world.api, "groups/create", {"name": "x", "botIds": [mine, theirs]}, world.alice_t)[0] == 400
    assert rpc(world.api, "groups/create", {"name": "ok", "botIds": [mine, other]}, world.alice_t)[0] == 200


def test_groups_belong_to_the_space_they_were_made_in(world):
    a1 = world.s.connect_agent(world.alice, {"harness_type": "claude-code", "name": "one"})["agent_id"]
    a2 = world.s.connect_agent(world.alice, {"harness_type": "codex", "name": "two"})["agent_id"]
    side = ok(world.api, "spaces/create", {"name": "Side"}, world.alice_t)["id"]
    made = ok(world.api, "groups/create", {"name": "Hidden crew", "botIds": [a1, a2]}, world.alice_t, side)
    assert made["spaceId"] == side
    assert "Hidden crew" not in {g["name"] for g in ok(world.api, "groups/list", {}, world.alice_t)}
    assert [g["name"] for g in ok(world.api, "groups/list", {}, world.alice_t, side)] == ["Hidden crew"]
    assert rpc(world.api, "groups/get", {"groupId": made["id"]}, world.alice_t)[0] == 404           # not in this space
    assert rpc(world.api, "threads/get", {"groupId": made["id"]}, world.alice_t, side)[0] == 200
    listing = ok(world.api, "spaces/list", {}, world.alice_t, side)
    validate("spaces/list", listing)


def test_mark_read_clears_unread(world):
    world.s.post_message(world.bob, world.room, {"text": "ping", "idempotency_key": "p1"})
    group = next(g for g in ok(world.api, "groups/list", {}, world.alice_t) if g["id"] == world.room)
    assert group["unread"] is True and group["preview"] == "ping"
    validate("threads/markRead", ok(world.api, "threads/markRead", {"groupId": world.room}, world.alice_t))
    group = next(g for g in ok(world.api, "groups/list", {}, world.alice_t) if g["id"] == world.room)
    assert group["unread"] is False


def test_the_group_stream_delivers_new_messages(world):
    api = world.api
    head = ok(api, "threads/head", {"groupId": world.room}, world.alice_t)
    world.server.rakazo.stream_options = {"poll_seconds": 0.02, "heartbeat_seconds": 0.3, "max_seconds": 3.0}
    conn = http.client.HTTPConnection("127.0.0.1", api.port, timeout=5)
    conn.request("POST", "/rpc/threads/subscribe", json.dumps({"json": {"groupId": world.room, "cursor": head["cursor"]}}),
                 {"content-type": "application/json", "accept": "text/event-stream", "origin": "rakazo://",
                  "authorization": f"Bearer {world.alice_t}"})
    resp = conn.getresponse()
    assert resp.status == 200
    world.s.post_message(world.bob, world.room, {"text": "live one", "idempotency_key": "live1"})
    frames = read_frames(resp, 6, stop=lambda fs: any(f.get("type") == "thread.message.created" for f in fs))
    conn.close()
    created = [f for f in frames if f.get("type") == "thread.message.created"]
    assert created and created[0]["payload"]["blocks"][0]["text"] == "live one" and created[0]["threadId"] == "thr_" + world.room
    assert created[0]["seq"] > head["cursor"] and created[0]["spaceId"] == "herdr"


def test_an_unlinked_account_gets_empty_lists_and_clear_errors(svc):
    server = ControlPlaneServer(ControlPlaneRepo(), collab=svc, rakazo_bridge=bridge_section(),
                                assistant_service=AssistantService(config=assistant_config(
                                    {"personal_assistant": {"enabled": True}})))
    threading.Thread(target=server.serve, daemon=True).start()
    server.wait_until_ready()
    try:
        api = Api(server.port)
        token = api.login()
        assert ok(api, "groups/list", {}, token) == [] and ok(api, "groups/listArchived", {}, token) == []
        status, body, _ = rpc(api, "groups/create", {"name": "x", "botIds": ["a", "b"]}, token)
        assert status == 400 and "collab_user" in body["json"]["message"]
        assert ok(api, "bots/list", {}, token) == []
    finally:
        server.shutdown()


def test_the_collab_user_link_is_validated_in_the_configuration():
    from herdr_engineering.control_plane.rakazo_bridge import rakazo_bridge_config
    cfg = rakazo_bridge_config(bridge_section(accounts=[{**ACCOUNTS[0], "collab_user": "alice@lab.example"}]))
    assert cfg["accounts"][0].collab_user == "alice@lab.example"
    for bad in ("", " ", "a b", 5, "x" * 201, "../x"):
        with pytest.raises(ValueError, match="collab_user"):
            rakazo_bridge_config(bridge_section(accounts=[{**ACCOUNTS[0], "collab_user": bad}]))
    assert SPACE == "x-rakazo-space-id"


def test_delegate_proposals_are_cards_that_the_owner_can_accept_or_reject(world, monkeypatch):
    seen = []
    proposal = {"proposal_id": "prop1", "state": "quarantined", "title": "Rename the module", "agent_id": "ag1",
                "task_id": "t1", "created_at": "2026-10-30T10:00:00+00:00"}
    monkeypatch.setattr(world.s, "list_proposals", lambda pr, room: {"proposals": [proposal], "enabled": True})
    monkeypatch.setattr(world.s, "get_proposal", lambda pr, room, pid: {"digest": "dg1"})
    monkeypatch.setattr(world.s, "decide_proposal", lambda pr, room, pid, body: seen.append((pid, body)) or {"state": "x"})
    snap = ok(world.api, "threads/get", {"groupId": world.room}, world.alice_t)
    validate("threads/get", snap)
    [card] = [m for m in snap["messages"] if m["id"] == "proposal:prop1"]
    assert card["blocks"][0]["status"] == "pending" and "not run" in card["blocks"][0]["detail"]
    ok_ = ok(world.api, "threads/answer", {"groupId": world.room, "messageId": "proposal:prop1", "answer": "allow"},
             world.alice_t)
    validate("threads/answer", ok_)
    ok(world.api, "threads/answer", {"groupId": world.room, "messageId": "proposal:prop1", "answer": "deny"}, world.alice_t)
    assert [(p, b["decision"], b["expected_digest"]) for p, b in seen] == [("prop1", "accept", "dg1"),
                                                                           ("prop1", "reject", "dg1")]
    status, _, _ = world.api.rpc("threads/answer", {"groupId": world.room, "messageId": "approval:x", "answer": "allow"},
                                 world.alice_t)
    assert status == 400


def test_a_reviewer_without_rights_sees_no_proposal_cards(world, monkeypatch):
    from herdr_engineering.control_plane.collab import CollabError

    def refuse(pr, room):
        raise CollabError(403, "no")
    monkeypatch.setattr(world.s, "list_proposals", refuse)
    snap = ok(world.api, "threads/get", {"groupId": world.room}, world.bob_t)
    assert all(not m["id"].startswith("proposal:") for m in snap["messages"])


def test_steering_requests_are_cards_that_an_owner_can_confirm_or_decline(world, monkeypatch):
    seen = []
    req = {"steering_id": "st1", "status": "pending", "action": "pause", "instruction": "Stop and wait", "digest": "dg",
           "target": {"fence": "fn1"}, "proposed_at": "2026-10-30T10:00:00+00:00"}
    done = {**req, "steering_id": "st2", "status": "confirmed"}
    monkeypatch.setattr(world.s, "list_steering", lambda pr, room, q: {"steering": [req, done]})
    monkeypatch.setattr(world.s, "confirm_steering", lambda pr, room, sid, body: seen.append(("c", sid, body)) or {})
    monkeypatch.setattr(world.s, "reject_steering", lambda pr, room, sid, body: seen.append(("r", sid, body)) or {})
    snap = ok(world.api, "threads/get", {"groupId": world.room}, world.alice_t)
    validate("threads/get", snap)
    [card] = [m for m in snap["messages"] if m["id"].startswith("steer:")]
    assert card["id"] == "steer:st1" and card["blocks"][0]["status"] == "pending"
    for answer in ("allow", "deny"):
        validate("threads/answer", ok(world.api, "threads/answer",
                                      {"groupId": world.room, "messageId": "steer:st1", "answer": answer}, world.alice_t))
    assert seen[0] == ("c", "st1", {"digest": "dg", "expected_fence": "fn1"}) and seen[1][:2] == ("r", "st1")
    assert world.api.rpc("threads/answer", {"groupId": world.room, "messageId": "steer:st1", "answer": "maybe"},
                         world.alice_t)[0] == 400
    assert world.api.rpc("threads/answer", {"groupId": world.room, "messageId": "steer:nope", "answer": "allow"},
                         world.alice_t)[0] == 404


def test_auto_join_adds_own_agents_to_listed_rooms_only_when_the_operator_opts_in(world):
    from herdr_engineering.control_plane.rakazo_bridge import rakazo_bridge_config
    agent, info = world.agent("alice", name="newcomer", room=world.room2)

    def members():
        groups = ok(world.api, "groups/list", {}, world.alice_t)
        return {g["id"]: {m["botId"] for m in g["members"] if m.get("botId")} for g in groups}
    assert info["agent_id"] not in members().get(world.room, set())        # off by default
    bridge = world.server.rakazo
    bridge.auto_join_rooms = (world.room,)
    joined = members()
    assert info["agent_id"] in joined[world.room]
    bridge.auto_join_rooms = ()
    cfg = rakazo_bridge_config(bridge_section(accounts=[ACCOUNTS[0]], auto_join_rooms=[world.room]))
    assert cfg["auto_join_rooms"] == (world.room,)
    for bad in ("room", [1], ["x"] * 51, [""]):
        with pytest.raises(ValueError, match="auto_join_rooms"):
            rakazo_bridge_config(bridge_section(accounts=[ACCOUNTS[0]], auto_join_rooms=bad))

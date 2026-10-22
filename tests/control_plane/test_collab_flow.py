# ruff: noqa: F811  (pytest fixtures are imported by name from the sibling test modules)
"""The room flow view: who hands work to whom, and its tie to fabric sessions."""
from __future__ import annotations

import json

import pytest

from herdr_engineering.control_plane.api import _fabric_enriched_flow
from tests.control_plane.test_collab_tasks import W, code, svc  # noqa: F401


@pytest.fixture
def w(svc):
    svc.max_agents_per_user = 60
    return W(svc)


def chain(w):
    a1, i1 = w.agent("alice", ("read", "execute", "instruct"), name="lead")
    a2, i2 = w.agent("bob", ("read", "execute"), name="coder")
    root = w.s.create_task(w.alice, w.room, {"target_agent_id": i1["agent_id"], "text": "ship it"})["task_id"]
    w.s.update_task(a1, root, {"status": "accepted"})
    sub = w.s.create_task(a1, w.room, {"target_agent_id": i2["agent_id"], "text": "write the parser",
                                        "parent_task_id": root})["task_id"]
    return a1, i1, a2, i2, root, sub


def test_flow_shows_people_agents_and_who_delegated_to_whom(w):
    a1, i1, a2, i2, root, sub = chain(w)
    f = w.s.room_flow(w.alice, w.room)
    names = {n["id"]: n for n in f["nodes"]}
    assert names["a:" + i1["agent_id"]]["name"] == "lead" and names["a:" + i2["agent_id"]]["family"] == "claude-code"
    assert names["u:alice"]["kind"] == "person"
    edges = {(e["from"], e["to"]): e["counts"] for e in f["edges"]}
    assert edges[("u:alice", "a:" + i1["agent_id"])] == {"accepted": 1}
    assert edges[("a:" + i1["agent_id"], "a:" + i2["agent_id"])] == {"pending": 1}
    assert {t["task_id"] for t in f["tasks"]} == {root, sub} and f["untrusted_text"] is True


def test_flow_is_room_scoped_and_never_for_outsiders_or_guests(w):
    chain(w)
    assert code(w.s.room_flow, w.mallory, w.room) == 404
    assert code(w.s.room_flow, w.dave, w.room) == 403
    assert w.s.room_flow(w.carol, w.room)["nodes"]                      # a viewer may watch
    assert w.s.room_flow(w.alice, w.room2) ["edges"] == []


def test_fabric_session_ids_are_only_shown_to_the_agents_owner(w):
    a1, i1, a2, i2, *_ = chain(w)
    w.s.link_agent(w.alice, i1["agent_id"], {"instance_id": "herdr:bender:srv:pane-1"})
    mine = {n["agent_id"]: n for n in w.s.room_flow(w.alice, w.room)["nodes"] if n["kind"] == "agent"}
    theirs = {n["agent_id"]: n for n in w.s.room_flow(w.bob, w.room)["nodes"] if n["kind"] == "agent"}
    assert mine[i1["agent_id"]]["instance_id"] == "herdr:bender:srv:pane-1"
    assert theirs[i1["agent_id"]]["instance_id"] is None
    assert "bender" not in json.dumps(w.s.room_flow(w.carol, w.room))
    assert w.s.list_my_agents(w.alice)["agents"][0]["instance_id"] in (None, "herdr:bender:srv:pane-1")


def test_only_the_owner_links_and_the_label_is_validated(w):
    _, i1 = w.agent("alice")
    assert code(w.s.link_agent, w.bob, i1["agent_id"], {"instance_id": "x"}) == 404
    for bad in ("", "x" * 201, 5, "a\x00b", ["x"]):
        assert code(w.s.link_agent, w.alice, i1["agent_id"], {"instance_id": bad}) == 400
    w.s.link_agent(w.alice, i1["agent_id"], {"instance_id": "ok"})
    w.s.link_agent(w.alice, i1["agent_id"], {"instance_id": None})      # cleared
    assert w.s.list_my_agents(w.alice)["agents"][0]["instance_id"] is None


def test_removed_agents_stay_visible_as_history_not_as_live_nodes(w):
    a1, i1, a2, i2, *_ = chain(w)
    w.s.remove_agent_from_room(w.bob, w.room, i2["agent_id"])
    f = w.s.room_flow(w.alice, w.room)
    assert {n["agent_id"]: n["status"] for n in f["nodes"] if n["kind"] == "agent"}[i2["agent_id"]] == "removed"


def test_dashboard_enrichment_joins_only_known_sessions_and_survives_junk():
    class Repo:
        def get_session(self, sid):
            return {"status": "active", "host_id": "host_1", "agent_role": "claude", "mission_id": "m1",
                    "cwd": "/w", "branch": "main"} if sid == "herdr:b:s:1" else None

        def get_mission(self, mid):
            return {"title": "Parser"}

    flow = {"nodes": [{"kind": "agent", "id": "a:1", "instance_id": "herdr:b:s:1"},
                      {"kind": "agent", "id": "a:2", "instance_id": "herdr:gone"},
                      {"kind": "agent", "id": "a:3", "instance_id": None}, {"kind": "person", "id": "u:x"}]}
    out = json.loads(_fabric_enriched_flow(Repo(), json.dumps(flow).encode()))
    assert out["nodes"][0]["fabric"]["mission_title"] == "Parser" and out["nodes"][0]["fabric"]["status"] == "active"
    assert out["nodes"][1]["fabric"]["status"] == "unknown"
    assert "fabric" not in out["nodes"][2] and "fabric" not in out["nodes"][3]
    assert _fabric_enriched_flow(Repo(), b"not json") == b"not json"


# ----------------------------------------------------------- the single room long-poll
def test_state_returns_everything_first_then_nothing_until_the_room_changes(w):
    chain(w)
    first = w.s.room_state(w.alice, w.room, -1, 0, 0)
    assert first["changed"] is True and first["flow"]["nodes"] and first["tasks"] and first["agents"]
    assert first["requests"] == []
    again = w.s.room_state(w.alice, w.room, first["seq"], 0, 0)
    assert again == {"seq": first["seq"], "changed": False}


def test_state_wakes_for_its_own_room_only(w):
    import threading
    import time
    base = w.s.room_state(w.alice, w.room, -1, 0, 0)["seq"]
    got = {}

    def waiter():
        t0 = time.monotonic()
        got["r"] = w.s.room_state(w.alice, w.room, base, 0, 8)
        got["dt"] = time.monotonic() - t0

    th = threading.Thread(target=waiter)
    th.start()
    time.sleep(0.4)
    w.s.post_message(w.alice, w.room2, {"text": "other room", "idempotency_key": "k0"})      # must not wake it
    time.sleep(0.4)
    assert th.is_alive()
    w.s.post_message(w.alice, w.room, {"text": "this room", "idempotency_key": "k1"})
    th.join(5)
    assert got["r"]["changed"] is True and [e["text"] for e in got["r"]["events"] if e["event_type"] == "message"][-1] == "this room"
    assert got["dt"] < 4


def test_state_is_authorised_capped_and_hides_what_a_guest_may_not_see(w):
    assert code(w.s.room_state, w.mallory, w.room, -1, 0, 0) == 404
    g = w.s.room_state(w.dave, w.room, -1, 0, 0)
    assert g["agents"] == [] and g["flow"] is None and g["requests"] is None
    assert w.s.room_state(w.carol, w.room, -1, 0, 0)["requests"] is None
    w.s._state_waiters[w.alice.key] = w.s.MAX_STATE_WAITERS
    assert code(w.s.room_state, w.alice, w.room, -1, 0, 0) == 429


def test_state_ends_when_access_is_lost_while_waiting(w):
    import threading
    import time
    seq = w.s.room_state(w.carol, w.room, -1, 0, 0)["seq"]
    out = {}

    def waiter():
        try:
            w.s.room_state(w.carol, w.room, seq, 0, 8)
        except CollabError as e:
            out["status"] = e.status

    from herdr_engineering.control_plane.collab import CollabError
    th = threading.Thread(target=waiter)
    th.start()
    time.sleep(0.4)
    w.s.remove_member(w.alice, w.room, "carol")
    th.join(5)
    assert out.get("status") == 404

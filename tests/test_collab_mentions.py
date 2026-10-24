# ruff: noqa: F811  (pytest fixtures are imported by name from the sibling test modules)
"""Room addressing: `@person`, `@owner.harness` agents, old names as aliases, groups, and mentions
the server derives from the text (a client never supplies them)."""
from __future__ import annotations

import pytest

from tests.control_plane.test_collab_tasks import USERS, W, code, svc  # noqa: F401


def link(s, uid, email):
    s._commit("identity_linked", {"actor_user_id": uid, "actor_agent_id": None, "user_id": uid, "email": email})


@pytest.fixture
def w(svc):
    svc.max_agents_per_user = 60
    room = W(svc)
    for uid in ("alice", "bob", "carol", "dave", "mallory"):
        link(svc, uid, f"{uid}@example.org")
    return room


def agent(w, owner, harness="pi", name=None, room=None, actions=("read", "post", "execute")):
    pr = getattr(w, owner)
    info = w.s.connect_agent(pr, {"harness_type": harness, **({"name": name} if name else {})})
    out = w.s.grant_agent(pr, room or w.room, {"agent_id": info["agent_id"], "actions": list(actions)})
    if out["status"] == "pending":
        w.s.decide_agent_request(w.alice, room or w.room, out["request_id"], {"decision": "approve"})
    return w.s.authenticate(f"Agent {info['credential']}", info["device_id"]), info["agent_id"]


def addresses(w):
    return {a["agent_id"]: a["address"] for a in w.s.list_room_agents(w.alice, w.room)["agents"]}


def say(w, who, text, **extra):
    status, out = w.s.post_message(who, w.room, {"text": text, "idempotency_key": f"k{len(text)}{id(extra)}", **extra})
    assert status == 201
    return out["event"]


def test_a_person_is_addressed_by_their_handle(w):
    assert w.s.resolve_mentions(w.room, "hi @bob, look") == [{"kind": "person", "id": "bob", "name": "bob"}]
    assert w.s.resolve_mentions(w.room, "thanks @Bob.") == [{"kind": "person", "id": "bob", "name": "bob"}]


def test_an_agent_is_addressed_as_owner_dot_harness(w):
    _, pi = agent(w, "alice", "pi")
    _, cc = agent(w, "bob", "claude-code")
    assert addresses(w) == {pi: "alice.pi", cc: "bob.claude"}
    assert w.s.resolve_mentions(w.room, "@alice.pi /skill:review src/") == [
        {"kind": "agent", "id": pi, "name": "alice.pi"}]
    assert w.s.resolve_mentions(w.room, "ping @bob.claude")[0]["id"] == cc


def test_a_second_agent_of_the_same_owner_and_harness_is_numbered_and_numbers_never_shift(w):
    _, first = agent(w, "alice", "pi")
    _, second = agent(w, "alice", "pi")
    assert addresses(w) == {first: "alice.pi", second: "alice.pi-2"}
    w.s.revoke_agent(w.alice, first)
    _, third = agent(w, "alice", "pi")
    assert addresses(w) == {second: "alice.pi-2", third: "alice.pi"}       # the freed slot is reused; -2 stays -2
    assert w.s.resolve_mentions(w.room, "@alice.pi-2 go")[0]["id"] == second


def test_existing_names_stay_valid_as_aliases(w):
    _, lead = agent(w, "alice", "pi", name="lead")
    assert w.s.resolve_mentions(w.room, "@lead go") == [{"kind": "agent", "id": lead, "name": "alice.pi"}]
    w.s.rename_agent(w.alice, lead, {"name": "architect"})
    assert w.s.resolve_mentions(w.room, "@lead and @architect")[0]["id"] == lead     # the old name still works
    assert len(w.s.resolve_mentions(w.room, "@lead and @architect")) == 1             # one agent, mentioned once
    _, newlead = agent(w, "bob", "codex", name="lead")                               # someone takes the name now
    assert w.s.resolve_mentions(w.room, "@lead")[0]["id"] == newlead


def test_an_old_alias_loses_to_a_person_or_an_address(w):
    _, imp = agent(w, "bob", "pi", name="alice")                                     # an alias that is alice's handle
    assert w.s.resolve_mentions(w.room, "@alice") == [{"kind": "person", "id": "alice", "name": "alice"}]
    assert w.s.resolve_mentions(w.room, "@bob.pi")[0]["id"] == imp


def test_unknown_names_addresses_and_code_are_not_mentions(w):
    agent(w, "alice", "pi")
    for text in ("@zzz hello", "mail bob@example.org", "C:\\x\\@bob", "`@bob` in code", "```\n@bob\n```",
                 "@mallory", "@" + "b" * 60):
        assert w.s.resolve_mentions(w.room, text) == [], text


def test_a_client_sent_mentions_field_is_ignored(w):
    forged = [{"kind": "person", "id": "carol", "name": "carol"}, {"kind": "group", "id": "all", "name": "all"}]
    ev = say(w, w.bob, "hi @alice", mentions=forged)
    assert ev["mentions"] == [{"kind": "person", "id": "alice", "name": "alice"}]
    assert ev["text"] == "hi @alice"
    stored = [e for e in w.s.read_events(w.carol, w.room, 0, 200)["events"] if e["event_type"] == "message"][-1]
    assert stored["mentions"] == ev["mentions"]


def test_literal_agent_skill_text_is_stored_byte_for_byte(w):
    agent(w, "alice", "pi")
    text = "@alice.pi /skill:review @src/app.js !ls $HOME"
    assert say(w, w.bob, text)["text"] == text


def test_groups_expand_to_this_rooms_people_and_agents_only(w):
    _, mine = agent(w, "alice", "pi")
    _, bobs = agent(w, "bob", "codex")
    _, elsewhere = agent(w, "alice", "codex", room=w.room2)
    people = {("person", u) for u in ("alice", "bob", "carol", "dave")}
    by_id = {m["id"]: m for m in w.s.resolve_mentions(w.room, "@all @agents @here")}
    targets = lambda g: {(t["kind"], t["id"]) for t in by_id[g]["targets"]}   # noqa: E731
    assert targets("agents") == {("agent", mine), ("agent", bobs)}
    assert targets("all") == people | {("agent", mine), ("agent", bobs)}
    assert ("person", "mallory") not in targets("all") and ("agent", elsewhere) not in targets("all")
    assert targets("here") == set()                                       # nobody has the room open yet
    w.s.room_state(w.bob, w.room, -1, 0, 0)                               # bob opens the room
    assert {(t["kind"], t["id"]) for t in w.s.resolve_mentions(w.room, "@here")[0]["targets"]} == {("person", "bob")}


def test_a_mentioned_agent_sees_that_it_was_addressed(w):
    pi, pid = agent(w, "alice", "pi")
    other, _ = agent(w, "bob", "codex")
    say(w, w.bob, "@alice.pi please look")
    say(w, w.bob, "@agents status?")
    feed = lambda pr: [e for e in w.s.read_events(pr, w.room, 0, 200)["events"] if e["event_type"] == "message"]  # noqa: E731
    assert [e["mentions_me"] for e in feed(pi)] == [True, True]
    assert [e["mentions_me"] for e in feed(other)] == [False, True]


def test_room_state_lists_people_by_handle_never_by_email(w):
    agent(w, "alice", "pi")
    st = w.s.room_state(w.bob, w.room, -1, 0, 0)
    assert {p["handle"] for p in st["people"]} == {"alice", "bob", "carol", "dave"}
    assert "example.org" not in repr(st["people"])
    assert st["agents"][0]["address"] == "alice.pi"
    assert w.s.room_state(w.dave, w.room, -1, 0, 0)["people"] == []      # a guest gets no roster


def test_person_handles_are_unique_and_never_look_like_an_address(w):
    link(w.s, "carol", "Al.Ice@other.org")                               # a new email keeps the old handle
    st = w.s.room_state(w.alice, w.room, -1, 0, 0)
    handles = {p["user_id"]: p["handle"] for p in st["people"]}
    assert handles["carol"] == "carol"                                   # frozen once assigned
    s = w.s
    s._commit("identity_linked", {"actor_user_id": "zed", "actor_agent_id": None, "user_id": "zed",
                                  "email": "alice@elsewhere.org"})
    s._commit("identity_linked", {"actor_user_id": "yan", "actor_agent_id": None, "user_id": "yan",
                                  "email": "first.last@x.org"})
    s._commit("identity_linked", {"actor_user_id": "hal", "actor_agent_id": None, "user_id": "hal",
                                  "email": "all@x.org"})
    assert s._user_handle["zed"] == "alice-2"
    assert s._user_handle["yan"] == "first-last"
    assert s._user_handle["hal"] == "all-2"                                # groups are reserved


def test_addresses_survive_a_restart(w, tmp_path):
    _, first = agent(w, "alice", "pi")
    _, second = agent(w, "alice", "pi")
    w.s.revoke_agent(w.alice, first)
    from herdr_engineering.control_plane.collab import CollabService
    again = CollabService(w.s.auth, now=w.s.clock)
    again.hydrate(w.s._log)
    alice = again.authenticate(f"Bearer {USERS['alice']}", None)
    assert {a["agent_id"]: a["address"] for a in again.list_room_agents(alice, w.room)["agents"]} == {second: "alice.pi-2"}
    assert again.resolve_mentions(w.room, "@alice.pi-2")[0]["id"] == second

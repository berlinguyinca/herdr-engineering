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
    _, imp = agent(w, "bob", "pi", name="alice")
    w.s.rename_agent(w.bob, imp, {"name": "helper"})                                # "alice" is now an old name
    assert w.s.resolve_mentions(w.room, "@alice") == [{"kind": "person", "id": "alice", "name": "alice"}]
    assert w.s.resolve_mentions(w.room, "@bob.pi")[0]["id"] == imp


def test_a_person_handle_equal_to_an_agents_current_name_addresses_nobody(w):
    _, coder = agent(w, "alice", "codex", name="carol")                           # agent "carol" + person carol
    assert w.s.resolve_mentions(w.room, "@carol please") == []
    assert w.s.resolve_mentions(w.room, "@alice.codex")[0]["id"] == coder        # the unambiguous form works


def test_an_old_name_two_agents_share_addresses_nobody(w):
    _, one = agent(w, "alice", "pi", name="lead")
    w.s.rename_agent(w.alice, one, {"name": "lead2"})
    _, two = agent(w, "bob", "pi", name="lead")
    w.s.rename_agent(w.bob, two, {"name": "lead3"})
    assert w.s.resolve_mentions(w.room, "@lead") == []


def test_unknown_names_addresses_and_code_are_not_mentions(w):
    agent(w, "alice", "pi")
    for text in ("@zzz hello", "mail bob@example.org", "C:\\x\\@bob", "`@bob` in code", "```\n@bob\n```", "~~~\n@bob\n~~~", "~~~py\n@bob",
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
    targets = lambda g: {(t["kind"], t["id"]) for t in w.s._group_members(w.room, g)}   # noqa: E731
    assert targets("agents") == {("agent", mine), ("agent", bobs)}
    assert targets("all") == people | {("agent", mine), ("agent", bobs)}
    assert ("person", "mallory") not in targets("all") and ("agent", elsewhere) not in targets("all")
    assert targets("here") == set()                                       # nobody has the room open yet
    w.s.room_state(w.bob, w.room, -1, 0, 0)                               # bob opens the room
    assert targets("here") == {("person", "bob")}
    assert w.s.resolve_mentions(w.room, "@all") == [{"kind": "group", "id": "all", "name": "all"}]


def test_a_group_mention_never_carries_the_roster_and_storage_stays_bounded(w):
    from tests.control_plane.test_collab_external import enroll
    ids = [agent(w, "alice", "pi")[1] for _ in range(5)]
    ext_pr, _, _ = enroll(w)
    ev = say(w, w.bob, "@all @agents @here " + " ".join(f"@alice.pi-{n}" for n in range(2, 40)))
    stored = w.s._log[ev["sequence"] - 1]["payload"]["mentions"]
    assert len(stored) <= 32 and all(set(m) == {"kind", "id", "name"} for m in stored)
    assert len(repr(stored)) < 4000
    for viewer in (w.dave, ext_pr):                                       # a guest and an external agent
        msgs = [e for e in w.s.read_events(viewer, w.room, 0, 200)["events"] if e["event_type"] == "message"]
        seen = repr(msgs[-1])
        assert "targets" not in seen and "carol" not in seen and not any(i in seen for i in ids)
        assert all(set(m) == {"kind", "name"} for m in msgs[-1]["mentions"])   # names only: they were typed
    assert [e for e in w.s.read_events(ext_pr, w.room, 0, 200)["events"] if e["event_type"] == "message"][-1]["mentions_me"] is True


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


def test_agents_report_skill_names_at_registration_and_the_room_only_lists_them(w):
    info = w.s.connect_agent(w.alice, {"harness_type": "pi", "skills": ["skill:review", "skill:pdf-tools", "skill:review"]})
    w.s.grant_agent(w.alice, w.room, {"agent_id": info["agent_id"], "actions": ["read", "post"]})
    row = w.s.list_room_agents(w.bob, w.room)["agents"][0]
    assert row["skills"] == ["skill:pdf-tools", "skill:review"]
    plain = w.s.connect_agent(w.alice, {"harness_type": "pi"})
    w.s.grant_agent(w.alice, w.room, {"agent_id": plain["agent_id"], "actions": ["read"]})
    assert {a["agent_id"]: a["skills"] for a in w.s.list_room_agents(w.bob, w.room)["agents"]}[plain["agent_id"]] == []
    for bad in ("skill:x", [1], ["has space"], ["x" * 65], ["a\nb"], ["ok"] * 101, [""]):
        assert code(w.s.connect_agent, w.alice, {"harness_type": "pi", "skills": bad}) == 400, bad


def test_join_and_enrollment_carry_skills_too(w):
    tok = w.s.create_join_token(w.alice, {"harness_type": "pi", "name_prefix": "fleet", "count": 2})["token"]
    joined = w.s.redeem_join_token({"token": tok, "skills": ["deploy"]})
    assert w.s._agents[joined["agent_id"]]["skills"] == ["deploy"]
    assert code(w.s.redeem_join_token, {"token": tok, "skills": "deploy"}) == 400
    start = w.s.start_enrollment(w.alice, {"device_id": "dev1", "harness_type": "pi"})
    done = w.s.complete_enrollment({"code": start["code"], "device_id": "dev1", "skills": ["skill:review"]})
    assert w.s._agents[done["agent_id"]]["skills"] == ["skill:review"]


def test_a_task_to_an_agent_keeps_the_literal_text_byte_for_byte(w):
    pi, pid = agent(w, "alice", "pi")
    text = "@alice.pi /skill:review @src/app.js !ls $HOME"
    tid = w.s.create_task(w.bob, w.room, {"target_agent_id": pid, "text": text})["task_id"]
    assert [t["text"] for t in w.s.list_tasks(pi, w.room)["tasks"] if t["task_id"] == tid] == [text]
    created = [e for e in w.s.read_events(pi, w.room, 0, 200)["events"] if e["event_type"] == "task_created"]
    assert created[-1]["task"]["text"] == text


def test_a_task_names_its_targets_address_and_aliases_without_touching_the_text(w):
    pi, pid = agent(w, "alice", "pi", name="lead")
    w.s.rename_agent(w.alice, pid, {"name": "boss"})
    w.s.create_task(w.bob, w.room, {"target_agent_id": pid, "text": "@lead /skill:x go"})
    t = w.s.task_inbox(pi)["tasks"][0]
    assert (t["target_address"], t["target_name"], t["target_aliases"], t["text"]) == ("alice.pi", "boss", ["lead"], "@lead /skill:x go")

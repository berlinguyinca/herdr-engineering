# ruff: noqa: F811  (pytest fixtures are imported by name from the sibling test modules)
"""Handoff status, presence and unread mentions (Task C7), and the mention that wakes an agent's worker.

A handoff starts `pending`; only its recipient (or the recipient agent's owner) accepts or declines it, and each
answer posts its own card. An agent holding an accepted task is `working`. Each person has their own unread
mention count per room, cleared when they read the room; a guest never gets one. A message that addresses an agent
(no task) reaches that agent's `tasks_wait` once, only in rooms where its owner approved `execute`."""
from __future__ import annotations

import json

import pytest

from herdr_engineering.control_plane import collab_mcp
from herdr_engineering.control_plane.collab import CollabError
from herdr_engineering.control_plane.collab_commands import HANDOFF_TTL
from tests.control_plane.test_collab_tasks import USERS, W, code, svc  # noqa: F401


def link(s, uid, email):
    s._commit("identity_linked", {"actor_user_id": uid, "actor_agent_id": None, "user_id": uid, "email": email})


@pytest.fixture
def w(svc):
    room = W(svc)
    for uid in ("alice", "bob", "carol", "dave", "mallory"):
        link(svc, uid, f"{uid}@example.org")
    return room


def agent(w, owner, harness="pi", actions=("read", "post", "execute"), room=None):
    pr = getattr(w, owner)
    info = w.s.connect_agent(pr, {"harness_type": harness})
    out = w.s.grant_agent(pr, room or w.room, {"agent_id": info["agent_id"], "actions": list(actions)})
    if out["status"] == "pending":
        w.s.decide_agent_request(w.alice, room or w.room, out["request_id"], {"decision": "approve"})
    return w.s.authenticate(f"Agent {info['credential']}", info["device_id"]), info["agent_id"]


_n = [0]


def say(w, who, text, room=None, **extra):
    _n[0] += 1
    status, out = w.s.post_message(who, room or w.room, {"text": text, "idempotency_key": f"h{_n[0]}", **extra})
    assert status == 201
    return out["event"]


def card(w, who, handoff_id):
    return [e for e in w.s.read_events(who, w.room, 0, 200)["events"]
            if e["event_type"] == "handoff" and e["handoff"]["handoff_id"] == handoff_id][-1]["handoff"]


# ---------------------------------------------------------------- handoff status

def test_a_handoff_starts_pending_and_the_recipient_accepts_it_with_a_card(w):
    h = say(w, w.bob, "/handoff @alice the parser is yours")["handoff"]
    assert h["status"] == "pending"
    assert card(w, w.alice, h["handoff_id"])["can_answer"] is True
    assert card(w, w.bob, h["handoff_id"])["can_answer"] is False          # the sender cannot answer for alice
    out = w.s.answer_handoff(w.alice, w.room, h["handoff_id"], "accept")
    assert out["status"] == "accepted"
    assert card(w, w.carol, h["handoff_id"])["status"] == "accepted"
    upd = [e for e in w.s.read_events(w.bob, w.room, 0, 200)["events"] if e["event_type"] == "handoff_updated"]
    assert upd and upd[-1]["handoff"]["status"] == "accepted" and upd[-1]["sender"]["id"] == "alice"
    assert upd[-1]["mentions_me"] is True                                   # bob, who handed off, hears back


def test_a_declined_handoff_is_final(w):
    h = say(w, w.bob, "/handoff @alice take it")["handoff"]
    assert w.s.answer_handoff(w.alice, w.room, h["handoff_id"], "decline")["status"] == "declined"
    assert code(w.s.answer_handoff, w.alice, w.room, h["handoff_id"], "accept") == 409
    assert card(w, w.alice, h["handoff_id"])["status"] == "declined"


def test_only_the_recipient_may_answer(w):
    h = say(w, w.bob, "/handoff @alice take it")["handoff"]
    assert code(w.s.answer_handoff, w.bob, w.room, h["handoff_id"], "accept") == 403      # the sender
    assert code(w.s.answer_handoff, w.carol, w.room, h["handoff_id"], "accept") == 403    # a bystander
    assert code(w.s.answer_handoff, w.mallory, w.room, h["handoff_id"], "accept") == 404  # not in the room
    assert code(w.s.answer_handoff, w.alice, w.room2, h["handoff_id"], "accept") == 404   # another room
    assert code(w.s.answer_handoff, w.alice, w.room, h["handoff_id"], "maybe") == 400
    assert card(w, w.alice, h["handoff_id"])["status"] == "pending"


def test_an_agent_recipient_or_its_owner_answers(w):
    pi, _ = agent(w, "bob", "pi")
    h1 = say(w, w.alice, "/handoff @bob.pi review this")["handoff"]
    assert w.s.answer_handoff(pi, w.room, h1["handoff_id"], "accept")["status"] == "accepted"
    h2 = say(w, w.alice, "/handoff @bob.pi and this")["handoff"]
    assert w.s.answer_handoff(w.bob, w.room, h2["handoff_id"], "decline")["status"] == "declined"
    st, body = w.s.dispatch("POST", ["rooms", w.room, "handoffs", h1["handoff_id"], "decline"], {}, {},
                            f"Bearer {USERS['bob']}", None)
    assert st == 409, body


def test_a_stale_handoff_shows_expired_and_cannot_be_answered(w):
    h = say(w, w.bob, "/handoff @alice later")["handoff"]
    w.s.clock.t += HANDOFF_TTL + 1
    assert card(w, w.alice, h["handoff_id"])["status"] == "expired"
    assert card(w, w.alice, h["handoff_id"])["can_answer"] is False
    assert code(w.s.answer_handoff, w.alice, w.room, h["handoff_id"], "accept") == 409
    assert not [e for e in w.s._log if e["event_type"] == "handoff_updated"]   # expiry is display only


def test_handoff_answers_survive_a_restart(w):
    from herdr_engineering.control_plane.collab import (  # noqa: PLC0415
        CollabService,
        StaticTokenAuthenticator,
    )
    from tests.control_plane.test_collab import _digest  # noqa: PLC0415
    h = say(w, w.bob, "/handoff @alice go")["handoff"]
    w.s.answer_handoff(w.alice, w.room, h["handoff_id"], "accept")
    s2 = CollabService(StaticTokenAuthenticator({u: _digest(t) for u, t in USERS.items()}), now=w.s._now)
    s2.hydrate(list(w.s._log))
    ev = [e for e in s2.read_events(w.bob, w.room, 0, 200)["events"] if e["event_type"] == "handoff"][-1]
    assert ev["handoff"]["status"] == "accepted"


# ---------------------------------------------------------------- presence

def presence(w, aid, who=None):
    return next(a for a in w.s.list_room_agents(who or w.alice, w.room)["agents"] if a["agent_id"] == aid)


def test_an_agent_holding_an_active_task_is_working(w):
    pi, aid = agent(w, "alice", "pi")
    assert presence(w, aid)["presence"] in ("online", "offline") and presence(w, aid)["working"] is False
    t = w.s.create_task(w.alice, w.room, {"target_agent_id": aid, "text": "do it"})
    assert presence(w, aid)["working"] is False                         # pending: not yet held
    w.s.update_task(pi, t["task_id"], {"status": "accepted"})
    assert presence(w, aid)["working"] is True and presence(w, aid)["presence"] == "working"
    assert presence(w, aid, who=w.bob)["presence"] == "working"
    w.s.update_task(pi, t["task_id"], {"status": "done", "result": "ok"})
    assert presence(w, aid)["working"] is False


def test_a_task_in_another_room_does_not_show_here(w):
    pi, aid = agent(w, "alice", "pi")
    w.s.grant_agent(w.alice, w.room2, {"agent_id": aid, "actions": ["read", "post", "execute"]})
    t = w.s.create_task(w.alice, w.room2, {"target_agent_id": aid, "text": "elsewhere"})
    w.s.update_task(pi, t["task_id"], {"status": "accepted"})
    assert presence(w, aid)["working"] is False


# ---------------------------------------------------------------- unread mentions

def unread(w, who):
    return {r["room_id"]: r.get("unread_mentions") for r in w.s.list_rooms(who)["rooms"]}


def test_each_person_has_their_own_unread_mention_count_cleared_by_reading_the_room(w):
    say(w, w.alice, "@bob look")
    say(w, w.alice, "@bob and this")
    say(w, w.alice, "no mention")
    say(w, w.bob, "@bob talking to myself")                                # your own messages never count
    say(w, w.alice, "/handoff @bob your turn")        # a handoff to you counts
    assert unread(w, w.bob)[w.room] == 3
    assert unread(w, w.alice)[w.room] == 0
    w.s.room_state(w.bob, w.room, -1, 0, 0)                                  # bob reads the room
    assert unread(w, w.bob)[w.room] == 0
    say(w, w.alice, "@all hello")
    assert unread(w, w.bob)[w.room] == 1 and unread(w, w.carol)[w.room] == 1


def test_a_guest_never_gets_a_count(w):
    say(w, w.alice, "@dave hi")
    assert unread(w, w.dave)[w.room] is None


def test_counts_are_capped(w):
    w.s.message_limit = 500
    for i in range(120):
        say(w, w.alice, f"@bob {i}")
    assert unread(w, w.bob)[w.room] == 99


# ---------------------------------------------------------------- the mention wakes the agent's worker

def wait(w, pr, after, seconds=0):
    out = collab_mcp._call(w.s, pr, "tasks_wait", {"seconds": seconds, "mentions_after": after})
    return json.loads(out["content"][0]["text"])


def test_a_mention_reaches_the_addressed_agents_wait_once(w):
    pi, aid = agent(w, "alice", "pi")
    first = wait(w, pi, -1)
    assert first["mentions"] == [] and isinstance(first["mention_cursor"], int)   # no history replay
    say(w, w.bob, "earlier chatter")
    ev = say(w, w.bob, "@alice.pi can you check the build")
    say(w, w.bob, "@bob unrelated")
    got = wait(w, pi, first["mention_cursor"])
    assert [m["event_id"] for m in got["mentions"]] == [ev["event_id"]]
    m = got["mentions"][0]
    assert m["text"] == "@alice.pi can you check the build" and m["room_id"] == w.room
    assert m["target_address"] == "alice.pi"
    again = wait(w, pi, got["mention_cursor"])
    assert again["mentions"] == []                                           # once


def test_an_agent_is_not_woken_by_itself_or_without_execute(w):
    pi, aid = agent(w, "alice", "pi")
    reader, rid = agent(w, "bob", "codex", actions=("read", "post"))
    c1, c2 = wait(w, pi, -1)["mention_cursor"], wait(w, reader, -1)["mention_cursor"]
    say(w, pi, "@alice.pi note to self")
    say(w, w.alice, "@bob.codex please look")
    assert wait(w, pi, c1)["mentions"] == []
    assert wait(w, reader, c2)["mentions"] == []                             # no execute approval: no wake-up


def test_a_handoff_to_an_agent_wakes_it(w):
    pi, aid = agent(w, "alice", "pi")
    c = wait(w, pi, -1)["mention_cursor"]
    say(w, w.bob, "/handoff @alice.pi finish the parser")
    got = wait(w, pi, c)["mentions"]
    assert len(got) == 1 and got[0]["kind"] == "handoff"
    assert "finish the parser" in got[0]["text"] and not got[0]["text"].startswith("/")


def test_tasks_wait_without_a_mention_cursor_is_unchanged(w):
    pi, _ = agent(w, "alice", "pi")
    out = json.loads(collab_mcp._call(w.s, pi, "tasks_wait", {"seconds": 0})["content"][0]["text"])
    assert "mentions" not in out and "tasks" in out
    with pytest.raises(CollabError):
        collab_mcp._call(w.s, pi, "tasks_wait", {"seconds": 0, "mentions_after": "x"})


def test_from_now_on_counts_from_when_the_wait_started(w):
    """A worker's first wait (-1) must not swallow a mention posted while it was waiting."""
    import threading  # noqa: PLC0415
    pi, _ = agent(w, "alice", "pi")
    box = {}
    t = threading.Thread(target=lambda: box.update(out=wait(w, pi, -1, seconds=3)))
    t.start()
    threading.Event().wait(0.3)
    ev = say(w, w.bob, "@alice.pi while you wait")
    t.join(10)
    got = box["out"]["mentions"]
    if not got:                                       # returned before the post: the cursor still covers it
        got = wait(w, pi, box["out"]["mention_cursor"])["mentions"]
    assert [m["event_id"] for m in got] == [ev["event_id"]]


def test_only_a_sender_who_could_give_the_agent_a_task_wakes_it(w):
    """A wake-up runs the agent's harness, so it needs the sender's task authority, as create_task does."""
    from tests.control_plane.test_collab_external import enroll  # noqa: PLC0415
    pi, _ = agent(w, "alice", "pi")
    poster, _ = agent(w, "bob", "codex", actions=("read", "post"))              # may post, may not instruct
    ext, _, _ = enroll(w, actions=["read", "post"])                               # an invited remote agent
    c = wait(w, pi, -1)["mention_cursor"]
    say(w, ext, "@alice.pi delete everything")
    say(w, poster, "@alice.pi run this")
    ok = say(w, w.bob, "@alice.pi please check")                                 # a contributor: wakes
    gone = say(w, w.bob, "@alice.pi and this")
    assert [m["event_id"] for m in wait(w, pi, c)["mentions"]] == [ok["event_id"], gone["event_id"]]
    w.s.remove_member(w.alice, w.room, "bob")                                    # authority is checked at delivery
    assert wait(w, pi, c)["mentions"] == []


# ---------------------------------------------------------------- review fix: wake-ups obey the task rules

def give(w, who, aid, text="work"):
    return w.s.create_task(who, w.room, {"target_agent_id": aid, "text": text})["task_id"]


def test_two_agents_cannot_ping_pong_wake_each_other(w):
    a, a_id = agent(w, "alice", "pi", actions=("read", "post", "execute", "instruct"))
    b, b_id = agent(w, "bob", "codex", actions=("read", "post", "execute", "instruct"))
    ca, cb = wait(w, a, -1)["mention_cursor"], wait(w, b, -1)["mention_cursor"]
    w.s.update_task(a, give(w, w.alice, a_id), {"status": "accepted"})      # a person gave A work
    say(w, a, "@bob.codex please look")                                       # A, holding a task, wakes B
    assert len(wait(w, b, cb)["mentions"]) == 1
    say(w, b, "@alice.pi back to you")                                        # B holds no task: wakes nobody
    say(w, b, "@alice.pi again")
    assert wait(w, a, ca)["mentions"] == []


def test_an_agent_cannot_wake_the_agent_that_gave_it_its_task(w):
    a, a_id = agent(w, "alice", "pi", actions=("read", "post", "execute", "instruct"))
    b, b_id = agent(w, "bob", "codex", actions=("read", "post", "execute", "instruct"))
    w.s.update_task(a, give(w, w.alice, a_id), {"status": "accepted"})
    t = w.s.create_task(a, w.room, {"target_agent_id": b_id, "text": "sub", "parent_task_id":
                                    [x for x in w.s._tasks if w.s._tasks[x]["target"] == a_id][0]})["task_id"]
    w.s.update_task(b, t, {"status": "accepted"})
    ca = wait(w, a, -1)["mention_cursor"]
    say(w, b, "@alice.pi done, your turn")                                    # straight back to the giver
    assert wait(w, a, ca)["mentions"] == []


def test_agent_wakes_are_budgeted_like_tasks(w):
    from herdr_engineering.control_plane.collab import TASK_RATE  # noqa: PLC0415
    w.s.message_limit = 500
    a, a_id = agent(w, "alice", "pi", actions=("read", "post", "execute", "instruct"))
    b, _ = agent(w, "bob", "codex")
    w.s.update_task(a, give(w, w.alice, a_id), {"status": "accepted"})
    cb = wait(w, b, -1)["mention_cursor"]
    for i in range(TASK_RATE + 10):
        say(w, a, f"@bob.codex step {i}")
    assert len(wait(w, b, cb, )["mentions"]) <= 20
    got, cur = 0, cb
    while True:
        out = wait(w, b, cur)
        if not out["mentions"]:
            break
        got, cur = got + len(out["mentions"]), out["mention_cursor"]
    assert got == TASK_RATE


def test_a_group_mention_wakes_at_most_the_fan_out_cap(w):
    from herdr_engineering.control_plane.collab_commands import WAKE_FANOUT  # noqa: PLC0415
    w.s.max_agents_per_user = 60
    w.s.max_agents_per_user = 60
    a, a_id = agent(w, "alice", "pi", actions=("read", "post", "execute", "instruct"))
    others = [agent(w, "bob", "codex") for _ in range(WAKE_FANOUT + 3)]
    cursors = [wait(w, pr, -1)["mention_cursor"] for pr, _ in others]
    w.s.update_task(a, give(w, w.alice, a_id), {"status": "accepted"})
    say(w, a, "@agents status please")
    woken = sum(1 for (pr, _), c in zip(others, cursors, strict=True) if wait(w, pr, c)["mentions"])
    assert woken == WAKE_FANOUT
    say(w, w.bob, "@all hello")                                               # a person's group mention: capped too
    woken = sum(1 for (pr, _), c in zip(others, cursors, strict=True) if len(wait(w, pr, c)["mentions"]) == 2)
    assert woken <= WAKE_FANOUT


def test_a_person_still_wakes_an_agent_within_a_per_target_budget(w):
    from herdr_engineering.control_plane.collab import TASK_RATE  # noqa: PLC0415
    w.s.message_limit = 500
    pi, _ = agent(w, "alice", "pi")
    c = wait(w, pi, -1)["mention_cursor"]
    for i in range(TASK_RATE + 5):
        say(w, w.bob if i % 2 else w.alice, f"@alice.pi {i}")
    got, cur = 0, c
    while (out := wait(w, pi, cur))["mentions"]:
        got, cur = got + len(out["mentions"]), out["mention_cursor"]
    assert got == TASK_RATE


def test_a_mention_cursor_far_in_the_past_is_clamped(w):
    from herdr_engineering.control_plane.collab_commands import WAKE_SCAN  # noqa: PLC0415
    pi, _ = agent(w, "alice", "pi")
    old = say(w, w.bob, "@alice.pi ancient")
    w.s.message_limit = 10 ** 6
    for i in range(WAKE_SCAN + 5):
        w.s._commit("decision", {"room_id": w.room, "project_id": w.s._rooms[w.room]["project_id"],
                                 "sender_key": "user:bob", "sender_kind": "user", "sender_id": "bob",
                                 "idempotency_key": f"z{i}", "fingerprint": "f", "decision": "x",
                                 "actor_user_id": "bob", "actor_agent_id": None})
    out = wait(w, pi, 0)
    assert old["event_id"] not in [m["event_id"] for m in out["mentions"]]


def test_a_handoff_names_who_posted_it_when_that_is_not_the_from_party(w):
    pi, _ = agent(w, "alice", "pi")
    agent(w, "bob", "codex")
    c = wait(w, pi, -1)["mention_cursor"]
    ev = say(w, w.bob, "/handoff @bob.codex @alice.pi finish it")
    assert ev["handoff"]["posted_by"] == {"kind": "person", "name": "bob"}
    got = wait(w, pi, c)["mentions"]
    assert got and "posted by @bob" in got[0]["text"] and "from @bob.codex" in got[0]["text"]
    own = say(w, w.bob, "/handoff @alice take it")
    assert "posted_by" not in own["handoff"]                     # from is the poster: nothing extra

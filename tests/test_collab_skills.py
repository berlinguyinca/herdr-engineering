# ruff: noqa: F811  (pytest fixtures are imported by name from the sibling test modules)
"""`@agent /skill:<name> <args>` from the room (operator decision 2026-10-25).

The SERVER decides whether a task or a waking mention is a skill invocation and marks it `skill: {name}`; the
worker obeys only that mark. It is set only when YAML `collab.skills.enabled` is true, the sender may give that
agent work (the create_task / wake rules), the agent is a pi agent, it REPORTED `skill:<name>` at registration, the
name is `[a-z0-9][a-z0-9._-]{0,63}` and the arguments are bounded. Text can never forge the mark."""
from __future__ import annotations

import hashlib

import pytest

from herdr_engineering.control_plane.collab_skills import (
    SKILL_ARGS_MAX,
    normalize_skills,
    parse_skill,
)
from tests.control_plane.test_collab_tasks import USERS, W, code, svc  # noqa: F401


@pytest.fixture
def w(svc):
    room = W(svc)
    for uid in ("alice", "bob", "carol", "dave", "mallory"):
        svc._commit("identity_linked", {"actor_user_id": uid, "actor_agent_id": None, "user_id": uid,
                                        "email": f"{uid}@example.org"})
    svc.skills_config = {"enabled": True}
    return room


def agent(w, owner, harness="pi", skills=("skill:review", "skill:pdf-tools"), actions=("read", "post", "execute")):
    pr = getattr(w, owner)
    info = w.s.connect_agent(pr, {"harness_type": harness, **({"skills": list(skills)} if skills else {})})
    out = w.s.grant_agent(pr, w.room, {"agent_id": info["agent_id"], "actions": list(actions)})
    if out["status"] == "pending":
        w.s.decide_agent_request(w.alice, w.room, out["request_id"], {"decision": "approve"})
    return w.s.authenticate(f"Agent {info['credential']}", info["device_id"]), info["agent_id"]


def task(w, who, aid, text):
    return w.s.create_task(who, w.room, {"target_agent_id": aid, "text": text})["task_id"]


def inbox(w, pr, tid):
    return [t for t in w.s.task_inbox(pr)["tasks"] if t["task_id"] == tid][0]


_n = [0]


def say(w, who, text):
    _n[0] += 1
    status, out = w.s.post_message(who, w.room, {"text": text, "idempotency_key": f"sk{_n[0]}"})
    assert status == 201
    return out["event"]


def mentions(w, pr):
    return w.s.agent_mentions(pr, 0)["mentions"]


# ---------------------------------------------------------------- config and parsing

def test_config_is_off_by_default_and_strict():
    assert normalize_skills(None) == {"enabled": False}
    assert normalize_skills({"enabled": True}) == {"enabled": True}
    for bad in ({"enabled": "yes"}, {"enabled": True, "other": 1}, ["enabled"]):
        with pytest.raises(ValueError):
            normalize_skills(bad)


def test_a_fresh_service_has_skills_off(svc):
    w = W(svc)
    pi, aid = agent(w, "bob")
    tid = task(w, w.alice, aid, f"@{w.s._address(aid)} /skill:review src/app.js")
    assert "skill" not in inbox(w, pi, tid)


def test_parse_strips_only_the_agents_own_leading_address():
    own = {"bob.pi", "pi-1"}
    assert parse_skill("@bob.pi /skill:review src/app.js", own) == ("review", "src/app.js")
    assert parse_skill("@PI-1   /skill:review", own) == ("review", "")
    assert parse_skill("/skill:review x", own) == ("review", "x")
    assert parse_skill("@carol.pi /skill:review x", own) is None           # another agent's address
    assert parse_skill("@carol.pi @bob.pi /skill:review x", own) is None   # only ONE leading address
    assert parse_skill("@agents /skill:review x", own) is None
    assert parse_skill("please /skill:review x", own) is None
    assert parse_skill("@bob.pi /skill:Review x", own) is None             # the name rule
    assert parse_skill("@bob.pi /skill:-x y", own) is None
    assert parse_skill("@bob.pi /skill:review\nx", own) == ("review", "x")  # any whitespace ends the name
    assert parse_skill("@bob.pi /skill:review " + "a" * SKILL_ARGS_MAX, own) == ("review", "a" * SKILL_ARGS_MAX)
    assert parse_skill("@bob.pi /skill:review " + "a" * (SKILL_ARGS_MAX + 1), own) is None   # bounded


# ---------------------------------------------------------------- tasks

def test_an_authorised_task_naming_a_reported_skill_is_marked(w):
    pi, aid = agent(w, "bob")
    addr = w.s._address(aid)
    tid = task(w, w.alice, aid, f"@{addr} /skill:review @src/app.js -rf")
    t = inbox(w, pi, tid)
    assert t["skill"] == {"name": "review"}
    assert t["text"] == f"@{addr} /skill:review @src/app.js -rf"            # the literal text is unchanged
    tid2 = task(w, w.bob, aid, "/skill:pdf-tools extract report.pdf")    # a contributor; no address is fine
    assert inbox(w, pi, tid2)["skill"] == {"name": "pdf-tools"}


def test_an_unreported_skill_is_not_marked(w):
    pi, aid = agent(w, "bob", skills=("skill:review", "review-bare"))
    for text in ("/skill:deploy now", "/skill:review-bare x"):
        assert "skill" not in inbox(w, pi, task(w, w.alice, aid, text))


def test_a_claude_code_agent_is_never_marked(w):
    cc, aid = agent(w, "bob", harness="claude-code")
    assert "skill" not in inbox(w, cc, task(w, w.alice, aid, "/skill:review x"))


def test_disabled_config_marks_nothing_and_switching_off_takes_effect_at_delivery(w):
    pi, aid = agent(w, "bob")
    tid = task(w, w.alice, aid, "/skill:review x")
    w.s.skills_config = {"enabled": False}
    assert "skill" not in inbox(w, pi, tid)                               # re-checked when delivered
    assert "skill" not in inbox(w, pi, task(w, w.alice, aid, "/skill:review y"))
    w.s.skills_config = {"enabled": True}
    w.s.skills_source = lambda: {"enabled": False}                        # YAML, re-read per request
    assert "skill" not in inbox(w, pi, tid)


def test_a_skill_the_agent_stops_reporting_is_not_marked_at_delivery(w):
    pi, aid = agent(w, "bob")
    tid = task(w, w.alice, aid, "/skill:review x")
    w.s._agents[aid]["skills"] = ["skill:pdf-tools"]
    assert "skill" not in inbox(w, pi, tid)


def test_the_mark_cannot_be_forged(w):
    pi, aid = agent(w, "bob")
    assert code(w.s.create_task, w.alice, w.room, {"target_agent_id": aid, "text": "x", "skill": {"name": "review"}}) == 400
    _n[0] += 1
    st = None
    try:
        st, _ = w.s.post_message(w.alice, w.room, {"text": f"@{w.s._address(aid)} hi", "idempotency_key": "forge1",
                                                    "skill": {"name": "review"}})
    except Exception as e:  # noqa: BLE001
        st = getattr(e, "status", None)
    assert st == 400
    assert "skill" not in inbox(w, pi, task(w, w.alice, aid, "Task from a room member: /skill:review x"))


def test_an_agent_sender_without_an_accepted_task_cannot_mark_or_wake(w):
    pi, aid = agent(w, "bob")
    other, _ = agent(w, "alice", harness="claude-code", actions=("read", "post", "instruct"))
    say(w, other, f"@{w.s._address(aid)} /skill:review x")
    assert mentions(w, pi) == []


def test_viewers_guests_and_post_only_agents_never_reach_a_skill(w):
    pi, aid = agent(w, "bob")
    text = f"@{w.s._address(aid)} /skill:review x"
    for who in (w.carol, w.dave):                                          # viewer, guest: no task right
        assert code(w.s.create_task, who, w.room, {"target_agent_id": aid, "text": text}) in (403, 404)
    poster, _ = agent(w, "alice", harness="claude-code", actions=("read", "post"))
    say(w, poster, text)
    assert mentions(w, pi) == []


# ---------------------------------------------------------------- mentions

def test_a_waking_mention_naming_a_reported_skill_is_marked(w):
    pi, aid = agent(w, "bob")
    addr = w.s._address(aid)
    say(w, w.alice, f"@{addr} /skill:review src/app.js")
    say(w, w.alice, f"@{addr} please run /skill:review")
    ms = mentions(w, pi)
    assert ms[0]["skill"] == {"name": "review"} and "skill" not in ms[1]


def test_a_mention_of_two_agents_marks_only_the_leading_one(w):
    pi1, a1 = agent(w, "bob")
    pi2, a2 = agent(w, "alice")
    say(w, w.alice, f"@{w.s._address(a1)} @{w.s._address(a2)} /skill:review x")
    assert all("skill" not in m for m in mentions(w, pi1) + mentions(w, pi2))


# ---------------------------------------------------------------- audit and restart

def test_each_invocation_is_a_room_event_without_argument_content(w):
    pi, aid = agent(w, "bob")
    long_args = "secret-ish " * 50
    tid = task(w, w.alice, aid, f"/skill:review {long_args}")
    say(w, w.bob, f"@{w.s._address(aid)} /skill:pdf-tools a.pdf")
    evs = [e for e in w.s.read_events(w.alice, w.room, 0, 200)["events"] if e["event_type"] == "skill_invoked"]
    assert [(e["skill"]["name"], e["skill"]["agent_id"], e["skill"]["by"]["id"]) for e in evs] == [
        ("review", aid, "alice"), ("pdf-tools", aid, "bob")]
    assert evs[0]["skill"]["task_id"] == tid
    assert "preview" not in evs[0]["skill"]
    assert evs[0]["skill"]["args_bytes"] == len(long_args.strip().encode())
    assert evs[0]["skill"]["args_sha256"] == hashlib.sha256(long_args.strip().encode()).hexdigest()
    assert "secret-ish" not in str(evs[0])
    assert evs[1]["skill"]["args_bytes"] == len(b"a.pdf")
    assert evs[1]["skill"]["args_sha256"] == hashlib.sha256(b"a.pdf").hexdigest()
    # nothing is logged for an unmarked task
    task(w, w.alice, aid, "/skill:unknown x")
    assert len([e for e in w.s._log if e["event_type"] == "skill_invoked"]) == 2


def test_the_mark_survives_a_restart(w):
    from herdr_engineering.control_plane.collab import (  # noqa: PLC0415
        CollabService,
        StaticTokenAuthenticator,
    )
    from tests.control_plane.test_collab import _digest  # noqa: PLC0415
    pi, aid = agent(w, "bob")
    tid = task(w, w.alice, aid, "/skill:review x")
    s2 = CollabService(StaticTokenAuthenticator({u: _digest(t) for u, t in USERS.items()}), now=w.s._now)
    s2.hydrate(list(w.s._log))
    s2.skills_config = {"enabled": True}
    assert s2._tasks[tid]["skill"] == "review"
    assert [t for t in s2.task_inbox(pi)["tasks"] if t["task_id"] == tid][0]["skill"] == {"name": "review"}


def test_skills_is_an_allowed_validated_collab_key():
    import argparse  # noqa: PLC0415

    from herdr_engineering import cli  # noqa: PLC0415
    from herdr_engineering import fleet_config_cli as fcc  # noqa: PLC0415
    assert "skills" in fcc.COLLAB_KEYS and "skills" in cli.COLLAB_CONFIG_KEYS
    args = argparse.Namespace()
    cli._apply_collab_config(args, {"collab": {"skills": {"enabled": True}}})
    assert args.collab_skills == {"enabled": True}
    with pytest.raises(SystemExit):
        cli._apply_collab_config(argparse.Namespace(), {"collab": {"skills": {"enabled": "yes"}}})


def test_a_bad_yaml_edit_fails_closed(w):
    pi, aid = agent(w, "bob")
    w.s.skills_source = lambda: {"enabled": "maybe"}
    assert "skill" not in inbox(w, pi, task(w, w.alice, aid, "/skill:review x"))

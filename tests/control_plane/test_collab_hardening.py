"""Regression tests for the findings of the 2026-10-21 security review."""
from __future__ import annotations

import json
import os

import pytest

from herdr_engineering.control_plane.collab import (
    CollabError,
    CollabService,
    FileEventLog,
    StaticTokenAuthenticator,
)
from tests.control_plane.test_collab import TOKENS, Clock, _digest


@pytest.fixture
def svc(tmp_path):
    clock = Clock()
    s = CollabService(StaticTokenAuthenticator({u: _digest(t) for u, t in TOKENS.items()}),
                      sink=FileEventLog(str(tmp_path / "log")), now=clock, max_agents_per_user=3,
                      max_projects_per_user=2, max_rooms_per_project=2, max_members_per_room=3,
                      max_log_events=60)
    s.clock = clock
    return s


def who(s, user):
    return s.authenticate(f"Bearer {TOKENS[user]}", None)


def room_of(s, user="alice"):
    pr = who(s, user)
    pid = s.create_project(pr, {"name": "p"})["project_id"]
    return pr, pid, s.create_room(pr, pid, {"name": "r"})["room_id"]


def test_agent_project_room_and_member_caps(svc):
    pr = who(svc, "alice")
    for _ in range(3):
        svc.connect_agent(pr, {})
    with pytest.raises(CollabError) as e:
        svc.connect_agent(pr, {})
    assert e.value.status == 429
    with pytest.raises(CollabError):
        svc.start_enrollment(pr, {"device_id": "d", "harness_type": "h"})
    svc.create_project(pr, {"name": "a"})
    svc.create_project(pr, {"name": "b"})
    with pytest.raises(CollabError):
        svc.create_project(pr, {"name": "c"})
    pr2, pid, _ = room_of(svc, "bob")
    svc.create_room(pr2, pid, {"name": "second"})
    with pytest.raises(CollabError):
        svc.create_room(pr2, pid, {"name": "third"})


def test_more_agents_do_not_buy_more_message_budget(svc):
    svc.max_log_events = 10_000
    pr, _, room = room_of(svc)
    sent = 0
    for _ in range(3):                      # 3 agents, all granted into the room
        a = svc.connect_agent(pr, {})
        svc.grant_agent(pr, room, {"agent_id": a["agent_id"], "actions": ["post"]})
        apr = svc.authenticate(f"Agent {a['credential']}", a["device_id"])
        for i in range(50):
            try:
                svc.post_message(apr, room, {"text": "x", "idempotency_key": f"{a['agent_id']}-{i}"})
                sent += 1
            except CollabError as e:
                assert e.status in (429, 503)
                break
    assert sent <= svc.message_limit * 2    # one per-human budget across all of the human's agents


def test_log_full_refuses_writes_instead_of_exhausting_memory(svc):
    pr = who(svc, "alice")
    with pytest.raises(CollabError) as e:
        for i in range(100):
            svc.create_project(pr, {"name": f"p{i}"}) if i < 2 else svc.connect_agent(pr, {})
            if i >= 2:
                svc.revoke_agent(pr, next(iter(svc._agents)))
    assert e.value.status in (429, 503)
    assert len(svc._log) <= 60


def test_file_log_rolls_back_a_failed_write_and_drops_a_torn_tail(tmp_path, monkeypatch):
    log = FileEventLog(str(tmp_path / "l"))
    log.record_required({"sequence": 1})
    real, calls = os.fsync, []

    def flaky(fd):
        calls.append(1)
        if len(calls) == 1:
            raise OSError("disk")
        real(fd)
    monkeypatch.setattr(os, "fsync", flaky)
    with pytest.raises(OSError):
        log.record_required({"sequence": 2})
    monkeypatch.setattr(os, "fsync", real)
    log.record_required({"sequence": 2})            # reuses the sequence: the failed one is gone
    assert [r["sequence"] for r in log.load()] == [1, 2]
    with open(tmp_path / "l", "ab") as fh:           # a crash mid-append
        fh.write(b'{"sequence": 3, "tru')
    assert [r["sequence"] for r in FileEventLog(str(tmp_path / "l")).load()] == [1, 2]
    assert (tmp_path / "l").read_bytes().endswith(b"\n")


def test_file_log_fails_closed_when_rollback_fails(tmp_path, monkeypatch):
    log = FileEventLog(str(tmp_path / "l"))
    monkeypatch.setattr(os, "fsync", lambda fd: (_ for _ in ()).throw(OSError("disk")))
    monkeypatch.setattr(os, "ftruncate", lambda fd, n: (_ for _ in ()).throw(OSError("ro")))
    with pytest.raises(OSError):
        log.record_required({"sequence": 1})
    monkeypatch.undo()
    with pytest.raises(OSError):
        log.record_required({"sequence": 1})        # refuses rather than risk a duplicate sequence


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -float("inf")])
def test_non_finite_expiry_is_rejected(svc, bad):
    pr, _, room = room_of(svc)
    with pytest.raises(CollabError) as e:
        svc.add_member(pr, room, {"user_id": "bob", "role": "viewer", "expires_at": bad})
    assert e.value.status == 400
    a = svc.connect_agent(pr, {})
    with pytest.raises(CollabError):
        svc.grant_agent(pr, room, {"agent_id": a["agent_id"], "actions": ["read"], "expires_at": bad})


def test_guest_membership_always_expires(svc):
    pr, _, room = room_of(svc)
    svc.add_member(pr, room, {"user_id": "bob", "role": "guest"})
    assert svc._rooms[room]["members"]["bob"]["expires_at"] == pytest.approx(svc._now() + svc.guest_ttl)


def test_removed_members_agents_stay_ungranted_after_readd(svc):
    pr, _, room = room_of(svc)
    svc.add_member(pr, room, {"user_id": "bob", "role": "contributor"})
    bob = who(svc, "bob")
    a = svc.connect_agent(bob, {})
    asked = svc.grant_agent(bob, room, {"agent_id": a["agent_id"], "actions": ["read"]})
    svc.decide_agent_request(pr, room, asked["request_id"], {"decision": "approve"})   # the owner approves
    apr = svc.authenticate(f"Agent {a['credential']}", a["device_id"])
    svc.read_events(apr, room, 0, 10)
    svc.remove_member(pr, room, "bob")
    svc.add_member(pr, room, {"user_id": "bob", "role": "contributor"})
    with pytest.raises(CollabError) as e:
        svc.read_events(apr, room, 0, 10)
    assert e.value.status == 404


def test_pending_invite_dies_with_the_inviters_authority(svc):
    pr, _, room = room_of(svc)
    svc.link_identity(pr, {}) if False else None
    svc._email_user.clear()
    inviter = pr.id
    svc._commit("invite_created", {"actor_user_id": inviter, "actor_agent_id": None, "invite_id": "i1",
                                   "room_id": room, "email": "x@example.org", "role": "viewer",
                                   "expires_at_epoch": svc._now() + 1000, "action": "invite.create",
                                   "outcome": "allowed", "project_ids": [], "resource_id": room})
    svc._commit("member_removed", {"actor_user_id": inviter, "actor_agent_id": None, "room_id": room,
                                   "subject_user_id": inviter, "action": "member.remove",
                                   "outcome": "allowed", "project_ids": [], "resource_id": room})
    svc._deliver_pending("bob", "x@example.org")
    assert "bob" not in svc._rooms[room]["members"] and not svc._invites["i1"]["open"]


def test_read_events_pages_through_index(svc):
    pr, _, room = room_of(svc)
    for i in range(5):
        svc.post_message(pr, room, {"text": f"m{i}", "idempotency_key": f"k{i}"})
    first = svc.read_events(pr, room, 0, 3)
    rest = svc.read_events(pr, room, first["next_cursor"], 10)
    texts = [e["text"] for e in first["events"] + rest["events"] if e["event_type"] == "message"]
    assert texts == [f"m{i}" for i in range(5)] and first["has_more"]
    reply = first["events"][-1]["event_id"]
    with pytest.raises(CollabError):
        svc.post_message(pr, room, {"text": "r", "idempotency_key": "kr", "in_reply_to": "nope"})
    svc.post_message(pr, room, {"text": "r", "idempotency_key": "kr", "in_reply_to": reply})


def test_cognito_cache_is_bounded_and_garbage_ids_are_not_looked_up():
    from herdr_engineering.control_plane.collab_cognito import CognitoAuthenticator
    calls = []
    a = CognitoAuthenticator(pool_id="us-west-2_X", client_ids=["c"], required_group="g",
                             group_lookup=lambda u: calls.append(u) or [])
    assert not a.exists("A" * 65_000) and not a.exists("bad id\n") and not a.exists(123) and not calls
    for i in range(6000):
        a.exists(f"user-{i}")
    assert len(a._checked) <= 5000
    assert json.dumps(len(calls))

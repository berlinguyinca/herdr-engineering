# ruff: noqa: F811  (pytest fixtures are imported by name from the sibling test modules)
"""Helper requests: who may ask, the limits, subtree revoke and the panic switch.

alice owns the room and lets her agents start helpers; the operator (mallory on the tailnet
listener) runs them. A helper's credential is minted by the server when the runner claims it."""
from __future__ import annotations

import json

import pytest

from herdr_engineering.control_plane import collab
from herdr_engineering.control_plane.collab import (
    CollabService,
    FileEventLog,
    StaticTokenAuthenticator,
)
from tests.control_plane.test_collab import _digest
from tests.control_plane.test_collab_tasks import USERS, W, code, svc, user  # noqa: F401

SECRET_TASK = "review the diff in src/zebra_quokka.py please"


@pytest.fixture
def w(svc):
    x = W(svc)
    x.op = user(svc, "mallory")
    x.op.admin = True
    svc.set_room_profile(x.alice, x.room, {"allowed_families": ["claude-code", "codex"]})
    x.root, x.root_info = x.agent("alice", actions=("read", "post", "spawn_helpers"))
    return x


def ask(w, pr=None, **body):
    b = {"room_id": w.room, "role": "reviewer", "task": SECRET_TASK, **body}
    return w.s.request_helper(pr or w.root, b)


def run(w, helper_id):
    """The runner claims a helper: the server mints its agent, which can then act."""
    c = w.s.claim_helper(w.op, helper_id)
    pr = w.s.authenticate(f"Agent {c['credential']}", c["device_id"])
    return pr, c


# ------------------------------------------------------------------ who may ask
def test_request_is_recorded_with_server_side_parent(w):
    h = ask(w, parent_agent_id="agent_someone_else", parent="x")
    assert h["status"] == "requested" and h["role"] == "reviewer" and h["family"] == "claude-code"
    assert h["requester_agent_id"] == w.root.id and h["parent_agent_id"] == w.root.id
    assert h["owner"] == "alice" and h["room_id"] == w.room and h["depth"] == 1
    assert h["model"] is None and h["model_reason"] == "unpicked"
    assert h["expires_epoch"] - h["created_epoch"] == 7200


def test_without_spawn_helpers_is_403(w):
    a, _ = w.agent("alice", actions=("read", "post"), name="plain")
    assert code(ask, w, a) == 403


def test_people_cannot_request_helpers(w):
    assert code(w.s.request_helper, w.alice, {"room_id": w.room, "role": "reviewer", "task": "x"}) == 403


def test_no_access_to_the_room_is_404(w):
    assert code(ask, w, room_id=w.room2) == 404


def test_room_without_allowed_families_refuses(w):
    w.s.set_room_profile(w.alice, w.room, {})
    assert code(ask, w) == 403


def test_family_must_be_allowed_and_known(w):
    assert ask(w, family="codex")["family"] == "codex"
    assert code(ask, w, family="pi") == 403
    assert code(ask, w, family="bash") == 400


@pytest.mark.parametrize("role", ["admin", "", None, 3, "Reviewer"])
def test_role_must_be_one_of_five(w, role):
    assert code(ask, w, role=role) == 400


@pytest.mark.parametrize("task", ["", "   ", "a\0b", "a\x1bb", "a\rb", "a\x7fb", "a\x85b", "\ud800",
                                  "x" * 8001, 5, None])
def test_bad_task_text_is_rejected(w, task):
    assert code(ask, w, task=task) == 400


def test_newline_tab_and_max_length_are_fine(w):
    assert ask(w, task="line one\n\tline two")["status"] == "requested"
    assert ask(w, task="y" * 8000)["status"] == "requested"


@pytest.mark.parametrize("field", ["actions", "command", "env", "path", "secret"])
def test_no_command_path_env_or_grants_in_the_body(w, field):
    assert code(ask, w, **{field: ["grant_agent"]}) == 400


def test_bad_model_hint_is_400(w):
    assert code(ask, w, model=" bad hint ") == 400
    assert ask(w, model="gpt-x:1")["status"] == "requested"


# ------------------------------------------------------------------ limits
def test_fourth_live_helper_is_429(w):
    for _ in range(3):
        ask(w)
    with pytest.raises(collab.CollabError) as e:
        ask(w)
    assert e.value.status == 429 and "live" in e.value.message


def test_finished_or_cancelled_helpers_free_a_slot(w):
    hs = [ask(w) for _ in range(3)]
    w.s.cancel_helper(w.root, hs[0]["helper_id"])
    ask(w)


def test_room_profile_may_lower_live_limit(w):
    w.s.set_room_profile(w.alice, w.room, {"allowed_families": ["claude-code"], "limits": {"live_per_agent": 1}})
    ask(w)
    assert code(ask, w) == 429


def test_expired_helpers_stop_counting(w):
    for _ in range(3):
        ask(w)
    w.s.clock.t += 7201
    assert ask(w)["status"] == "requested"
    assert sum(h["status"] == "expired" for h in w.s.list_helpers_for_runner(w.op)["helpers"]) == 0


def test_depth_three_is_429(w):
    h1, _ = run(w, ask(w)["helper_id"])
    h2_rec = ask(w, h1)
    assert h2_rec["depth"] == 2 and h2_rec["parent_agent_id"] == h1.id
    h2, _ = run(w, h2_rec["helper_id"])
    with pytest.raises(collab.CollabError) as e:
        ask(w, h2)
    assert e.value.status == 429 and "depth" in e.value.message


def test_twenty_first_in_an_hour_is_429_per_person_across_rooms(w):
    w.s.set_room_profile(w.alice, w.room2, {"allowed_families": ["claude-code"]})
    other, _ = w.agent("alice", actions=("read", "spawn_helpers"), room=w.room2, name="other")
    for i in range(20):
        h = ask(w, other, room_id=w.room2) if i % 2 else ask(w)
        w.s.cancel_helper(w.alice, h["helper_id"])           # cancelled ones still count
    with pytest.raises(collab.CollabError) as e:
        ask(w)
    assert e.value.status == 429 and "hour" in e.value.message
    assert code(ask, w, other, room_id=w.room2) == 429
    w.s.clock.t += 3601
    ask(w)


def test_global_ceiling(w, monkeypatch):
    monkeypatch.setattr(collab, "HELPERS_GLOBAL_LIVE", 2)
    b, _ = w.agent("alice", actions=("read", "spawn_helpers"), name="second")
    ask(w)
    ask(w, b)
    with pytest.raises(collab.CollabError) as e:
        ask(w)
    assert e.value.status == 429 and "global" in e.value.message


def test_global_ceiling_default_is_24():
    assert collab.HELPERS_GLOBAL_LIVE == 24


# ------------------------------------------------------------------ helpers cannot widen their reach
def test_helper_cannot_name_another_room_or_gain_grants(w):
    w.s.set_room_profile(w.alice, w.room2, {"allowed_families": ["claude-code"]})
    w.s.grant_agent(w.alice, w.room2, {"agent_id": w.root.id, "actions": ["read", "spawn_helpers"]})
    h, c = run(w, ask(w)["helper_id"])
    assert set(c["actions"]) == {"read", "post", "spawn_helpers"}
    assert [r["room_id"] for r in w.s.agent_rooms(h)["rooms"]] == [w.room]
    assert code(ask, w, h, room_id=w.room2) == 404
    assert code(ask, w, h, actions=["read", "post", "instruct", "execute"]) == 400


def test_helper_grant_is_only_what_the_parent_has_now(w):
    rec = ask(w)
    w.s.grant_agent(w.alice, w.room, {"agent_id": w.root.id, "actions": ["read", "spawn_helpers"]})
    _, c = run(w, rec["helper_id"])
    assert set(c["actions"]) == {"read", "spawn_helpers"}


def test_claim_rechecks_the_parent_and_the_profile(w):
    a = ask(w)
    b = ask(w, family="codex")
    w.s.set_room_profile(w.alice, w.room, {"allowed_families": ["claude-code"]})
    assert code(w.s.claim_helper, w.op, b["helper_id"]) == 409
    w.s.grant_agent(w.alice, w.room, {"agent_id": w.root.id, "actions": ["read"]})
    assert code(w.s.claim_helper, w.op, a["helper_id"]) == 409
    assert w.s.helper_status(w.root, a["helper_id"])["status"] == "cancelled"


# ------------------------------------------------------------------ runner side
def test_runner_lists_claims_once_and_finishes(w):
    rec = ask(w)
    rows = w.s.list_helpers_for_runner(w.op)
    assert rows["paused"] is False and rows["helpers"][0]["task"] == SECRET_TASK
    h, c = run(w, rec["helper_id"])
    assert c["helper_agent_id"] == h.id and c["status"] == "claimed" and c["task"] == SECRET_TASK
    assert c["requester"]["name"] == w.root_info["name"]
    assert code(w.s.claim_helper, w.op, rec["helper_id"]) == 409
    w.s.finish_helper(w.op, rec["helper_id"], {"status": "running"})
    assert w.s.helper_status(w.root, rec["helper_id"])["status"] == "running"
    w.s.finish_helper(w.op, rec["helper_id"], {"status": "done", "result": "looks fine\n"})
    out = w.s.helper_result(w.root, rec["helper_id"])
    assert out["status"] == "done" and out["result"] == "looks fine\n" and out["untrusted"] is True
    assert code(w.s.authenticate, f"Agent {c['credential']}", c["device_id"]) == 401   # its life is over
    assert code(w.s.finish_helper, w.op, rec["helper_id"], {"status": "done"}) == 409


def test_helper_credential_lives_no_longer_than_the_helper(w):
    rec = ask(w)
    _, c = run(w, rec["helper_id"])
    assert c["expires_at"] <= rec["expires_epoch"]
    w.s.clock.t += 7201
    assert code(w.s.authenticate, f"Agent {c['credential']}", c["device_id"]) == 401


def test_rotating_a_helper_credential_cannot_outlive_the_helper(w):
    rec = ask(w)
    h, _ = run(w, rec["helper_id"])
    out = w.s.rotate_credential(h)
    assert out["expires_at"] <= rec["expires_epoch"]


def test_runner_methods_are_operator_only(w):
    rec = ask(w)
    for pr in (w.alice, w.root):
        assert code(w.s.list_helpers_for_runner, pr) == 404
        assert code(w.s.claim_helper, pr, rec["helper_id"]) == 404
        assert code(w.s.finish_helper, pr, rec["helper_id"], {"status": "failed"}) == 404
        assert code(w.s.set_helpers_paused, pr, True) == 404


def test_status_and_result_only_for_the_requester_or_its_owner(w):
    rec = ask(w)
    assert w.s.helper_status(w.alice, rec["helper_id"])["status"] == "requested"
    bob_agent, _ = w.agent("bob", actions=("read",))
    for pr in (w.bob, bob_agent, w.carol):
        assert code(w.s.helper_status, pr, rec["helper_id"]) == 404
        assert code(w.s.helper_result, pr, rec["helper_id"]) == 404
        assert code(w.s.cancel_helper, pr, rec["helper_id"]) == 404
    assert code(w.s.helper_status, w.root, "nope") == 404


def test_finish_result_is_size_limited_and_cleaned(w):
    rec = ask(w)
    run(w, rec["helper_id"])
    w.s.finish_helper(w.op, rec["helper_id"], {"status": "failed", "result": "a\0b" + "z" * 100_000})
    out = w.s.helper_result(w.root, rec["helper_id"])
    assert "\0" not in out["result"] and len(out["result"]) <= collab.MAX_HELPER_RESULT


# ------------------------------------------------------------------ a helper holds no more than its parent, live
def test_narrowing_the_parent_narrows_its_helper(w):
    h, c = run(w, ask(w)["helper_id"])
    assert w.s.post_message(h, w.room, {"text": "hi", "idempotency_key": "k1"})[0] == 201
    w.s.grant_agent(w.alice, w.room, {"agent_id": w.root.id, "actions": ["read"]})
    assert code(w.s.post_message, h, w.room, {"text": "hi", "idempotency_key": "k1"}) == 403
    assert code(ask, w, h) == 403


def test_narrowing_a_middle_helper_narrows_its_child(w):
    h1, c1 = run(w, ask(w)["helper_id"])
    h2, _ = run(w, ask(w, h1)["helper_id"])
    w.s.grant_agent(w.alice, w.room, {"agent_id": c1["helper_agent_id"], "actions": ["read", "spawn_helpers"]})
    assert code(w.s.post_message, h2, w.room, {"text": "hi", "idempotency_key": "k1"}) == 403
    assert w.s.read_events(h2, w.room, 0, 1)["events"] is not None      # still reads: read survived
    assert code(ask, w, h2) == 429                                     # depth, but spawn_helpers is still in the chain


def test_a_helper_cannot_be_granted_into_another_room(w):
    _, c = run(w, ask(w)["helper_id"])
    aid = c["helper_agent_id"]
    assert code(w.s.grant_agent, w.alice, w.room2, {"agent_id": aid, "actions": ["read"]}) == 403


def test_a_helper_request_into_another_room_cannot_be_approved(w):
    # bob's helper; bob asks alice's room2 for access for it, which alice may not approve
    w.s.add_member(w.alice, w.room2, {"user_id": "bob", "role": "contributor"})
    b, _ = w.agent("bob", actions=("read", "spawn_helpers"))
    _, c = run(w, ask(w, b)["helper_id"])
    rid = "grant_fake"
    w.s._commit("agent_access_requested", {
        "actor_user_id": "bob", "actor_agent_id": None, "project_id": w.s._rooms[w.room2]["project_id"],
        "room_id": w.room2, "agent_id": c["helper_agent_id"], "request_id": rid, "actions": ["read"],
        "created_epoch": w.s.clock.t, "action": "agent.request", "outcome": "allowed", "project_ids": [],
        "resource_id": c["helper_agent_id"]})
    assert code(w.s.decide_agent_request, w.alice, w.room2, rid, {"decision": "approve"}) == 403
    assert code(w.s.grant_agent, w.bob, w.room2, {"agent_id": c["helper_agent_id"], "actions": ["read"]}) == 403


# ------------------------------------------------------------------ revoke and cancel end the whole subtree
def test_revoking_the_parent_ends_every_descendant(w):
    h1, c1 = run(w, ask(w)["helper_id"])
    h2, c2 = run(w, ask(w, h1)["helper_id"])
    waiting = ask(w, h2, role="successor")              # a successor at depth 2 is still in the subtree
    queued = ask(w)
    w.s.revoke_agent(w.alice, w.root.id)
    for c in (c1, c2):
        assert code(w.s.authenticate, f"Agent {c['credential']}", c["device_id"]) == 401
    for hid in (waiting["helper_id"], queued["helper_id"], c1["helper_id"], c2["helper_id"]):
        assert w.s.helper_status(w.alice, hid)["status"] == "cancelled"
    assert w.s.list_helpers_for_runner(w.op)["helpers"] == []


def test_removing_the_parent_from_the_room_ends_its_helpers_there(w):
    _, c = run(w, ask(w)["helper_id"])
    queued = ask(w)
    w.s.remove_agent_from_room(w.alice, w.room, w.root.id)
    assert code(w.s.authenticate, f"Agent {c['credential']}", c["device_id"]) == 401
    assert w.s.helper_status(w.alice, queued["helper_id"])["status"] == "cancelled"


def test_parent_credential_expiry_ends_the_subtree_from_housekeeping(w):
    _, c1 = run(w, ask(w)["helper_id"])
    w.s._agents[w.root.id]["expires_at"] = w.s.clock.t + 10      # the root's credential runs out first
    w.s.clock.t += 11
    w.s.agent_rooms(w.alice)                                     # any housekeeping call, no helper call
    assert w.s._helpers[c1["helper_id"]]["status"] == "cancelled"
    assert w.s._helpers[c1["helper_id"]]["reason"] == "parent expired"
    assert w.s._agents[c1["helper_agent_id"]]["status"] == "revoked"


def test_owner_losing_the_room_or_being_disabled_frees_the_slots(w):
    b, _ = w.agent("bob", actions=("read", "spawn_helpers"))
    rec = ask(w, b)
    _, c = run(w, rec["helper_id"])
    w.s.remove_member(w.alice, w.room, "bob")
    assert w.s._helpers[rec["helper_id"]]["status"] == "cancelled"
    assert w.s._agents[c["helper_agent_id"]]["status"] == "revoked"


def test_an_agent_minted_by_an_interrupted_claim_is_revoked(w):
    rec = ask(w)
    real = w.s._commit
    def cut(t, p):
        if t == "helper_claimed":
            raise collab.CollabError(503, "collaboration log unavailable")
        return real(t, p)
    w.s._commit = cut
    assert code(w.s.claim_helper, w.op, rec["helper_id"]) == 503
    w.s._commit = real
    orphan = next(a for a in w.s._helper_minted)
    assert w.s._agents[orphan]["status"] == "active"
    w.s.list_helpers_for_runner(w.op)
    assert w.s._agents[orphan]["status"] == "revoked"


def test_runner_error_text_never_reaches_an_audited_event(w):
    rec = ask(w)
    run(w, rec["helper_id"])
    w.s.finish_helper(w.op, rec["helper_id"], {"status": "failed", "error": "sk-NOTE kangaroo at /home/x"})
    assert w.s.helper_status(w.root, rec["helper_id"])["error"] == "failed"
    assert "kangaroo" in w.s.helper_result(w.root, rec["helper_id"])["error_text"]
    audited = [e for e in w.s._log if "action" in e["payload"]]
    assert all("kangaroo" not in json.dumps(e["payload"]) for e in audited)
    rec2 = ask(w)
    run(w, rec2["helper_id"])
    w.s.finish_helper(w.op, rec2["helper_id"], {"status": "failed", "error": "timeout"})
    assert w.s.helper_status(w.root, rec2["helper_id"])["error"] == "timeout"


def test_cancel_ends_the_helper_and_its_children(w):
    rec = ask(w)
    h1, c1 = run(w, rec["helper_id"])
    _, c2 = run(w, ask(w, h1)["helper_id"])
    w.s.cancel_helper(w.root, rec["helper_id"])
    for c in (c1, c2):
        assert code(w.s.authenticate, f"Agent {c['credential']}", c["device_id"]) == 401


def test_kicking_the_owner_ends_their_helpers(tmp_path):
    from tests.control_plane.test_collab import Clock
    from tests.control_plane.test_collab_approval import OpenAuth, who
    s = CollabService(OpenAuth(), sink=FileEventLog(str(tmp_path / "log")), now=Clock(),
                      approval=collab.ApprovalPolicy(auto_groups=["local.dev"]))
    lab, op = who(s, "lab"), who(s, "op")
    op.admin = True
    pid = s.create_project(lab, {"name": "p"})["project_id"]
    room = s.create_room(lab, pid, {"name": "r"})["room_id"]
    s.set_room_profile(lab, room, {"allowed_families": ["claude-code"]})
    info = s.connect_agent(lab, {})
    s.grant_agent(lab, room, {"agent_id": info["agent_id"], "actions": ["read", "spawn_helpers"]})
    a = s.authenticate(f"Agent {info['credential']}", info["device_id"])
    rec = s.request_helper(a, {"room_id": room, "role": "worker", "task": "t"})
    c = s.claim_helper(op, rec["helper_id"])
    assert s.dispatch("POST", ["admin", "users", "lab", "kick"], {}, {}, "", None, operator="op")[0] == 200
    assert s.list_helpers_for_runner(op)["helpers"] == []
    assert s._helpers[rec["helper_id"]]["status"] == "cancelled"
    assert s._agents[c["helper_agent_id"]]["status"] == "revoked"


# ------------------------------------------------------------------ successor
def test_successor_replaces_parent_at_the_same_depth(w):
    h1, _ = run(w, ask(w)["helper_id"])
    s = ask(w, h1, role="successor")
    assert s["depth"] == 1 and s["parent_agent_id"] == w.root.id and s["chain"] == 1
    hs, _ = run(w, s["helper_id"])
    assert w.s.agent_rooms(h1)["rooms"]                  # the parent is not revoked here; it ends itself
    w.s.finish_helper(w.op, s["helper_id"], {"status": "running"})
    w.s.revoke_agent(w.alice, h1.id)                       # revoking a predecessor that is still active ends it
    assert not w.s.still_valid(hs)
    assert w.s.helper_status(w.alice, s["helper_id"])["status"] == "cancelled"


def test_revoking_a_finished_predecessor_leaves_its_successor_alone(w):
    rec = ask(w)
    h1, _ = run(w, rec["helper_id"])
    s = ask(w, h1, role="successor")
    hs, _ = run(w, s["helper_id"])
    w.s.finish_helper(w.op, s["helper_id"], {"status": "running"})
    w.s.finish_helper(w.op, rec["helper_id"], {"status": "done"})      # the hand-over
    w.s.revoke_agent(w.alice, h1.id)
    assert w.s.still_valid(hs)
    assert w.s.helper_status(w.alice, s["helper_id"])["status"] == "running"


def test_a_successor_not_yet_running_dies_with_a_revoked_parent(w):
    h1, _ = run(w, ask(w)["helper_id"])
    queued = ask(w, h1, role="successor")
    claimed = ask(w, h1, role="successor")
    hs, _ = run(w, claimed["helper_id"])
    w.s.revoke_agent(w.alice, h1.id)
    assert not w.s.still_valid(hs)
    assert {w.s.helper_status(w.alice, x["helper_id"])["status"] for x in (queued, claimed)} == {"cancelled"}


def test_a_successor_starts_after_its_predecessor_finished(w):
    rec = ask(w)
    h1, _ = run(w, rec["helper_id"])
    s = ask(w, h1, role="successor")
    w.s.finish_helper(w.op, rec["helper_id"], {"status": "done", "result": "handoff written"})
    assert not w.s.still_valid(h1)
    hs, c = run(w, s["helper_id"])
    assert set(c["actions"]) == {"read", "post", "spawn_helpers"} and c["parent_agent_id"] == w.root.id
    assert w.s.still_valid(hs)


def test_root_successor_has_no_parent_and_ends_when_the_root_is_revoked(w):
    s = ask(w, role="successor")
    assert s["depth"] == 0 and s["parent_agent_id"] is None
    hs, _ = run(w, s["helper_id"])
    w.s.finish_helper(w.op, s["helper_id"], {"status": "running"})
    w.s.revoke_agent(w.alice, w.root.id)
    assert not w.s.still_valid(hs)
    assert w.s.helper_status(w.alice, s["helper_id"])["status"] == "cancelled"


def test_successor_chain_of_six_is_refused(w):
    cur = w.root
    for n in range(1, 6):
        rec = ask(w, cur, role="successor")
        assert rec["chain"] == n
        cur, _ = run(w, rec["helper_id"])
    with pytest.raises(collab.CollabError) as e:
        ask(w, cur, role="successor")
    assert e.value.status == 429 and "successor" in e.value.message
    assert ask(w, cur)["chain"] == 0                       # an ordinary helper is still fine


# ------------------------------------------------------------------ panic switch
def test_pause_refuses_new_requests_and_stops_running_ones(w):
    rec = ask(w)
    _, c = run(w, rec["helper_id"])
    queued = ask(w)
    w.s.set_helpers_paused(w.op, True)
    with pytest.raises(collab.CollabError) as e:
        ask(w)
    assert e.value.status == 503 and e.value.message == "helpers are paused"
    assert code(w.s.claim_helper, w.op, queued["helper_id"]) == 503
    rows = w.s.list_helpers_for_runner(w.op)
    assert rows["paused"] is True and rows["helpers"] == []
    assert code(w.s.authenticate, f"Agent {c['credential']}", c["device_id"]) == 401
    w.s.set_helpers_paused(w.op, False)
    assert ask(w)["status"] == "requested"


def test_pause_routes_are_operator_listener_only(w):
    def call(method, parts, body=None, operator=None, auth=""):
        return w.s.dispatch(method, parts, {}, body or {}, auth, None, operator=operator)
    bearer = f"Bearer {USERS['mallory']}"
    assert call("POST", ["fleet", "helpers", "pause"], {"paused": True}, auth=bearer)[0] == 404
    assert call("GET", ["fleet", "helpers"], auth=bearer)[0] == 404
    assert call("POST", ["fleet", "helpers", "pause"], {"paused": "yes"}, operator="mallory")[0] == 400
    assert call("POST", ["fleet", "helpers", "pause"], {"paused": True}, operator="mallory") == (200, {"paused": True})
    assert call("GET", ["fleet", "helpers"], operator="mallory")[1]["paused"] is True
    call("POST", ["fleet", "helpers", "pause"], {"paused": False}, operator="mallory")
    rec = ask(w)
    st, out = call("POST", ["fleet", "helpers", rec["helper_id"], "claim"], operator="mallory")
    assert st == 200 and out["credential"].startswith("hca_")
    assert call("POST", ["fleet", "helpers", rec["helper_id"], "claim"], auth=bearer)[0] == 404
    st, out = call("POST", ["fleet", "helpers", rec["helper_id"], "finish"], {"status": "failed", "error": "boom"},
                   operator="mallory")
    assert st == 200 and out["status"] == "failed"


# ------------------------------------------------------------------ audit and room events never hold the task
def test_audit_and_feed_hold_ids_role_and_model_only(w):
    rec = ask(w)
    run(w, rec["helper_id"])
    w.s.finish_helper(w.op, rec["helper_id"], {"status": "done", "result": "RESULT-zebra"})
    pid = w.s._rooms[w.room]["project_id"]
    rows = [r for r in w.s.audit(w.alice, pid, 0, 500)["events"] if r["event_type"].startswith("helper_")]
    assert {r["event_type"] for r in rows} >= {"helper_requested", "helper_claimed", "helper_finished"}
    req = next(r for r in rows if r["event_type"] == "helper_requested")
    assert req["helper"] == {"helper_id": rec["helper_id"], "role": "reviewer", "model": None}
    feed = w.s.read_events(w.alice, w.room, 0, 200)["events"]
    blob = json.dumps(rows) + json.dumps(feed)
    assert "zebra" not in blob
    audited = [e for e in w.s._log if e["payload"].get("action", "").startswith("helper.")]
    assert audited and all("zebra" not in json.dumps(e["payload"]) for e in audited)


def test_refusals_are_audited(w):
    for _ in range(3):
        ask(w)
    code(ask, w)
    assert any(e["event_type"] == "audit_denied" and e["payload"]["action"] == "helper.request"
               for e in w.s._log)


# ------------------------------------------------------------------ restart
def test_hydrate_rebuilds_helpers_links_chain_and_pause(w, tmp_path):
    h1, _ = run(w, ask(w)["helper_id"])
    s_rec = ask(w, h1, role="successor")
    w.s.set_helpers_paused(w.op, True)
    fresh = CollabService(StaticTokenAuthenticator({u: _digest(t) for u, t in USERS.items()}),
                          sink=None, now=w.s.clock, max_agents_per_user=20)
    fresh.hydrate(list(w.s._log))
    rows = fresh.list_helpers_for_runner(w.op)
    assert rows["paused"] is True
    assert fresh._helpers[s_rec["helper_id"]]["chain"] == 1
    assert fresh._helpers[s_rec["helper_id"]]["task"] == SECRET_TASK
    assert fresh._helper_by_agent[h1.id]["depth"] == 1


def test_waiters_wake_on_helper_change(w):
    import threading
    seq, woke = w.s.seq, []
    t = threading.Thread(target=lambda: (w.s.wait_for_change(seq, 5.0), woke.append(w.s.seq)))
    t.start()
    ask(w)
    t.join(5)
    assert woke and woke[0] > seq

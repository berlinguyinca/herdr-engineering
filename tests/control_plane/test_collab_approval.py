"""Approval mode: anyone with a pool account may ask to join; nothing works until an approver or the
operator says yes. The operator can also disable and remove people."""
from __future__ import annotations

import pytest

from herdr_engineering.control_plane.collab import (
    ApprovalPolicy,
    CollabError,
    CollabService,
    FileEventLog,
)
from tests.control_plane.test_collab import Clock

TOKENS = {  # token -> (user id, cognito groups)
    "t-op": ("op", ["local.dev"]), "t-bob": ("bob", []), "t-carol": ("carol", []), "t-dave": ("dave", []),
    "t-adm": ("adm", ["Administrators"]), "t-mgr": ("mgr", ["Managers"]), "t-lab": ("lab", ["local.dev"]),
    "t-mal": ("mal", ["Users"]),
}


class OpenAuth:
    """What CognitoAuthenticator(open_signup=True) presents: any valid token, with its group claims."""

    def verify_claims(self, token):
        got = TOKENS.get(token)
        return {"user": got[0], "exp": 4e9, "groups": list(got[1])} if got else None

    def verify(self, token):
        c = self.verify_claims(token)
        return (c["user"], c["exp"]) if c else None

    def user_for(self, token):
        c = self.verify_claims(token)
        return c["user"] if c else None

    def exists(self, uid):
        return isinstance(uid, str) and bool(uid)


def code(fn, *a, **kw):
    with pytest.raises(CollabError) as e:
        fn(*a, **kw)
    return e.value.status


@pytest.fixture
def s(tmp_path):
    svc = CollabService(OpenAuth(), sink=FileEventLog(str(tmp_path / "log")), now=Clock(),
                        approval=ApprovalPolicy(auto_groups=["local.dev"], approver_groups=["Administrators", "Managers"]))
    return svc


def who(s, name):
    pr = s.authenticate(f"Bearer t-{name}", None)
    if name not in s._user_email:                     # the page links the verified email right after sign-in
        s._commit("identity_linked", {"actor_user_id": name, "actor_agent_id": None, "user_id": name,
                                      "email": f"{name}@example.org"})
    return pr


def call(s, method, parts, name, body=None, query=None, operator=None):
    return s.dispatch(method, parts, query or {}, body or {}, f"Bearer t-{name}" if name else "", None, operator=operator)


def test_a_new_person_waits_and_can_do_nothing_but_ask(s):
    pr = who(s, "bob")
    assert pr.status == "pending" and not pr.approver
    assert call(s, "GET", ["me"], "bob")[1]["status"] == "pending"
    for method, parts in (("GET", ["rooms"]), ("POST", ["projects"]), ("POST", ["agents", "connect"]),
                          ("GET", ["agents"]), ("POST", ["agents", "bulk"]), ("GET", ["admin", "users"])):
        st, out = call(s, method, parts, "bob", {"name": "x"})
        assert st == 403 and "approval" in out["error"], parts


def test_members_of_the_trusted_groups_are_approved_on_sight(s):
    assert who(s, "lab").status == "approved"
    adm = who(s, "adm")
    assert adm.status == "approved" and adm.approver
    assert who(s, "mgr").approver and not who(s, "lab").approver
    assert not who(s, "mal").approver and who(s, "mal").status == "pending"


def test_an_approver_sees_and_decides_only_people_who_are_waiting(s):
    who(s, "bob"); who(s, "lab")
    st, out = call(s, "GET", ["admin", "users"], "adm")
    assert st == 200 and [u["user_id"] for u in out["users"]] == ["bob"] and out["can_manage"] is False
    assert call(s, "GET", ["admin", "users"], "lab")[0] == 404 and call(s, "GET", ["admin", "users"], "bob")[0] == 403
    st, out = call(s, "POST", ["admin", "users", "bob", "approve"], "adm", {"reason": "joins the MS team"})
    assert st == 200 and out["status"] == "approved" and out["decided_by"] == "adm"
    assert who(s, "bob").status == "approved"
    # an approver can't touch anyone who is not waiting, and can't disable or remove
    assert call(s, "POST", ["admin", "users", "bob", "disable"], "adm")[0] == 403
    assert call(s, "POST", ["admin", "users", "lab", "deny"], "adm")[0] == 403
    assert call(s, "POST", ["admin", "users", "bob", "kick"], "mgr")[0] == 403
    assert call(s, "POST", ["admin", "users", "adm", "approve"], "adm")[0] in (400, 403)      # not yourself


def test_an_ordinary_member_cannot_approve_anyone(s):
    who(s, "bob")
    for name in ("lab", "mal"):
        assert call(s, "POST", ["admin", "users", "bob", "approve"], name)[0] in (403, 404)
    assert who(s, "bob").status == "pending"


def test_the_operator_disables_enables_and_kicks(s):
    for n in ("bob", "carol"):
        who(s, n)
        call(s, "POST", ["admin", "users", n, "approve"], None, operator="op")
    st, out = call(s, "GET", ["admin", "users"], None, operator="op")
    assert st == 200 and out["can_manage"] and {u["user_id"] for u in out["users"]} == {"op", "bob", "carol"}
    assert out["counts"]["approved"] == 3

    call(s, "POST", ["admin", "users", "bob", "disable"], None, {"reason": "left the project"}, operator="op")
    assert code(s.authenticate, "Bearer t-bob", None) == 403
    assert call(s, "POST", ["admin", "users", "bob", "enable"], None, operator="op")[1]["status"] == "approved"
    assert who(s, "bob").status == "approved"

    # carol has a room membership and an agent; kicking removes both and shuts the agent out
    pr = who(s, "carol")
    info = s.connect_agent(pr, {"harness_type": "claude-code"})
    agent = s.authenticate(f"Agent {info['credential']}", info["device_id"])
    op = who(s, "op")
    pid = s.create_project(op, {"name": "p"})["project_id"]
    room = s.create_room(op, pid, {"name": "r"})["room_id"]
    s.add_member(op, room, {"user_id": "carol", "role": "contributor"})
    assert s.get_room(pr, room)
    call(s, "POST", ["admin", "users", "carol", "kick"], None, {"reason": "left the lab"}, operator="op")
    assert code(s.authenticate, "Bearer t-carol", None) == 403
    assert code(s.authenticate, f"Agent {info['credential']}", info["device_id"]) == 401
    assert "carol" not in s._rooms[room]["members"]
    assert not s.still_valid(agent)
    # only the operator can bring them back
    assert call(s, "POST", ["admin", "users", "carol", "approve"], "adm")[0] == 403
    call(s, "POST", ["admin", "users", "carol", "approve"], None, operator="op")
    assert who(s, "carol").status == "approved"


def test_a_declined_person_stays_out_until_the_operator_changes_it(s):
    who(s, "mal")
    call(s, "POST", ["admin", "users", "mal", "deny"], "adm")
    assert code(s.authenticate, "Bearer t-mal", None) == 403
    assert call(s, "POST", ["admin", "users", "mal", "approve"], "mgr")[0] == 403
    call(s, "POST", ["admin", "users", "mal", "approve"], None, operator="op")
    assert who(s, "mal").status == "approved"


def test_disabled_people_agents_stop_working_and_an_approved_one_cannot_be_enabled_twice(s):
    who(s, "bob")
    call(s, "POST", ["admin", "users", "bob", "approve"], "adm")
    info = s.connect_agent(who(s, "bob"), {"harness_type": "pi"})
    s.authenticate(f"Agent {info['credential']}", info["device_id"])
    call(s, "POST", ["admin", "users", "bob", "disable"], None, operator="op")
    assert code(s.authenticate, f"Agent {info['credential']}", info["device_id"]) == 401
    assert call(s, "POST", ["admin", "users", "bob", "disable"], None, operator="op")[0] == 409
    call(s, "POST", ["admin", "users", "bob", "enable"], None, operator="op")
    assert call(s, "POST", ["admin", "users", "bob", "enable"], None, operator="op")[0] == 409
    s.authenticate(f"Agent {info['credential']}", info["device_id"])        # back, nothing was revoked


def test_the_operator_cannot_be_locked_out_or_act_on_themselves(s):
    call(s, "GET", ["me"], None, operator="op")
    assert call(s, "POST", ["admin", "users", "op", "kick"], None, operator="op")[0] == 400
    assert call(s, "POST", ["admin", "users", "nobody", "approve"], None, operator="op")[0] == 404
    assert call(s, "POST", ["admin", "users", "bob", "explode"], None, operator="op")[0] == 404
    assert call(s, "POST", ["admin", "users", "bob", "approve"], None, {"reason": "x" * 201}, operator="op")[0] in (400, 404)


def test_the_queue_is_bounded(s, monkeypatch):
    from herdr_engineering.control_plane import collab
    monkeypatch.setattr(collab, "MAX_PENDING_USERS", 2)
    TOKENS.update({f"t-u{i}": (f"u{i}", []) for i in range(3)})
    who(s, "u0"); who(s, "u1")
    assert code(s.authenticate, "Bearer t-u2", None) == 429
    assert who(s, "lab").status == "approved"                           # trusted groups are never queued


def test_decisions_survive_a_restart_and_people_already_using_it_keep_working(s, tmp_path):
    who(s, "bob")
    call(s, "POST", ["admin", "users", "bob", "approve"], "adm", {"reason": "ok"})
    who(s, "carol")
    log = list(s._log)
    again = CollabService(OpenAuth(), now=Clock(), approval=ApprovalPolicy(["local.dev"], ["Administrators"]))
    again.hydrate(log)
    assert who(again, "bob").status == "approved" and who(again, "carol").status == "pending"
    kinds = [e["event_type"] for e in log]
    assert "user_registered" in kinds and "user_decided" in kinds
    ev = next(e for e in log if e["event_type"] == "user_decided" and e["payload"]["user_id"] == "bob")
    assert ev["payload"]["decided_by"] == "adm" and ev["payload"]["action"] == "user.approve"

    # a service that ran before approval mode: its existing members are grandfathered in
    old = CollabService(OpenAuth(), now=Clock())
    pid = old.create_project(old.authenticate("Bearer t-lab", None), {"name": "p"})["project_id"]
    old.add_member(old.authenticate("Bearer t-lab", None), old.create_room(old.authenticate("Bearer t-lab", None), pid, {"name": "r"})["room_id"],
                   {"user_id": "bob", "role": "viewer"})
    fresh = CollabService(OpenAuth(), now=Clock(), approval=ApprovalPolicy(["local.dev"], ["Administrators"]))
    fresh.hydrate(list(old._log))
    assert who(fresh, "bob").status == "approved" and who(fresh, "lab").status == "approved"
    assert who(fresh, "dave").status == "pending"


def test_without_approval_mode_nothing_changes(tmp_path):
    plain = CollabService(OpenAuth(), now=Clock())
    assert plain.authenticate("Bearer t-bob", None).status == "approved"
    assert plain.dispatch("GET", ["admin", "users"], {}, {}, "Bearer t-bob", None)[0] == 404


def test_the_decision_log_is_for_the_operator_only(s):
    who(s, "bob")
    call(s, "POST", ["admin", "users", "bob", "approve"], "adm", {"reason": "ok"})
    call(s, "POST", ["admin", "users", "bob", "disable"], None, {"reason": "paused"}, operator="op")
    st, out = call(s, "GET", ["admin", "users", "log"], None, operator="op")
    assert st == 200
    acts = [(r["user_id"], r["action"], r["by"], r["reason"]) for r in out["log"] if r["user_id"] == "bob"]
    assert acts == [("bob", "user.disable", "op", "paused"), ("bob", "user.approve", "adm", "ok"), ("bob", "user.register", None, None)]
    assert call(s, "GET", ["admin", "users", "log"], "adm")[0] == 404


def test_nobody_is_approved_before_their_verified_email_is_known(s):
    s.authenticate("Bearer t-bob", None)                    # signed in, email not linked yet
    st, out = call(s, "POST", ["admin", "users", "bob", "approve"], "adm")
    assert st == 409 and "verified email" in out["error"]
    who(s, "bob")
    assert call(s, "POST", ["admin", "users", "bob", "approve"], "adm")[0] == 200

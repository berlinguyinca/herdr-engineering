# ruff: noqa: F811  (pytest fixtures are imported by name from the sibling test modules)
"""Helper fixes from the final review: dead code, IPv6 hosts, per-room model endpoints, per-person limits,
the pause in the audit log, what guests and agents see, and the successor hand-over (service side)."""
from __future__ import annotations

import pytest

from herdr_engineering.control_plane import collab
from tests.control_plane.test_collab_helpers import ask, run, w  # noqa: F401
from tests.control_plane.test_collab_tasks import USERS, code, svc  # noqa: F401


def call(w, method, parts, who=None, body=None, operator=None):
    auth = f"Bearer {USERS[who]}" if who else ""
    return w.s.dispatch(method, parts, {}, body or {}, auth, None, operator=operator)


# ------------------------------------------------------------------ dead code (review item 8)
def test_egress_denied_is_not_an_audited_code_any_more():
    assert "egress-denied" not in collab.HELPER_ERRORS


def test_wait_is_not_a_service_field(w):
    # helper_spawn's wait is handled by the MCP layer, which never passes it on
    assert code(ask, w, wait=True) == 400


def test_host_access_requests_are_listed_for_the_operator_only(w):
    assert code(w.s.list_egress, w.alice) == 404 and code(w.s.list_egress, w.alice, w.room) == 404
    assert w.s.list_egress(w.op)["requests"] == []


def test_a_person_lists_views_and_cancels_their_own_agents_helpers(w):
    rec = ask(w)
    st, out = call(w, "GET", ["helpers"], "alice")
    assert st == 200 and [h["helper_id"] for h in out["helpers"]] == [rec["helper_id"]]
    assert "task" not in out["helpers"][0] and "result" not in out["helpers"][0]
    st, one = call(w, "GET", ["helpers", rec["helper_id"]], "alice")
    assert st == 200 and one["status"] == "requested"
    for who in ("bob", "carol", "dave", "mallory"):                  # somebody else's helper: 404 everywhere
        assert call(w, "GET", ["helpers"], who)[1]["helpers"] == []
        assert call(w, "GET", ["helpers", rec["helper_id"]], who)[0] == 404
        assert call(w, "POST", ["helpers", rec["helper_id"], "cancel"], who)[0] == 404
    assert w.s._helpers[rec["helper_id"]]["status"] == "requested"
    st, out = call(w, "POST", ["helpers", rec["helper_id"], "cancel"], "alice")
    assert st == 200 and out["status"] == "cancelled"


def test_agents_have_no_rest_route_to_helpers(w):
    info = w.root_info
    auth = f"Agent {info['credential']}"
    for method, parts in (("GET", ["helpers"]), ("GET", ["helpers", "x"]), ("POST", ["helpers", "x", "cancel"])):
        assert w.s.dispatch(method, parts, {}, {}, auth, info["device_id"])[0] == 404


# ------------------------------------------------------------------ IPv6 hosts (review item 9)
V6 = "2606:4700:4700::1111"                    # a global address


def test_a_bracketed_ipv6_host_can_be_requested_approved_and_reaches_the_allowlist(w):
    r = w.s.request_egress(w.op, w.room, "a1", f"[{V6.upper()}]", 443)
    assert r["host_port"] == f"[{V6}]:443"
    w.s.decide_egress(w.op, r["id"], "approve")
    assert f"[{V6}]:443" in w.s.egress_allowlist(w.room)
    assert w.s.request_egress(w.op, w.room, "a1", f"[{V6}]", 443)["state"] == "allowed"
    out = w.s.promote_egress(w.op, w.room, f"[{V6}]:443")
    assert f"[{V6}]:443" in out["permanent_hosts"]
    assert f"[{V6}]:443" not in w.s.remove_egress(w.op, w.room, f"[{V6}]:443")["allowlist"]


@pytest.mark.parametrize("host", [V6, f"[{V6}", f"[{V6}]x", "[fe80::1%eth0]", "[::ffff:1.2.3.4%1]", "[]",
                                  "[1.2.3.4]", "[not-v6]"])
def test_odd_ipv6_spellings_are_refused(w, host):
    assert code(w.s.request_egress, w.op, w.room, "a1", host, 443) == 400


def test_ipv6_in_the_profile_is_canonical_and_internal_ones_are_refused(w):
    p = w.s.set_room_profile(w.alice, w.room, {"permanent_hosts": [f"[{V6.upper()}]:0443"], "services": [
        {"name": "v6", "upstream_url": f"https://[{V6}]:8443/v1", "header": "Authorization", "secret_name": "k"}]})
    assert p["permanent_hosts"] == [f"[{V6}]:443"] and p["services"][0]["upstream_url"].startswith("https://[")
    for bad in ("[::1]:443", "[fd00::1]:443", "[fe80::1]:443", "[::ffff:127.0.0.1]:443", "[64:ff9b::a00:1]:443"):
        assert code(w.s.set_room_profile, w.alice, w.room, {"permanent_hosts": [bad]}) == 400, bad
    for bad in ("https://[::1]/", "https://[fd00::1]/v1"):
        assert code(w.s.set_room_profile, w.alice, w.room, {"services": [
            {"name": "x", "upstream_url": bad, "header": "Authorization", "secret_name": "k"}]}) == 400, bad


# ------------------------------------------------------------------ per-person limits (review item 4)
def test_one_person_holds_at_most_six_live_helpers(w):
    agents = [w.root] + [w.agent("alice", actions=("read", "post", "spawn_helpers"), name=f"lead{i}")[0]
                         for i in range(2)]
    for a in agents[:2]:
        for _ in range(3):
            ask(w, a)
    with pytest.raises(collab.CollabError) as e:
        ask(w, agents[2])
    assert e.value.status == 429 and "person" in e.value.message
    assert collab.HELPERS_PER_OWNER_LIVE == 6
    bob_agent, _ = w.agent("bob", actions=("read", "post", "spawn_helpers"), name="bobs")
    assert ask(w, bob_agent)["status"] == "requested"                  # somebody else is not affected


def test_an_unclaimed_request_expires_after_ten_minutes_and_frees_its_slot(w):
    hs = [ask(w) for _ in range(3)]
    assert code(ask, w) == 429
    w.s.clock.t += collab.HELPER_CLAIM_WINDOW - 1
    assert code(ask, w) == 429
    w.s.clock.t += 2
    assert ask(w)["status"] == "requested"
    assert {w.s.helper_status(w.root, h["helper_id"])["status"] for h in hs} == {"expired"}
    assert w.s._helpers[hs[0]["helper_id"]]["reason"] == "not claimed in time"
    assert code(w.s.claim_helper, w.op, hs[0]["helper_id"]) == 409
    assert collab.HELPER_CLAIM_WINDOW == 600


def test_a_claimed_helper_does_not_expire_with_the_claim_window(w):
    rec = ask(w)
    run(w, rec["helper_id"])
    w.s.clock.t += collab.HELPER_CLAIM_WINDOW + 5
    assert w.s.helper_status(w.root, rec["helper_id"])["status"] == "claimed"


# ------------------------------------------------------------------ the panic switch in the audit log (review item 5)
def test_pause_and_resume_appear_in_every_projects_audit_with_who_when_and_state(w):
    w.s.clock.t += 5
    w.s.set_helpers_paused(w.op, True)
    w.s.set_helpers_paused(w.op, False)
    pid = w.s._rooms[w.room]["project_id"]
    rows = [e for e in w.s.audit(w.alice, pid, 0, 500)["events"] if e["event_type"] == "helpers_paused"]
    assert [(e["action"], e["paused"], e["actor_user_id"]) for e in rows] == [
        ("helpers.pause", True, "mallory"), ("helpers.resume", False, "mallory")]
    assert all(e["created_at"] for e in rows)


def test_pause_and_resume_appear_in_the_operators_user_log(tmp_path):
    from tests.control_plane.test_collab import Clock
    from tests.control_plane.test_collab_approval import OpenAuth, who
    s = collab.CollabService(OpenAuth(), sink=collab.FileEventLog(str(tmp_path / "log")), now=Clock(),
                             approval=collab.ApprovalPolicy(auto_groups=["local.dev"]))
    op = who(s, "op")
    op.admin = True
    s.set_helpers_paused(op, True)
    s.set_helpers_paused(op, False)
    log = s.user_log(op)["log"]
    rows = [r for r in log if str(r["action"]).startswith("helpers.")]
    assert [(r["action"], r["status"], r["by"], r["user_id"]) for r in rows] == [
        ("helpers.resume", "resumed", "op", "op"), ("helpers.pause", "paused", "op", "op")]     # newest first
    assert all(r["at"] for r in rows)


# ------------------------------------------------------------------ what guests and agents see (review item 10)
SVC = {"name": "gh", "upstream_url": "https://api.github.com/v3", "header": "Authorization", "secret_name": "gh_token"}


def test_guests_see_no_helper_events_and_their_cursor_still_moves_past_them(w):
    w.s.post_message(w.alice, w.room, {"text": "before", "idempotency_key": "k1"})
    first = w.s.read_events(w.dave, w.room, 0, 200)["next_cursor"]
    for _ in range(3):
        ask(w)                                                          # nothing but helper events after this
    page = w.s.read_events(w.dave, w.room, first, 200)
    assert page["events"] == [] and page["next_cursor"] > first         # never stuck on hidden events
    assert w.s.read_events(w.dave, w.room, page["next_cursor"], 200)["events"] == []
    small = w.s.read_events(w.dave, w.room, first, 1)
    assert small["events"] == [] and small["next_cursor"] > first
    everything = w.s.read_events(w.dave, w.room, 0, 200)["events"]
    assert not any(e["event_type"].startswith("helper") or "helper" in e for e in everything)
    assert any(e["event_type"].startswith("helper_") for e in w.s.read_events(w.carol, w.room, 0, 200)["events"])
    st = w.s.room_state(w.dave, w.room, -1, 0, 0)
    assert not any(e["event_type"].startswith("helper_") for e in st["events"])


def test_profile_detail_goes_to_members_names_only_to_agents_and_nothing_to_guests(w):
    w.s.set_room_profile(w.alice, w.room, {"allowed_families": ["claude-code"], "services": [SVC],
                                           "permanent_hosts": ["git.example.org:22"]})
    for who in (w.alice, w.bob, w.carol):
        p = w.s.get_room_profile(who, w.room)
        assert p["services"] == [SVC] and p["permanent_hosts"] == ["git.example.org:22"]
    assert code(w.s.get_room_profile, w.dave, w.room) == 404
    p = w.s.get_room_profile(w.root, w.room)                           # an agent with a read grant
    assert p["services"] == [{"name": "gh"}] and "permanent_hosts" not in p
    assert "api.github.com" not in repr(p) and "git.example.org" not in repr(p)
    assert p["allowed_families"] == ["claude-code"] and p["limits"]["depth"] == 2



# ------------------------------------------------------------------ successor hand-over (review item 7)
HANDOFF = "HANDOFF: tests 1-4 pass, continue with step 5 in notes.md"


def test_a_helper_predecessor_ends_when_its_successor_is_claimed_and_its_handoff_is_its_result(w):
    rec = ask(w, role="worker")
    h1, _ = run(w, rec["helper_id"])
    w.s.finish_helper(w.op, rec["helper_id"], {"status": "running"})
    s = ask(w, h1, role="successor", task=HANDOFF)
    hs, c = run(w, s["helper_id"])
    assert c["handoff"] == {"from_agent_id": h1.id, "from_helper_id": rec["helper_id"], "text": HANDOFF}
    assert not w.s.still_valid(h1) and w.s.still_valid(hs)             # the predecessor ended, its successor did not
    pred = w.s.helper_result(w.root, rec["helper_id"])                 # the artifact: what the tree parent reads
    assert pred["status"] == "done" and HANDOFF in pred["result"] and s["helper_id"] in pred["result"]
    assert w.s.helper_result(w.root, s["helper_id"])["status"] == "claimed"   # the tree parent follows the successor
    w.s.revoke_agent(w.alice, h1.id)                                   # revoking the handed-over predecessor: no effect
    assert w.s.still_valid(hs)
    assert code(w.s.finish_helper, w.op, rec["helper_id"], {"status": "done"}) == 409   # its runner's late finish


def test_other_waiting_successors_of_the_same_predecessor_are_superseded(w):
    h1, _ = run(w, ask(w)["helper_id"])
    first, second = ask(w, h1, role="successor", task=HANDOFF), ask(w, h1, role="successor", task="other")
    run(w, first["helper_id"])
    assert w.s.helper_status(w.root, second["helper_id"])["status"] == "cancelled"
    assert w.s._helpers[second["helper_id"]]["reason"] == "superseded"


def test_a_root_predecessor_is_not_ended_by_its_successor(w):
    s = ask(w, role="successor", task=HANDOFF)
    hs, c = run(w, s["helper_id"])
    assert c["handoff"] == {"from_agent_id": w.root.id, "from_helper_id": None, "text": HANDOFF}
    assert w.s.still_valid(w.root) and w.s.still_valid(hs)


def test_an_ordinary_helper_carries_no_handoff(w):
    _, c = run(w, ask(w)["helper_id"])
    assert c["handoff"] is None


# ------------------------------------------------------------------ profile changes reach running helpers (item 6)
def test_runner_rows_carry_the_allowed_families_and_a_profile_change_is_an_audited_code(w):
    rec = ask(w)
    row = next(h for h in w.s.list_helpers_for_runner(w.op)["helpers"] if h["helper_id"] == rec["helper_id"])
    assert row["profile"]["allowed_families"] == ["claude-code", "codex"]
    run(w, rec["helper_id"])
    w.s.finish_helper(w.op, rec["helper_id"], {"status": "failed", "error": "profile-changed"})
    assert w.s.helper_status(w.root, rec["helper_id"])["error"] == "profile-changed"


# ------------------------------------------------------------------ the operator cancels any helper
def test_the_operator_cancels_another_owners_helper_on_the_operator_listener(w):
    rec = ask(w)
    _, c = run(w, rec["helper_id"])
    child = ask(w, w.s.authenticate(f"Agent {c['credential']}", c["device_id"]))
    st, out = call(w, "POST", ["fleet", "helpers", rec["helper_id"], "cancel"], operator="mallory")
    assert st == 200 and out["status"] == "cancelled"
    assert w.s._helpers[child["helper_id"]]["status"] == "cancelled"       # the subtree goes too
    assert code(w.s.authenticate, f"Agent {c['credential']}", c["device_id"]) == 401
    pid = w.s._rooms[w.room]["project_id"]
    rows = [e for e in w.s.audit(w.alice, pid, 0, 500)["events"]
            if e["action"] == "helper.cancel" and e["resource_id"] == rec["helper_id"]]
    assert rows and rows[0]["actor_user_id"] == "mallory" and rows[0]["actor_agent_id"] is None


def test_nobody_else_reaches_the_operator_cancel(w):
    rec = ask(w)
    for who in ("bob", "mallory"):                                    # the public listener: 404, even for mallory
        assert call(w, "POST", ["fleet", "helpers", rec["helper_id"], "cancel"], who)[0] == 404
        assert call(w, "POST", ["helpers", rec["helper_id"], "cancel"], who)[0] == 404
    assert call(w, "POST", ["fleet", "helpers", "nope", "cancel"], operator="mallory")[0] == 404
    assert w.s._helpers[rec["helper_id"]]["status"] == "requested"

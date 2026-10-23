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

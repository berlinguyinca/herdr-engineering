# ruff: noqa: F811  (pytest fixtures are imported by name from the sibling test modules)
"""Host-access requests: an agent asks to reach a host, the operator approves for a while."""
from __future__ import annotations

import pytest

from herdr_engineering.control_plane.collab import FAMILY_ENDPOINTS
from tests.control_plane.test_collab_tasks import W, code, svc, user  # noqa: F401


@pytest.fixture
def w(svc):
    x = W(svc)
    x.op = user(svc, "mallory")          # the operator need not be a member of any room
    x.op.admin = True
    return x


def ask(w, host="api.example.org", port=443, room=None, agent="a1"):
    return w.s.request_egress(w.op, room or w.room, agent, host, port)


def test_unlisted_host_is_pending_then_approved_until_ttl(w):
    r = ask(w)
    assert r["state"] == "open" and r["host_port"] == "api.example.org:443"
    assert "api.example.org:443" not in w.s.egress_allowlist(w.room)
    out = w.s.decide_egress(w.op, r["id"], "approve", ttl_seconds=600)
    assert out["state"] == "approved"
    assert "api.example.org:443" in w.s.egress_allowlist(w.room)
    w.s.clock.t += 599
    assert "api.example.org:443" in w.s.egress_allowlist(w.room)
    w.s.clock.t += 2
    assert "api.example.org:443" not in w.s.egress_allowlist(w.room)


def test_default_ttl_is_a_day_and_requests_dedup(w):
    r = ask(w)
    assert ask(w)["id"] == r["id"] and len(w.s.list_egress(w.op)["requests"]) == 1
    w.s.decide_egress(w.op, r["id"], "approve")
    w.s.clock.t += 86399
    assert "api.example.org:443" in w.s.egress_allowlist(w.room)
    w.s.clock.t += 2
    assert "api.example.org:443" not in w.s.egress_allowlist(w.room)
    assert ask(w)["state"] == "open"                       # expired approval: ask again


def test_approved_host_reports_allowed_without_a_new_request(w):
    w.s.decide_egress(w.op, ask(w)["id"], "approve")
    assert ask(w)["state"] == "allowed" and len(w.s.list_egress(w.op)["requests"]) == 1


def test_denial_blocks_rerequest_for_an_hour(w):
    r = ask(w)
    assert w.s.decide_egress(w.op, r["id"], "deny")["state"] == "denied"
    assert ask(w) == {"state": "denied"}
    w.s.clock.t += 3599
    assert ask(w) == {"state": "denied"}
    assert len(w.s.list_egress(w.op)["requests"]) == 1     # no new request was made
    w.s.clock.t += 2
    assert ask(w)["state"] == "open"


def test_promote_makes_it_permanent_and_remove_takes_it_away(w):
    w.s.decide_egress(w.op, ask(w)["id"], "approve", ttl_seconds=60)
    w.s.promote_egress(w.op, w.room, "api.example.org:443")
    w.s.clock.t += 10_000
    assert "api.example.org:443" in w.s.egress_allowlist(w.room)
    assert "api.example.org:443" in w.s._profile(w.room)["permanent_hosts"]
    w.s.remove_egress(w.op, w.room, "api.example.org:443")
    assert "api.example.org:443" not in w.s.egress_allowlist(w.room)


def test_remove_also_revokes_a_live_approval(w):
    w.s.decide_egress(w.op, ask(w)["id"], "approve")
    w.s.remove_egress(w.op, w.room, "api.example.org:443")
    assert "api.example.org:443" not in w.s.egress_allowlist(w.room)


def test_allowlist_is_per_room_and_always_has_family_endpoints(w):
    w.s.decide_egress(w.op, ask(w)["id"], "approve")
    assert "api.example.org:443" not in w.s.egress_allowlist(w.room2)
    for hosts in FAMILY_ENDPOINTS.values():
        assert set(hosts) <= set(w.s.egress_allowlist(w.room2))
    assert FAMILY_ENDPOINTS["claude-code"] == ["api.anthropic.com:443"]


def test_permanent_hosts_from_the_profile_are_listed(w):
    w.s.set_room_profile(w.alice, w.room, {"permanent_hosts": ["bender.example.org:22"]})
    assert "bender.example.org:22" in w.s.egress_allowlist(w.room)


def test_only_the_operator_decides_promotes_removes_and_requests(w):
    r = ask(w)
    for who in (w.alice, w.bob, w.carol):                   # alice owns the room but is not the operator
        assert code(w.s.decide_egress, who, r["id"], "approve") == 404
        assert code(w.s.promote_egress, who, w.room, "api.example.org:443") == 404
        assert code(w.s.remove_egress, who, w.room, "api.example.org:443") == 404
        assert code(w.s.request_egress, who, w.room, "a1", "x.example.org", 443) == 404
    assert "api.example.org:443" not in w.s.egress_allowlist(w.room)


def test_list_operator_sees_all_owner_sees_own_rooms(w):
    ask(w)
    ask(w, room=w.room2, host="b.example.org")
    assert len(w.s.list_egress(w.op)["requests"]) == 2
    assert len(w.s.list_egress(w.op, w.room)["requests"]) == 1
    assert len(w.s.list_egress(w.alice)["requests"]) == 2
    assert w.s.list_egress(w.bob)["requests"] == [] and code(w.s.list_egress, w.bob, w.room) == 404


def test_flood_is_capped_at_60_open_per_room(w):
    for i in range(60):
        ask(w, host=f"h{i}.example.org")
    assert code(ask, w, host="h60.example.org") == 429
    assert ask(w, host="h0.example.org")["state"] == "open"   # dedup still answers
    ask(w, room=w.room2, host="h60.example.org")              # another room is unaffected


@pytest.mark.parametrize("host,port", [("", 443), ("A B", 443), ("a.org", 0), ("a.org", 70000), ("a.org", True),
                                       ("a.org", "443"), ("a:b", 443), (5, 443), ("x" * 300, 443)])
def test_bad_hosts_are_refused(w, host, port):
    assert code(ask, w, host=host, port=port) == 400


def test_bad_decisions_unknown_ids_and_room(w):
    r = ask(w)
    assert code(w.s.decide_egress, w.op, r["id"], "maybe") == 400
    assert code(w.s.decide_egress, w.op, r["id"], "approve", ttl_seconds=0) == 400
    assert code(w.s.decide_egress, w.op, r["id"], "approve", ttl_seconds=True) == 400
    assert code(w.s.decide_egress, w.op, "nope", "approve") == 404
    assert code(w.s.request_egress, w.op, "no-room", "a1", "a.org", 443) == 404
    w.s.decide_egress(w.op, r["id"], "deny")
    assert code(w.s.decide_egress, w.op, r["id"], "approve") == 409     # already decided


def test_state_survives_hydrate(w, svc, tmp_path):
    w.s.decide_egress(w.op, ask(w)["id"], "approve", ttl_seconds=600)
    w.s.promote_egress(w.op, w.room, "p.example.org:80")
    d = ask(w, host="d.example.org")
    w.s.decide_egress(w.op, d["id"], "deny")
    from herdr_engineering.control_plane.collab import CollabService
    s2 = CollabService(w.s.auth, now=w.s.clock)
    s2.hydrate(w.s._log)
    al = s2.egress_allowlist(w.room)
    assert "api.example.org:443" in al and "p.example.org:80" in al
    assert s2.request_egress(w.op, w.room, "a1", "d.example.org", 443) == {"state": "denied"}


def test_routes_are_operator_listener_only(w):
    def call(method, parts, body=None, operator="mallory", auth=""):
        return w.s.dispatch(method, parts, {}, body or {}, auth, None, operator=operator)
    st, r = call("POST", ["fleet", "egress", "request"],
                 {"room_id": w.room, "agent_id": "a1", "host": "x.example.org", "port": 8080})
    assert st == 200 and r["state"] == "open"
    assert call("GET", ["fleet", "egress"])[1]["requests"][0]["id"] == r["id"]
    st, d = call("POST", ["fleet", "egress", r["id"], "decide"], {"decision": "approve", "ttl_seconds": 300})
    assert st == 200 and d["state"] == "approved"
    assert "x.example.org:8080" in call("GET", ["fleet", "egress", w.room])[1]["allowlist"]
    assert call("POST", ["fleet", "egress", "promote"], {"room_id": w.room, "host_port": "x.example.org:8080"})[0] == 200
    assert call("POST", ["fleet", "egress", "remove"], {"room_id": w.room, "host_port": "x.example.org:8080"})[0] == 200
    assert "x.example.org:8080" not in call("GET", ["fleet", "egress", w.room])[1]["allowlist"]
    tok = f"Bearer {__import__('tests.control_plane.test_collab_tasks', fromlist=['USERS']).USERS['alice']}"
    for method, parts in (("GET", ["fleet", "egress"]), ("GET", ["fleet", "egress", w.room]),
                          ("POST", ["fleet", "egress", "request"]), ("POST", ["fleet", "egress", r["id"], "decide"]),
                          ("POST", ["fleet", "egress", "promote"]), ("POST", ["fleet", "egress", "remove"])):
        assert call(method, parts, operator=None, auth=tok)[0] == 404      # the public listener never sees them


def test_stale_open_request_cannot_be_decided_is_hidden_and_swept(w):
    from herdr_engineering.control_plane.collab import EGRESS_KEEP, EGRESS_OPEN_TTL
    r = ask(w)
    w.s.clock.t += EGRESS_OPEN_TTL + 1
    assert code(w.s.decide_egress, w.op, r["id"], "approve") == 409
    assert w.s.list_egress(w.op)["requests"] == []
    assert w.s.list_egress(w.alice, w.room)["requests"] == []
    again = ask(w)                                          # a fresh request, not the stale one
    assert again["state"] == "open" and again["id"] != r["id"]
    assert r["id"] in w.s._egress
    w.s.clock.t += EGRESS_KEEP + EGRESS_OPEN_TTL
    w.s._housekeeping()
    assert r["id"] not in w.s._egress


def test_old_decided_rows_are_swept_but_live_approvals_stay(w):
    from herdr_engineering.control_plane.collab import EGRESS_KEEP
    d = ask(w, host="d.example.org")
    w.s.decide_egress(w.op, d["id"], "deny")
    a = ask(w)
    w.s.decide_egress(w.op, a["id"], "approve", ttl_seconds=30 * 86400)
    w.s.clock.t += EGRESS_KEEP + 10
    w.s._housekeeping()
    assert d["id"] not in w.s._egress and a["id"] in w.s._egress
    assert "api.example.org:443" in w.s.egress_allowlist(w.room)


def test_promote_and_remove_canonicalise_the_port(w):
    w.s.promote_egress(w.op, w.room, "a.org:0443")
    assert "a.org:443" in w.s.egress_allowlist(w.room)
    w.s.promote_egress(w.op, w.room, "a.org:443")           # duplicate promote adds nothing
    assert w.s._profile(w.room)["permanent_hosts"] == ["a.org:443"]
    w.s.remove_egress(w.op, w.room, "a.org:00443")
    assert "a.org:443" not in w.s.egress_allowlist(w.room)


def test_promote_beyond_the_cap_is_refused(w):
    from herdr_engineering.control_plane.collab import MAX_PROFILE_HOSTS
    for i in range(MAX_PROFILE_HOSTS):
        w.s.promote_egress(w.op, w.room, f"h{i}.example.org:80")
    assert code(w.s.promote_egress, w.op, w.room, "extra.example.org:80") == 400
    w.s.promote_egress(w.op, w.room, "h0.example.org:80")   # already present: still fine


@pytest.mark.parametrize("host", ["..", "-", ".:1", ".a.org", "a.org.", "-a.org", "a.org\n", "a.org ", "é.org"])
def test_malformed_hosts_are_refused(w, host):
    assert code(ask, w, host=host) == 400
    assert code(w.s.promote_egress, w.op, w.room, f"{host}:443") == 400


def test_non_string_ids_are_400_not_a_crash(w):
    for bad in ([], {}, 5, None):
        assert code(w.s.request_egress, w.op, bad, "a1", "a.org", 443) == 400
        assert code(w.s.promote_egress, w.op, bad, "a.org:443") == 400
        assert code(w.s.remove_egress, w.op, bad, "a.org:443") == 400
        assert code(w.s.decide_egress, w.op, bad, "approve") == 400
        assert code(w.s.list_egress, w.op, bad) == 400 if bad is not None else True


def test_allowlist_route_for_an_unknown_room_is_404(w):
    assert w.s.dispatch("GET", ["fleet", "egress", "no-such-room"], {}, {}, "", None, operator="mallory")[0] == 404

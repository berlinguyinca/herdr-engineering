# ruff: noqa: F811  (pytest fixtures are imported by name from the sibling test modules)
"""Helper fixes from the final review: dead code, IPv6 hosts, per-room model endpoints, per-person limits,
the pause in the audit log, what guests and agents see, and the successor hand-over (service side)."""
from __future__ import annotations

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

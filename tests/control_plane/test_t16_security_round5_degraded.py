# ruff: noqa: F811  (pytest fixtures are imported by name)
"""T16 security, round 5, BLOCKER 2: a degraded start (the durable collab log failed its replay at a gap) has no
lineage side effects and still reserves the namespaces of external agents it could not replay."""
from __future__ import annotations

from herdr_engineering.control_plane.collab import CollabService, StaticTokenAuthenticator
from herdr_engineering.control_plane.collab_context import normalize_contextual
from tests.control_plane.test_collab import _digest
from tests.control_plane.test_collab_external import enroll, fab  # noqa: F401
from tests.control_plane.test_collab_links import w  # noqa: F401
from tests.control_plane.test_collab_tasks import USERS, svc  # noqa: F401
from tests.control_plane.test_contextual import cx, mk  # noqa: F401


# ======================================================================== BLOCKER 2: degraded means no side effects
def _degraded_after_a_gap(cx):
    """A log whose replay stops at a gap right before a link's revocation, with an external agent enrolled after the
    gap. Returns (fresh degraded service, link id, external agent id, lineage-sink calls)."""
    s = cx.s
    s.lineage_sink = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("graph down"))   # the link stays unpublished
    lid = mk(cx)[1]["link"]["link_id"]
    s.post_message(cx.alice, cx.room, {"text": "between", "idempotency_key": "gap-1"})
    s.revoke_link(cx.bob, cx.room, lid)
    ext_pr, _, _ = enroll(cx, label="late")
    rows = list(s._log)
    gap = next(i for i, e in enumerate(rows) if e["event_type"] == "message" and e["payload"].get("text") == "between")
    del rows[gap]                                     # sequence gap between the link and its revocation
    again = CollabService(StaticTokenAuthenticator({u: _digest(t) for u, t in USERS.items()}), now=s.clock)
    again.contextual = normalize_contextual({"enabled": True})
    calls = []
    again.lineage_sink = lambda *a, **k: calls.append(a) or {"ok": True}
    again.graph_source = s.graph_source
    assert again.hydrate_or_degrade(rows) is False and again.degraded
    assert again._xlinks[lid]["state"] == "active" and not again._xlinks[lid]["published"]   # the partial view
    return again, lid, ext_pr.id, calls


def test_a_degraded_start_with_a_gap_writes_no_lineage_edge(cx):
    again, lid, _, calls = _degraded_after_a_gap(cx)
    again._xlink_flush_at = 0.0
    again.maintain()                                  # what api._link_flush_loop calls every 30 s
    again._xlink_sweep(cx.room)                       # what graph_read calls directly
    assert again._xlink_publish(lid) is False
    again._housekept = 0.0
    again._housekeeping()
    assert calls == []
    assert again._xlinks[lid]["published"] is False


def test_a_degraded_start_still_reserves_an_external_agent_enrolled_after_the_gap(cx):
    again, _, late_agent, _ = _degraded_after_a_gap(cx)
    assert late_agent not in again._agents            # the partial state never saw it
    assert again.is_external_node(f"task:{late_agent}.x") is True
    assert again.is_external_node("task:a") is False   # ordinary ids are untouched

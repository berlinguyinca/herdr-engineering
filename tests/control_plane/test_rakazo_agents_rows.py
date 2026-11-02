from datetime import datetime, timezone

from herdr_engineering.control_plane.rakazo_agents import (
    STATUS_ORDER, agent_row, agent_status, sort_rows, visible)

NOW = 1_800_000_000.0


def _iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace("+00:00", "Z")


def _row(sid, status, age=0, **kw):
    return {"id": sid, "name": sid, "machine": None, "status": status, "summary": "",
            "updated_at": _iso(NOW - age), "has_conversation": True, **kw}


def test_agent_status_maps_herdr_values_and_closed_set():
    assert STATUS_ORDER == ("blocked", "done", "working", "idle", "unknown", "closed")
    for live in ("blocked", "done", "working", "idle", "unknown"):
        assert agent_status({"status": live}) == live
    assert agent_status({"status": "Blocked"}) == "blocked"
    for closed in ("closed", "completed", "failed", "exited", "terminated"):
        assert agent_status({"status": closed}) == "closed"


def test_unknown_or_missing_status_is_unknown():
    assert agent_status({}) == "unknown"
    assert agent_status({"status": None}) == "unknown"
    assert agent_status({"status": "active"}) == "unknown"


def test_sort_blocked_first_then_done_working_idle_unknown_closed_newest_first_within_group():
    rows = [_row("c", "closed"), _row("u", "unknown"), _row("i", "idle"),
            _row("w_old", "working", 100), _row("w_new", "working", 10),
            _row("d", "done"), _row("b", "blocked")]
    assert [r["id"] for r in sort_rows(rows)] == [
        "b", "d", "w_new", "w_old", "i", "u", "c"]


def test_visible_hides_closed_and_week_old_idle_unless_show_older():
    week = 7 * 86400
    rows = [_row("closed", "closed"), _row("old_idle", "idle", week + 1),
            _row("edge_idle", "idle", week), _row("old_done", "done", week * 3),
            _row("old_unknown", "unknown", week * 3), _row("new_idle", "idle", 5)]
    ids = lambda rs: [r["id"] for r in rs]
    assert ids(visible(rows, show_older=False, now=NOW)) == [
        "edge_idle", "old_done", "old_unknown", "new_idle"]
    assert ids(visible(rows, show_older=True, now=NOW)) == ids(rows)


def test_summary_falls_back_to_status_line_when_none():
    s = {"session_id": "s1", "status": "blocked", "name": "alpha", "machine": "m1",
         "updated_at": "2026-01-01T00:00:00Z", "agent_session": "conv"}
    row = agent_row(s, None)
    assert row["summary"] and row["summary"] == agent_row(s, "")["summary"]
    assert agent_row(s, "Fixing the build")["summary"] == "Fixing the build"
    assert row["id"] == "s1" and row["name"] == "alpha" and row["machine"] == "m1"
    assert row["status"] == "blocked" and row["has_conversation"] is True
    assert agent_row({"session_id": "s2"}, None)["has_conversation"] is False
    nested = agent_row({"session_id": "s3", "detail": {"machine": "m2"}}, None)
    assert nested["machine"] == "m2"

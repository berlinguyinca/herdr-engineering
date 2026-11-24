"""The private snapshot store: bounded, 0600, redacted, tolerant of damage."""
import json
import stat

import pytest

from herdr_engineering import summary_snapshots as ss

POINT = {"prompts": 5, "events": 40, "tool_calls": 10, "tool_failures": 2, "compactions": 0, "agents": 1,
         "worked_s": 600, "fresh": 1000, "cached": 5000, "segments": 2, "drift": False, "coverage": "complete"}


def test_snapshot_point_is_computed_from_the_history_and_graph():
    history = {"prompts_total": 7, "events_total": 90, "counts": {"tool_calls": 30, "tool_failures": 3},
               "compactions": 1, "dispatch_ts": ["2026-01-01T09:00:00Z"],
               "activity_ts": ["2026-01-01T09:00:00Z", "2026-01-01T09:10:00Z", "2026-01-01T11:00:00Z"],
               "usage": [{"ts": None, "fresh": 10, "cached": 40}, {"ts": None, "fresh": 5, "cached": 0}],
               "coverage": {"verdict": "partial", "gaps": ["x"]}}
    graph = {"segments": [{}, {}, {}], "drift": {"at": 4}}
    point = ss.snapshot_point(history, graph)
    assert point["prompts"] == 7 and point["events"] == 90 and point["tool_failures"] == 3
    assert point["worked_s"] == 600                      # the two-hour gap is not work
    assert point["fresh"] == 15 and point["cached"] == 40 and point["agents"] == 1
    assert point["segments"] == 3 and point["drift"] is True and point["coverage"] == "partial"


def test_unrecorded_figures_stay_none_never_zero():
    point = ss.snapshot_point({"prompts_total": 1, "events_total": 2, "coverage": {"verdict": "limited"}})
    assert point["worked_s"] is None and point["fresh"] is None and point["segments"] is None
    assert point["drift"] is None and point["tool_calls"] is None


@pytest.mark.parametrize("bad", [None, [], "x", {"events": 1}, {"prompts": "3", "events": 1}, {"prompts": True, "events": 1}])
def test_clean_point_rejects_what_is_not_a_point(bad):
    assert ss.clean_point(bad) is None


def test_clean_point_whitelists_keys_and_types():
    point = ss.clean_point({**POINT, "evil": "<script>", "worked_s": "5", "fresh": -1, "coverage": "great",
                            "taken_at": "yesterday", "drift": "yes"})
    assert "evil" not in point and point["worked_s"] is None and point["fresh"] is None
    assert point["coverage"] is None and point["taken_at"] is None and point["drift"] is None


def test_record_writes_a_private_redacted_numbered_series(tmp_path):
    first = ss.record(tmp_path / "c", "claude", "s1", POINT, "# Session summary\nkey sk-abcdefghijklmnopqrstuvwxyz0123456789")
    second = ss.record(tmp_path / "c", "claude", "s1", {**POINT, "prompts": 9}, "# Session summary two")
    path = ss.store_path(tmp_path / "c", "claude", "s1")
    assert (first["n"], second["n"]) == (1, 2)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and stat.S_IMODE((tmp_path / "c").stat().st_mode) == 0o700
    assert "sk-abcdefghijklmnopqrstuvwxyz0123456789" not in path.read_text()
    assert [r["prompts"] for r in ss.load(path)] == [5, 9]


def test_only_the_newest_snapshots_keep_report_text(tmp_path):
    for i in range(8):
        ss.record(tmp_path, "claude", "s1", {**POINT, "prompts": i + 1}, f"# Session summary {i}")
    rows = ss.load(ss.store_path(tmp_path, "claude", "s1"))
    assert [bool(r["report"]) for r in rows] == [False] * 3 + [True] * 5


def test_store_is_capped_and_keeps_the_first_snapshot(tmp_path):
    for i in range(ss.MAX_SNAPSHOTS + 7):
        ss.record(tmp_path, "claude", "s1", {**POINT, "prompts": i + 1})
    rows = ss.load(ss.store_path(tmp_path, "claude", "s1"))
    assert len(rows) == ss.MAX_SNAPSHOTS and rows[0]["n"] == 1 and rows[-1]["n"] == ss.MAX_SNAPSHOTS + 7
    assert [r["n"] for r in rows] == sorted(r["n"] for r in rows)


@pytest.mark.parametrize("content", ["", "not json", "[]", '{"version": 9, "snapshots": []}',
                                     '{"version": 1, "snapshots": "x"}', '{"version": 1, "snapshots": [1, {"n": 1}]}'])
def test_a_damaged_store_is_an_empty_history_and_is_rewritten(tmp_path, content):
    path = ss.store_path(tmp_path, "claude", "s1")
    path.write_text(content)
    assert ss.load(path) == []
    assert ss.record(tmp_path, "claude", "s1", POINT)["n"] == 1
    assert len(ss.load(path)) == 1


def test_previous_carries_a_clipped_report_and_series_drops_text(tmp_path):
    assert ss.previous(tmp_path, "claude", "s1") is None
    ss.record(tmp_path, "claude", "s1", POINT, "# Session summary\n" + "x" * 20_000)
    prev = ss.previous(tmp_path, "claude", "s1")
    assert prev["n"] == 1 and prev["count"] == 1 and len(prev["report"]) == ss.CARRY_CHARS
    assert all("report" not in r for r in ss.series(ss.load(ss.store_path(tmp_path, "claude", "s1"))))
    json.dumps(prev)


def test_record_refuses_a_non_point(tmp_path):
    with pytest.raises(ValueError):
        ss.record(tmp_path, "claude", "s1", {"x": 1})

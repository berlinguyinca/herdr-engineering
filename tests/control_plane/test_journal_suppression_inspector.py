"""Inspector journal suppression: tool update rows with an end row and repeated all-zero usage rows stay out of the journal.

Same method as test_journal_suppression.py: one workload through a real sink into real PostgreSQL, suppression on and off,
both journals replayed and compared, the suppressed one smaller. No mocks.
"""
from __future__ import annotations

import pytest

from herdr_engineering.control_plane.journal_suppression import OFF, JournalSuppression
from tests.control_plane import test_journal_suppression as base
from tests.control_plane.test_fabric_compaction import asyncpg
from tests.control_plane.test_journal_suppression import SID, _run
from tests.control_plane.test_postgres_recovery import _inspector_view, _projections

pg, db = base.pg, base.db      # the disposable-database fixtures

pytestmark = pytest.mark.skipif(asyncpg is None, reason="asyncpg required")


def _tool(call, phase, **kw):
    return {"kind": "tool", "call_id": call, "phase": phase, "tool": "bash", "args_preview": "ls", **kw}


def _inspector_workload(repo, clock):
    repo.upsert_session(SID, host_id="h", model="model", status="active")
    ins = repo.inspector
    for call in range(3):
        cid = f"c{call}"
        ins.ingest(SID, {"events": [_tool(cid, "start")]})
        for n in range(1, 9):
            clock.t += 1
            ins.ingest(SID, {"events": [_tool(cid, "update", output="x" * (n * 50))]})
        clock.t += 1
        ins.ingest(SID, {"events": [_tool(cid, "end", output="x" * 400, status="ok", exit_code=0)]})
    for _ in range(10):                              # all-zero usage reports that repeat the latest
        ins.ingest(SID, {"events": [{"kind": "usage", "input_tokens": 0, "output_tokens": 0}]})
    ins.ingest(SID, {"events": [{"kind": "usage", "input_tokens": 5, "output_tokens": 7}]})
    ins.ingest(SID, {"events": [{"kind": "usage", "input_tokens": 0, "output_tokens": 0}]})   # replaces a non-zero latest
    ins.ingest(SID, {"events": [{"kind": "usage", "input_tokens": 0, "output_tokens": 0}]})


def _detail(repo):
    view = _inspector_view(repo, SID)
    return {"tools": sorted((e["data"]["call_id"], e["data"].get("status"), e["data"].get("output"),
                             e["data"].get("tool"), e["data"].get("args_preview"))
                            for e in view["events"] if e.get("kind") == "tool"),
            "usage": view["summary"]["usage"], "running": view["summary"]["running_tools"]}


def test_tool_updates_and_zero_usage_replay_the_same_and_the_journal_shrinks(db):
    on, rows_on, _ = _run(db, JournalSuppression(), _inspector_workload)
    off, rows_off, _ = _run(db, OFF, _inspector_workload)
    assert rows_off["inspector.tool"] == 3 * 10
    assert rows_on["inspector.tool"] == 3 * 2, rows_on           # start and end remain
    assert rows_on["inspector.usage"] < rows_off["inspector.usage"]
    assert _detail(on) == _detail(off)
    assert _detail(on)["tools"] and _detail(on)["usage"]
    assert _projections(on) == _projections(off)


def test_a_call_cut_off_by_an_interruption_keeps_its_last_update(db):
    def workload(repo, clock):
        repo.upsert_session(SID, host_id="h", model="model", status="active")
        ins = repo.inspector
        ins.ingest(SID, {"events": [_tool("c", "start")]})
        for n in range(1, 4):
            ins.ingest(SID, {"events": [_tool("c", "update", output="y" * (n * 10))]})
        ins.ingest(SID, {"events": [{"kind": "lifecycle", "phase": "interrupted"}]})
    on, rows_on, _ = _run(db, JournalSuppression(), workload)
    off, rows_off, _ = _run(db, OFF, workload)
    assert rows_on["inspector.tool"] == 2 and rows_off["inspector.tool"] == 4     # start + the last update only
    assert _detail(on)["tools"] == _detail(off)["tools"]
    [(_, status, output, *_)] = _detail(on)["tools"]
    assert status != "running" and output == "y" * 30


def test_an_end_row_without_output_does_not_replace_the_updates(db):
    def workload(repo, clock):
        repo.upsert_session(SID, host_id="h", model="model", status="active")
        ins = repo.inspector
        ins.ingest(SID, {"events": [_tool("c", "start")]})
        ins.ingest(SID, {"events": [_tool("c", "update", output="partial")]})
        ins.ingest(SID, {"events": [_tool("c", "end", status="ok")]})
    on, _, _ = _run(db, JournalSuppression(), workload)
    off, _, _ = _run(db, OFF, workload)
    assert _detail(on)["tools"] == _detail(off)["tools"]

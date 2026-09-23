"""Session journal / timeline tests (spec 0090)."""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from herdr_engineering.contracts import ActivityEvent
from herdr_engineering.journal import SessionJournal


def _ev(eid, session, etype, summary, occurred, **kw):
    return ActivityEvent(id=eid, occurred_at=occurred, source="herdr",
                         type=etype, summary=summary, herdr_session_id=session, **kw)


def test_idempotent_ingest(tmp_path):
    j = SessionJournal(store_path=tmp_path / "j.jsonl")
    ev = _ev("e1", "s1", "session.started", "started", "2026-09-23T00:00:00+00:00")
    assert j.ingest(ev) is True
    assert j.ingest(ev) is False  # dedup
    assert j.count() == 1


def test_out_of_order_safe_and_persists(tmp_path):
    path = tmp_path / "j.jsonl"
    j = SessionJournal(store_path=path)
    j.ingest(_ev("a", "s1", "task.started", "build", "2026-09-23T10:00:00+00:00"))
    j.ingest(_ev("b", "s1", "task.completed", "done", "2026-09-23T09:00:00+00:00"))
    j2 = SessionJournal(store_path=path)  # survives restart
    assert j2.count() == 2
    # newest-first
    assert j2.timeline("s1")[0].id == "a"


def test_summary_projection(tmp_path):
    j = SessionJournal(store_path=tmp_path / "j.jsonl")
    j.ingest(_ev("1", "s", "session.started", "began", "2026-09-23T00:00:00+00:00"))
    j.ingest(_ev("2", "s", "task.started", "current task", "2026-09-23T00:01:00+00:00"))
    j.ingest(_ev("3", "s", "task.completed", "previous task", "2026-09-23T00:02:00+00:00"))
    s = j.summary("s")
    assert s.started == "began"
    assert s.current == "current task"
    assert "previous task" in s.completed


def test_search(tmp_path):
    j = SessionJournal(store_path=tmp_path / "j.jsonl")
    j.ingest(_ev("1", "s1", "git.commit", "commit fix", "2026-09-23T00:00:00+00:00",
                 repository="repo-a", branch="main"))
    j.ingest(_ev("2", "s1", "note", "note next", "2026-09-23T00:01:00+00:00",
                 repository="repo-b"))
    assert len(j.search(repository="repo-a")) == 1
    assert len(j.search(text="next")) == 1


def test_no_chain_of_thought_persisted(tmp_path):
    j = SessionJournal(store_path=tmp_path / "j.jsonl")
    j.ingest(_ev("1", "s", "note", "public note", "2026-09-23T00:00:00+00:00",
                 metadata={"kind": "next"}))
    raw = (tmp_path / "j.jsonl").read_text()
    assert "reasoning" not in raw.lower() and "hidden" not in raw.lower()

"""Pi Engineering / AutoSpec integration tests (spec 0100)."""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from herdr_engineering.pi_autospec import (
    AutoSpecAdapter,
    EngineeringCorrelator,
    PiEngineeringAdapter,
    Worker,
)


def test_mission_worker_ingest_and_dedupe():
    pi = PiEngineeringAdapter()
    pi.ingest_mission("msn_1", title="Fix things", status="running", spec_id="0110")
    pi.ingest_worker(worker_id="w1", mission_id="msn_1",
                     herdr_session_id="s1", worktree_id="wt1")
    pi.ingest_worker(worker_id="w1", mission_id="msn_1", status="done")
    mission = pi.mission("msn_1")
    assert mission.spec_id == "0110"
    assert len(mission.workers) == 1  # deduped
    assert mission.workers[0].status == "done"


def test_autospec_issue():
    a = AutoSpecAdapter()
    a.ingest_issue("i1", spec_id="0010", title="bootstrap", repository="r")
    assert a.get("i1").title == "bootstrap"


def test_correlator_links():
    c = EngineeringCorrelator()
    w = Worker(worker_id="w1", mission_id="m1", herdr_session_id="s1",
               worktree_id="wt1", repository="r", issue_id="i1", spec_id="0110")
    c.link(worker=w)
    assert c.by_worktree("wt1")["spec_id"] == "0110"
    assert c.by_session("s1")["mission_id"] == "m1"

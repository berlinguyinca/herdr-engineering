"""Multi-agent candidate tests (spec 0140)."""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from herdr_engineering.candidates import Candidate, CandidateGroup, candidate_id


def test_deterministic_candidate_id():
    assert candidate_id("r", "wt", "b") == candidate_id("r", "wt", "b")
    assert candidate_id("r", "wt", "b") != candidate_id("r", "wt", "other")


def test_group_upsert_list():
    g = CandidateGroup("grp")
    c = Candidate(id="c1", worker_session_id="owner1", dev_ports=[18000])
    g.upsert(c)
    assert len(g.list()) == 1
    assert g.get("c1") == c


def test_cleanup_requires_owner_and_token():
    g = CandidateGroup("grp")
    closed = []
    materialized = []
    c = Candidate(id="c1", worker_session_id="owner1", dev_ports=[18000])
    g.upsert(c)
    # non-owner cannot cleanup
    assert g.cleanup("c1", owned_by="someone_else", owner_token="t",
                     dev_fabric_close=closed.append, evidence_materializer=materialized.append) is False
    # owner without token cannot cleanup
    assert g.cleanup("c1", owned_by="owner1", owner_token="",
                     dev_fabric_close=closed.append, evidence_materializer=materialized.append) is False
    # owner with token can
    assert g.cleanup("c1", owned_by="owner1", owner_token="tok",
                     dev_fabric_close=closed.append, evidence_materializer=materialized.append) is True
    assert 18000 in closed
    assert c.status == "cleaned"

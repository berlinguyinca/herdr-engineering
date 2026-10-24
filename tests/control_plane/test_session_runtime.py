from herdr_engineering.control_plane.bridge import _reconstruct_from_events
from herdr_engineering.control_plane.ingest import IngestService
from herdr_engineering.control_plane.repo import ControlPlaneRepo, _runtime

FACTS = {"runtime_kind": "pi-bolt", "runtime_source": "role", "js_engine": "bun",
         "js_engine_version": "1.4.3", "cpu_s": 2.0, "rss_mib": 50.0}


def test_runtime_is_an_allowlisted_bounded_shape():
    assert _runtime(FACTS) == FACTS
    assert _runtime(None) is None and _runtime("x") is None
    assert _runtime({"runtime_kind": "rm -rf"}) is None
    out = _runtime({**FACTS, "evil": "<script>", "cpu_s": -1, "rss_mib": True, "js_engine": "deno",
                    "runtime_source": "x" * 500, "fallback_reason": "ok\x00bad"})
    assert "evil" not in out and "cpu_s" not in out and "rss_mib" not in out and "js_engine" not in out
    assert len(out["runtime_source"]) == 32 and "fallback_reason" not in out


def test_runtime_survives_update_and_is_returned_with_the_session():
    repo = ControlPlaneRepo()
    repo.upsert_session("s1", host_id="h", agent_role="reviewer", runtime=FACTS)
    assert repo.get_session("s1")["runtime"] == FACTS
    repo.upsert_session("s1", host_id="h", status="active")           # no runtime: must not erase it
    assert repo.get_session("s1")["runtime"] == FACTS
    repo.upsert_session("s1", host_id="h", runtime={**FACTS, "cpu_s": 3.5})
    assert repo.get_session("s1")["runtime"]["cpu_s"] == 3.5


def test_runtime_flows_through_generic_ingest():
    repo = ControlPlaneRepo()
    IngestService(repo).ingest({"entity_type": "session", "entity_id": "s3", "event_type": "session.updated",
                                "payload": {"host_id": "h", "agent_role": "planner", "runtime": FACTS}})
    assert repo.get_session("s3")["runtime"] == FACTS
    IngestService(repo).ingest({"entity_type": "session", "entity_id": "s4", "event_type": "session.updated",
                                "payload": {"host_id": "h", "runtime": {"runtime_kind": "bogus"}}})
    assert "runtime" not in {k for k, v in repo.get_session("s4").items() if v is not None}


def test_runtime_is_rebuilt_from_the_event_log():
    ev = {"entity_type": "session", "entity_id": "s2", "event_type": "SessionUpdated",
          "payload": {"host_id": "h", "status": "active", "runtime": FACTS},
          "ingest_timestamp": "2026-10-23T00:00:00Z", "sequence": 1}
    _, sessions, *_ = _reconstruct_from_events({"session:s2": [ev]})
    assert sessions["s2"]["runtime"] == FACTS

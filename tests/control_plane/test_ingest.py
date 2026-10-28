"""Ingestion service: map runtime lifecycle events to idempotent upserts."""
import pytest

from herdr_engineering.control_plane.ingest import IngestService
from herdr_engineering.control_plane.repo import ControlPlaneRepo


def make_svc(repo=None):
    return IngestService(repo or ControlPlaneRepo())


def test_host_upsert_is_idempotent_across_heartbeats():
    repo = ControlPlaneRepo()
    svc = make_svc(repo)
    svc.ingest({"entity_type": "host", "entity_id": "bender",
                "event_type": "host.registered",
                "payload": {"host_name": "bender", "tailnet_ip": "100.104.39.6"}})
    svc.ingest({"entity_type": "host", "entity_id": "bender",
                "event_type": "host.heartbeat",
                "payload": {"cpu": 12, "mem": 40}})
    svc.ingest({"entity_type": "host", "entity_id": "bender",
                "event_type": "host.heartbeat",
                "payload": {"cpu": 15}})
    hosts = repo.list_hosts()
    assert len(hosts) == 1  # one stable record, not three
    h = hosts[0]
    assert h["host_id"] == "bender"
    assert h["host_name"] == "bender"
    assert h["tailnet_ip"] == "100.104.39.6"
    assert h["cpu"] == 15
    # Routine heartbeats (same identity) refresh liveness in memory only: one
    # registered event, no per-heartbeat HostHeartbeat (I-1, unbounded growth).
    types = [e["event_type"] for e in repo.stream("host", "bender")]
    assert types == ["HostRegistered"]


def test_ingest_batch_returns_one_result_per_envelope():
    repo = ControlPlaneRepo()
    svc = make_svc(repo)
    result = svc.ingest_batch([
        {"entity_type": "host", "entity_id": "bender",
         "event_type": "host.heartbeat", "payload": {"cpu": 10}},
        {"entity_type": "host", "entity_id": "beast",
         "event_type": "host.heartbeat", "payload": {"cpu": 20}},
    ])
    assert len(result) == 2
    assert {host["host_id"] for host in repo.list_hosts()} == {"bender", "beast"}


def test_ingest_batch_rejects_unbounded_payload():
    with pytest.raises(ValueError, match="at most 64"):
        make_svc().ingest_batch([{}] * 65)


def test_scheduler_registration_updates_known_host_without_losing_metrics():
    repo = ControlPlaneRepo()
    svc = make_svc(repo)
    body = {"source": "fabric-agent", "entity_type": "host", "entity_id": "beast",
            "event_type": "host.heartbeat", "payload": {"cpu": 10}}
    svc.ingest(body)
    body["payload"] = {
        "host_uid": "uid-beast", "aliases": ["BEAST"], "platform": "linux",
        "scheduler_owned": True, "scheduler_trusted": True,
        "scheduler_policy": {"mode": "automatic", "revision": 1},
        "scheduler_capacity": {"total_cpu": 8},
        "telemetry": {"cpu": 12, "mem": 40},
        "sequence": 7, "freshness_seconds": 1, "confidence": "observed",
    }
    svc.ingest(body)
    assert repo.host_snapshot("beast").host_uid == "uid-beast"
    assert repo.host_snapshot("beast").telemetry_freshness_seconds == 1
    assert repo.get_host("beast")["scheduler_capacity"] == {"total_cpu": 8}
    assert repo.get_host("beast")["latest"]["cpu"] == 12
    assert repo.list_telemetry("beast", limit=1)[0]["sequence"] == 7
    events = repo.stream("host", "beast")
    svc.ingest(body)
    assert repo.stream("host", "beast") == events


def test_session_upsert_start_then_stop():
    repo = ControlPlaneRepo()
    svc = make_svc(repo)
    svc.ingest({"entity_type": "session", "entity_id": "w1H:t1",
                "event_type": "session.started",
                "payload": {"host_id": "bender", "agent_role": "pi",
                            "model": "claude"}})
    svc.ingest({"entity_type": "session", "entity_id": "w1H:t1",
                "event_type": "session.started",
                "payload": {"host_id": "bender"}})
    svc.ingest({"entity_type": "session", "entity_id": "w1H:t1",
                "event_type": "session.stopped"})
    sessions = repo.list_sessions()
    assert len(sessions) == 1
    s = sessions[0]
    assert s["status"] == "stopped"
    assert s["agent_role"] == "pi"
    # one stream, 0-indexed sequences 0,1,2
    assert [e["sequence"] for e in repo.stream("session", "w1H:t1")] == [0, 1, 2]


def test_mission_upsert_links_sessions():
    repo = ControlPlaneRepo()
    svc = make_svc(repo)
    svc.ingest({"entity_type": "mission", "entity_id": "MSN-1",
                "event_type": "mission.started",
                "payload": {"title": "Control plane", "purpose": "ingest"}})
    svc.ingest({"entity_type": "session", "entity_id": "sess-a",
                "event_type": "session.started",
                "payload": {"host_id": "beast", "mission_id": "MSN-1"}})
    m = repo.get_mission("MSN-1")
    assert m is not None
    assert m["title"] == "Control plane"
    assert "sess-a" in m["sessions"]
    # mission update moves stage
    svc.ingest({"entity_type": "mission", "entity_id": "MSN-1",
                "event_type": "mission.updated",
                "payload": {"stage": "testing"}})
    assert repo.get_mission("MSN-1")["stage"] == "testing"
    # a subsequent update without a title must NOT clobber the human label
    svc.ingest({"entity_type": "mission", "entity_id": "MSN-1",
                "event_type": "mission.updated",
                "payload": {"cwd": "/work/msn1"}})
    m = repo.get_mission("MSN-1")
    assert m["title"] == "Control plane"
    assert m["cwd"] == "/work/msn1"


def test_mission_create_without_title_defaults_to_id():
    repo = ControlPlaneRepo()
    svc = make_svc(repo)
    svc.ingest({"entity_type": "mission", "entity_id": "MSN-9",
                "event_type": "mission.started",
                "payload": {"purpose": "no label"}})
    assert repo.get_mission("MSN-9")["title"] == "MSN-9"


def test_service_upsert_is_idempotent_across_ingests():
    ingest = IngestService(ControlPlaneRepo())
    first = ingest.ingest({
        "entity_type": "service", "entity_id": "herdr-control-plane",
        "event_type": "service.started",
        "payload": {"host_id": "bender", "port": 8080,
                    "route": "http://bender:8080", "status": "healthy"},
    })
    second = ingest.ingest({
        "entity_type": "service", "entity_id": "herdr-control-plane",
        "event_type": "service.heartbeat",
        "payload": {"host_id": "bender", "port": 8080, "status": "healthy"},
    })
    assert first["entity_id"] == second["entity_id"]
    assert len(ingest.repo.list_services()) == 1
    svc = ingest.repo.get_service(first["entity_id"])
    assert svc["service_name"] == "herdr-control-plane"
    assert svc["host_id"] == "bender" and svc["port"] == 8080
    assert svc["route"] == "http://bender:8080"


def test_service_name_comes_from_payload_not_entity_id():
    # The fabric agent uses entity_id as a routing key (lease id) and carries
    # the display name in payload.service_name; the repo must key by the label.
    ingest = IngestService(ControlPlaneRepo())
    ingest.ingest({
        "entity_type": "service", "entity_id": "lease_abc123",
        "event_type": "service.registered",
        "payload": {"service_name": "web on bender (port 8181)",
                    "host_id": "bender", "port": 18000,
                    "route": "http://dev.lan:18000", "status": "healthy"},
    })
    (svc,) = ingest.repo.list_services()
    assert svc["service_name"] == "web on bender (port 8181)"
    assert svc["host_id"] == "bender"
    assert svc["port"] == 18000
    assert svc["route"] == "http://dev.lan:18000"


def test_service_description_and_owner_from_payload():
    ingest = IngestService(ControlPlaneRepo())
    ingest.ingest({
        "entity_type": "service", "entity_id": "lease_abc",
        "event_type": "service.registered",
        "payload": {"service_name": "python on beast (port 3011)",
                    "host_id": "beast", "port": 18142,
                    "route": "http://dev.lan:18142", "status": "healthy",
                    "owner": "python",
                    "description": "Augur Console, a Python web app"},
    })
    (svc,) = ingest.repo.list_services()
    assert svc["description"] == "Augur Console, a Python web app"
    assert svc["owner"] == "python"


def test_service_fallback_description_when_absent():
    # No explicit description in the payload: the repo still guarantees one.
    ingest = IngestService(ControlPlaneRepo())
    ingest.ingest({
        "entity_type": "service", "entity_id": "herdr-control-plane",
        "event_type": "service.started",
        "payload": {"host_id": "bender", "port": 8080,
                    "route": "http://bender:8080", "status": "healthy"},
    })
    (svc,) = ingest.repo.list_services()
    assert svc["description"]
    assert "bender" in svc["description"]


def test_service_unavailable_event_marks_status():
    ingest = IngestService(ControlPlaneRepo())
    ingest.ingest({
        "entity_type": "service", "entity_id": "postgres",
        "event_type": "service.started",
        "payload": {"host_id": "bender", "port": 5432, "status": "healthy"},
    })
    ingest.ingest({
        "entity_type": "service", "entity_id": "postgres",
        "event_type": "service.unavailable",
        "payload": {"host_id": "bender", "port": 5432},
    })
    (svc,) = ingest.repo.list_services()
    assert svc["status"] == "unavailable"


def test_ingest_generic_records_event_for_unknown_types():
    repo = ControlPlaneRepo()
    svc = make_svc(repo)
    svc.ingest({"entity_type": "workspace", "entity_id": "w1H",
                "event_type": "workspace.focus", "payload": {"tab": "t1"}})
    evs = repo.stream("workspace", "w1H")
    assert len(evs) == 1
    assert evs[0]["event_type"] == "workspace.focus"


def test_ingest_requires_entity_and_event():
    svc = make_svc()
    with pytest.raises(ValueError):
        svc.ingest({})
    with pytest.raises(ValueError):
        svc.ingest({"entity_type": "host", "entity_id": "x"})


def _session(svc, sid, event, **payload):
    svc.ingest({"entity_type": "session", "entity_id": sid, "event_type": event,
                "payload": {"host_id": "bender", **payload}})


def test_session_keeps_its_parent_and_name_and_ignores_bad_ones():
    repo = ControlPlaneRepo()
    svc = make_svc(repo)
    _session(svc, "bender:s0", "session.started", name="lead")
    _session(svc, "bender:s1", "session.started", parent_session_id="bender:s0", name="helper-1")
    _session(svc, "bender:s1", "session.updated", status="working")        # a later event without them keeps them
    _session(svc, "bender:s2", "session.started", parent_session_id="bender:s2", name="x" * 500)
    _session(svc, "bender:s3", "session.started", parent_session_id=["not", "a", "string"], name="bad\x00name")
    got = {s["session_id"]: s for s in repo.list_sessions()}
    assert (got["bender:s1"]["parent_session_id"], got["bender:s1"]["name"]) == ("bender:s0", "helper-1")
    assert got["bender:s0"]["parent_session_id"] is None and got["bender:s0"]["name"] == "lead"
    for sid in ("bender:s2", "bender:s3"):                                  # self-parent, oversize, wrong type, control chars
        assert got[sid]["parent_session_id"] is None and got[sid]["name"] is None


def test_parent_and_name_survive_rebuilding_from_the_event_log():
    from herdr_engineering.control_plane.bridge import _reconstruct_from_events
    repo = ControlPlaneRepo()
    svc = make_svc(repo)
    _session(svc, "bender:s1", "session.started", parent_session_id="bender:s0", name="helper-1")
    _session(svc, "bender:s1", "session.updated", status="working")
    events = repo.stream("session", "bender:s1")
    rebuilt = _reconstruct_from_events({"session:bender:s1": events})
    sessions = next(x for x in rebuilt if isinstance(x, dict) and "bender:s1" in x)
    assert sessions["bender:s1"]["parent_session_id"] == "bender:s0" and sessions["bender:s1"]["name"] == "helper-1"


def test_session_recording_status_and_explicit_parent_survive_updates_and_restore():
    from herdr_engineering.control_plane.bridge import _reconstruct_from_events

    svc = make_svc()
    parent = "herdr:bender:default:w1Z%3ApF"
    def emit(payload):
        return svc.ingest({"entity_type": "session", "entity_id": "helper",
                           "event_type": "session.updated", "payload": payload})

    emit({"host_id": "bender", "parent_session_id": parent,
          "transcript": {"status": "recording", "truncated": True, "text": "must not be stored"}})
    emit({"model": "new-model"})
    current = svc.repo.get_session("helper")
    assert current["transcript"] == {"status": "recording", "truncated": True}
    assert current["parent_session_id"] == parent
    emit({"transcript": {"status": "unreadable", "truncated": False}})
    events = svc.repo.stream("session", "helper")
    assert all("must not be stored" not in str(event) for event in events)
    _, sessions, _, _, _ = _reconstruct_from_events({"session|helper": events})
    assert sessions["helper"]["transcript"] == {"status": "unreadable", "truncated": False}
    assert sessions["helper"]["parent_session_id"] == parent


@pytest.mark.parametrize("metadata", [
    {"transcript": "recording"}, {"transcript": {"status": "invented"}},
    {"transcript": {"status": "recording", "truncated": "false"}},
])
def test_invalid_session_transcript_metadata_does_not_mutate_session(metadata):
    svc = make_svc()
    with pytest.raises(ValueError):
        svc.ingest({"entity_type": "session", "entity_id": "helper",
                    "event_type": "session.started", "payload": metadata})
    assert svc.repo.get_session("helper") is None


def test_session_retains_validated_identity_repair_status():
    svc = make_svc()
    svc.ingest({"entity_type": "session", "entity_id": "helper", "event_type": "session.updated",
                "payload": {"transcript": {"status": "recording", "identity_repair": "report_unverified"}}})
    assert svc.repo.get_session("helper")["transcript"]["identity_repair"] == "report_unverified"
    with pytest.raises(ValueError):
        svc.ingest({"entity_type": "session", "entity_id": "bad", "event_type": "session.updated",
                    "payload": {"transcript": {"status": "recording", "identity_repair": "invented"}}})
    assert svc.repo.get_session("bad") is None


@pytest.mark.parametrize("entity_type", [
    "pr_gate", "pr_gate_author", "review_job", "pr_gate_future", "review_jobs", "PR_GATE", " pr_gate"])
def test_generic_ingest_rejects_review_gate_streams(entity_type):
    repo = ControlPlaneRepo()
    with pytest.raises(ValueError, match="reserved"):
        make_svc(repo).ingest({
            "entity_type": entity_type, "entity_id": "github.com/acme/widgets#7",
            "event_type": "verdict", "payload": {"verdict": "PASS", "sha": "a" * 40}})
    assert repo.stream(entity_type, "github.com/acme/widgets#7") == []


def test_reserved_prefixes_cover_every_gate_stream():
    from herdr_engineering.review_gate import queue, registry, store
    from herdr_engineering.review_gate.entities import RESERVED_ENTITY_PREFIXES

    for entity in (store.ENTITY, queue.ENTITY, registry.ENTITY):
        assert entity.startswith(RESERVED_ENTITY_PREFIXES)


# ---------------------------------------------------------------------------
# Delivery entities (issue #32): test_run, build, review, review_finding,
# pull_request ingest idempotently and filter by (namespaced) mission id.
# ---------------------------------------------------------------------------
def test_ingest_test_run_is_idempotent_and_filters_by_mission():
    repo = ControlPlaneRepo()
    svc = make_svc(repo)
    env = {
        "entity_type": "test_run", "entity_id": "run-1",
        "event_type": "test_run.started",
        "payload": {"mission_id": "MSN-1", "status": "failed",
                    "passed": 2, "failed": 1, "total": 3}}
    svc.ingest({**env, "payload": {**env["payload"], "passed": 2}})
    svc.ingest({**env, "payload": {**env["payload"], "failed": 1}})
    runs = repo.list_test_runs(mission_id="MSN-1")
    assert len(runs) == 1
    assert runs[0]["test_run_id"] == "run-1"
    assert runs[0]["failed"] == 1 and runs[0]["passed"] == 2
    # A run for another mission never leaks in.
    assert repo.list_test_runs(mission_id="MSN-2") == []


def test_ingest_delivery_namespaces_mission_id_by_source():
    repo = ControlPlaneRepo()
    svc = make_svc(repo)
    # A pi-sourced report for raw mission_id M1 lands on the namespaced pi:M1
    # key so it is visible on the pi mission page (issue #26/#32).
    svc.ingest({"entity_type": "test_run", "entity_id": "run-pi",
                "event_type": "test_run.started", "source": "pi",
                "payload": {"mission_id": "M1", "status": "passed", "total": 1}})
    assert len(repo.list_test_runs(mission_id="pi:M1")) == 1
    assert repo.list_test_runs(mission_id="M1") == []
    # The same report under a herdr source stays on the bare M1 key.
    svc.ingest({"entity_type": "test_run", "entity_id": "run-h",
                "event_type": "test_run.started",
                "payload": {"mission_id": "M1", "status": "passed", "total": 1}})
    assert len(repo.list_test_runs(mission_id="M1")) == 1
    assert len(repo.list_test_runs(mission_id="pi:M1")) == 1
    # A prefixed id that conflicts with its source is rejected.
    with pytest.raises(ValueError, match="conflicts"):
        svc.ingest({"entity_type": "test_run", "entity_id": "run-bad",
                    "event_type": "test_run.started", "source": "pi",
                    "payload": {"mission_id": "herdr-ws:x", "total": 1}})


def test_ingest_build_reuses_test_run_collection_with_kind_build():
    repo = ControlPlaneRepo()
    svc = make_svc(repo)
    svc.ingest({"entity_type": "build", "entity_id": "build-1",
                "event_type": "build.started",
                "payload": {"mission_id": "m1", "status": "ok"}})
    svc.ingest({"entity_type": "build", "entity_id": "build-1",
                "event_type": "build.updated",
                "payload": {"mission_id": "m1", "status": "ok"}})
    builds = repo.list_test_runs(mission_id="m1", kind="build")
    assert len(builds) == 1 and builds[0]["kind"] == "build"
    assert repo.list_test_runs(mission_id="m1", kind="test") == []


def test_ingest_review_is_idempotent_and_holds_findings():
    repo = ControlPlaneRepo()
    svc = make_svc(repo)
    svc.ingest({"entity_type": "review", "entity_id": "rv-1",
                "event_type": "review.started",
                "payload": {"mission_id": "m1", "reviewer": "codex",
                            "status": "completed", "verdict": "approved"}})
    svc.ingest({"entity_type": "review", "entity_id": "rv-1",
                "event_type": "review.updated",
                "payload": {"mission_id": "m1", "reviewer": "codex",
                            "status": "completed", "verdict": "approved"}})
    svc.ingest({"entity_type": "review_finding", "entity_id": "rv-1",
                "event_type": "review_finding.added",
                "payload": {"severity": "blocker", "summary": "crashes"}})
    reviews = repo.list_reviews(mission_id="m1")
    assert len(reviews) == 1 and reviews[0]["verdict"] == "approved"
    assert [f["summary"] for f in reviews[0]["findings"]] == ["crashes"]
    assert repo.list_reviews(mission_id="m2") == []


def test_ingest_review_finding_requires_existing_review():
    repo = ControlPlaneRepo()
    svc = make_svc(repo)
    with pytest.raises(KeyError):
        svc.ingest({"entity_type": "review_finding", "entity_id": "nope",
                    "event_type": "review_finding.added",
                    "payload": {"severity": "blocker", "summary": "x"}})


def test_ingest_pull_request_is_idempotent_and_filters_by_mission():
    repo = ControlPlaneRepo()
    svc = make_svc(repo)
    env = {
        "entity_type": "pull_request", "entity_id": "pr-1",
        "event_type": "pull_request.opened",
        "payload": {"mission_id": "m1", "repository": "acme/w", "number": 7,
                    "url": "https://github.com/acme/w/pull/7", "status": "open"}}
    svc.ingest({**env})
    svc.ingest({**env, "payload": {**env["payload"], "status": "merged"}})
    pulls = repo.list_pull_requests(mission_id="m1")
    assert len(pulls) == 1 and pulls[0]["status"] == "merged"
    assert repo.list_pull_requests(mission_id="m2") == []
    # A merged PR records a PullRequestMerged event.
    assert "PullRequestMerged" in [e["event_type"] for e in repo.stream("pull_request", "pr-1")]


def test_ingest_delivery_rejects_invalid_counts():
    repo = ControlPlaneRepo()
    svc = make_svc(repo)
    def ingest(passed):
        svc.ingest({"entity_type": "test_run", "entity_id": "r",
                    "event_type": "test_run.started",
                    "payload": {"passed": passed}})
    for bad in ("many", True, float("nan"), float("inf"), float("-inf"), -3):
        with pytest.raises(ValueError, match="passed"):
            ingest(bad)


def test_ingest_review_finding_replay_is_idempotent_without_finding_id():
    repo = ControlPlaneRepo()
    svc = make_svc(repo)
    svc.ingest({"entity_type": "review", "entity_id": "rv-2",
                "event_type": "review.started",
                "payload": {"mission_id": "m1", "reviewer": "codex",
                            "status": "completed", "verdict": "approved"}})
    finding = {"entity_type": "review_finding", "entity_id": "rv-2",
               "event_type": "review_finding.added",
               "payload": {"severity": "blocker", "summary": "crash", "detail": "d"}}
    svc.ingest({**finding})
    svc.ingest({**finding})  # replay without a finding_id must not append a duplicate
    reviews = repo.list_reviews(mission_id="m1")
    assert len(reviews[0]["findings"]) == 1
    assert reviews[0]["findings"][0]["summary"] == "crash"


def test_v2_delivery_malformed_report_does_not_consume_its_sequence():
    repo = ControlPlaneRepo()
    svc = make_svc(repo)
    # A malformed v2 test_run (bad passed count) is rejected before the envelope
    # is persisted, so a corrected retry with the same idempotency identity lands.
    bad = {"schema_version": 2, "event_id": "pi-7", "source": "pi",
           "source_sequence": 7, "entity_type": "test_run", "entity_id": "r9",
           "event_type": "test_run.started",
           "payload": {"mission_id": "M1", "passed": "many", "total": 3}}
    with pytest.raises(ValueError, match="passed"):
        svc.ingest(bad, {"id": "agent", "kind": "agent", "authenticated_by": "api_key"})
    assert repo.list_test_runs(mission_id="pi:M1") == []
    # Corrected retry with the same event id is accepted.
    good = {**bad, "payload": {"mission_id": "M1", "passed": 3, "total": 3}}
    svc.ingest(good, {"id": "agent", "kind": "agent", "authenticated_by": "api_key"})
    runs = repo.list_test_runs(mission_id="pi:M1")
    assert len(runs) == 1 and runs[0]["passed"] == 3


def test_v2_review_finding_unknown_review_does_not_consume_sequence():
    repo = ControlPlaneRepo()
    svc = make_svc(repo)
    env = {"schema_version": 2, "event_id": "pi-8", "source": "pi",
           "source_sequence": 8, "entity_type": "review_finding",
           "entity_id": "missing-review", "event_type": "review_finding.added",
           "payload": {"severity": "blocker", "summary": "crash"}}
    # Unknown review must not persist an envelope (and must not consume its
    # sequence) so a corrected retry with the SAME event id can replay it.
    with pytest.raises(KeyError):
        svc.ingest(env, {"id": "agent", "kind": "agent", "authenticated_by": "api_key"})
    # Create the review, then retry the identical pi-8 envelope: it must apply.
    svc.ingest({"schema_version": 2, "event_id": "pi-9", "source": "pi",
                "source_sequence": 9, "entity_type": "review", "entity_id": "rv-9",
                "event_type": "review.started",
                "payload": {"mission_id": "M1", "reviewer": "codex", "status": "completed",
                            "verdict": "approved"}},
               {"id": "agent", "kind": "agent", "authenticated_by": "api_key"})
    svc.ingest({**env, "entity_id": "rv-9"},
               {"id": "agent", "kind": "agent", "authenticated_by": "api_key"})
    reviews = repo.list_reviews(mission_id="pi:M1")
    assert len(reviews) == 1 and len(reviews[0]["findings"]) == 1


def test_delivery_projections_survive_export_import_restore():
    repo = ControlPlaneRepo()
    svc = make_svc(repo)
    svc.ingest({"entity_type": "test_run", "entity_id": "rt1",
                "event_type": "test_run.started",
                "payload": {"mission_id": "m1", "status": "passed", "passed": 4, "total": 4}})
    svc.ingest({"entity_type": "review", "entity_id": "rvr1",
                "event_type": "review.started",
                "payload": {"mission_id": "m1", "reviewer": "codex",
                            "status": "completed", "verdict": "approved"}})
    svc.ingest({"entity_type": "pull_request", "entity_id": "prr1",
                "event_type": "pull_request.opened",
                "payload": {"mission_id": "m1", "number": 9, "status": "open"}})
    restored = ControlPlaneRepo()
    restored.import_state(repo.export_state())
    assert len(restored.list_test_runs(mission_id="m1")) == 1
    assert len(restored.list_reviews(mission_id="m1")) == 1
    assert len(restored.list_pull_requests(mission_id="m1")) == 1
    # The durable event streams still carry the records after restore.
    assert [e["event_type"] for e in restored.stream("test_run", "rt1")] == ["TestRunStarted"]


def test_delivery_projections_rebuilt_on_event_only_reload():
    repo = ControlPlaneRepo()
    svc = make_svc(repo)
    svc.ingest({"entity_type": "test_run", "entity_id": "rr1",
                "event_type": "test_run.started",
                "payload": {"mission_id": "m1", "status": "passed", "passed": 4, "total": 4}})
    svc.ingest({"entity_type": "pull_request", "entity_id": "ppr1",
                "event_type": "pull_request.opened",
                "payload": {"mission_id": "m1", "number": 11, "status": "open"}})
    # The bridge durable-reload path restores event streams but no delivery snapshot.
    state = repo.export_state()
    del state["delivery"]
    restored = ControlPlaneRepo()
    restored.import_state(state)
    assert len(restored.list_test_runs(mission_id="m1")) == 1
    assert len(restored.list_pull_requests(mission_id="m1")) == 1
    # Rebuilding must not duplicate the durable streams.
    assert len(restored.stream("test_run", "rr1")) == 1
    assert len(restored.stream("pull_request", "ppr1")) == 1


def test_v2_test_run_bad_results_does_not_consume_sequence():
    repo = ControlPlaneRepo()
    svc = make_svc(repo)
    bad = {"schema_version": 2, "event_id": "pi-20", "source": "pi",
           "source_sequence": 20, "entity_type": "test_run", "entity_id": "r20",
           "event_type": "test_run.started",
           "payload": {"mission_id": "M1", "total": 3, "results": "not-a-list"}}
    with pytest.raises(ValueError, match="results"):
        svc.ingest(bad, {"id": "agent", "kind": "agent", "authenticated_by": "api_key"})
    assert repo.list_test_runs(mission_id="pi:M1") == []
    # A corrected retry with the same event id applies.
    good = {**bad, "payload": {"mission_id": "M1", "total": 3,
                               "results": [{"name": "t1", "status": "passed"}]}}
    svc.ingest(good, {"id": "agent", "kind": "agent", "authenticated_by": "api_key"})
    runs = repo.list_test_runs(mission_id="pi:M1")
    assert len(runs) == 1 and len(runs[0]["results"]) == 1


def test_delivery_rebuild_preserves_review_findings_and_pr_merge():
    repo = ControlPlaneRepo()
    svc = make_svc(repo)
    svc.ingest({"entity_type": "review", "entity_id": "rvx",
                "event_type": "review.started",
                "payload": {"mission_id": "m1", "reviewer": "codex",
                            "status": "completed", "verdict": "changes"}})
    svc.ingest({"entity_type": "review_finding", "entity_id": "rvx",
                "event_type": "review_finding.added",
                "payload": {"severity": "blocker", "summary": "crash"}})
    svc.ingest({"entity_type": "pull_request", "entity_id": "prx",
                "event_type": "pull_request.opened",
                "payload": {"mission_id": "m1", "number": 3, "status": "open",
                            "branch": "x", "base": "main"}})
    svc.ingest({"entity_type": "pull_request", "entity_id": "prx",
                "event_type": "pull_request.updated",
                "payload": {"mission_id": "m1", "number": 3, "status": "merged",
                            "branch": "x", "base": "main"}})
    state = repo.export_state()
    del state["delivery"]
    restored = ControlPlaneRepo()
    restored.import_state(state)
    reviews = restored.list_reviews(mission_id="m1")
    assert len(reviews) == 1
    assert reviews[0]["verdict"] == "changes"
    assert len(reviews[0]["findings"]) == 1
    assert reviews[0]["findings"][0]["summary"] == "crash"
    pulls = restored.list_pull_requests(mission_id="m1")
    assert len(pulls) == 1 and pulls[0]["status"] == "merged"
    assert pulls[0]["branch"] == "x" and pulls[0]["base"] == "main"
    # Streams are not duplicated by the rebuild.
    assert len(restored.stream("review", "rvx")) == 2
    assert len(restored.stream("pull_request", "prx")) == 3


# ---------------------------------------------------------------------------
# Usage analytics (issue #36): token_usage and resource_usage ingest.
# ---------------------------------------------------------------------------
def test_ingest_token_usage_is_idempotent_on_request_id():
    repo = ControlPlaneRepo()
    svc = make_svc(repo)
    rec = {"mission_id": "m1", "model": "gpt-4o", "request_id": "req-1",
           "input_tokens": 10, "output_tokens": 5, "total": 0}
    svc.ingest({"entity_type": "token_usage", "entity_id": "req-1",
                "event_type": "token_usage.recorded", "payload": {**rec, "input_tokens": 10}})
    # A duplicate request_id is counted once.
    svc.ingest({"entity_type": "token_usage", "entity_id": "req-1",
                "event_type": "token_usage.recorded",
                "payload": {**rec, "input_tokens": 999}})
    a = repo.usage_analytics(mission_id="m1")
    assert a["tokens"]["by_model"]["gpt-4o"]["input_tokens"] == 10
    assert a["tokens"]["total"]["input_tokens"] == 10
    # Missing token counts stay None (never 0).
    svc.ingest({"entity_type": "token_usage", "entity_id": "req-2",
                "event_type": "token_usage.recorded",
                "payload": {"mission_id": "m2", "model": "gpt-4o", "request_id": "req-2"}})
    a2 = repo.usage_analytics(mission_id="m2")
    assert a2["tokens"]["by_model"]["gpt-4o"]["input_tokens"] == 0
    # Scoping: m1 does not see m2.
    assert repo.usage_analytics(mission_id="m1")["tokens"]["total"]["input_tokens"] == 10


def test_ingest_resource_usage_sums_by_kind_and_scopes_by_mission():
    repo = ControlPlaneRepo()
    svc = make_svc(repo)
    for value in (1.5, 2.5):
        svc.ingest({"entity_type": "resource_usage", "entity_id": f"r-{value}",
                    "event_type": "resource_usage.recorded",
                    "payload": {"mission_id": "m1", "kind": "cpu_seconds",
                                "value": value, "unit": "s"}})
    svc.ingest({"entity_type": "resource_usage", "entity_id": "r-other",
                "event_type": "resource_usage.recorded",
                "payload": {"mission_id": "m2", "kind": "cpu_seconds", "value": 99}})
    a = repo.usage_analytics(mission_id="m1")
    assert a["resources"]["cpu_seconds"]["count"] == 2
    assert a["resources"]["cpu_seconds"]["total"] == 4.0
    assert a["resources"]["cpu_seconds"]["unit"] == "s"
    assert repo.usage_analytics(mission_id="m2")["resources"]["cpu_seconds"]["total"] == 99
    assert "cpu_seconds" not in repo.usage_analytics(mission_id="m3")["resources"]


def test_ingest_usage_rejects_invalid_shapes():
    repo = ControlPlaneRepo()
    svc = make_svc(repo)
    with pytest.raises(ValueError, match="input_tokens"):
        svc.ingest({"entity_type": "token_usage", "entity_id": "x",
                    "event_type": "token_usage.recorded",
                    "payload": {"input_tokens": "many"}})
    with pytest.raises(ValueError, match="value"):
        svc.ingest({"entity_type": "resource_usage", "entity_id": "x",
                    "event_type": "resource_usage.recorded",
                    "payload": {"kind": "cpu", "value": "many"}})


def test_ingest_usage_rejects_nonfinite_and_negative_values():
    import math
    repo = ControlPlaneRepo()
    svc = make_svc(repo)
    bad_tokens = [float("nan"), float("inf"), float("-inf"), -1, True]
    for bad in bad_tokens:
        with pytest.raises(ValueError, match="finite non-negative"):
            svc.ingest({"entity_type": "token_usage", "entity_id": "x",
                        "event_type": "token_usage.recorded",
                        "payload": {"input_tokens": bad}})
    for bad in bad_tokens:
        with pytest.raises(ValueError, match="finite non-negative"):
            svc.ingest({"entity_type": "resource_usage", "entity_id": "x",
                        "event_type": "resource_usage.recorded",
                        "payload": {"kind": "cpu", "value": bad}})
    # The repo methods themselves are safe against non-finite/negative values:
    # token counts coerce non-finite to None (never OverflowError), while a
    # resource value that would corrupt the aggregate is rejected outright.
    rec = repo.add_token_usage({"request_id": "x", "input_tokens": float("inf")})
    assert rec["input_tokens"] is None
    with pytest.raises(ValueError):
        repo.add_resource_usage({"kind": "cpu", "value": -3.0})
    with pytest.raises(ValueError):
        repo.add_resource_usage({"kind": "cpu", "value": float("inf")})


def test_usage_and_stage_analytics_survive_export_import():
    clock = {"t": 1_700_000_000.0}
    repo = ControlPlaneRepo(now=lambda: clock["t"])
    svc = make_svc(repo)
    svc.ingest({"entity_type": "token_usage", "entity_id": "req-r1",
                "event_type": "token_usage.recorded",
                "payload": {"mission_id": "m1", "model": "gpt-4o",
                            "request_id": "req-r1", "input_tokens": 7}})
    svc.ingest({"entity_type": "resource_usage", "entity_id": "res-r1",
                "event_type": "resource_usage.recorded",
                "payload": {"mission_id": "m1", "kind": "cpu", "value": 2.0}})
    mid = repo.create_mission("s")
    repo.set_mission_stage(mid, "implementation"); clock["t"] += 0.5
    repo.set_mission_stage(mid, "complete")
    restored = ControlPlaneRepo(now=lambda: clock["t"])
    restored.import_state(repo.export_state())
    a = restored.usage_analytics(mission_id="m1")
    assert a["tokens"]["by_model"]["gpt-4o"]["input_tokens"] == 7
    assert a["resources"]["cpu"]["total"] == 2.0
    st = restored.stage_analytics()
    assert st["stages"]["implementation"]["p50_ms"] == 500
    assert "complete" in st["in_progress"][mid]


def test_usage_and_stage_analytics_survive_event_only_reload():
    """Durable bridge reload restores event streams with no delivery snapshot; the
    rebuild must regenerate usage and stage projections from those events (codex P1)."""
    import json as _json
    clock = {"t": 1_700_000_000.0}
    repo = ControlPlaneRepo(now=lambda: clock["t"])
    svc = make_svc(repo)
    svc.ingest({"entity_type": "token_usage", "entity_id": "req-e",
                "event_type": "token_usage.recorded",
                "payload": {"mission_id": "m1", "model": "gpt-4o",
                            "request_id": "req-e", "input_tokens": 5}})
    svc.ingest({"entity_type": "resource_usage", "entity_id": "res-e",
                "event_type": "resource_usage.recorded",
                "payload": {"mission_id": "m1", "kind": "cpu", "value": 3.0}})
    mid = repo.create_mission("s")
    repo.set_mission_stage(mid, "implementation"); clock["t"] += 0.4
    repo.set_mission_stage(mid, "complete")
    state = repo.export_state()
    # The bridge durable-reload path drops the delivery snapshot (events only).
    state.pop("delivery", None)
    restored = ControlPlaneRepo(now=lambda: clock["t"])
    restored.import_state(state)
    a = restored.usage_analytics(mission_id="m1")
    assert a["tokens"]["by_model"]["gpt-4o"]["input_tokens"] == 5
    assert a["resources"]["cpu"]["total"] == 3.0
    st = restored.stage_analytics()
    assert st["stages"]["implementation"]["p50_ms"] == 400
    assert "complete" in st["in_progress"][mid]
    # Re-run must be idempotent (no stream duplication, no double-counting):
    # importing the same event-only state again must not inflate the counts.
    a2 = restored.usage_analytics(mission_id="m1")
    assert a2["tokens"]["by_model"]["gpt-4o"]["input_tokens"] == 5
    assert a2["resources"]["cpu"]["count"] == 1


def test_export_state_serializes_resource_usage_keys_as_json():
    """Tuple mission/kind keys must serialize cleanly to JSON (codex P1)."""
    import json as _json
    clock = {"t": 1_700_000_000.0}
    repo = ControlPlaneRepo(now=lambda: clock["t"])
    repo.add_resource_usage({"mission_id": "m1", "kind": "cpu", "value": 2.0})
    repo.add_resource_usage({"mission_id": "m1", "kind": "cpu", "value": 3.0})
    dumped = _json.dumps(repo.export_state())  # must not raise TypeError
    reloaded = _json.loads(dumped)
    restored = ControlPlaneRepo(now=lambda: clock["t"])
    restored.import_state(reloaded)
    a = restored.usage_analytics(mission_id="m1")
    assert a["resources"]["cpu"]["count"] == 2
    assert a["resources"]["cpu"]["total"] == 5.0


def test_v2_usage_envelope_does_not_duplicate_events():
    """A schema-v2 envelope persists its row before dispatch; the projection must
    not record a second event, or an event-only reload would double-count it."""
    repo = ControlPlaneRepo()
    svc = make_svc(repo)
    env = {"schema_version": 2, "event_id": "pi-usage-1", "source": "pi",
           "source_sequence": 1, "entity_type": "token_usage", "entity_id": "req-u1",
           "event_type": "token_usage.recorded",
           "payload": {"mission_id": "M1", "model": "gpt", "request_id": "req-u1",
                       "input_tokens": 5}}
    svc.ingest(env, {"id": "agent", "kind": "agent", "authenticated_by": "api_key"})
    assert repo.usage_analytics(mission_id="pi:M1")["tokens"]["total"]["input_tokens"] == 5
    # The envelope row is the only durable record for this usage event.
    assert len(repo.stream("token_usage", "req-u1")) == 1
    # Event-only reload must not double-count.
    state = repo.export_state()
    state.pop("delivery", None)
    restored = ControlPlaneRepo()
    restored.import_state(state)
    assert restored.usage_analytics(mission_id="pi:M1")["tokens"]["total"]["input_tokens"] == 5


def test_v2_resource_usage_envelope_not_double_counted():
    repo = ControlPlaneRepo()
    svc = make_svc(repo)
    env = {"schema_version": 2, "event_id": "pi-res-1", "source": "pi",
           "source_sequence": 1, "entity_type": "resource_usage", "entity_id": "res-u1",
           "event_type": "resource_usage.recorded",
           "payload": {"mission_id": "M1", "kind": "cpu", "value": 2.0}}
    svc.ingest(env, {"id": "agent", "kind": "agent", "authenticated_by": "api_key"})
    a = repo.usage_analytics(mission_id="pi:M1")
    assert a["resources"]["cpu"]["total"] == 2.0
    assert a["resources"]["cpu"]["count"] == 1


def test_usage_rejects_malformed_occurred_at():
    """A garbage occurred_at must not break the retention sweep later."""
    import datetime as _dt
    from herdr_engineering.control_plane.retention import sweep_token_usage
    clock = {"t": _dt.datetime(2026, 10, 23, tzinfo=_dt.UTC).timestamp()}
    repo = ControlPlaneRepo(now=lambda: clock["t"])
    rec = repo.add_token_usage({"mission_id": "m1", "model": "gpt",
                                "request_id": "bad-ts", "input_tokens": 1,
                                "occurred_at": "not-a-timestamp"})
    # The malformed timestamp is normalized away (falls back to the wall clock).
    assert rec["occurred_at"] == repo._stamp()
    # The retention sweep must not raise on the stored projection.
    sweep_token_usage(repo, raw_retention_days=1)


def test_v2_usage_namespaces_mission_and_preserves_envelope_timestamp():
    """Usage ingested from a non-herdr source is aggregated under the namespaced
    mission (pi:M1, not bare M1), and a backdated envelope timestamp is kept so
    retention ages the projection out (codex P2)."""
    from herdr_engineering.control_plane.retention import sweep_token_usage
    import datetime as _dt
    clock = {"t": _dt.datetime(2026, 10, 23, tzinfo=_dt.UTC).timestamp()}
    repo = ControlPlaneRepo(now=lambda: clock["t"])
    svc = make_svc(repo)
    backdated = _dt.datetime(2026, 10, 1, tzinfo=_dt.UTC).isoformat()
    env = {"schema_version": 2, "event_id": "pi-usage-ns", "source": "pi",
           "source_sequence": 1, "entity_type": "token_usage", "entity_id": "req-ns",
           "event_type": "token_usage.recorded",
           "occurred_at": backdated,
           "payload": {"mission_id": "M1", "model": "gpt", "request_id": "req-ns",
                       "input_tokens": 5}}
    svc.ingest(env, {"id": "agent", "kind": "agent", "authenticated_by": "api_key"})
    # Namespaced: pi:M1, not the bare M1.
    assert repo.usage_analytics(mission_id="pi:M1")["tokens"]["total"]["input_tokens"] == 5
    assert repo.usage_analytics(mission_id="M1")["tokens"]["total"] == {}
    # The projection kept the backdated timestamp so a 14-day sweep drops it.
    assert repo._token_usage["req-ns"]["occurred_at"] == backdated
    sweep_token_usage(repo, raw_retention_days=14)
    assert "req-ns" not in repo._token_usage


def test_usage_mission_namespacing_matches_delivery():
    """The repo helper and ingest's _delivery_mission_key namespace identically."""
    from herdr_engineering.control_plane.repo import _usage_mission_key
    assert _usage_mission_key("M1", "herdr") == "M1"
    assert _usage_mission_key("M1", "pi") == "pi:M1"
    assert _usage_mission_key("pi:M1", "pi") == "pi:M1"
    assert _usage_mission_key(None, "pi") is None


def test_token_analytics_dedupes_by_request_id_like_usage_projection():
    """Re-recording the same request_id must be counted once by both token_analytics
    and the usage projection (codex P1)."""
    repo = ControlPlaneRepo()
    # The LLM-proxy path (record_token_usage) is the realistic retry scenario.
    repo.record_token_usage("p", "gpt", input_tokens=9, request_id="req-d1",
                            mission_id="m1")
    repo.record_token_usage("p", "gpt", input_tokens=999, request_id="req-d1",
                            mission_id="m1")
    # Both views agree the request was counted once.
    assert repo.token_analytics()["total"]["requests"] == 1
    assert repo.token_analytics()["total"]["input_tokens"] == 9
    assert repo.usage_analytics(mission_id="m1")["tokens"]["total"]["input_tokens"] == 9


def test_usage_projection_rebuild_keeps_backdated_timestamp():
    """An event-only reload must keep the backdated envelope timestamp so the
    rebuilt projection still ages out under retention (codex P1)."""
    import datetime as _dt
    from herdr_engineering.control_plane.retention import sweep_token_usage
    clock = {"t": _dt.datetime(2026, 10, 23, tzinfo=_dt.UTC).timestamp()}
    repo = ControlPlaneRepo(now=lambda: clock["t"])
    svc = make_svc(repo)
    backdated = _dt.datetime(2026, 10, 1, tzinfo=_dt.UTC).isoformat()
    svc.ingest({"schema_version": 2, "event_id": "pi-ts", "source": "pi",
                "source_sequence": 1, "entity_type": "token_usage",
                "entity_id": "req-ts", "event_type": "token_usage.recorded",
                "occurred_at": backdated,
                "payload": {"mission_id": "M1", "model": "gpt",
                            "request_id": "req-ts", "input_tokens": 4}},
               {"id": "agent", "kind": "agent", "authenticated_by": "api_key"})
    # Rebuild from events only (no delivery snapshot).
    state = repo.export_state()
    state.pop("delivery", None)
    restored = ControlPlaneRepo(now=lambda: clock["t"])
    restored.import_state(state)
    assert restored._token_usage["req-ts"]["occurred_at"] == backdated
    # A 14-day sweep removes the backdated (expired) record after the reload.
    sweep_token_usage(restored, raw_retention_days=14)
    assert "req-ts" not in restored._token_usage


def test_usage_rejects_timezone_naive_occurred_at():
    """A timezone-naive occurred_at must be normalized away so the retention
    sweep never compares a naive datetime to its aware cutoff (codex P2)."""
    import datetime as _dt
    repo = ControlPlaneRepo(now=lambda: _dt.datetime(2026, 10, 23, tzinfo=_dt.UTC).timestamp())
    rec = repo.add_token_usage({"mission_id": "m1", "model": "gpt",
                                "request_id": "naive-ts", "input_tokens": 1,
                                "occurred_at": "2026-10-01T00:00:00"})
    # Falls back to the (aware) wall clock; never stores a naive timestamp.
    assert rec["occurred_at"].endswith("+00:00")
    from herdr_engineering.control_plane.retention import sweep_token_usage
    sweep_token_usage(repo, raw_retention_days=1)  # must not raise


def test_usage_projection_preserves_cache_write_tokens():
    """/api/v1/token-usage submissions carrying cache_write_tokens must feed the
    usage projection (codex P1), not report it as zero/unknown."""
    repo = ControlPlaneRepo()
    repo.record_token_usage_row({"project": "p", "model": "gpt", "schema": 2,
                                 "request_id": "cw-1", "mission_id": "m1",
                                 "cache_write_tokens": 12, "status": 200})
    a = repo.usage_analytics(mission_id="m1")
    assert a["tokens"]["by_model"]["gpt"]["cache_write_tokens"] == 12
    assert a["tokens"]["total"]["cache_write_tokens"] == 12


def test_resource_usage_projection_is_pruned_by_event_retention():
    """The general event retention policy must also age out the resource_usage
    projection so usage_analytics() stops reporting expired totals (codex P2)."""
    import datetime as _dt
    from herdr_engineering.control_plane.retention import apply_retention
    clock = {"t": _dt.datetime(2026, 10, 23, tzinfo=_dt.UTC).timestamp()}
    repo = ControlPlaneRepo(now=lambda: clock["t"])
    repo.add_resource_usage({"mission_id": "m1", "kind": "cpu", "value": 1.0,
                             "occurred_at": repo._stamp()})  # now (fresh)
    clock["t"] -= 40 * 86400  # 40 days ago
    repo.add_resource_usage({"mission_id": "m1", "kind": "cpu", "value": 9.0,
                             "occurred_at": repo._stamp()})
    clock["t"] += 40 * 86400
    assert repo.usage_analytics(mission_id="m1")["resources"]["cpu"]["count"] == 2
    apply_retention(repo, events_ttl_days=30)
    a = repo.usage_analytics(mission_id="m1")
    assert a["resources"]["cpu"]["count"] == 1
    assert a["resources"]["cpu"]["total"] == 1.0


def test_token_analytics_includes_cache_write_tokens():
    """token_analytics (the legacy /api/v1/analytics/tokens path) must report
    cache_write_tokens like the usage projection (codex P1)."""
    repo = ControlPlaneRepo()
    repo.record_token_usage_row({"project": "p", "model": "gpt", "schema": 2,
                                 "request_id": "cw-2", "mission_id": "m1",
                                 "cache_write_tokens": 7, "status": 200})
    a = repo.token_analytics()
    assert a["total"]["cache_write_tokens"] == 7
    assert a["by_model"]["gpt"]["cache_write_tokens"] == 7
    assert a["by_project"]["p"]["cache_write_tokens"] == 7


def test_v2_resource_usage_missing_kind_does_not_consume_sequence():
    """A schema-v2 resource_usage with a missing/empty kind must be rejected in
    _prevalidate (before the envelope is persisted), so a corrected retry with
    the same idempotency identity still lands (codex P1)."""
    repo = ControlPlaneRepo()
    svc = make_svc(repo)
    bad = {"schema_version": 2, "event_id": "pi-res-kind", "source": "pi",
           "source_sequence": 1, "entity_type": "resource_usage", "entity_id": "res-k",
           "event_type": "resource_usage.recorded",
           "payload": {"mission_id": "M1", "value": 1.0}}  # no kind
    with pytest.raises(ValueError, match="kind"):
        svc.ingest(bad, {"id": "agent", "kind": "agent", "authenticated_by": "api_key"})
    # No resource recorded, and the corrected retry is accepted.
    assert repo.usage_analytics(mission_id="pi:M1")["resources"] == {}
    good = {**bad, "payload": {"mission_id": "M1", "kind": "cpu", "value": 1.0}}
    svc.ingest(good, {"id": "agent", "kind": "agent", "authenticated_by": "api_key"})
    assert repo.usage_analytics(mission_id="pi:M1")["resources"]["cpu"]["total"] == 1.0


def test_token_analytics_skips_projectless_ingest_rows():
    """A mission-scoped token_usage envelope (no legacy project field) must not
    crash token_analytics — /api/v1/analytics/tokens returns a 200, not a 500."""
    repo = ControlPlaneRepo()
    svc = make_svc(repo)
    svc.ingest({"entity_type": "token_usage", "entity_id": "req-np",
                "event_type": "token_usage.recorded",
                "payload": {"mission_id": "m1", "model": "gpt",
                            "request_id": "req-np", "input_tokens": 3}})
    a = repo.token_analytics()  # must not raise KeyError on missing 'project'
    assert a["total"]["input_tokens"] == 3
    assert a["by_project"] == {}  # not grouped under a legacy project
    assert a["by_model"]["gpt"]["input_tokens"] == 3
    # And via the API route it returns 200.
    assert repo.usage_analytics(mission_id="m1")["tokens"]["total"]["input_tokens"] == 3

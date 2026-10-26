from herdr_engineering.control_plane import repo


def test_mission_create_get_list_stage():
    r = repo.ControlPlaneRepo()
    mid = r.create_mission("Dynamic Aliases", purpose="alias resolution")
    assert mid.startswith("mission_")
    m = r.get_mission(mid)
    assert m["title"] == "Dynamic Aliases"
    assert m["status"] == "ready"
    assert len(r.list_missions()) == 1
    r.set_mission_stage(mid, "implementation")
    assert r.get_mission(mid)["stage"] == "implementation"
    assert r.get_mission(mid)["status"] == "implementation"


def test_mission_unknown_returns_none():
    r = repo.ControlPlaneRepo()
    assert r.get_mission("mission_nope") is None


def test_session_create_and_list():
    r = repo.ControlPlaneRepo()
    mid = r.create_mission("m")
    sid = r.create_session(mission_id=mid, agent_role="implementer", model="gpt-4o")
    assert sid.startswith("session_")
    s = r.get_session(sid)
    assert s["mission_id"] == mid
    assert len(r.list_sessions(status="active")) == 1


def test_session_action_records_event_and_is_allowed():
    r = repo.ControlPlaneRepo()
    sid = r.create_session()
    r.session_action(sid, "steer")
    r.session_action(sid, "approve")
    assert r.get_session(sid)["status"] == "active"
    # unknown session action is rejected
    try:
        r.session_action(sid, "bogus")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_service_register_list_get():
    r = repo.ControlPlaneRepo()
    sid = r.create_session()
    svcid = r.register_service(service_name="web", host_id="host_h",
                               port=8080, session_id=sid)
    assert svcid.startswith("service_")
    svcs = r.list_services()
    assert any(s["service_id"] == svcid for s in svcs)
    assert r.get_service(svcid)["port"] == 8080


def test_service_upsert_is_idempotent_by_host_and_name():
    r = repo.ControlPlaneRepo()
    a = r.upsert_service("bender", "web", 8080, status="healthy",
                         route="http://bender:8080")
    b = r.upsert_service("bender", "web", 8081, status="healthy",
                         route="http://bender:8081")
    # same (host, name) -> same id, updated in place, no duplicate
    assert a == b
    assert len(r.list_services()) == 1
    svc = r.get_service(a)
    assert svc["port"] == 8081 and svc["route"] == "http://bender:8081"
    # same name on a different host is a distinct service
    c = r.upsert_service("beast", "web", 9090)
    assert c != a
    assert len(r.list_services()) == 2
    # events carry the idempotent id so hydration reconstructs one record
    stream = r.stream("service", a)
    assert [e["event_type"] for e in stream] == ["ServiceRegistered", "ServiceUpdated"]
    assert stream[0]["entity_id"] == stream[1]["entity_id"] == a


def test_service_always_has_description():
    r = repo.ControlPlaneRepo()
    svcid = r.register_service(service_name="web", host_id="bender",
                               port=8080, route="http://dev.lan:18000")
    svc = r.get_service(svcid)
    assert svc["description"]
    assert "bender" in svc["description"]
    assert "8080" in svc["description"]
    # list view guarantees the invariant for every service
    assert all(s["description"] for s in r.list_services())


def test_service_explicit_description_wins():
    r = repo.ControlPlaneRepo()
    svcid = r.register_service(service_name="web", host_id="beast",
                               port=3011,
                               description="Augur Console dashboard")
    assert r.get_service(svcid)["description"] == "Augur Console dashboard"


def test_service_description_uses_owner_process():
    r = repo.ControlPlaneRepo()
    svcid = r.register_service(service_name="python on bender (port 3011)",
                               host_id="bender", port=3011, owner="python")
    assert r.get_service(svcid)["description"].startswith(
        "python service on bender")


def test_service_upsert_keeps_existing_description():
    r = repo.ControlPlaneRepo()
    a = r.upsert_service("bender", "web", 8080, description="Augur Console")
    b = r.upsert_service("bender", "web", 8081, status="healthy")
    assert a == b
    # a heartbeat without a new description must not clobber the old one
    assert r.get_service(a)["description"] == "Augur Console"
    # an explicit description replaces it
    c = r.upsert_service("bender", "web", 8082, description="Renamed app")
    assert r.get_service(c)["description"] == "Renamed app"


def test_service_description_derived_for_imported_records():
    """Records hydrated from an older event log (no description) still get a
    human description at read time."""
    r = repo.ControlPlaneRepo()
    r.import_state({"services": {
        "svc_old": {"service_id": "svc_old",
                    "service_name": "node on mac.lan (port 4000)",
                    "host_id": "mac.lan", "port": 4000, "status": "healthy",
                    "route": "http://dev.lan:18050",
                    "created_at": "2026-01-01T00:00:00Z",
                    "updated_at": "2026-01-01T00:00:00Z"},
    }})
    svc = r.get_service("svc_old")
    assert svc["description"]
    assert "mac.lan" in svc["description"]


def test_host_heartbeat_and_unavailable():
    r = repo.ControlPlaneRepo()
    hid = r.register_host("bender", "100.104.39.6")
    r.record_heartbeat(hid)
    assert r.get_host(hid)["status"] == "healthy"
    # marking unavailable retains the host (never deletes)
    r.mark_host_unavailable(hid)
    assert r.get_host(hid)["status"] == "unavailable"


def test_artifact_register_materialize():
    r = repo.ControlPlaneRepo()
    sid = r.create_session()
    aid = r.register_artifact(session_id=sid, original_filename="shot.png",
                              mime_type="image/png", size_bytes=3,
                              sha256="abc", storage_key="sha256/ab/cd/abc")
    assert aid.startswith("artifact_")
    a = r.get_artifact(aid)
    assert a["session_id"] == sid and a["status"] == "available"
    r.materialize_artifact(aid, host_id="host_h", session_id=sid,
                           local_path="/var/cache/herdr/artifacts/abc")
    assert r.get_artifact(aid)["materializations"] == 1


def test_events_sequenced_and_recent():
    r = repo.ControlPlaneRepo()
    sid = r.create_session()
    r.record_event("session", sid, "SessionMessageSent", {"text": "hi"},
                   session_id=sid)
    r.record_event("session", sid, "SessionToolCalled", {"tool": "bash"},
                   session_id=sid)
    # the session stream is sequenced 0,1,2 (created + two events)
    evs = r.stream("session", sid)
    assert [e["event_type"] for e in evs] == ["SessionCreated",
                                              "SessionMessageSent",
                                              "SessionToolCalled"]
    assert [e["sequence"] for e in evs] == [0, 1, 2]
    # recent_events returns newest-first across all streams, bounded by limit
    assert r.recent_events(limit=2)[0]["event_type"] == "SessionToolCalled"


def test_mission_analytics_counts_by_stage():
    r = repo.ControlPlaneRepo()
    r.create_mission("a")
    m2 = r.create_mission("b")
    r.set_mission_stage(m2, "failed")
    r.create_mission("c")
    a = r.mission_analytics()
    assert a["by_stage"]["ready"] == 2
    assert a["by_stage"]["failed"] == 1
    assert a["total"] == 3


def test_health_reports_self_observability():
    r = repo.ControlPlaneRepo()
    h = r.health()
    assert h["status"] == "ok"
    # repo-level stats; deployment context (source/version/commit) is overlaid
    # by the HTTP server, not the repo itself
    assert "events" in h and "missions" in h and "timestamp" in h
    assert "postgres" not in h
    assert h["events"]["ingested"] == 0


def test_leases_list_and_close():
    r = repo.ControlPlaneRepo()
    lease = r.register_lease(host_id="host_h", target_port=8080,
                             dev_port=18000)
    assert r.list_leases() == [lease]
    assert r.close_lease(lease["lease_id"]) is True
    assert r.list_leases() == []
    assert r.close_lease("lease_nope") is False


def test_identity_is_stable_across_location_change():
    r = repo.ControlPlaneRepo()
    hid = r.register_host("bender", "100.104.39.6")
    before = r.get_host(hid)
    # location (ip) can change; identity (host_id) does not
    r.record_heartbeat(hid, tailnet_ip="100.104.40.1")
    after = r.get_host(hid)
    assert before["host_id"] == after["host_id"]
    assert after["tailnet_ip"] == "100.104.40.1"


def test_message_is_queued_without_dispatcher_and_replay_does_not_send():
    r = repo.ControlPlaneRepo()
    sid = r.create_session()
    first = r.send_message(sid, "hello", operation_id="operation-one")
    assert first["status"] == "queued"
    assert r.get_session(sid)["messages"] == 0
    restored = repo.ControlPlaneRepo()
    restored.import_state(r.export_state())
    def unexpected(*args):
        raise AssertionError("a queued receipt with unknown delivery must not resend")
    assert restored.send_message(sid, "hello", operation_id="operation-one", dispatcher=unexpected) == first


def test_message_delivery_is_acknowledged_once_and_payload_reuse_rejected(acknowledged_sink):
    import pytest
    r = repo.ControlPlaneRepo(sink=acknowledged_sink)
    sid = r.create_session()
    observed = []
    def deliver(session, text, artifacts):
        observed.append((session["session_id"], text, artifacts))
        return {"status": "delivered", "receipt": {"ok": True}}
    result = r.send_message(sid, "hello", operation_id="one", dispatcher=deliver)
    assert result["status"] == "delivered"
    assert r.send_message(sid, "hello", operation_id="one", dispatcher=deliver) == result
    assert observed == [(sid, "hello", [])]
    assert r.get_session(sid)["messages"] == 1
    with pytest.raises(ValueError, match="operation"):
        r.send_message(sid, "different", operation_id="one", dispatcher=deliver)
    restored = repo.ControlPlaneRepo()
    restored.import_state(r.export_state())
    assert restored.send_message(sid, "hello", operation_id="one", dispatcher=deliver) == result
    assert len(observed) == 1


def test_transport_failure_never_counts_as_delivered(acknowledged_sink):
    r = repo.ControlPlaneRepo(sink=acknowledged_sink)
    sid = r.create_session()
    def broken(*args):
        raise OSError("unreachable")
    result = r.send_message(sid, "hello", dispatcher=broken)
    assert result["status"] == "failed"
    assert r.get_session(sid)["messages"] == 0
    assert all(e["event_type"] != "SessionMessageSent" for e in r.stream("session", sid))


def test_unsupported_resume_does_not_fabricate_running_status():
    r = repo.ControlPlaneRepo()
    r.upsert_session("agent", status="stopped")
    result = r.session_action("agent", "resume")
    assert result["status"] == "failed"
    assert result["error"] == "unsupported_action"
    assert r.get_session("agent")["status"] == "stopped"


def test_legacy_session_collision_is_refused_without_overwriting_first_host():
    import pytest
    r = repo.ControlPlaneRepo()
    r.upsert_session("w1:p1", host_id="bender")
    with pytest.raises(ValueError, match="host"):
        r.upsert_session("w1:p1", host_id="beast")
    assert r.get_session("w1:p1")["host_id"] == "bender"


def test_identity_survives_ingest_and_durable_event_replay(acknowledged_sink):
    from herdr_engineering.control_plane.bridge import _reconstruct_from_events
    from herdr_engineering.control_plane.ingest import IngestService
    r = repo.ControlPlaneRepo(sink=acknowledged_sink)
    rec = IngestService(r).ingest({
        "entity_type": "session", "entity_id": "herdr:beast:eng:w1%3Ap1",
        "event_type": "session.updated", "payload": {
            "host_id": "beast", "pane_id": "w1:p1", "terminal_id": "term-1",
            "herdr_session": "eng", "machine": "beast", "revision": 4,
            "interactive_ready": False, "launch_pending": True, "state_change_seq": 0,
            "agent_session": {"source": "integration", "agent": "pi", "kind": "path", "value": "/tmp/s.jsonl"},
        }})
    assert rec["terminal_id"] == "term-1"
    assert rec["herdr_session"] == "eng"
    r.send_message(rec["session_id"], "hello", operation_id="one", dispatcher=lambda *args: {"status": "delivered"})
    r.session_action(rec["session_id"], "stop")
    _, sessions, _, _, _ = _reconstruct_from_events(r.export_state()["events"])
    restored = sessions[rec["session_id"]]
    assert restored["messages"] == 1
    assert restored["status"] == "active"
    assert restored["agent_session"] == rec["agent_session"]
    assert restored["revision"] == 4
    assert restored["interactive_ready"] is False
    assert restored["state_change_seq"] == 0


def test_message_does_not_dispatch_without_required_durable_queue_receipt():
    class BrokenSink:
        def record(self, event):
            pass  # existing telemetry sink is intentionally best-effort
        def record_required(self, event):
            raise OSError("database unavailable")
    r = repo.ControlPlaneRepo(sink=BrokenSink())
    sid = r.create_session()
    delivered = []
    def dispatch(*args):
        delivered.append(args)
        return {"status": "delivered"}
    receipt = r.send_message(sid, "hello", operation_id="durable-one", dispatcher=dispatch)
    assert receipt["status"] == "failed"
    assert receipt["error"] == "receipt_persistence_failed"
    assert not delivered


def test_delivery_settlement_failure_retains_queue_and_does_not_resubmit():
    class Sink:
        def record(self, event):
            pass
        def record_required(self, event):
            if event["event_type"] == "SessionCommandSettled":
                raise OSError("connection lost after submission")
    r = repo.ControlPlaneRepo(sink=Sink())
    sid = r.create_session()
    submissions = []
    def dispatch(*args):
        submissions.append(args)
        return {"status": "delivered"}
    receipt = r.send_message(sid, "hello", operation_id="one", dispatcher=dispatch)
    assert receipt["status"] == "queued"
    assert receipt["error"] == "receipt_settlement_unknown"
    r.send_message(sid, "hello", operation_id="one", dispatcher=dispatch)
    assert len(submissions) == 1
    assert r.get_session(sid)["messages"] == 0


def test_required_receipt_cannot_race_telemetry_stream_sequence():
    import threading
    from concurrent.futures import ThreadPoolExecutor
    entered = threading.Event()
    release = threading.Event()
    telemetry_finished = threading.Event()
    class Sink:
        def record(self, event):
            pass
        def record_required(self, event):
            if event["event_type"] == "SessionCommandQueued":
                entered.set()
                assert release.wait(2)
    r = repo.ControlPlaneRepo(sink=Sink())
    sid = r.create_session()
    def telemetry():
        r.record_tool_call(sid, "bash")
        telemetry_finished.set()
    with ThreadPoolExecutor(max_workers=2) as pool:
        command = pool.submit(r.send_message, sid, "hello")
        assert entered.wait(2)
        update = pool.submit(telemetry)
        try:
            assert not telemetry_finished.wait(0.05)
        finally:
            release.set()
        command.result(timeout=2)
        update.result(timeout=2)
    assert [e["sequence"] for e in r.stream("session", sid)] == [0, 1, 2]


def test_message_audit_keeps_redacted_text_but_transport_receives_original(acknowledged_sink):
    r = repo.ControlPlaneRepo(sink=acknowledged_sink)
    sid = r.create_session()
    received = []
    def deliver(session, text, artifacts):
        received.append(text)
        return {"status": "delivered"}
    result = r.send_message(sid, "set token=super-secret-value", dispatcher=deliver)
    assert received == ["set token=super-secret-value"]
    assert "super-secret-value" not in result["text"]
    assert "set " in result["text"]


def test_recent_events_after_sparse_durable_restore_preserves_order_and_limits():
    r = repo.ControlPlaneRepo()
    r.import_state({'events': {
        'session|one': [
            {'sequence': 100, 'event_type': 'old'},
            {'sequence': 900, 'event_type': 'new'},
        ],
        'session|two': [{'sequence': 8000, 'event_type': 'other'}],
    }, 'event_order': [('session', 'one', 100), ('session', 'two', 8000),
                       ('session', 'one', 900)]})
    assert [e['event_type'] for e in r.recent_events(2)] == ['new', 'other']
    assert [e['event_type'] for e in r.recent_events(10)] == ['new', 'other', 'old']
    r.record_event('session', 'one', 'fresh')
    assert [e['event_type'] for e in r.recent_events(2)] == ['fresh', 'new']


def test_recent_events_preserves_distinct_events_with_duplicate_durable_sequences():
    r = repo.ControlPlaneRepo()
    r.import_state({'events': {
        'mission|one': [
            {'sequence': 0, 'event_id': 'first'},
            {'sequence': 0, 'event_id': 'second'},
            {'sequence': 500, 'event_id': 'third'},
            {'sequence': 500, 'event_id': 'fourth'},
        ],
        'host|two': [{'sequence': 80, 'event_id': 'other'}],
    }, 'event_order': [('mission', 'one', 0), ('host', 'two', 80),
                       ('mission', 'one', 0), ('mission', 'one', 500),
                       ('mission', 'one', 500)]})
    assert [e['event_id'] for e in r.recent_events(10)] == [
        'fourth', 'third', 'second', 'other', 'first']
    assert [e['event_id'] for e in r.recent_events(2)] == ['fourth', 'third']
    assert len({e['event_id'] for e in r.recent_events(10)}) == 5
    r.record_event('mission', 'one', 'new')
    assert r.recent_events(1)[0]['sequence'] == 501
    assert [e['event_id'] for e in r.recent_events(10)[1:]] == [
        'fourth', 'third', 'second', 'other', 'first']


def test_stage_analytics_percentiles_bottleneck_and_in_progress():
    clock = {"t": 1_700_000_000.0}
    r = repo.ControlPlaneRepo(now=lambda: clock["t"])
    mid = r.create_mission("stage-a")
    # implementation: 300ms, review: 600ms (bottleneck). The final 'complete'
    # transition leaves that stage's run open (in_progress), so it is excluded
    # from percentiles and stages.
    r.set_mission_stage(mid, "implementation"); clock["t"] += 0.3
    r.set_mission_stage(mid, "review");        clock["t"] += 0.6
    r.set_mission_stage(mid, "complete");      clock["t"] += 0.1
    # A second mission leaves its current stage open (in_progress).
    mid2 = r.create_mission("stage-b")
    r.set_mission_stage(mid2, "implementation"); clock["t"] += 0.2
    r.set_mission_stage(mid2, "review")
    a = r.stage_analytics()
    assert a["bottleneck"] == "review"
    assert a["stages"]["review"]["p50_ms"] == 600
    assert a["stages"]["implementation"]["runs"] == 2
    # Nearest-rank of [200, 300]: p50 = 1st value (200), p95 = 2nd value (300).
    assert a["stages"]["implementation"]["p50_ms"] == 200
    assert a["stages"]["implementation"]["p95_ms"] == 300
    # 'complete' for mid and 'review' for mid2 remain open (in_progress).
    assert "complete" not in a["stages"]
    assert a["stages"]["review"]["runs"] == 1
    assert mid in a["in_progress"] and a["in_progress"][mid] == ["complete"]
    assert mid2 in a["in_progress"] and a["in_progress"][mid2] == ["review"]

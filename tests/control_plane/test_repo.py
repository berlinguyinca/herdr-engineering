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

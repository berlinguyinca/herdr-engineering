"""Plannotator receipts must survive restart and remain bound to their subject."""
import hashlib

import pytest

from herdr_engineering.control_plane.repo import ControlPlaneRepo
from herdr_engineering.control_plane.reviews import ReviewService, safe_review_url


def launch(service, **extra):
    return service.register(dict(review_id="review-one", instance_id="instance-one", kind="plan",
                                 plan="# Verify the change", revision="commit-one", **extra))


def binding(record):
    return {k: record[k] for k in ("instance_id", "revision", "plan_digest")}


def test_launch_ready_result_survives_event_replay(acknowledged_sink):
    repo = ControlPlaneRepo(sink=acknowledged_sink)
    service = ReviewService(repo)
    row = launch(service, mission_id="mission-one", session_id="session-one")
    assert row["status"] == "starting"
    assert row["plan_digest"] == hashlib.sha256(b"# Verify the change").hexdigest()
    service.heartbeat(row["review_id"], dict(binding(row), review_url="http://bender.lan:49187"))
    restored = ControlPlaneRepo(sink=acknowledged_sink)
    restored.import_state(repo.export_state())
    resumed = ReviewService(restored)
    assert resumed.get(row["review_id"])["review_url"] == "http://bender.lan:49187"
    result = dict(binding(row), result={"decision": "approved", "feedback": "Keep the tests"})
    done = resumed.complete(row["review_id"], result)
    assert done["status"] == "approved"
    assert done["feedback"] == "Keep the tests"
    assert done["receipt"]["verification"] == "host_reported"
    assert resumed.list_reviews(mission_id="mission-one")[0]["review_id"] == row["review_id"]
    count = len(restored._event_order)
    assert resumed.complete(row["review_id"], result)["status"] == "approved"
    assert len(restored._event_order) == count


@pytest.mark.parametrize("field,value", [("instance_id", "another-launch"), ("revision", "another-commit"), ("plan_digest", "another-plan")])
def test_reject_mismatched_receipt_subject(field, value, acknowledged_sink):
    service = ReviewService(ControlPlaneRepo(sink=acknowledged_sink))
    row = launch(service)
    result = dict(binding(row), result={"decision": "approved"})
    result[field] = value
    with pytest.raises(ValueError, match="match"):
        service.complete(row["review_id"], result)
    assert service.get(row["review_id"])["status"] == "starting"


def test_final_receipt_cannot_be_overwritten_or_reopened(acknowledged_sink):
    service = ReviewService(ControlPlaneRepo(sink=acknowledged_sink))
    row = launch(service)
    service.complete(row["review_id"], dict(binding(row), result={"decision": "annotated", "message": "Add a test"}))
    with pytest.raises(ValueError, match="different final"):
        service.complete(row["review_id"], dict(binding(row), result={"decision": "approved"}))
    assert service.heartbeat(row["review_id"], binding(row))["status"] == "changes_requested"


def test_fail_closed_when_durable_write_fails():
    class BrokenSink:
        def record_required(self, event):
            raise RuntimeError("database unavailable")
    service = ReviewService(ControlPlaneRepo(sink=BrokenSink()))
    with pytest.raises(RuntimeError, match="database unavailable"):
        launch(service)
    assert service.get("review-one") is None


def test_heartbeat_means_launcher_alive_not_review_progress(acknowledged_sink):
    clock = [1000.0]
    service = ReviewService(ControlPlaneRepo(now=lambda: clock[0], sink=acknowledged_sink))
    row = launch(service)
    clock[0] += 100
    assert service.get(row["review_id"])["stale"]
    alive = service.heartbeat(row["review_id"], binding(row))
    assert not alive["stale"]
    assert alive["updated_at"] == row["updated_at"]
    assert alive["decision"] is None
    assert "plan" not in acknowledged_sink.events[-1]["payload"]


@pytest.mark.parametrize("url", ["javascript:alert(1)", "//evil.test", "https://user:secret@host", "http://host/?token=secret", "http://host/#token", "http://host:bad", "http://host\\@evil.test", "https://host/\npath"])
def test_reject_unsafe_or_credential_carrying_urls(url):
    with pytest.raises(ValueError):
        safe_review_url(url)


def test_dynamic_url_never_used_as_review_identity(acknowledged_sink):
    service = ReviewService(ControlPlaneRepo(sink=acknowledged_sink))
    row = launch(service, review_url="http://bender:50111")
    changed = service.heartbeat(row["review_id"], dict(binding(row), review_url="http://bender:50999"))
    assert changed["review_id"] == row["review_id"]
    assert changed["review_url"].endswith(":50999")


def test_receipts_redact_secrets_before_persistence(acknowledged_sink):
    repo = ControlPlaneRepo(sink=acknowledged_sink)
    service = ReviewService(repo)
    row = launch(service)
    service.complete(row["review_id"], dict(binding(row), result={"decision": "denied", "feedback": "Do not use password=abc123456"}))
    assert "abc123456" not in str(repo.export_state())


def test_missing_durable_sink_is_not_accepted_as_a_persisted_review():
    service = ReviewService(ControlPlaneRepo())
    with pytest.raises(RuntimeError, match="durable review storage"):
        launch(service)
    assert service.get("review-one") is None


def test_host_launcher_uses_authenticated_http_routes(tmp_path, monkeypatch, acknowledged_sink,
                                                      fake_host_config):
    import threading

    from herdr_engineering.control_plane.api import ControlPlaneServer
    from herdr_engineering.plannotator import run_review
    from herdr_engineering.session_env import _get

    fake_host_config({"agent": {"api_key": "fixture-private-key"}})
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    executable = tmp_path / "plannotator"
    executable.write_text("#!/usr/bin/python3\n" + '''import json,os,time
from pathlib import Path
Path(os.environ['PLANNOTATOR_READY_FILE']).write_text(json.dumps({'url':'http://fixture.lan:49175'}))
time.sleep(.3)
print(json.dumps({'decision':'annotated','feedback':'Add the missing test'}))
''')
    executable.chmod(0o700)
    repo = ControlPlaneRepo(sink=acknowledged_sink)
    server = ControlPlaneServer(repo, api_key="fixture-private-key")
    thread = threading.Thread(target=server.serve, daemon=True)
    thread.start()
    server.wait_until_ready()
    try:
        base = f"http://127.0.0.1:{server.port}"
        result = run_review(kind="code", cwd=tmp_path, control_plane=base,
                            binary=str(executable), revision="fixture-commit")
        assert result["decision"] == "annotated"
        rows = _get(base, "/api/v1/reviews")["reviews"]
        assert rows[0]["status"] == "changes_requested"
        assert rows[0]["review_url"] == "http://fixture.lan:49175"
        assert "feedback" not in rows[0]
        detail = _get(base, "/api/v1/reviews/" + rows[0]["review_id"])
        assert detail["feedback"] == "Add the missing test"
        assert acknowledged_sink.events[-1]["event_type"] == "PlannotatorReviewCompleted"
        restored = ControlPlaneRepo(sink=acknowledged_sink)
        restored.import_state(repo.export_state())
        assert ReviewService(restored).get(rows[0]["review_id"])["status"] == "changes_requested"
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_heartbeat_persists_without_global_notifications(acknowledged_sink):
    repo = ControlPlaneRepo(sink=acknowledged_sink)
    notifications = []
    repo.subscribe(notifications.append)
    service = ReviewService(repo)
    row = launch(service)
    notifications.clear()
    service.heartbeat(row["review_id"], binding(row))
    assert acknowledged_sink.events[-1]["event_type"] == "PlannotatorHeartbeat"
    assert notifications == []
    service.complete(row["review_id"], dict(binding(row), result={"decision": "approved"}))
    assert [event["event_type"] for event in notifications] == ["PlannotatorReviewCompleted"]


@pytest.mark.parametrize("entity_type", ["plannotator_review", "ci_pipeline"])
def test_ingest_key_cannot_forge_provider_outcomes(entity_type, acknowledged_sink):
    import json
    import threading
    import urllib.error
    import urllib.request

    from herdr_engineering.control_plane.api import ControlPlaneServer

    repo = ControlPlaneRepo(sink=acknowledged_sink)
    server = ControlPlaneServer(repo, api_keys={"fixture-ingest": "ingest", "fixture-operator": "operator"})
    thread = threading.Thread(target=server.serve, daemon=True)
    thread.start()
    server.wait_until_ready()
    base = f"http://127.0.0.1:{server.port}"
    def post(path, payload, key):
        return urllib.request.urlopen(urllib.request.Request(
            base + path, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "X-API-Key": key}, method="POST"), timeout=5)
    try:
        with pytest.raises(urllib.error.HTTPError) as error:
            post("/api/v1/ingest", {"entity_type": entity_type, "entity_id": "forged",
                                   "event_type": "PlannotatorReviewCompleted",
                                   "payload": {"review_id": "forged", "status": "approved"}}, "fixture-ingest")
        assert error.value.code == 400
        assert (entity_type, "forged") not in repo._events
        with post("/api/v1/reviews", {"review_id": "legitimate", "instance_id": "launch-one", "kind": "plan"}, "fixture-operator") as response:
            row = json.load(response)
        with post("/api/v1/reviews/legitimate/result", dict(binding(row), result={"decision": "approved"}), "fixture-operator") as response:
            assert json.load(response)["status"] == "approved"
    finally:
        server.shutdown()
        thread.join(timeout=5)

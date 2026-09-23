"""Tests for the five cross-cutting typed contracts."""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import pytest

from herdr_engineering.contracts import (
    ActivityEvent,
    ArtifactRef,
    CIEvent,
    DevServiceLease,
    TestEvent,
    to_json,
)


def test_artifact_ref_roundtrip():
    ref = ArtifactRef(id="art_1", kind="screenshot", title="shot",
                      uri="herdr-artifact://p/s/a/x.png", sha256="abc123")
    d = ref.to_dict()
    assert d["integrity"]["sha256"] == "abc123"
    assert d["producer"]["system"] == "herdr-engineering"
    # secret credentials in https uris rejected
    with pytest.raises(ValueError):
        ArtifactRef(id="a", kind="k", title="t", uri="https://user:pass@h/x")


def test_activity_event_validation_and_correlation():
    ev = ActivityEvent(id="ev_1", occurred_at="2026-09-23T00:00:00+00:00",
                       source="herdr", type="task.started", summary="build",
                       machine_id="fry", mission_id="msn_1", worktree_id="wt_1")
    assert ev.correlation["mission_id"] == "msn_1"
    with pytest.raises(ValueError):
        ActivityEvent(id="ev_2", occurred_at="x", source="nope",
                      type="task.started", summary="bad")
    with pytest.raises(ValueError):
        ActivityEvent(id="ev_3", occurred_at="x", source="herdr",
                      type="badtype", summary="bad")


def test_dev_service_lease_validation():
    lease = DevServiceLease(id="lease_1", external_port=18000, protocol="http",
                            target_machine_id="fry", target_host="127.0.0.1",
                            target_port=8080)
    d = lease.to_dict()
    assert d["target"]["port"] == 8080
    with pytest.raises(ValueError):
        DevServiceLease(id="l", external_port=70000, protocol="http",
                        target_machine_id="m", target_host="h", target_port=80)


def test_test_event():
    ev = TestEvent(run_id="r1", type="test.finished", test_id="t1",
                   result_status="failed", result_message="boom")
    d = ev.to_dict()
    assert d["result"]["status"] == "failed"
    assert d["test"]["id"] == "t1"
    with pytest.raises(ValueError):
        TestEvent(run_id="r", type="nonsense")


def test_ci_event():
    ev = CIEvent(repository="r", pipeline_id="p1", state="success",
                 capabilities_retry=True)
    d = ev.to_dict()
    assert d["capabilities"]["retry"] is True
    with pytest.raises(ValueError):
        CIEvent(repository="r", state="bogus")


def test_to_json_is_valid():
    ev = ActivityEvent(id="ev_1", occurred_at="2026-09-23T00:00:00+00:00",
                       source="git", type="git.commit", summary="c")
    import json
    assert json.loads(to_json(ev))["id"] == "ev_1"

"""Host bridge captures upstream CLI readiness and final JSON without shell parsing."""
import json
import os
from pathlib import Path

import pytest

from herdr_engineering.plannotator import command_for, run_review


def fake_cli(tmp_path, outcome=None):
    executable = tmp_path / "plannotator"
    executable.write_text("#!/usr/bin/python3\n" + '''import json,os,sys,time
from pathlib import Path
assert os.environ['PLANNOTATOR_PORT'] == '0'
assert 'HERDR_ENGINEERING_CONTROL_PLANE_KEY' not in os.environ
assert 'HERDR_CP_API_KEYS' not in os.environ
assert 'OPENAI_API_KEY' not in os.environ
assert 'GITHUB_TOKEN' not in os.environ
Path(os.environ['PLANNOTATOR_READY_FILE']).write_text(json.dumps({'url':'http://host.lan:49517','port':49517}))
time.sleep(.3)
''' + "print(" + repr(json.dumps(outcome or {"decision": "approved", "message": "Ship with tests"})) + ")\n")
    executable.chmod(0o700)
    return str(executable)


def test_host_bridge_registers_dynamic_url_and_binds_decision(tmp_path, monkeypatch):
    binary = fake_cli(tmp_path)
    monkeypatch.setenv("HERDR_ENGINEERING_CONTROL_PLANE_KEY", "private-never-in-child")
    monkeypatch.setenv("HERDR_CP_API_KEYS", "private-control-plane-keys")
    monkeypatch.setenv("OPENAI_API_KEY", "private-openai-key")
    monkeypatch.setenv("GITHUB_TOKEN", "private-github-token")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    plan = tmp_path / "original.md"
    plan.write_text("# A plan")
    calls = []
    result = run_review(kind="plan", cwd=tmp_path, control_plane="http://herdr", plan_file=plan,
                        binary=binary, revision="abc", post=lambda base, path, body: calls.append((path, body)))
    assert result["decision"] == "approved"
    assert calls[0][0] == "/api/v1/reviews"
    assert calls[0][1]["plan"] == "# A plan"
    ready = [body for path, body in calls if path.endswith("/heartbeat") and body.get("review_url")]
    assert ready[0]["review_url"] == "http://host.lan:49517"
    assert calls[-1][0].endswith("/result")
    assert calls[-1][1]["plan_digest"] == calls[0][1]["plan_digest"]
    assert not list((tmp_path / "state").rglob("*.json"))


def test_completed_receipt_retained_when_upload_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    calls = []
    def post(base, path, body):
        calls.append(path)
        if path.endswith("/result"):
            raise RuntimeError("network unavailable")
    with pytest.raises(RuntimeError, match="network unavailable"):
        run_review(kind="code", cwd=tmp_path, control_plane="http://herdr", binary=fake_cli(tmp_path),
                   revision="abc", post=post)
    receipts = list((tmp_path / "state").rglob("*.json"))
    assert len(receipts) == 1
    assert os.stat(receipts[0]).st_mode & 0o077 == 0
    assert json.loads(receipts[0].read_text())["receipt"]["result"]["decision"] == "approved"
    assert not any(p.endswith("/failure") for p in calls)


def test_durable_registration_failure_never_launches(tmp_path):
    marker = tmp_path / "launched"
    binary = tmp_path / "plannotator"
    binary.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
    binary.chmod(0o700)
    def fail(*args):
        raise RuntimeError("database offline")
    with pytest.raises(RuntimeError, match="database offline"):
        run_review(kind="code", cwd=tmp_path, control_plane="http://herdr", binary=str(binary), post=fail)
    assert not marker.exists()


def test_command_keeps_paths_as_arguments():
    assert command_for("plan", Path("a name; $danger.md")) == [
        "plannotator", "annotate", "a name; $danger.md", "--gate", "--json"]


def test_replay_private_receipt_is_idempotent_and_removes_only_after_ack(tmp_path):
    from herdr_engineering.plannotator import replay_receipt
    saved = tmp_path / "receipt.json"
    saved.write_text(json.dumps({"review_id": "rev_fixture", "receipt": {"result": {"decision": "approved"}}}))
    saved.chmod(0o600)
    def failing(*args):
        raise RuntimeError("offline")
    with pytest.raises(RuntimeError):
        replay_receipt(saved, "http://fixture", post=failing)
    assert saved.exists()
    calls = []
    replay_receipt(saved, "http://fixture", post=lambda *args: calls.append(args))
    assert calls[0][1] == "/api/v1/reviews/rev_fixture/result"
    assert not saved.exists()

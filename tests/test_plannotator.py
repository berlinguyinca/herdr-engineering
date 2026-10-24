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
    assert result["plan_digest"] == calls[0][1]["plan_digest"]   # echoed for the caller to verify
    assert result["review_id"] == calls[0][1]["review_id"]
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


def git_fixture(path, origin):
    import subprocess
    path.mkdir()
    clean = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=path, env=clean,
                                       stderr=subprocess.DEVNULL, text=True).strip()
    git("init")
    git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.test", "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null",
        "commit", "--allow-empty", "-m", "fixture")
    git("remote", "add", "origin", origin)
    return git("rev-parse", "HEAD")


def test_auto_identity_uses_actual_git_cwd_and_shared_project_mapping(tmp_path, monkeypatch):
    from herdr_engineering.plannotator import _review_identity
    target, other = tmp_path / "actual", tmp_path / "other"
    sha = git_fixture(target, "git@github.com:Owner/Backend.git")
    git_fixture(other, "https://gitlab.com/unrelated/project.git")
    mapping = tmp_path / "projects.json"
    mapping.write_text(json.dumps({"github.com/owner/backend": "shared-product"}))
    monkeypatch.setenv("HERDR_ENGINEERING_PROJECT_REPOS_FILE", str(mapping))
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(other))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "remote.origin.url")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "https://evil.example/wrong/repository")
    assert _review_identity(target, None, None, None) == ("github.com/owner/backend", "shared-product", sha)


def test_explicit_identity_overrides_preserved_and_matching_short_name_resolved(tmp_path, monkeypatch):
    from herdr_engineering.plannotator import _review_identity
    target = tmp_path / "actual"
    sha = git_fixture(target, "https://github.com/Owner/App.git")
    monkeypatch.delenv("HERDR_ENGINEERING_PROJECT_REPOS_FILE", raising=False)
    assert _review_identity(target, "Owner/App", None, None) == ("github.com/owner/app", "github.com/owner/app", sha)
    assert _review_identity(target, "Owner/Different", None, None) == ("Owner/Different", None, sha)
    assert _review_identity(target, "https://gitlab.com/other/app.git", "custom-product", "explicit-commit") == (
        "gitlab.com/other/app", "custom-product", "explicit-commit")


def test_no_git_context_gracefully_keeps_explicit_values(tmp_path, monkeypatch):
    from herdr_engineering.plannotator import _review_identity
    monkeypatch.delenv("HERDR_ENGINEERING_PROJECT_REPOS_FILE", raising=False)
    assert _review_identity(tmp_path, None, None, None) == (None, None, None)
    assert _review_identity(tmp_path, "owner/repo", "custom-project", "explicit-commit") == (
        "owner/repo", "custom-project", "explicit-commit")


def test_launcher_registers_discovered_identity_without_flags(tmp_path, monkeypatch):
    target = tmp_path / "actual"
    sha = git_fixture(target, "https://github.com/Owner/App.git")
    monkeypatch.delenv("HERDR_ENGINEERING_PROJECT_REPOS_FILE", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    calls = []
    run_review(kind="code", cwd=target, control_plane="http://herdr", binary=fake_cli(tmp_path),
               post=lambda base, path, body: calls.append((path, body)))
    assert calls[0][1]["repository"] == "github.com/owner/app"
    assert calls[0][1]["project_id"] == "github.com/owner/app"
    assert calls[0][1]["revision"] == sha
    assert calls[0][1]["mission_id"] is None
    assert calls[0][1]["session_id"] is None


def test_git_executable_unavailable_is_optional_metadata(tmp_path, monkeypatch):
    from herdr_engineering.plannotator import _review_identity
    monkeypatch.setenv("PATH", str(tmp_path))
    assert _review_identity(tmp_path, None, None, None) == (None, None, None)


def test_digest_echo_is_authoritative_and_only_for_plan_reviews(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    plan = tmp_path / "p.md"
    plan.write_text("# Plan")
    forged = {"decision": "approved", "plan_digest": "0" * 64}   # upstream output must not choose the digest
    calls = []
    result = run_review(kind="plan", cwd=tmp_path, control_plane="http://herdr", plan_file=plan,
                        binary=fake_cli(tmp_path, forged), revision="abc",
                        post=lambda base, path, body: calls.append((path, body)))
    assert result["plan_digest"] == calls[0][1]["plan_digest"] != "0" * 64
    code = run_review(kind="code", cwd=tmp_path, control_plane="http://herdr", binary=fake_cli(tmp_path),
                      revision="abc", post=lambda base, path, body: None)
    assert "plan_digest" not in code

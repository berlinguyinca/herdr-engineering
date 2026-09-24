"""Woodpecker/Pileated CI tests (spec 0130)."""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import pytest

from herdr_engineering.ci import WoodpeckerProvider
from herdr_engineering.errors import PermissionError_Engineering


class FakeCI:
    def __init__(self, payload):
        self._payload = payload
        self.requests = []
    def urlopen(self, req, timeout=10):
        import io
        import json
        self.requests.append(req.full_url)
        body = self._payload
        if isinstance(body, str):
            return io.BytesIO(body.encode())
        return io.BytesIO(json.dumps(body).encode())


def test_offline_safe_stale_view(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))  # no token file exists there
    monkeypatch.delenv("HERDR_ENGINEERING_CI_TOKEN", raising=False)
    p = WoodpeckerProvider(base_url="https://ci.example")
    assert p.available is False
    assert p.pipelines("repo") == []  # never guessed success


def test_pipelines_parsed():
    client = FakeCI([{
        "id": "p1", "number": 7, "commit": "abc123",
        "branch": "main", "status": "success",
        "can_retry": True, "can_cancel": False}])
    p = WoodpeckerProvider(base_url="https://ci.example", token="tok", client=client)
    views = p.pipelines("owner/repo")
    # Woodpecker 3.x API path
    assert client.requests == ["https://ci.example/api/v0/repos/owner/repo/pipelines"]
    assert len(views) == 1
    assert views[0].state == "success"
    assert views[0].can_retry is True and views[0].can_cancel is False


def test_repos_parsed():
    client = FakeCI([{
        "owner": {"name": "berlinguyinca"}, "name": "herdr-engineering",
        "private": False, "active": True, "branch": "main"}])
    p = WoodpeckerProvider(base_url="https://ci.example", token="tok", client=client)
    repos = p.repos()
    assert client.requests == ["https://ci.example/api/v0/repos"]
    assert repos[0]["repository"] == "berlinguyinca/herdr-engineering"


def test_pipeline_detail_and_step_log():
    client = FakeCI({"id": "9", "number": 7, "status": "success",
                     "build": {"steps": [{"name": "test", "status": "success"}]}})
    p = WoodpeckerProvider(base_url="https://ci.example", token="tok", client=client)
    detail = p.pipeline(7)
    assert client.requests[-1] == "https://ci.example/api/v0/pipelines/7"
    assert detail["build"]["steps"][0]["name"] == "test"
    client2 = FakeCI("line1\nline2\n")
    p2 = WoodpeckerProvider(base_url="https://ci.example", token="tok", client=client2)
    log = p2.step_log("owner/repo", 7, "test")
    assert client2.requests == [
        "https://ci.example/api/v0/repos/owner/repo/builds/7/logs/test"]
    assert log == "line1\nline2\n"


def test_token_from_machine_local_file(tmp_path, monkeypatch):
    tok_file = tmp_path / ".config" / "herdr-engineering" / "ci-token"
    tok_file.parent.mkdir(parents=True)
    tok_file.write_text("sekrit\n")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("HERDR_ENGINEERING_CI_TOKEN", raising=False)
    p = WoodpeckerProvider(base_url="https://ci.example")
    assert p.token == "sekrit"
    assert p.available is True


def test_retry_cancel_require_authorization():
    p = WoodpeckerProvider(base_url="https://ci.example", token="tok",
                           client=FakeCI([]))
    with pytest.raises(PermissionError_Engineering):
        p.retry("repo", "p1", authorized=False)
    assert p.retry("repo", "p1", authorized=True) is True


def test_debug_with_pi_handoff_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))  # hermetic: no token file
    monkeypatch.delenv("HERDR_ENGINEERING_CI_TOKEN", raising=False)
    p = WoodpeckerProvider()
    p.pipelines("repo")  # offline empty
    h = p.debug_with_pi_handoff("repo", __import__("herdr_engineering.ci", fromlist=["PipelineView"]).PipelineView(
        pipeline_id="p1", run_number=1, repository="r", revision="abc"), "step1", "log" * 5000)
    assert len(h["log_summary"]) == 4000  # bounded
    assert h["target_owner"] == "pi-engineering"

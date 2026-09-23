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
    def urlopen(self, req, timeout=10):
        import io
        import json
        return io.BytesIO(json.dumps(self._payload).encode())


def test_offline_safe_stale_view(monkeypatch):
    monkeypatch.delenv("HERDR_ENGINEERING_CI_TOKEN", raising=False)
    p = WoodpeckerProvider(base_url="https://ci.example", token=None)
    assert p.available is False
    assert p.pipelines("repo") == []  # never guessed success


def test_pipelines_parsed():
    p = WoodpeckerProvider(base_url="https://ci.example", token="tok",
                           client=FakeCI([{
                               "id": "p1", "number": 7, "commit": "abc123",
                               "branch": "main", "status": "success",
                               "can_retry": True, "can_cancel": False}]))
    views = p.pipelines("owner/repo")
    assert len(views) == 1
    assert views[0].state == "success"
    assert views[0].can_retry is True and views[0].can_cancel is False


def test_retry_cancel_require_authorization():
    p = WoodpeckerProvider(base_url="https://ci.example", token="tok",
                           client=FakeCI([]))
    with pytest.raises(PermissionError_Engineering):
        p.retry("repo", "p1", authorized=False)
    assert p.retry("repo", "p1", authorized=True) is True


def test_debug_with_pi_handoff_is_bounded():
    p = WoodpeckerProvider()
    p.pipelines("repo")  # offline empty
    h = p.debug_with_pi_handoff("repo", __import__("herdr_engineering.ci", fromlist=["PipelineView"]).PipelineView(
        pipeline_id="p1", run_number=1, repository="r", revision="abc"), "step1", "log" * 5000)
    assert len(h["log_summary"]) == 4000  # bounded
    assert h["target_owner"] == "pi-engineering"

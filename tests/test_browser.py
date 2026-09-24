"""Browser preview & Plannotator tests (spec 0070)."""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from herdr_engineering.browser import (
    NoopBrowserAdapter,
    PlannotatorAdapter,
    PlaywrightBrowserAdapter,
)


def test_noop_browser_fallback_is_safe():
    adapter = NoopBrowserAdapter()
    assert adapter.available is False
    cap = adapter.capture("http://127.0.0.1:9999/x", project="p", mission="m")
    assert cap.ok is False
    assert "fallback" in cap.error


def test_playwright_reports_unavailable_when_missing():
    adapter = PlaywrightBrowserAdapter()
    if not adapter.available:
        cap = adapter.capture("http://127.0.0.1/x", project="p", mission="m")
        assert cap.ok is False  # graceful degradation


def test_playwright_capture_publishes_artifact(tmp_path):
    """When Playwright is available, a capture must publish a screenshot
    artifact (spec 0070: screenshots/recordings are linked as artifacts)."""
    try:
        import playwright  # noqa: F401
    except ImportError:
        import pytest
        pytest.skip("playwright not installed")
    from herdr_engineering.artifacts import FilesystemArtifactWorkspace
    ws = FilesystemArtifactWorkspace(root=tmp_path / "art")
    adapter = PlaywrightBrowserAdapter(artifacts=ws)
    if not adapter.available:
        import pytest
        pytest.skip("playwright browser not available")
    cap = adapter.capture("data:text/html,<h1>herdr</h1>", viewport="desktop",
                          project="p", mission="m")
    assert cap.ok is True, cap.error
    assert cap.artifact is not None
    assert cap.artifact.kind == "screenshot"
    data = ws.read(cap.artifact)
    assert data is not None
    assert data[:8] == b"\x89PNG\r\n\x1a\n"  # PNG signature


def test_plannotator_review_lifecycle():
    p = PlannotatorAdapter()
    review = p.request_review(repository="r", session_id="s", candidate_id="c")
    assert review.status == "requested"
    resolved = p.resolve(review.id, "approved", comment="lgtm")
    assert resolved.status == "approved"
    assert "lgtm" in resolved.comments
    assert p.get(review.id).decision == "approved"

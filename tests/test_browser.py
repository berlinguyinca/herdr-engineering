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


def test_plannotator_review_lifecycle():
    p = PlannotatorAdapter()
    review = p.request_review(repository="r", session_id="s", candidate_id="c")
    assert review.status == "requested"
    resolved = p.resolve(review.id, "approved", comment="lgtm")
    assert resolved.status == "approved"
    assert "lgtm" in resolved.comments
    assert p.get(review.id).decision == "approved"

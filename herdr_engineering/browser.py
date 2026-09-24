"""Browser preview and Plannotator integration (spec 0070).

Browser preview captures screenshots as ArtifactRef and opens URLs tied to a
session/worktree/dev service. Uses Playwright when available; otherwise a
documented safe adapter with graceful fallback. Plannotator review adapter
preserves review identity, comments, decision and evidence links.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .contracts import ArtifactRef

VIEWPORTS = {"desktop": (1280, 800), "tablet": (768, 1024), "phone": (375, 667)}


@dataclass
class BrowserCapture:
    ok: bool
    url: str
    artifact: ArtifactRef | None = None
    console_errors: list[str] = field(default_factory=list)
    error: str | None = None


class PlaywrightBrowserAdapter:
    """Capture screenshots via Playwright when available."""

    def __init__(self, artifacts=None, enabled: bool | None = None) -> None:
        self.artifacts = artifacts
        self._enabled = enabled
        try:
            import playwright  # noqa: F401
            self._available = True
        except ImportError:
            self._available = False

    @property
    def available(self) -> bool:
        if self._enabled is False:
            return False
        return self._available

    def capture(self, url: str, viewport: str = "desktop",
                *, project: str, mission: str, machine_id: str | None = None) -> BrowserCapture:
        if not self.available:
            return BrowserCapture(ok=False, url=url,
                                  error="playwright not installed; safe fallback")
        w, h = VIEWPORTS.get(viewport, VIEWPORTS["desktop"])
        from playwright.sync_api import sync_playwright
        console_errors: list[str] = []
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch()
                page = browser.new_page(viewport={"width": w, "height": h})
                page.on("console", lambda m: console_errors.append(m.text)
                        if m.type == "error" else None)
                page.goto(url, wait_until="networkidle", timeout=30000)
                png = page.screenshot()
                browser.close()
        except Exception as exc:
            # The playwright *package* can be present while the browser binary
            # for this version is not (e.g. `playwright install` not run). Degrade
            # gracefully instead of crashing the caller.
            return BrowserCapture(ok=False, url=url,
                                  error=f"browser launch failed: {exc}")
        art = None
        if self.artifacts:
            res = self.artifacts.publish_bytes(
                title=f"screenshot {url}", kind="screenshot", data=png,
                media_type="image/png", project=project,
                mission_or_session=mission, machine_id=machine_id, rel_name="shot.png")
            art = res.artifact
        return BrowserCapture(ok=True, url=url, artifact=art,
                              console_errors=console_errors[:20])


class NoopBrowserAdapter(PlaywrightBrowserAdapter):
    """Documented safe fallback when Playwright/CDP is unavailable."""

    def __init__(self, artifacts=None) -> None:
        super().__init__(artifacts, enabled=False)


@dataclass
class ReviewRecord:
    id: str
    repository: str | None = None
    session_id: str | None = None
    candidate_id: str | None = None
    status: str = "requested"      # requested|in_progress|approved|changes_requested|closed
    decision: str | None = None
    comments: list[str] = field(default_factory=list)
    evidence_links: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items()}


class PlannotatorAdapter:
    """Review integration adapter (Plannotator remains review authority)."""

    def __init__(self, store=None) -> None:
        self._store = store or {}
        self._reviews: dict[str, ReviewRecord] = {}

    def request_review(self, *, repository: str | None = None,
                       session_id: str | None = None,
                       candidate_id: str | None = None,
                       evidence_links: list[str] | None = None) -> ReviewRecord:
        import uuid
        rid = f"rev_{uuid.uuid4().hex[:16]}"
        review = ReviewRecord(id=rid, repository=repository, session_id=session_id,
                              candidate_id=candidate_id,
                              evidence_links=evidence_links or [])
        self._reviews[rid] = review
        return review

    def resolve(self, review_id: str, decision: str, comment: str | None = None) -> ReviewRecord:
        review = self._reviews.get(review_id)
        if review is None:
            raise KeyError(review_id)
        review.status = decision
        review.decision = decision
        if comment:
            review.comments.append(comment)
        return review

    def get(self, review_id: str) -> ReviewRecord | None:
        return self._reviews.get(review_id)

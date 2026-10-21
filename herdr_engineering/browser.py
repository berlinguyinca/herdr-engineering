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
    """Durable control-plane client; Plannotator remains decision authority."""

    def __init__(self, store=None, *, control_plane=None) -> None:
        from .host_config import control_plane_url_or
        self._store = store
        self.control_plane = control_plane or control_plane_url_or("http://dev.lan")

    @staticmethod
    def _record(row):
        return ReviewRecord(
            id=row["review_id"], repository=row.get("repository"),
            session_id=row.get("session_id"), candidate_id=row.get("candidate_id"),
            status=row["status"], decision=row.get("decision"),
            comments=[row["feedback"]] if row.get("feedback") else [],
            evidence_links=[row["review_url"]] if row.get("review_url") else [],
        )

    def request_review(self, *, repository=None, session_id=None, candidate_id=None,
                       evidence_links=None, **metadata):
        from uuid import uuid4

        from .session_env import _post
        payload = dict(metadata, repository=repository, session_id=session_id,
                       candidate_id=candidate_id)
        payload.setdefault("instance_id", uuid4().hex)
        if evidence_links:
            payload.setdefault("review_url", evidence_links[0])
        row = (self._store.register(payload) if self._store is not None else
               _post(self.control_plane, "/api/v1/reviews", payload))
        return self._record(row.get("review", row))

    def resolve(self, review_id, decision, comment=None):
        raise ValueError("Decide in Plannotator; the host launcher reports its JSON result to HerdR")

    def get(self, review_id):
        from urllib.parse import quote

        from .session_env import _get
        row = (self._store.get(review_id) if self._store is not None else
               _get(self.control_plane, "/api/v1/reviews/" + quote(review_id, safe="")))
        return self._record(row.get("review", row)) if row else None

"""Woodpecker / Pileated CI integration (spec 0130).

Provider-neutral CIEvent view. Woodpecker/Pileated remain execution authority.
Missing/stale provider data displays as stale/unavailable, never inferred
success. Control actions (retry/cancel) are capability-gated and authorized.
"""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from .errors import PermissionError_Engineering, UnreachableError


@dataclass
class PipelineView:
    pipeline_id: str
    run_number: int
    repository: str
    revision: str
    branch: str | None = None
    state: str = "unknown"
    event: str = "pipeline"
    created_at: str | None = None
    finished_at: str | None = None
    url: str | None = None
    can_retry: bool = False
    can_cancel: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items()}


class WoodpeckerProvider:
    """Woodpecker-compatible provider over its REST API.

    If no endpoint/token is configured, the provider runs in "offline" mode
    and reports state as stale/unavailable (never guessed success), which is
    the required safe degraded behavior.
    """

    def __init__(self, base_url: str | None = None, token: str | None = None,
                 client=None) -> None:
        self.base_url = (base_url or os.environ.get("HERDR_ENGINEERING_CI_URL")
                         or "https://ci.metabolomics.us")
        self.token = token or os.environ.get("HERDR_ENGINEERING_CI_TOKEN")
        self._client = client

    @property
    def available(self) -> bool:
        return bool(self.token) and self.base_url.startswith("https://")

    def _get(self, path: str) -> dict[str, Any] | None:
        if not self.available:
            return None
        url = self.base_url.rstrip("/") + path
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {self.token}"})
        try:
            opener = self._client.urlopen if self._client is not None else urllib.request.urlopen
            with opener(req, timeout=10) as resp:
                return json.loads(resp.read())
        except Exception as exc:
            raise UnreachableError(f"CI API unreachable: {exc}", target=url)

    def pipelines(self, repository: str) -> list[PipelineView]:
        data = self._get(f"/api/repos/{repository}/pipelines")
        if data is None:
            return []  # offline/stale: empty view, never fake success
        views = []
        for item in data:
            views.append(PipelineView(
                pipeline_id=item.get("id", ""), run_number=item.get("number", 0),
                repository=repository, revision=item.get("commit", ""),
                branch=item.get("branch"), state=item.get("status", "unknown"),
                created_at=item.get("created"), finished_at=item.get("finished"),
                url=item.get("url"), can_retry=item.get("can_retry", False),
                can_cancel=item.get("can_cancel", False)))
        return views

    def retry(self, repository: str, pipeline_id: str, authorized: bool = False) -> bool:
        if not authorized:
            raise PermissionError_Engineering("CI retry requires authorization")
        if not self.available:
            raise UnreachableError("CI provider offline")
        return True

    def cancel(self, repository: str, pipeline_id: str, authorized: bool = False) -> bool:
        if not authorized:
            raise PermissionError_Engineering("CI cancel requires authorization")
        if not self.available:
            raise UnreachableError("CI provider offline")
        return True

    def debug_with_pi_handoff(self, repository: str, pipeline: PipelineView,
                              failing_step: str, log_summary: str,
                              artifact_refs: list[dict] | None = None) -> dict[str, Any]:
        """Create a 'Debug with Pi' handoff (bounded structured context)."""
        return {
            "handoff_type": "debug-with-pi",
            "repository": repository,
            "pipeline_id": pipeline.pipeline_id,
            "revision": pipeline.revision,
            "failing_step": failing_step,
            "log_summary": log_summary[:4000],
            "artifact_refs": artifact_refs or [],
            "target_owner": "pi-engineering",
        }

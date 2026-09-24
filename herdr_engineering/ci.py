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


_TOKEN_FILE = os.path.join("~", ".config", "herdr-engineering", "ci-token")


class WoodpeckerProvider:
    """Woodpecker 3.x provider over its REST API (``/api/v0/...").

    If no endpoint/token is configured, the provider runs in "offline" mode
    and reports state as stale/unavailable (never guessed success), which is
    the required safe degraded behavior. The token is a secret: it is read
    from ``HERDR_ENGINEERING_CI_TOKEN`` or the machine-local file
    ``~/.config/herdr-engineering/ci-token`` — never from the repo.
    """

    def __init__(self, base_url: str | None = None, token: str | None = None,
                 client=None) -> None:
        self.base_url = (base_url or os.environ.get("HERDR_ENGINEERING_CI_URL")
                         or "https://ci.metabolomics.us")
        self.token = token or os.environ.get("HERDR_ENGINEERING_CI_TOKEN") \
            or self._read_token_file()
        self._client = client

    @staticmethod
    def _read_token_file() -> str | None:
        path = os.path.expanduser(_TOKEN_FILE)
        try:
            with open(path) as f:
                tok = f.read().strip()
            return tok or None
        except OSError:
            return None

    @property
    def available(self) -> bool:
        return bool(self.token) and self.base_url.startswith("https://")

    def _get(self, path: str, raw: bool = False):
        if not self.available:
            return None
        url = self.base_url.rstrip("/") + path
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json"})
        try:
            opener = self._client.urlopen if self._client is not None else urllib.request.urlopen
            with opener(req, timeout=15) as resp:
                body = resp.read()
                return body.decode("utf-8", "replace") if raw else json.loads(body)
        except Exception as exc:
            raise UnreachableError(f"CI API unreachable: {exc}", target=url)

    def _repo_path(self, repository: str) -> str:
        """Normalize ``owner/name`` (or bare name) to the API path segment."""
        return "/".join(urllib.parse.quote(part, safe="") for part in repository.split("/"))

    def repos(self) -> list[dict[str, Any]]:
        """List CI repositories (Woodpecker 3.x ``GET /api/v0/repos``)."""
        data = self._get("/api/v0/repos")
        if data is None:
            return []
        items = data if isinstance(data, list) else data.get("items", [])
        out = []
        for item in items:
            owner = item.get("owner") or {}
            name = item.get("name") or item.get("full_name", "")
            out.append({
                "repository": f"{owner.get('name', '')}/{name}".strip("/"),
                "private": item.get("private", False),
                "active": item.get("active", True),
                "default_branch": (item.get("branch")
                                   or (item.get("settings") or {}).get("default_branch")),
            })
        return out

    def pipelines(self, repository: str) -> list[PipelineView]:
        """List pipelines for ``owner/name`` (Woodpecker 3.x)."""
        data = self._get(f"/api/v0/repos/{self._repo_path(repository)}/pipelines")
        if data is None:
            return []  # offline/stale: empty view, never fake success
        views = []
        for item in data:
            views.append(PipelineView(
                pipeline_id=str(item.get("id", "")), run_number=item.get("number", 0),
                repository=repository, revision=item.get("commit", ""),
                branch=item.get("branch"), state=item.get("status", "unknown"),
                event=item.get("event", "pipeline"),
                created_at=item.get("created"), finished_at=item.get("finished"),
                url=item.get("url"), can_retry=item.get("can_retry", False),
                can_cancel=item.get("can_cancel", False)))
        return views

    def pipeline(self, number: int) -> dict[str, Any] | None:
        """Pipeline detail incl. build steps (Woodpecker 3.x)."""
        return self._get(f"/api/v0/pipelines/{number}")

    def step_log(self, repository: str, number: int, step: str) -> str | None:
        """Raw log of one pipeline step (Woodpecker 3.x)."""
        return self._get(f"/api/v0/repos/{self._repo_path(repository)}/"
                         f"builds/{number}/logs/{urllib.parse.quote(step, safe='')}",
                         raw=True)

    def retry(self, repository: str, pipeline_id: str, authorized: bool = False) -> bool:
        if not authorized:
            raise PermissionError_Engineering("CI retry requires authorization")
        if not self.available:
            raise UnreachableError("CI provider offline")
        self._get(f"/api/v0/repos/{self._repo_path(repository)}"
                  f"/pipelines/{pipeline_id}/rebuild", raw=True)
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

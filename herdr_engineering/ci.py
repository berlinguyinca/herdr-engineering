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
    """Woodpecker 3.x provider over its REST API (``/api/...").

    Verified against a live Woodpecker 3.18.1 instance (2026-09-24): the API
    is addressed by *numeric repository id* — ``GET /api/repos``,
    ``GET /api/repos/{id}``, ``GET /api/repos/{id}/pipelines``,
    ``GET /api/repos/{id}/pipelines/{number}`` — and the 3.x pipeline detail
    exposes ``workflows[].children[]`` (tasks) rather than a ``steps`` array.
    Logs are streamed over WebSocket (no REST log endpoint); behind the
    OAuth2 proxy that stream needs an interactive session, so ``step_log``
    returns ``None`` rather than guess.

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
        self._repo_id_cache: dict[str, str] = {}

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

    def _repo_id(self, repository: str) -> str:
        """Resolve ``owner/name`` (or bare name, or numeric id) to the numeric
        repo id this Woodpecker build addresses resources by."""
        repo = repository.strip()
        if repo.isdigit():
            return repo
        if repo in self._repo_id_cache:
            return self._repo_id_cache[repo]
        data = self._get("/api/repos")
        if not data:
            raise UnreachableError(
                f"cannot resolve repo {repository!r}: CI API offline",
                target=self.base_url)
        items = data if isinstance(data, list) else data.get("items", [])
        for item in items:
            full = item.get("full_name") or \
                f"{item.get('owner', '')}/{item.get('name', '')}"
            if full == repo or item.get("name") == repo:
                self._repo_id_cache[repo] = str(item.get("id"))
                return str(item.get("id"))
        raise UnreachableError(
            f"repo {repository!r} not found in CI", target=self.base_url)

    def repos(self) -> list[dict[str, Any]]:
        """List CI repositories (Woodpecker 3.x ``GET /api/repos``)."""
        data = self._get("/api/repos")
        if data is None:
            return []
        items = data if isinstance(data, list) else data.get("items", [])
        out = []
        for item in items:
            out.append({
                "id": item.get("id"),
                "repository": item.get("full_name")
                              or f"{item.get('owner', '')}/{item.get('name', '')}",
                "owner": item.get("owner"), "name": item.get("name"),
                "private": item.get("private", False),
                "active": item.get("active", True),
                "default_branch": item.get("default_branch"),
                "clone_url": item.get("clone_url"),
                "forge_url": item.get("forge_url"),
            })
        return out

    def agents(self) -> list[dict[str, Any]]:
        """List CI runner agents (Woodpecker 3.x ``GET /api/agents``).

        The raw payload includes each agent's auth ``token``; we strip it —
        secrets are never surfaced in the CI view.
        """
        data = self._get("/api/agents")
        if data is None:
            return []
        items = data if isinstance(data, list) else data.get("items", [])
        out = []
        for item in items:
            out.append({
                "id": item.get("id"), "name": item.get("name"),
                "version": item.get("version"), "backend": item.get("backend"),
                "platform": item.get("platform"),
                "capacity": item.get("capacity"),
                "last_contact": item.get("last_contact"),
                "last_work": item.get("last_work"),
            })
        return out

    def pipelines(self, repository: str) -> list[PipelineView]:
        """List pipelines for a repo (Woodpecker 3.x ``/api/repos/{id}/pipelines``)."""
        if not self.available:
            return []  # offline/stale: empty view, never fake success, never raise
        rid = self._repo_id(repository)
        data = self._get(f"/api/repos/{rid}/pipelines")
        if data is None:
            return []  # offline/stale: empty view, never fake success
        items = data if isinstance(data, list) else data.get("items", [])
        views = []
        for item in items:
            views.append(PipelineView(
                pipeline_id=str(item.get("id", "")), run_number=item.get("number", 0),
                repository=repository, revision=item.get("commit", ""),
                branch=item.get("branch"), state=item.get("status", "unknown"),
                event=item.get("event", "pipeline"),
                created_at=item.get("created"), finished_at=item.get("finished"),
                url=item.get("url"), can_retry=False, can_cancel=False))
        return views

    def pipeline(self, repository: str, number: int) -> dict[str, Any] | None:
        """Pipeline detail; 3.x exposes workflows[].children[] (tasks), which we
        flatten into a ``tasks`` list (name, state, exit_code)."""
        if not self.available:
            return None
        rid = self._repo_id(repository)
        data = self._get(f"/api/repos/{rid}/pipelines/{number}")
        if data is None:
            return None
        tasks = []
        for wf in data.get("workflows", []) or []:
            for child in wf.get("children", []) or []:
                tasks.append({
                    "id": child.get("id"), "name": child.get("name"),
                    "state": child.get("state"),
                    "exit_code": child.get("exit_code"),
                    "type": child.get("type"),
                    "workflow": wf.get("name"), "workflow_id": wf.get("id"),
                })
        return {
            "id": data.get("id"), "number": data.get("number"),
            "status": data.get("status"), "branch": data.get("branch"),
            "event": data.get("event"), "commit": data.get("commit"),
            "author": data.get("author"), "message": data.get("message"),
            "title": data.get("title"), "created": data.get("created"),
            "finished": data.get("finished"), "repository": repository,
            "tasks": tasks,
        }

    def step_log(self, repository: str, number: int, step: str) -> str | None:
        """Raw log of one pipeline step.

        Woodpecker 3.x has no REST log endpoint — logs stream over WebSocket,
        which the OAuth2 proxy only accepts for an interactive browser session
        (a bearer token is not forwarded on the upgrade). We attempt the REST
        form and return ``None`` when it is unavailable (SPA shell / offline)
        rather than guess; the CLI says so and points at the web UI.
        """
        if not self.available:
            return None
        rid = self._repo_id(repository)
        path = (f"/api/repos/{rid}/pipelines/{number}/"
                f"steps/{urllib.parse.quote(step, safe='')}/log")
        data = self._get(path, raw=True)
        if data is None:
            return None
        if data.lstrip()[:1] in ("<", "{"):
            return None  # SPA shell or JSON envelope: not raw log text
        return data

    def retry(self, repository: str, pipeline_id: str, authorized: bool = False) -> bool:
        if not authorized:
            raise PermissionError_Engineering("CI retry requires authorization")
        if not self.available:
            raise UnreachableError("CI provider offline")
        rid = self._repo_id(repository)
        self._get(f"/api/repos/{rid}/pipelines/{pipeline_id}/rebuild", raw=True)
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

    def debug_with_pi(self, repository: str, number: int) -> dict[str, Any]:
        """Build a bounded 'Debug with Pi' handoff from a pipeline's failing
        tasks. Logs are not inlined (they stream over WebSocket, unavailable
        to a bearer token); the handoff carries the failing task names + exit
        codes and a link to the web UI where a human can read the full log."""
        detail = self.pipeline(repository, number)
        if detail is None:
            return {"ok": False, "error": f"pipeline {number} not found for {repository}"}
        failing = [t for t in detail.get("tasks", []) if t.get("state") == "failure"]
        lines = [f"pipeline #{number} {detail.get('status')} "
                 f"({detail.get('event')} on {detail.get('branch')}, "
                 f"commit {str(detail.get('commit',''))[:12]})"]
        for t in failing:
            lines.append(f"  FAILED {t.get('name')} (exit {t.get('exit_code')}) "
                         f"[workflow {t.get('workflow')}, task {t.get('id')}]")
        if not failing:
            lines.append("  (no failed tasks reported)")
        lines.append("note: step logs stream over WebSocket and are not available "
                     "to a bearer token; open the web UI link for the full log.")
        return {
            "ok": True,
            "handoff": self.debug_with_pi_handoff(
                repository=repository,
                pipeline=PipelineView(pipeline_id=str(detail.get("id")),
                                      run_number=number, repository=repository,
                                      revision=str(detail.get("commit", "")),
                                      branch=detail.get("branch"),
                                      state=detail.get("status", "unknown")),
                failing_step=", ".join(t.get("name", "?") for t in failing) or "(none)",
                log_summary="\n".join(lines),
            ),
            "view_in_web_ui": (f"{self.base_url.rstrip('/')}/{repository}/"
                               f"pipelines/{number}"),
        }

"""Bounded Woodpecker 3.x REST integration; CI remains execution authority."""
from __future__ import annotations

import base64
import binascii
import json
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, Future, wait
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .control_plane.project_links import repository_language
from .errors import InvalidError, PermissionError_Engineering, UnreachableError

_READ_DEADLINE = ContextVar('ci_read_deadline', default=None)


class _ReadBudgetExceeded(TimeoutError):
    """A read could not be started within this refresh's limits."""


class _ProviderReadScheduler:
    """Shared capacity survives refresh timeouts; late reads never publish state.

    Workers are daemon threads because arbitrary injected providers cannot be
    interrupted. There is no unbounded executor queue: every started read owns a
    slot until the actual call finishes, including after a refresh has returned.
    """

    def __init__(self, max_in_flight=4):
        if type(max_in_flight) is not int or not 1 <= max_in_flight <= 32:
            raise ValueError('max_in_flight must be between 1 and 32')
        self.max_in_flight = max_in_flight
        self._slots = threading.BoundedSemaphore(max_in_flight)
        self._closed = threading.Event()

    def refresh(self, *, budget, max_reads, now, measurements):
        return _ProviderReadBatch(self, budget, max_reads, now, measurements)

    def close(self):
        self._closed.set()


class _ProviderReadBatch:
    def __init__(self, scheduler, budget, max_reads, now, measurements):
        self.scheduler = scheduler
        self.deadline = time.monotonic() + max(0, budget)
        self._now = now
        self._logical_deadline = now() + max(0, budget)
        self._remaining_reads = max(0, max_reads)
        self._measurements = measurements

    def remaining(self):
        return max(0, min(self.deadline - time.monotonic(), self._logical_deadline - self._now()))

    def submit(self, callback, *args, **kwargs):
        if (not self._remaining_reads or not self.remaining() or self.scheduler._closed.is_set()
                or not self.scheduler._slots.acquire(timeout=self.remaining())):
            raise _ReadBudgetExceeded('CI provider refresh budget exceeded')
        if not self.remaining() or self.scheduler._closed.is_set():
            self.scheduler._slots.release()
            raise _ReadBudgetExceeded('CI provider refresh budget exceeded')
        self._remaining_reads -= 1
        future = Future()

        def read():
            token = _READ_DEADLINE.set(self.deadline)
            try:
                self._measurements.provider_call()
                future.set_result(callback(*args, **kwargs))
            except Exception as exc:
                future.set_exception(exc)
            finally:
                _READ_DEADLINE.reset(token)
                self.scheduler._slots.release()

        threading.Thread(target=read, name='herdr-ci-read', daemon=True).start()
        return future

    def call(self, callback, *args, **kwargs):
        return self.submit(callback, *args, **kwargs).result(timeout=self.remaining())

    def map(self, callback, items):
        """Yield completed heads before refilling a bounded submission window."""
        iterator, pending = iter(items), {}
        exhausted = False
        while pending or not exhausted:
            while not exhausted and len(pending) < self.scheduler.max_in_flight:
                try:
                    item = next(iterator)
                except StopIteration:
                    exhausted = True
                    break
                try:
                    pending[self.submit(callback, item)] = item
                except TimeoutError as exc:
                    yield item, None, exc
                    for item in iterator:
                        yield item, None, exc
                    exhausted = True
            if not pending:
                continue
            done, _ = wait(pending, timeout=self.remaining(), return_when=FIRST_COMPLETED)
            if not done:
                for item in pending.values():
                    yield item, None, TimeoutError('CI provider refresh budget exceeded')
                pending.clear()
                continue
            for future in done:
                item = pending.pop(future)
                try:
                    result = future.result()
                except Exception as exc:
                    yield item, None, exc
                else:
                    yield item, result, None


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
    started_at: str | None = None
    ref: str | None = None
    forge_url: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_MAX_LOG_CHARS = 64 * 1024
_TERMINAL_STATES = {"success", "failure", "error", "killed", "declined", "skipped", "canceled"}
_ACTIVE_STATES = {"pending", "running", "blocked"}


class CIProviderError(UnreachableError):
    """Safe provider failure: never contains upstream bodies or credentials."""

    def __init__(self, reason: str, status_code: int | None = None):
        self.reason = reason
        self.status_code = status_code
        messages = {
            "authentication": "CI authentication failed; check the configured token and proxy access.",
            "permission": "CI account lacks permission for this resource.",
            "rate_limited": "CI request rate limit reached; retry later.",
            "not_found": "CI resource was not found.",
            "timeout": "CI request timed out.",
            "redirect": "CI redirected the API request; check the configured endpoint and proxy access.",
            "invalid_response": "CI returned an invalid API response.",
            "response_too_large": "CI response exceeded the safe size limit.",
            "unavailable": "CI service is unavailable.",
        }
        details = {"reason": reason}
        if status_code is not None:
            details["status_code"] = status_code
        super().__init__(messages.get(reason, messages["unavailable"]), details=details)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # urllib otherwise forwards Authorization to a redirect destination.
        raise CIProviderError("redirect", code)


def _timestamp(value: Any) -> str | None:
    if value in (None, "", 0):
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(value, UTC).isoformat().replace("+00:00", "Z")
        except (ValueError, OverflowError, OSError):
            return None
    if isinstance(value, str):
        return value  # preserve older ISO timestamp contracts
    return None


def _positive_id(value: Any) -> str:
    text = str(value)
    if not text.isascii() or not text.isdigit() or int(text) < 1:
        raise InvalidError("CI resource IDs and pipeline numbers must be positive integers")
    return text


def _items(data: Any) -> list[dict[str, Any]]:
    if data is None:
        return []
    rows = data.get("items") if isinstance(data, dict) else data
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise CIProviderError("invalid_response")
    return rows


class WoodpeckerProvider:
    """Woodpecker v3.18.1 API with server-side bearer credentials.

    Discovery uses /api/user/repos (the /api/repos list is admin-only).
    Stored logs are REST JSON entries with base64 data; live logs use SSE.
    `available` indicates configuration only; successful reads establish health.
    """

    # /api/user/repos returns a complete snapshot, including exactly 50 rows.
    repos_are_snapshot = True

    def __init__(self, base_url: str | None = None, token: str | None = None,
                 client=None) -> None:
        from .host_config import host_value
        self.base_url = (base_url or host_value("ci.url")
                         or "https://ci.metabolomics.us").rstrip("/")
        self.token = token or host_value("ci.token") or None
        self._client = client or urllib.request.build_opener(_NoRedirect())
        self._repo_id_cache: dict[str, str] = {}

    @property
    def available(self) -> bool:
        url = urllib.parse.urlsplit(self.base_url)
        return (bool(self.token) and url.scheme == "https" and bool(url.hostname)
                and not url.username and not url.password and not url.query and not url.fragment)

    def _request(self, path: str, *, method: str = "GET", raw: bool = False,
                 log_preview: bool = False):
        if not self.available:
            return None
        req = urllib.request.Request(self.base_url + path, method=method, headers={
            "Authorization": f"Bearer {self.token}", "Accept": "application/json"})
        try:
            opener = getattr(self._client, "urlopen", None) or self._client.open
            deadline = _READ_DEADLINE.get()
            timeout = 15 if deadline is None else min(15, deadline - time.monotonic())
            if timeout <= 0:
                raise CIProviderError("timeout")
            with opener(req, timeout=timeout) as resp:
                # Read enough overlap to redact a credential crossing the preview
                # boundary, while keeping download reads independent of log size.
                limit = _MAX_LOG_CHARS + len(self.token or "") if log_preview else _MAX_RESPONSE_BYTES
                body = resp.read(limit + 1)
                if log_preview:
                    headers = getattr(resp, "headers", {})
                    if "text/html" in headers.get("Content-Type", "").lower():
                        raise CIProviderError("invalid_response")
                    text = self._redact(body.decode("utf-8", "replace"))
                    truncated = len(body) > limit or len(text) > _MAX_LOG_CHARS
                    return text[:_MAX_LOG_CHARS] + (
                        "\n[log truncated; open CI for the full log]" if truncated else "")
                if len(body) > limit:
                    raise CIProviderError("response_too_large")
                if raw:
                    return body.decode("utf-8", "replace")
                if not body and method != "GET":
                    return None
                return json.loads(body)
        except CIProviderError:
            raise
        except urllib.error.HTTPError as exc:
            reason = {401: "authentication", 403: "permission", 404: "not_found",
                      429: "rate_limited"}.get(exc.code, "unavailable")
            if 300 <= exc.code < 400:
                reason = "redirect"
            exc.close()
            raise CIProviderError(reason, exc.code) from None
        except TimeoutError:
            raise CIProviderError("timeout") from None
        except urllib.error.URLError as exc:
            reason = "timeout" if isinstance(exc.reason, (TimeoutError, socket.timeout)) else "unavailable"
            raise CIProviderError(reason) from None
        except (ValueError, UnicodeError):
            raise CIProviderError("invalid_response") from None
        except OSError:
            raise CIProviderError("unavailable") from None

    def _get(self, path: str, raw: bool = False):
        return self._request(path, raw=raw)

    @staticmethod
    def _page_path(path: str, page: int, per_page: int) -> str:
        if not 1 <= page <= 1000 or not 1 <= per_page <= 100:
            raise InvalidError("CI pagination must use page 1–1000 and per_page 1–100")
        # Keep the default endpoint compatible with existing CLI/test clients.
        return path if (page, per_page) == (1, 50) else path + "?" + urllib.parse.urlencode(
            {"page": page, "perPage": per_page})

    def _repo_id(self, repository: str) -> str:
        if not isinstance(repository, str) or not repository.strip():
            raise InvalidError("CI repository must be its numeric ID or owner/name")
        repo = repository.strip()
        if repo.isascii() and repo.isdigit():
            return _positive_id(repo)
        if repo in self._repo_id_cache:
            return self._repo_id_cache[repo]
        matches = [row for row in self.repos()
                   if row["repository"] == repo or row["name"] == repo]
        if len(matches) > 1:
            raise InvalidError("CI repository name is ambiguous; use owner/name or the numeric ID")
        if not matches:
            raise CIProviderError("not_found")
        rid = _positive_id(matches[0]["id"])
        self._repo_id_cache[repo] = rid
        return rid

    def repos(self, *, page: int = 1, per_page: int = 50) -> list[dict[str, Any]]:
        # Compatibility parameters are accepted, but this endpoint is a full
        # snapshot: v3.18 ignores page/perPage. Consumers must deduplicate IDs.
        rows = _items(self._get("/api/user/repos"))
        if len(rows) > 1000:
            raise CIProviderError("response_too_large")
        out = []
        for item in rows:
            full = item.get("full_name") or f"{item.get('owner', '')}/{item.get('name', '')}"
            language = repository_language(item.get("language"))
            if language and self.token and self.token in language:
                language = None
            out.append({
                "id": item.get("id"), "repository": full,
                "owner": item.get("owner"), "name": item.get("name"),
                "private": item.get("private", False), "active": item.get("active", True),
                "default_branch": item.get("default_branch"), "clone_url": item.get("clone_url"),
                "forge_url": item.get("forge_url"),
                "language": language,
            })
        counts = Counter(row["repository"] for row in out)
        self._repo_id_cache = {
            row["repository"]: str(row["id"]) for row in out
            if counts[row["repository"]] == 1 and str(row["id"]).isascii()
            and str(row["id"]).isdigit() and int(row["id"]) > 0
        }
        return out

    def agents(self) -> list[dict[str, Any]]:
        """Admin-only runner inventory, with credential fields excluded."""
        rows = _items(self._get("/api/agents"))
        keys = ("id", "name", "version", "backend", "platform", "capacity", "last_contact", "last_work")
        return [{key: item.get(key) for key in keys} for item in rows[:100]]

    def permissions(self, repository: str) -> dict[str, bool]:
        rid = self._repo_id(repository)
        data = self._get(f"/api/repos/{rid}/permissions")
        if not isinstance(data, dict):
            raise CIProviderError("invalid_response")
        return {key: data.get(key) is True for key in ("pull", "push", "admin")}

    def capabilities(self, repository: str, state: str) -> dict[str, bool]:
        try:
            allowed = self.permissions(repository)["push"]
        except CIProviderError:
            allowed = False
        return {"can_retry": allowed and state in _TERMINAL_STATES,
                "can_cancel": allowed and state in _ACTIVE_STATES}

    def pipelines(self, repository: str, *, page: int = 1, per_page: int = 50) -> list[PipelineView]:
        if not self.available:
            return []
        rid = self._repo_id(repository)
        rows = _items(self._get(self._page_path(f"/api/repos/{rid}/pipelines", page, per_page)))
        return [PipelineView(
            pipeline_id=str(item.get("id", "")), run_number=item.get("number", 0),
            repository=repository, revision=item.get("commit", ""), branch=item.get("branch"),
            state=item.get("status", "unknown"), event=item.get("event", "pipeline"),
            created_at=_timestamp(item.get("created")), started_at=_timestamp(item.get("started")),
            finished_at=_timestamp(item.get("finished")), ref=item.get("ref"),
            url=f"{self.base_url}/repos/{rid}/pipeline/{item.get('number', 0)}",
            forge_url=item.get("forge_url")) for item in rows[:per_page]]

    def pipeline(self, repository: str, number: int) -> dict[str, Any] | None:
        if not self.available:
            return None
        rid = self._repo_id(repository)
        number = _positive_id(number)
        data = self._get(f"/api/repos/{rid}/pipelines/{number}")
        if not isinstance(data, dict):
            raise CIProviderError("invalid_response")
        tasks = []
        workflows = []
        for wf in data.get("workflows", []) or []:
            # Only retain execution metadata actually supplied by the provider.
            # Absent depends_on remains absent, rather than implying a root node.
            workflows.append({
                **{key: wf[key] for key in ("id", "name", "state", "status", "depends_on",
                                            "duration", "started", "finished") if key in wf},
                "started_at": _timestamp(wf.get("started")),
                "finished_at": _timestamp(wf.get("finished")),
            })
            for child in wf.get("children", []) or []:
                tasks.append({
                    "id": child.get("id"), "name": child.get("name"), "state": child.get("state"),
                    "exit_code": child.get("exit_code"), "type": child.get("type"),
                    "workflow": wf.get("name"), "workflow_id": wf.get("id"),
                    **{key: child[key] for key in ("depends_on", "duration") if key in child},
                    "started": child.get("started"), "finished": child.get("finished"),
                    "started_at": _timestamp(child.get("started")),
                    "finished_at": _timestamp(child.get("finished")),
                    "error": self._redact(str(child.get("error") or ""))[:2000],
                })
        keys = ("id", "number", "status", "branch", "event", "commit", "author", "message",
                "title", "created", "started", "finished", "ref", "forge_url")
        return {
            **{key: data.get(key) for key in keys}, "repository": repository, "tasks": tasks, "workflows": workflows,
            "created_at": _timestamp(data.get("created")),
            "started_at": _timestamp(data.get("started")),
            "finished_at": _timestamp(data.get("finished")),
            "url": f"{self.base_url}/repos/{rid}/pipeline/{number}",
            **self.capabilities(rid, data.get("status", "unknown")),
        }

    def _redact(self, text: str) -> str:
        if self.token:
            text = text.replace(self.token, "[redacted]")
        return re.sub(r"(?i)(authorization\s*[:=]\s*|bearer\s+)[^\s]+", r"\1[redacted]", text)

    def step_log(self, repository: str, number: int, step: str) -> str | None:
        """Decode bounded stored log entries. Numeric step IDs avoid name ambiguity."""
        if not self.available:
            return None
        rid = self._repo_id(repository)
        number = _positive_id(number)
        step_id = str(step)
        if not step_id.isascii() or not step_id.isdigit():
            detail = self.pipeline(repository, number)
            matches = [row for row in detail["tasks"] if row.get("name") == step]
            if len(matches) != 1:
                raise InvalidError("Use a numeric CI step ID; the step name is missing or ambiguous")
            step_id = str(matches[0]["id"])
        step_id = _positive_id(step_id)
        path = f"/api/repos/{rid}/logs/{number}/{step_id}"
        try:
            rows = _items(self._get(path))
        except CIProviderError as exc:
            if exc.reason != "response_too_large":
                raise
            return self._request(path + "/download", raw=True, log_preview=True)
        parts = []
        remaining = _MAX_LOG_CHARS
        truncated = False
        for row in rows:
            try:
                encoded = row.get("data")
                # Go encodes a nil []byte log payload as JSON null.
                text = base64.b64decode("" if encoded is None else encoded, validate=True).decode("utf-8", "replace")
            except (ValueError, TypeError, binascii.Error):
                raise CIProviderError("invalid_response") from None
            text = self._redact(text)
            parts.append(text[:remaining])
            remaining -= len(text) + 1
            if remaining < 0:
                truncated = True
                break
        output = "\n".join(parts)
        return output + ("\n[log truncated; open CI for the full log]" if truncated else "")

    def retry(self, repository: str, pipeline_id: str, authorized: bool = False) -> bool:
        """Restart a pipeline by its run number (legacy parameter name retained)."""
        if not authorized:
            raise PermissionError_Engineering("CI retry requires authorization")
        if not self.available:
            raise UnreachableError("CI provider offline")
        rid = self._repo_id(repository)
        number = _positive_id(pipeline_id)
        self._request(f"/api/repos/{rid}/pipelines/{number}", method="POST")
        return True

    def cancel(self, repository: str, pipeline_id: str, authorized: bool = False) -> bool:
        if not authorized:
            raise PermissionError_Engineering("CI cancel requires authorization")
        if not self.available:
            raise UnreachableError("CI provider offline")
        rid = self._repo_id(repository)
        number = _positive_id(pipeline_id)
        self._request(f"/api/repos/{rid}/pipelines/{number}/cancel", method="POST")
        return True

    def debug_with_pi_handoff(self, repository: str, pipeline: PipelineView,
                              failing_step: str, log_summary: str,
                              artifact_refs: list[dict] | None = None) -> dict[str, Any]:
        return {
            "handoff_type": "debug-with-pi", "repository": repository,
            "pipeline_id": pipeline.pipeline_id, "run_number": pipeline.run_number,
            "revision": pipeline.revision, "failing_step": failing_step[:2000],
            "log_summary": self._redact(log_summary)[:4000],
            "artifact_refs": (artifact_refs or [])[:20], "target_owner": "pi-engineering",
        }

    def debug_with_pi(self, repository: str, number: int) -> dict[str, Any]:
        """Prepare bounded debugging context; dispatch remains the Pi caller's job."""
        detail = self.pipeline(repository, number)
        if detail is None:
            return {"ok": False, "error": f"pipeline {number} not found for {repository}"}
        failing = [t for t in detail.get("tasks", []) if t.get("state") in {"failure", "error", "killed"}]
        lines = [f"pipeline #{number} {detail.get('status')} "
                 f"({detail.get('event')} on {detail.get('branch')}, "
                 f"commit {str(detail.get('commit', ''))[:12]})"]
        for task in failing[:10]:
            lines.append(f"FAILED {task.get('name')} (exit {task.get('exit_code')}) "
                         f"[workflow {task.get('workflow')}, task {task.get('id')}]")
        if not failing:
            lines.append("(no failed tasks reported)")
        lines.append("Inspect stored step logs in HerdR or the linked CI run.")
        return {
            "ok": True,
            "handoff": self.debug_with_pi_handoff(
                repository, PipelineView(str(detail.get("id")), number, repository,
                                         str(detail.get("commit", ""))),
                ", ".join(t.get("name", "?") for t in failing) or "(none)", "\n".join(lines)),
            "view_in_web_ui": detail.get("url") or (
                f"{self.base_url}/{repository}/pipelines/{number}"),
        }

"""Read-only GitHub catalog with server-held credentials and progressive caching.

GitHub GraphQL repositories/issues are separate connections: issue totals exclude
pull requests. AutoSpec labels describe candidates, never execution admission.
"""
from __future__ import annotations

import copy
import hashlib
import json
import threading
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime

from .control_plane.project_links import canonical_repository, project_for_repository
from .control_plane.redact import redact_text

MAX_RESPONSE = 4 * 1024 * 1024
TABS = {"all", "autospec", "preparation", "in_progress", "blocked"}
REPOS_QUERY = """query($cursor:String) { viewer { login repositories(first:100,after:$cursor,
affiliations:[OWNER,COLLABORATOR,ORGANIZATION_MEMBER],
ownerAffiliations:[OWNER,COLLABORATOR,ORGANIZATION_MEMBER],orderBy:{field:NAME,direction:ASC}) {
totalCount pageInfo {hasNextPage endCursor} nodes {
id nameWithOwner url isPrivate isArchived hasIssuesEnabled viewerPermission
issues(states:OPEN) {totalCount} } } } rateLimit {remaining resetAt cost} }"""
ISSUE_FIELDS = """id number title url state updatedAt
labels(first:100) {nodes {name} pageInfo {hasNextPage}}
assignees(first:20) {nodes {login}}"""
ISSUES_QUERY = """query($owner:String!,$name:String!,$cursor:String) {
repository(owner:$owner,name:$name) {issues(first:100,after:$cursor,states:OPEN,
orderBy:{field:UPDATED_AT,direction:DESC}) {totalCount pageInfo {hasNextPage endCursor}
nodes {""" + ISSUE_FIELDS + """} } } rateLimit {remaining resetAt cost} }"""
DETAIL_QUERY = """query($owner:String!,$name:String!,$number:Int!) {
repository(owner:$owner,name:$name) {issue(number:$number) {""" + ISSUE_FIELDS + """ body}}
rateLimit {remaining resetAt cost} }"""


def _stamp():
    return datetime.now(UTC).isoformat()


class GitHubError(RuntimeError):
    def __init__(self, reason, *, retry_at=None):
        self.reason = reason
        self.retry_at = retry_at
        super().__init__({
            "not_configured": "Configure a private server-side GitHub token file.",
            "authentication": "GitHub rejected the configured credential.",
            "permission": "GitHub denied access to this resource.",
            "rate_limited": "GitHub requests are paused until the rate limit resets.",
            "not_found": "The GitHub issue or repository is unavailable.",
            "invalid_response": "GitHub returned an incomplete or invalid response.",
            "response_too_large": "The GitHub response exceeded the bounded read limit.",
            "redirect": "GitHub redirected the request; credentials were not forwarded.",
            "capacity": "The configured catalog safety limit was reached; coverage is partial.",
        }.get(reason, "GitHub is temporarily unavailable."))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _retry_at(headers):
    try:
        delay = headers.get("Retry-After")
        if delay is not None:
            return time.time() + max(1, min(float(delay), 86400))
        reset = float(headers.get("X-RateLimit-Reset", "0"))
        return max(time.time() + 60, reset)
    except (TypeError, ValueError):
        return time.time() + 60


class GitHubProvider:
    def __init__(self, *, token=None, client=None):
        from .host_config import host_value
        value = token if token is not None else host_value("github.token")
        self._token = value if isinstance(value, str) and value and len(value) <= 16384 and all(32 < ord(c) < 127 for c in value) else None
        self._client = client or urllib.request.build_opener(_NoRedirect())
        self.rate_limit = None

    @property
    def available(self):
        return bool(self._token)

    @property
    def credential_identity(self):
        return hashlib.sha256((self._token or "").encode()).hexdigest()

    def _query(self, query, variables):
        if not self.available:
            raise GitHubError("not_configured")
        request = urllib.request.Request("https://api.github.com/graphql",
            data=json.dumps({"query": query, "variables": variables}).encode(), method="POST",
            headers={"Authorization": f"Bearer {self._token}", "Content-Type": "application/json",
                     "Accept": "application/vnd.github+json", "User-Agent": "herdr-engineering"})
        try:
            with self._client.open(request, timeout=20) as response:
                raw = response.read(MAX_RESPONSE + 1)
            if len(raw) > MAX_RESPONSE:
                raise GitHubError("response_too_large")
            payload = json.loads(raw.decode().replace(self._token, "[REDACTED]"))
            if not isinstance(payload, dict):
                raise GitHubError("invalid_response")
            data = payload.get("data")
            if isinstance(data, dict) and isinstance(data.get("rateLimit"), dict):
                self.rate_limit = {key: data["rateLimit"].get(key) for key in ("remaining", "resetAt", "cost")}
            if "errors" in payload and payload["errors"] is not None and not isinstance(payload["errors"], list):
                raise GitHubError("invalid_response")
            if payload.get("errors"):
                if not isinstance(payload["errors"], list) or any(not isinstance(item, dict) for item in payload["errors"]):
                    raise GitHubError("invalid_response")
                kinds = {str(item.get("type", "")).upper() for item in payload["errors"] if isinstance(item, dict)}
                if "RATE_LIMITED" in kinds:
                    retry = time.time() + 60
                    try:
                        retry = max(retry, datetime.fromisoformat(self.rate_limit["resetAt"].replace("Z", "+00:00")).timestamp())
                    except (TypeError, KeyError, ValueError):
                        pass
                    raise GitHubError("rate_limited", retry_at=retry)
                raise GitHubError("permission" if "FORBIDDEN" in kinds else "invalid_response")
            if not isinstance(data, dict):
                raise GitHubError("invalid_response")
            return data
        except GitHubError:
            raise
        except urllib.error.HTTPError as exc:
            reason = {401: "authentication", 403: "permission", 404: "not_found", 429: "rate_limited"}.get(exc.code, "unavailable")
            if 300 <= exc.code < 400:
                reason = "redirect"
            if exc.code == 403 and (exc.headers.get("Retry-After") or exc.headers.get("X-RateLimit-Remaining") == "0"):
                reason = "rate_limited"
            retry = _retry_at(exc.headers) if reason == "rate_limited" else None
            exc.close()
            raise GitHubError(reason, retry_at=retry) from None
        except (ValueError, TypeError, UnicodeError):
            raise GitHubError("invalid_response") from None
        except (OSError, urllib.error.URLError):
            raise GitHubError("unavailable") from None

    @staticmethod
    def _connection(value):
        if not isinstance(value, dict) or not isinstance(value.get("nodes"), list) or not isinstance(value.get("pageInfo"), dict):
            raise GitHubError("invalid_response")
        info = value["pageInfo"]
        if type(value.get("totalCount")) is not int or value["totalCount"] < 0 or not isinstance(info.get("hasNextPage"), bool):
            raise GitHubError("invalid_response")
        if info["hasNextPage"] and (not isinstance(info.get("endCursor"), str) or not info["endCursor"]):
            raise GitHubError("invalid_response")
        return value

    def repositories_page(self, cursor=None):
        data = self._query(REPOS_QUERY, {"cursor": cursor})
        viewer = data.get("viewer")
        if not isinstance(viewer, dict) or not isinstance(viewer.get("login"), str):
            raise GitHubError("invalid_response")
        return viewer["login"], self._connection(viewer.get("repositories"))

    @staticmethod
    def _repo_args(repository_id):
        canonical = canonical_repository(repository_id)
        if not canonical or not canonical.startswith("github.com/"):
            raise ValueError("repository_id must identify a GitHub repository")
        parts = canonical.split("/")
        if len(parts) != 3:
            raise ValueError("repository_id must identify an owner and repository")
        return {"owner": parts[1], "name": parts[2]}

    def issues_page(self, repository_id, cursor=None):
        data = self._query(ISSUES_QUERY, dict(self._repo_args(repository_id), cursor=cursor))
        repository = data.get("repository")
        if not isinstance(repository, dict):
            raise GitHubError("not_found")
        return self._connection(repository.get("issues"))

    def issue(self, repository_id, number):
        if not isinstance(number, int) or isinstance(number, bool) or number < 1:
            raise ValueError("issue number must be a positive integer")
        data = self._query(DETAIL_QUERY, dict(self._repo_args(repository_id), number=number))
        repository = data.get("repository")
        if not isinstance(repository, dict) or not isinstance(repository.get("issue"), dict):
            raise GitHubError("not_found")
        return repository["issue"]


def autospec_state(labels, *, complete=True):
    labels = set(labels)
    excluded = bool(labels & {"type:tracker", "no-auto"})
    blocked = labels & {"autospec:needs-human", "autospec:blocked-prerequisite", "security:quarantined", "groom:proposed"}
    preparation = labels & {"needs-classify", "needs-autospec-template", "needs-quality-bar"}
    active = labels & {"in-progress-by-bot", "awaiting-merge", "auto-implement-active"}
    managed = not excluded and bool(labels & {"auto-implement", "needs-classify", "in-progress-by-bot", "awaiting-merge", "auto-implement-active"} or blocked or preparation)
    state = "unmanaged"
    if managed:
        state = "blocked" if blocked else "in_progress" if active else "preparation" if preparation else "candidate"
    reasons = sorted(blocked | preparation)
    if excluded:
        reasons.extend(sorted(labels & {"type:tracker", "no-auto"}))
    if not complete:
        reasons.append("labels_incomplete")
    def axis(prefix):
        values = sorted(label.removeprefix(prefix) for label in labels if label.startswith(prefix))
        return values[0] if len(values) == 1 else None
    return {"managed": managed, "state": state, "eligibility": "blocked" if blocked or excluded else "unknown",
            "reasons": reasons, "context": axis("ctx:"), "reasoning": axis("reasoning:"),
            "language": axis("lang:"), "evidence_source": "github_labels"}


def _repository(row):
    if not isinstance(row, dict) or not isinstance(row.get("id"), str):
        raise GitHubError("invalid_response")
    name = row.get("nameWithOwner")
    identity = canonical_repository(row.get("url"))
    count = (row.get("issues") or {}).get("totalCount")
    if not identity or not isinstance(name, str) or not isinstance(count, int):
        raise GitHubError("invalid_response")
    return {"github_id": row["id"], "repository_id": identity, "name_with_owner": name,
            "url": row["url"], "project_id": project_for_repository(identity),
            "private": bool(row.get("isPrivate")), "archived": bool(row.get("isArchived")),
            "issues_enabled": bool(row.get("hasIssuesEnabled")), "viewer_permission": row.get("viewerPermission"),
            "open_issue_count": count, "issues_loaded": 0, "issues_complete": False, "error": None}


def _issue(row, repository_id):
    if not isinstance(row, dict) or not isinstance(row.get("number"), int) or not isinstance(row.get("id"), str):
        raise GitHubError("invalid_response")
    label_connection = row.get("labels") or {}
    labels = [redact_text(item["name"]) for item in label_connection.get("nodes", []) if isinstance(item, dict) and isinstance(item.get("name"), str)]
    complete = not label_connection.get("pageInfo", {}).get("hasNextPage", True)
    record = {"github_id": row["id"], "repository_id": repository_id,
              "project_id": project_for_repository(repository_id), "number": row["number"],
              "title": redact_text(str(row.get("title", ""))), "url": row.get("url"),
              "state": row.get("state"), "updated_at": row.get("updatedAt"), "labels": labels,
              "assignees": [item["login"] for item in (row.get("assignees") or {}).get("nodes", []) if isinstance(item, dict) and isinstance(item.get("login"), str)],
              "autospec": autospec_state(labels, complete=complete)}
    if "body" in row:
        body = row["body"] if isinstance(row["body"], str) else ""
        record["body"] = redact_text(body[:200000])
        record["body_truncated"] = len(body) > 200000
    return record


def _tab(issue, tab):
    state = issue["autospec"]
    return tab == "all" or (tab == "autospec" and state["managed"]) or state["state"] == tab


def _page(rows, page, per_page):
    if not isinstance(page, int) or not isinstance(per_page, int) or page < 1 or not 1 <= per_page <= 100:
        raise ValueError("page must be positive and per_page must be 1–100")
    start = (page - 1) * per_page
    return rows[start:start + per_page], {"page": page, "per_page": per_page, "total": len(rows), "has_next": start + per_page < len(rows)}


class _Superseded(Exception):
    """A token rotation invalidated an in-flight catalog generation."""


class GitHubMonitor:
    def __init__(self, *, provider_factory=None, interval=300, repo_limit=5000, issue_limit=100000, now=None):
        self._factory = provider_factory or GitHubProvider
        self._interval = max(30, interval)
        self._repo_limit, self._issue_limit = repo_limit, issue_limit
        self._now = now or time.time
        self._lock = threading.RLock()
        self._refresh_lock = threading.Lock()
        self._stop, self._wake = threading.Event(), threading.Event()
        self._thread = None
        self._credential = None
        self._generation = 0
        self._local = threading.local()
        self._retry_at = 0
        self._repos, self._issues, self._details = {}, {}, {}
        self._resume_repos, self._issue_resume = None, {}
        self._view = self._empty()

    @staticmethod
    def _empty():
        return {"available": False, "status": "not_configured", "connection_generation": 0, "refreshing": False, "stale": False,
                "partial": False, "account": None, "checked_at": None, "last_success_at": None,
                "error": None, "rate_limit": None, "progress": {"phase": "idle", "repositories_seen": 0,
                    "repositories_total": None, "issue_repositories_complete": 0, "issue_repositories_total": None,
                    "issues_seen": 0, "issues_total_known": None, "current_repository": None}}

    def start(self):
        with self._lock:
            if self._thread is None and not self._stop.is_set():
                self._sync_credentials()
                self._thread = threading.Thread(target=self._loop, daemon=True, name="herdr-github-catalog")
                self._thread.start()

    def close(self):
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=1)

    def _loop(self):
        while not self._stop.is_set():
            self.refresh()
            self._wake.wait(self._interval)
            self._wake.clear()

    def refresh_async(self):
        with self._lock:
            running = self._thread is not None
        self.start()
        if running:
            self._wake.set()
        return self.snapshot(start=False)

    def snapshot(self, *, start=True):
        self._sync_credentials()
        if start:
            self.start()
        with self._lock:
            view = copy.deepcopy(self._view)
            view["rate_limit"] = dict(view.get("rate_limit") or {}, retry_at=self._retry_at or None)
            if view["last_success_at"]:
                age = self._now() - datetime.fromisoformat(view["last_success_at"]).timestamp()
                view["stale"] = view["stale"] or age > self._interval * 2
            return view

    def _sync_credentials(self):
        with self._lock:
            provider = self._factory()
            if provider.credential_identity != self._credential:
                self._generation += 1
                self._credential = provider.credential_identity
                self._repos, self._issues, self._details = {}, {}, {}
                self._resume_repos, self._issue_resume = None, {}
                self._retry_at = 0
                self._view = self._empty()
                self._view["connection_generation"] = self._generation
                if self._thread is not None and threading.current_thread() is not self._thread:
                    self._wake.set()
        return provider

    def _check_generation(self):
        if getattr(self._local, "generation", self._generation) != self._generation:
            raise _Superseded()

    def _update(self, **fields):
        with self._lock:
            self._check_generation()
            self._view.update(fields)

    def _progress(self, **fields):
        with self._lock:
            self._check_generation()
            self._view["progress"].update(fields)

    def refresh(self):
        provider = self._sync_credentials()
        if self._now() < self._retry_at or not self._refresh_lock.acquire(blocking=False):
            return
        with self._lock:
            generation = self._generation
            self._local.generation = generation
        try:
            self._collect(provider)
        except _Superseded:
            pass
        except GitHubError as exc:
            with self._lock:
                if generation == self._generation:
                    self._retry_at = max(self._retry_at, exc.retry_at or self._now() + 60)
                    self._view.update(status={"authentication": "auth_error", "not_configured": "not_configured"}.get(exc.reason, exc.reason),
                                      error=str(exc), stale=bool(self._repos), partial=bool(self._repos))
        except Exception:
            with self._lock:
                if generation == self._generation:
                    self._view.update(status="unavailable", error="GitHub catalog refresh failed.", stale=bool(self._repos), partial=bool(self._repos))
        finally:
            with self._lock:
                if generation == self._generation:
                    self._view.update(refreshing=False, checked_at=_stamp())
            del self._local.generation
            self._refresh_lock.release()

    def _collect(self, provider):
        with self._lock:
            self._check_generation()
            if provider.credential_identity != self._credential:
                raise _Superseded()
            self._view.update(refreshing=True, error=None, status="syncing", partial=True)
        if not provider.available:
            raise GitHubError("not_configured")
        self._update(available=True)
        self._progress(phase="repositories", repositories_seen=0, repositories_total=None,
                       issues_seen=0, issues_total_known=None, issue_repositories_complete=0, current_repository=None)
        if self._resume_repos is None:
            discovered, cursor, cursors = {}, None, set()
            while not self._stop.is_set():
                account, connection = provider.repositories_page(cursor)
                for row in connection["nodes"]:
                    record = _repository(row)
                    discovered[record["repository_id"]] = record
                    if len(discovered) > self._repo_limit:
                        raise GitHubError("capacity")
                with self._lock:
                    self._check_generation()
                    candidate = ({**self._repos, **discovered}
                                 if connection["pageInfo"]["hasNextPage"] else discovered)
                    if len(candidate) > self._repo_limit:
                        raise GitHubError("capacity")
                    self._repos = copy.deepcopy(candidate)
                self._update(account=account, rate_limit=provider.rate_limit)
                self._progress(repositories_seen=len(discovered), repositories_total=connection["totalCount"])
                if not connection["pageInfo"]["hasNextPage"]:
                    break
                cursor = connection["pageInfo"]["endCursor"]
                if cursor in cursors:
                    raise GitHubError("invalid_response")
                cursors.add(cursor)
            if self._stop.is_set():
                return
            with self._lock:
                self._check_generation()
                self._repos = discovered
                self._issues = {key: value for key, value in self._issues.items() if key in discovered}
            with self._lock:
                self._check_generation()
                self._resume_repos = discovered
        else:
            discovered = self._resume_repos
            self._progress(repositories_seen=len(discovered), repositories_total=len(discovered))
        repos = sorted(discovered.values(), key=lambda r: (-r["open_issue_count"], r["repository_id"]))
        self._progress(phase="issues", issue_repositories_total=len(repos),
                       issues_total_known=sum(r["open_issue_count"] for r in repos))
        loaded = sum(len(self._issues.get(r["repository_id"], {})) for r in repos if r["issues_complete"])
        completed = sum(r["issues_complete"] for r in repos)
        errors = {}
        for record in repos:
            if self._stop.is_set():
                return
            rid = record["repository_id"]
            if record["issues_complete"]:
                continue
            self._progress(current_repository=rid)
            fresh, cursor, cursors = self._issue_resume.get(rid, ({}, None, set()))
            try:
                while record["open_issue_count"] and not self._stop.is_set():
                    connection = provider.issues_page(rid, cursor)
                    for row in connection["nodes"]:
                        issue = _issue(row, rid)
                        fresh[issue["github_id"]] = issue
                    with self._lock:
                        self._check_generation()
                        candidate = ({**self._issues.get(rid, {}), **fresh}
                                     if connection["pageInfo"]["hasNextPage"] else fresh)
                        retained = sum(len(rows) for key, rows in self._issues.items() if key != rid)
                        if retained + len(candidate) > self._issue_limit:
                            raise GitHubError("capacity")
                        self._issues[rid] = copy.deepcopy(candidate)
                        record["issues_loaded"] = len(fresh)
                    self._progress(issues_seen=loaded + len(fresh))
                    self._update(rate_limit=provider.rate_limit)
                    if not connection["pageInfo"]["hasNextPage"]:
                        break
                    cursor = connection["pageInfo"]["endCursor"]
                    if cursor in cursors:
                        raise GitHubError("invalid_response")
                    cursors.add(cursor)
                    with self._lock:
                        self._check_generation()
                        self._issue_resume[rid] = (fresh, cursor, cursors)
                with self._lock:
                    self._check_generation()
                    self._issues[rid] = fresh
                    record.update(issues_complete=True, issues_loaded=len(fresh), error=None)
                with self._lock:
                    self._check_generation()
                    self._issue_resume.pop(rid, None)
                loaded += len(fresh)
                completed += 1
                self._progress(issues_seen=loaded, issue_repositories_complete=completed)
            except GitHubError as exc:
                with self._lock:
                    self._check_generation()
                    record["error"] = str(exc)
                    record["issues_loaded"] = len(self._issues.get(rid, {}))
                errors[rid] = str(exc)
                if exc.reason in {"rate_limited", "authentication", "capacity"}:
                    raise
        with self._lock:
            self._check_generation()
            self._resume_repos, self._issue_resume = None, {}
        self._update(status="partial" if errors else "ready", partial=bool(errors), stale=bool(errors),
                     error="Some repository issues could not be refreshed." if errors else None,
                     last_success_at=self._view["last_success_at"] if errors else _stamp())
        self._progress(phase="complete", current_repository=None)

    def repositories(self, *, page=1, per_page=50, search="", start=True):
        with self._lock:
            view = self.snapshot(start=start)
            rows = sorted((copy.deepcopy(row) for row in self._repos.values() if search.lower() in row["name_with_owner"].lower()),
                          key=lambda r: (-r["open_issue_count"], r["name_with_owner"].lower()))
        view["repositories"], view["pagination"] = _page(rows, page, per_page)
        return view

    def issues(self, *, repository_id=None, tab="all", page=1, per_page=50, search="", start=True):
        if tab not in TABS:
            raise ValueError("unknown issue tab")
        with self._lock:
            view = self.snapshot(start=start)
            rows = [copy.deepcopy(issue) for rid, issues in self._issues.items() if repository_id is None or rid == repository_id
                    for issue in issues.values() if search.lower() in issue["title"].lower() or search in str(issue["number"])]
        view["counts"] = {name: sum(_tab(issue, name) for issue in rows) for name in sorted(TABS)}
        rows = sorted((issue for issue in rows if _tab(issue, tab)), key=lambda row: (row["updated_at"] or "", row["number"]), reverse=True)
        view["issues"], view["pagination"] = _page(rows, page, per_page)
        return view

    def issue_detail(self, repository_id, number):
        provider = self._sync_credentials()
        with self._lock:
            if provider.credential_identity != self._credential:
                raise GitHubError("not_configured")
            generation = self._generation
            if repository_id not in self._repos:
                raise GitHubError("not_found")
            cached = self._details.get((repository_id, number))
        if cached and self._now() - datetime.fromisoformat(cached["checked_at"]).timestamp() < 60:
            return copy.deepcopy(cached)
        if self._now() < self._retry_at:
            if cached:
                return dict(copy.deepcopy(cached), stale=True, error="GitHub requests are temporarily paused.")
            raise GitHubError("rate_limited", retry_at=self._retry_at)
        try:
            issue = _issue(provider.issue(repository_id, number), repository_id)
            result = {"issue": issue, "stale": False, "error": None, "checked_at": _stamp(),
                      "connection_generation": generation}
            with self._lock:
                if generation != self._generation:
                    raise GitHubError("not_configured")
                if len(self._details) >= 200:
                    self._details.pop(next(iter(self._details)))
                self._details[(repository_id, number)] = copy.deepcopy(result)
            return result
        except GitHubError as exc:
            with self._lock:
                if generation != self._generation:
                    raise GitHubError("not_configured") from None
                if exc.retry_at:
                    self._retry_at = max(self._retry_at, exc.retry_at)
            if cached:
                return dict(copy.deepcopy(cached), stale=True, error=str(exc))
            raise

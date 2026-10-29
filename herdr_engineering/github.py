"""Read-only GitHub catalog with server-held credentials and progressive caching.

GitHub GraphQL repositories/issues are separate connections: issue totals exclude
pull requests. AutoSpec labels describe candidates, never execution admission.

Credentials come from ``github_auth.credentials()``: with ``github.app`` every call uses the
installation token of the owner it reads (repositories and issues: ``issues`` + ``metadata``
read; Projects v2: also ``organization_projects`` read), the listing walks each configured
owner (``repositoryOwner``) instead of ``viewer``, and the catalog identity is the App's (stable
across hourly token rotation). Otherwise the static ``github.token`` works as before.
"""
from __future__ import annotations

import contextlib
import copy
import hashlib
import json
import logging
import math
import threading
import time
import urllib.error
import urllib.request
from collections import OrderedDict
from datetime import UTC, datetime, timedelta

from . import github_auth
from .control_plane.project_links import (
    canonical_repository,
    project_for_repository,
    repository_language,
)
from .control_plane.redact import redact_text
from .errors import InvalidError

MAX_RESPONSE = 4 * 1024 * 1024
TABS = {"all", "autospec", "preparation", "in_progress", "blocked"}
REPOS_QUERY = """query($cursor:String) { viewer { login repositories(first:100,after:$cursor,
affiliations:[OWNER,COLLABORATOR,ORGANIZATION_MEMBER],
ownerAffiliations:[OWNER,COLLABORATOR,ORGANIZATION_MEMBER],orderBy:{field:NAME,direction:ASC}) {
totalCount pageInfo {hasNextPage endCursor} nodes {
id nameWithOwner url isPrivate isArchived hasIssuesEnabled viewerPermission
primaryLanguage {name}
issues(states:OPEN) {totalCount}
newest: issues(first:1,orderBy:{field:UPDATED_AT,direction:DESC}) {nodes {updatedAt}} } } }
rateLimit {remaining resetAt cost} }"""
# The App listing: one configured owner's repositories (an installation token has no useful
# viewer). Same node fields as REPOS_QUERY, so _connection/_repository parse it unchanged.
OWNER_REPOS_QUERY = """query($login:String!,$cursor:String) { repositoryOwner(login:$login) { login
repositories(first:100,after:$cursor,orderBy:{field:NAME,direction:ASC}) {
totalCount pageInfo {hasNextPage endCursor} nodes {
id nameWithOwner url isPrivate isArchived hasIssuesEnabled viewerPermission
primaryLanguage {name}
issues(states:OPEN) {totalCount}
newest: issues(first:1,orderBy:{field:UPDATED_AT,direction:DESC}) {nodes {updatedAt}} } } }
rateLimit {remaining resetAt cost} }"""
# Projects v2 node id -> owner (the owner whose App token reads its items); bounded.
_PROJECT_OWNERS: OrderedDict = OrderedDict()
_PROJECT_OWNERS_LOCK = threading.Lock()
PROJECT_OWNERS_MAX = 4096
# GitHub's updatedAt can trail an event by a few seconds (G0 spike), so an
# incremental read re-reads this much below the stored watermark.
WATERMARK_OVERLAP = timedelta(seconds=10)
# A pass that resumes deferred work skips the listing; after this many in a row
# (or one that made no progress) the next pass lists again, so new and removed
# repositories never wait on a backlog.
RESUME_PASSES_BEFORE_RELIST = 3
# At most this many deferred repositories are listed in a view (``deferred_total`` counts them all).
DEFERRED_SHOWN = 50
# Repository fields that change while syncing and are never persisted (a
# re-upsert of unchanged data must not bump the catalog revision).
VOLATILE_REPOSITORY_FIELDS = ("issues_loaded", "issues_complete", "error", "newest_updated_at")
ISSUE_FIELDS = """id number title url state updatedAt
labels(first:100) {nodes {name} pageInfo {hasNextPage}}
assignees(first:20) {nodes {login}}"""
ISSUES_QUERY = """query($owner:String!,$name:String!,$cursor:String) {
repository(owner:$owner,name:$name) {issues(first:100,after:$cursor,states:OPEN,
orderBy:{field:UPDATED_AT,direction:DESC}) {totalCount pageInfo {hasNextPage endCursor}
nodes {""" + ISSUE_FIELDS + """} } } rateLimit {remaining resetAt cost} }"""
# Issues that left the open set since a watermark. filterBy.since filters on
# updatedAt (G0 spike), so the read is bounded by the watermark already.
CLOSED_QUERY = """query($owner:String!,$name:String!,$since:DateTime!,$cursor:String) {
repository(owner:$owner,name:$name) {issues(first:100,after:$cursor,states:CLOSED,filterBy:{since:$since},
orderBy:{field:UPDATED_AT,direction:DESC}) {totalCount pageInfo {hasNextPage endCursor}
nodes {id number updatedAt} } } rateLimit {remaining resetAt cost} }"""
DETAIL_QUERY = """query($owner:String!,$name:String!,$number:Int!) {
repository(owner:$owner,name:$name) {issue(number:$number) {""" + ISSUE_FIELDS + """ body}}
rateLimit {remaining resetAt cost} }"""
# Projects v2 (G9). An owner's projects are listed newest updatedAt first with their item count
# and Status options, so a project whose (updatedAt, item count) matches the stored probe costs
# nothing more. ProjectV2.items can only be ordered by POSITION (ProjectV2ItemOrderField has no
# UPDATED_AT), so items cannot be read newest first down to a watermark like issues: a changed
# project's items are read in full (unchanged projects are still skipped). Issue.state and
# PullRequest.state are different enums, so they are aliased (issueState / prState): one response
# key for both fails GraphQL's SameResponseShape validation. title, number, url and repository
# have the same type on every content type and share their keys.
PROJECTS_QUERY = """query($login:String!,$cursor:String) { repositoryOwner(login:$login) { login
... on ProjectV2Owner { projectsV2(first:50,after:$cursor,orderBy:{field:UPDATED_AT,direction:DESC}) {
totalCount pageInfo {hasNextPage endCursor} nodes { id number title url closed updatedAt
items {totalCount}
field(name:"Status") { ... on ProjectV2SingleSelectField { id options {id name} } } } } } }
rateLimit {remaining resetAt cost} }"""
PROJECT_ITEMS_QUERY = """query($id:ID!,$cursor:String) { node(id:$id) { ... on ProjectV2 {
items(first:100,after:$cursor) { totalCount pageInfo {hasNextPage endCursor} nodes {
id type isArchived updatedAt
status: fieldValueByName(name:"Status") { ... on ProjectV2ItemFieldSingleSelectValue { name optionId } }
content { ... on Issue { title number url issueState: state repository {nameWithOwner} }
... on PullRequest { title number url prState: state repository {nameWithOwner} }
... on DraftIssue { title } } } } } }
rateLimit {remaining resetAt cost} }"""


def _stamp():
    return datetime.now(UTC).isoformat()


def _when(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _iso(when):
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _latest(*values):
    """The newest of several GitHub timestamps (``None`` and unparsable values ignored)."""
    known = [(when, value) for value in values if (when := _when(value)) is not None]
    return max(known)[1] if known else None


class GitHubError(RuntimeError):
    def __init__(self, reason, *, retry_at=None, message=None):
        self.reason = reason
        self.retry_at = retry_at
        super().__init__(message or {
            "not_configured": "Configure a private server-side GitHub token file.",
            "authentication": "GitHub rejected the configured credential.",
            "permission": "GitHub denied access to this resource.",
            "rate_limited": "GitHub requests are paused until the rate limit resets.",
            "not_found": "The GitHub issue or repository is unavailable.",
            "invalid_response": "GitHub returned an incomplete or invalid response.",
            "response_too_large": "The GitHub response exceeded the bounded read limit.",
            "redirect": "GitHub redirected the request; credentials were not forwarded.",
            "capacity": "The configured catalog safety limit was reached; coverage is partial.",
            "invalid_config": "github.organizations must be a list of non-empty GitHub owner logins; fix the YAML configuration.",
            "insufficient_scope": "The GitHub token lacks a scope this read needs (Projects need read:project).",
        }.get(reason, "GitHub is temporarily unavailable."))


AUTH_FAILURES_BEFORE_PURGE = 2  # consecutive 401s before the stored catalog is wiped


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
    """``token``: a static token (tests, legacy); otherwise ``credentials`` (default: the live
    ``github_auth.credentials()``): a GitHub App provider or the static ``github.token``."""

    def __init__(self, *, token=None, client=None, credentials=None):
        self._app = None
        if token is not None:
            value = token
        else:
            creds = credentials if credentials is not None else github_auth.credentials()
            if getattr(creds, "mode", None) == "app":
                self._app, value = creds, None
            else:
                value = creds.token_for(None)
        self._token = value if github_auth.static_token_ok(value) else None
        self._client = client or urllib.request.build_opener(_NoRedirect())
        self.rate_limit = None
        self.last_cost = None  # the last call's reported rateLimit.cost (None: not reported)

    @property
    def available(self):
        return bool(self._token) or self._app is not None

    @property
    def credential_identity(self):
        if self._app is not None:
            return self._app.identity
        return hashlib.sha256((self._token or "").encode()).hexdigest()

    def _bearer(self, owner, permissions):
        if self._app is None:
            return self._token
        try:
            return self._app.token_or_raise(owner, permissions)
        except github_auth.GitHubAuthError as exc:
            if exc.reason == "rate_limited":
                raise GitHubError("rate_limited", retry_at=time.time() + (exc.retry_s or 60)) from None
            raise GitHubError("unavailable", message="No GitHub App installation token for this owner.") from None

    def _query(self, query, variables, *, owner=None, permissions=github_auth.CATALOG_PERMISSIONS):
        if not self.available:
            raise GitHubError("not_configured")
        self.last_cost = None
        bearer = self._bearer(owner, permissions)
        try:
            return self._post(query, variables, bearer)
        except GitHubError as exc:
            if exc.reason == "authentication" and self._app is not None:
                self._app.invalidate(owner)
            raise
        finally:
            bearer = None

    def _post(self, query, variables, bearer):
        request = urllib.request.Request("https://api.github.com/graphql",
            data=json.dumps({"query": query, "variables": variables}).encode(), method="POST",
            headers={"Authorization": f"Bearer {bearer}", "Content-Type": "application/json",
                     "Accept": "application/vnd.github+json", "User-Agent": "herdr-engineering"})
        try:
            with self._client.open(request, timeout=20) as response:
                raw = response.read(MAX_RESPONSE + 1)
            if len(raw) > MAX_RESPONSE:
                raise GitHubError("response_too_large")
            payload = json.loads(raw.decode().replace(bearer, "[REDACTED]"))
            if not isinstance(payload, dict):
                raise GitHubError("invalid_response")
            data = payload.get("data")
            if isinstance(data, dict) and isinstance(data.get("rateLimit"), dict):
                self.rate_limit = {key: data["rateLimit"].get(key) for key in ("remaining", "resetAt", "cost")}
                self.last_cost = self.rate_limit["cost"]
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
                if "INSUFFICIENT_SCOPES" in kinds:
                    raise GitHubError("insufficient_scope")
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

    @property
    def app_account(self):
        """The catalog's account under App auth (not a GitHub login: it has a colon)."""
        return f"github-app:{self._app.config.app_id}" if self._app is not None else None

    def _app_repositories_page(self, cursor):
        """The configured owners' repositories, owner by owner; the cursor is ``<index>:<inner>``."""
        owners = list(self._app.owners)
        index, inner = 0, None
        if cursor is not None:
            head, sep, rest = str(cursor).partition(":")
            if not sep or not head.isdigit() or int(head) >= len(owners):
                raise GitHubError("invalid_response")
            index, inner = int(head), (rest or None)
        login = owners[index]
        data = self._query(OWNER_REPOS_QUERY, {"login": login, "cursor": inner}, owner=login)
        node = data.get("repositoryOwner")
        if not isinstance(node, dict):
            raise GitHubError("not_found")
        connection = self._connection(node.get("repositories"))
        # an owner's listing can include repositories it does not own: never route them to it
        nodes = [row for row in connection["nodes"] if isinstance(row, dict)
                 and github_auth.owner_of(row.get("nameWithOwner")) == login]
        info = connection["pageInfo"]
        if info["hasNextPage"]:
            page = {"hasNextPage": True, "endCursor": f"{index}:{info['endCursor']}"}
        elif index + 1 < len(owners):
            page = {"hasNextPage": True, "endCursor": f"{index + 1}:"}
        else:
            page = {"hasNextPage": False, "endCursor": None}
        return self.app_account, {"totalCount": connection["totalCount"], "pageInfo": page, "nodes": nodes}

    def repositories_page(self, cursor=None):
        if self._app is not None:
            return self._app_repositories_page(cursor)
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
        args = self._repo_args(repository_id)
        data = self._query(ISSUES_QUERY, dict(args, cursor=cursor), owner=args["owner"].lower())
        repository = data.get("repository")
        if not isinstance(repository, dict):
            raise GitHubError("not_found")
        return self._connection(repository.get("issues"))

    def closed_issues_page(self, repository_id, since, cursor=None):
        args = self._repo_args(repository_id)
        data = self._query(CLOSED_QUERY, dict(args, since=since, cursor=cursor), owner=args["owner"].lower())
        repository = data.get("repository")
        if not isinstance(repository, dict):
            raise GitHubError("not_found")
        return self._connection(repository.get("issues"))

    def issue(self, repository_id, number):
        if not isinstance(number, int) or isinstance(number, bool) or number < 1:
            raise ValueError("issue number must be a positive integer")
        args = self._repo_args(repository_id)
        data = self._query(DETAIL_QUERY, dict(args, number=number), owner=args["owner"].lower())
        repository = data.get("repository")
        if not isinstance(repository, dict) or not isinstance(repository.get("issue"), dict):
            raise GitHubError("not_found")
        return repository["issue"]

    def projects_page(self, login, cursor=None):
        """One page of an owner's Projects v2 (organization or user), newest ``updatedAt`` first."""
        if not isinstance(login, str) or not login:
            raise ValueError("login must name a GitHub owner")
        data = self._query(PROJECTS_QUERY, {"login": login, "cursor": cursor}, owner=login.lower(),
                           permissions=github_auth.PROJECTS_PERMISSIONS)
        owner = data.get("repositoryOwner")
        if not isinstance(owner, dict):
            raise GitHubError("not_found")
        connection = self._connection(owner.get("projectsV2"))
        with _PROJECT_OWNERS_LOCK:
            for row in connection["nodes"]:
                if isinstance(row, dict) and isinstance(row.get("id"), str):
                    _PROJECT_OWNERS[row["id"]] = login.lower()
                    _PROJECT_OWNERS.move_to_end(row["id"])
            while len(_PROJECT_OWNERS) > PROJECT_OWNERS_MAX:
                _PROJECT_OWNERS.popitem(last=False)
        return connection

    def project_items_page(self, project_id, cursor=None):
        """One page of a project's items, in board position order."""
        with _PROJECT_OWNERS_LOCK:
            owner = _PROJECT_OWNERS.get(project_id) if isinstance(project_id, str) else None
        if self._app is not None and owner is None:
            raise GitHubError("not_found")  # whose installation would read it is unknown: fail closed
        data = self._query(PROJECT_ITEMS_QUERY, {"id": project_id, "cursor": cursor}, owner=owner,
                           permissions=github_auth.PROJECTS_PERMISSIONS)
        node = data.get("node")
        if not isinstance(node, dict):
            raise GitHubError("not_found")
        return self._connection(node.get("items"))


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
    primary_language = row.get("primaryLanguage")
    language = repository_language(primary_language.get("name")) if isinstance(primary_language, dict) else None
    newest_nodes = (row.get("newest") or {}).get("nodes") if isinstance(row.get("newest"), dict) else None
    newest = newest_nodes[0].get("updatedAt") if isinstance(newest_nodes, list) and newest_nodes and isinstance(newest_nodes[0], dict) else None
    return {"github_id": row["id"], "repository_id": identity, "name_with_owner": name,
            "language": language,
            "url": row["url"], "project_id": project_for_repository(identity),
            "private": bool(row.get("isPrivate")), "archived": bool(row.get("isArchived")),
            "issues_enabled": bool(row.get("hasIssuesEnabled")), "viewer_permission": row.get("viewerPermission"),
            "open_issue_count": count, "newest_updated_at": newest if _when(newest) else None,
            "issues_loaded": 0, "issues_complete": False, "error": None}


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


def _owner(record):
    return record["name_with_owner"].split("/", 1)[0].lower()


def _project(row, owner):
    """A listed Projects v2 row (``owner`` lower-case) with its Status options in column order;
    ``status_options`` is ``None`` for a project without a single-select Status field."""
    if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not isinstance(row.get("number"), int):
        raise GitHubError("invalid_response")
    count = (row.get("items") or {}).get("totalCount") if isinstance(row.get("items"), dict) else None
    if type(count) is not int:
        raise GitHubError("invalid_response")
    field = row.get("field") if isinstance(row.get("field"), dict) else None
    options = None
    if field is not None and isinstance(field.get("options"), list):
        options = [{"id": o["id"], "name": redact_text(o["name"])} for o in field["options"]
                   if isinstance(o, dict) and isinstance(o.get("id"), str) and isinstance(o.get("name"), str)]
    return {"project_id": row["id"], "owner": owner.lower(), "number": row["number"],
            "title": redact_text(str(row.get("title") or "")), "url": row.get("url") if isinstance(row.get("url"), str) else None,
            "closed": bool(row.get("closed")), "updated_at": row.get("updatedAt") if _when(row.get("updatedAt")) else None,
            # listed_item_count is GitHub's totalCount (archived items included): the change probe.
            # item_count is what the page shows: the items read, archived ones excluded.
            "listed_item_count": count, "item_count": count, "status_options": options}


def _project_item(row, project_id):
    """One project item, or ``None`` for an archived one (archived items are not shown). Content
    may be an issue, a pull request, a draft or nothing (redacted for this token)."""
    if not isinstance(row, dict) or not isinstance(row.get("id"), str):
        raise GitHubError("invalid_response")
    if row.get("isArchived"):
        return None
    content = row.get("content") if isinstance(row.get("content"), dict) else {}
    status = row.get("status") if isinstance(row.get("status"), dict) else {}
    repository = content.get("repository") if isinstance(content.get("repository"), dict) else {}
    return {"item_id": row["id"], "project_id": project_id,
            "type": row.get("type") if isinstance(row.get("type"), str) else "UNKNOWN",
            "updated_at": row.get("updatedAt") if _when(row.get("updatedAt")) else None,
            "status": redact_text(status["name"]) if isinstance(status.get("name"), str) else None,
            "status_option": status.get("optionId") if isinstance(status.get("optionId"), str) else None,
            "title": redact_text(str(content.get("title") or "")) or None,
            "number": content.get("number") if isinstance(content.get("number"), int) else None,
            "url": content.get("url") if isinstance(content.get("url"), str) else None,
            "state": next((content[key] for key in ("issueState", "prState") if isinstance(content.get(key), str)), None),
            "repository": repository.get("nameWithOwner") if isinstance(repository.get("nameWithOwner"), str) else None}


def _configured_organizations():
    """``github.organizations`` from the central fleet file's ``github`` section.

    That YAML is what control-plane-web reads (live, like ``github.token``), so
    an edit applies on the next pass without a restart.
    """
    from .host_config import host_value
    return host_value("github.organizations")


def _configured_projects():
    """``github.projects`` from the same fleet-file section (live, like the allowlist)."""
    from .host_config import host_value
    return host_value("github.projects")


def _configured_sync():
    """``github.sync`` from the same fleet-file section (live, like the allowlist)."""
    from .host_config import host_value
    return host_value("github.sync")


def build_monitor(pg_dsn=None):
    """The control-plane-web monitor: catalog in Postgres when ``pg_dsn`` is set, else in memory."""
    from .control_plane.github_store import catalog_store_factory
    return GitHubMonitor(store=catalog_store_factory(pg_dsn))


def _tab(issue, tab):
    state = issue["autospec"]
    return tab == "all" or (tab == "autospec" and state["managed"]) or state["state"] == tab


RESYNC_PAGE = 100  # rows a ``since`` resync returns; the rest comes from the paginated list


def _page(rows, page, per_page):
    if not isinstance(page, int) or not isinstance(per_page, int) or page < 1 or not 1 <= per_page <= 100:
        raise ValueError("page must be positive and per_page must be 1–100")
    start = (page - 1) * per_page
    return rows[start:start + per_page], {"page": page, "per_page": per_page, "total": len(rows), "has_next": start + per_page < len(rows)}


_UNREADABLE = object()  # _open_store: the store could not be built or read (not a purge)


class _Superseded(Exception):
    """A token rotation invalidated an in-flight catalog generation."""


class _OverBudget(Exception):
    """This pass spent its ``github.sync.point_budget``; the repository waits for the next pass."""


class GitHubMonitor:
    """Background catalog sync, incremental against a persistent store.

    ``store`` is a factory ``(identity, viewer) -> store`` (the
    ``github_store`` interface), called once per credential fingerprint and once
    more when the first listing reveals the viewer login, so a rotated token can
    never read or write another credential's catalog. ``None`` keeps the
    catalog in memory only. In-memory dicts serve every read; the store is
    written through and loaded only at start (served as ``stale``) and on a
    credential change.

    A pass lists repositories (one call per 100, each with the open count and
    the newest ``updatedAt`` as a change probe). A repository whose probe
    matches the values stored when its issues were last synced costs nothing
    more; a changed one is read newest first and stops below its watermark
    minus ``WATERMARK_OVERLAP``, then reads the issues closed since then. If
    the open count still disagrees (a delete or a transfer, which never pass
    through CLOSED), that repository alone is read in full. As a safety net
    every repository is read in full once per ``full_reconcile_interval``, at
    most ``reconcile_per_pass`` per pass (default: enough to cover all of them
    in one interval), staggered so a restart does not make them all due.

    Each pass may spend ``github.sync.point_budget`` GraphQL points (the
    reported ``rateLimit.cost``). The listing always completes; an issue read
    is not started once the budget is spent, so the repositories that still
    need one are deferred (``deferred_repos``), keep their cached rows, and come
    first in the next pass, which resumes before listing again. A rate limit
    mid-pass defers the rest the same way.
    """

    def __init__(self, *, provider_factory=None, interval=None, repo_limit=5000, issue_limit=100000, now=None,
                 store=None, full_reconcile_interval=None, reconcile_per_pass=None, organizations=None,
                 point_budget=None, projects=None):
        self._factory = provider_factory or GitHubProvider
        self._repo_limit, self._issue_limit = repo_limit, issue_limit
        self._now = now or time.time
        self._store_factory = store
        self._retired = []  # superseded stores, closed once no pass can still hold their lock
        # github.sync.*: an explicit argument wins; None = read the YAML at the start of each pass
        self._fixed_sync = {"interval": interval, "full_reconcile_interval": full_reconcile_interval,
                            "point_budget": point_budget}
        self._apply_sync_settings(strict=False)
        self._reconcile_per_pass = reconcile_per_pass
        # owner allowlist: a list, a callable returning one, or None = read the YAML each pass
        self._organizations = organizations
        # github.projects: a mapping wins; None = read the YAML each pass (default: off)
        self._fixed_projects = projects
        self._lock = threading.RLock()
        self._refresh_lock = threading.Lock()
        self._stop, self._wake = threading.Event(), threading.Event()
        self._thread = None
        self._credential = None
        self._generation = 0
        self._local = threading.local()
        self._retry_at = 0
        self._reset_catalog()
        self._view = self._empty()

    def _reset_catalog(self):
        self._repos, self._issues, self._details = {}, {}, {}
        # per repository: the probe values its issues were last synced at, and the watermark
        self._marks, self._watermarks = {}, {}
        self._full_at = {}  # repository -> time of its last full read (scheduled reconcile)
        self._deferred = {}  # repository -> when it was first deferred (budget or rate limit)
        # _issue_resume: repository -> its unfinished sync (see _sync_repository)
        self._resume_repos, self._issue_resume, self._resume_passes = None, {}, 0
        self._floor, self._pass_reads = None, 0
        self._store, self._store_viewer, self._store_dirty = None, None, False
        self._auth_failures = 0  # consecutive 401s: the persisted catalog is purged on the second
        self._detail_window = [0, 0]  # issue_detail points spent: [window start, points]
        self._bind_failed = False  # this pass already failed to read the viewer store
        self._revision = None  # the store's catalog revision after its last load or write
        self._purging = None  # the store a revoked token is purging: no revision adopted from it meanwhile
        self._reset_projects()
        self._touch()

    def _reset_projects(self):
        """Projects v2 (G9): rows by project id (listed fields + volatile ``complete``/``error``),
        items per project in board order, the probe each was last read in full at, watermarks,
        last full read times, and whether project writes failed (memory ahead of the store)."""
        self._projects, self._project_items, self._project_marks = {}, {}, {}
        self._project_watermarks, self._project_full_at = {}, {}
        # project -> when its item read was first deferred (the longest-waiting one is the pass's
        # progress floor), and an item read cut short: {probe, cursor, items} to resume from
        self._project_deferred, self._project_resume = {}, {}
        self._projects_unsaved = False

    def _touch(self):
        """Count an in-memory change (lock held) so an ETag never outlives what it describes."""
        self._epoch = getattr(self, "_epoch", 0) + 1

    @staticmethod
    def _empty():
        return {"available": False, "status": "not_configured", "connection_generation": 0, "refreshing": False, "stale": False,
                "partial": False, "account": None, "checked_at": None, "last_success_at": None,
                "scope": {"owners": [], "excluded_repositories": 0, "restricted": False}, "deferred_repos": [],
                "deferred_total": 0,
                "projects_sync": {"enabled": False, "status": "disabled", "error": None, "projects": 0,
                                  "deferred": 0, "last_success_at": None},
                "error": None, "rate_limit": None, "progress": {"phase": "idle", "repositories_seen": 0,
                    "repositories_total": None, "issue_repositories_complete": 0, "issue_repositories_total": None,
                    "issues_seen": 0, "issues_total_known": None, "current_repository": None}}

    def start(self):
        self._sync_credentials()  # opens the store: never under the monitor lock
        with self._lock:
            if self._thread is None and not self._stop.is_set():
                self._thread = threading.Thread(target=self._loop, daemon=True, name="herdr-github-catalog")
                self._thread.start()

    def close(self):
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=1)
        with self._lock:
            self._retire(self._store)
            self._store = None
        self._close_retired()

    def _sync_settings(self):
        """``github.sync`` settings for this pass; ``GitHubError("invalid_config")`` when invalid."""
        from .config import github_sync
        fixed = {key: value for key, value in self._fixed_sync.items() if value is not None}
        try:
            settings = github_sync(None if len(fixed) == len(self._fixed_sync) else _configured_sync())
        except InvalidError as exc:
            raise GitHubError("invalid_config", message=f"{exc}; fix the YAML configuration.") from None
        settings.update(fixed)
        return settings

    def _apply_sync_settings(self, *, strict=True):
        try:
            settings = self._sync_settings()
        except GitHubError:
            if strict:
                raise
            from .config import GITHUB_SYNC_DEFAULTS
            settings = dict(GITHUB_SYNC_DEFAULTS, **{k: v for k, v in self._fixed_sync.items() if v is not None})
        self._interval = max(30, settings["interval"])
        self._full_interval = max(self._interval, settings["full_reconcile_interval"])
        self._budget, self._spent = settings["point_budget"], 0

    def _retire(self, store):
        """Retire a superseded store (lock held) and queue it for closing.

        Retired, it refuses every later read and write, so a pass still holding
        it can neither purge nor claim the catalog its successor owns; it is
        closed later because that pass may still hold its advisory lock.
        """
        if store is not None and store not in self._retired:
            with contextlib.suppress(Exception):
                store.retire()
            self._retired.append(store)

    def _close_retired(self):
        with self._lock:
            retired, self._retired = [s for s in self._retired if s is not self._store], \
                [s for s in self._retired if s is self._store]
        for store in retired:
            with contextlib.suppress(Exception):
                store.close()

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

    def snapshot(self, *, start=True, sync=True):
        if sync:  # False: the caller synced the credentials before taking the lock
            self._sync_credentials()
        if start:
            self.start()
        with self._lock:
            view = copy.deepcopy(self._view)
            view["rate_limit"] = dict(view.get("rate_limit") or {}, retry_at=self._retry_at or None)
            if view["last_success_at"]:
                age = self._now() - datetime.fromisoformat(view["last_success_at"]).timestamp()
                view["stale"] = view["stale"] or age > self._interval * 2
            deferred = sorted((since, rid) for rid, since in self._deferred.items())
            view["deferred_repos"] = [{"repository_id": rid, "deferred_since": _iso(datetime.fromtimestamp(since, UTC))}
                                      for since, rid in deferred[:DEFERRED_SHOWN]]
            view["deferred_total"] = len(deferred)
            # a repository the budget keeps deferring shows as stale, not silently old (ruling 6)
            view["stale"] = view["stale"] or any(self._now() - since > self._interval * 2 for since, _ in deferred)
            return view

    def _ready(self, start):
        """Sync credentials (and open the store) and start the worker, before any lock is taken."""
        self._sync_credentials()
        if start:
            self.start()

    def repository_languages(self, *, start=True):
        """Return current-generation primary languages without a network read.

        Discovery stays on the existing background worker. Partial/stale cache
        entries remain useful, but token rotation clears them before exposure.
        """
        self._sync_credentials()
        if start:
            self.start()
        with self._lock:
            return {identity: record.get("language") for identity, record in self._repos.items()}

    def _sync_credentials(self):
        """Follow the credential. The store is opened and read OUTSIDE the monitor lock (a slow or
        hung database must not stall every request) and swapped in under it."""
        provider = self._factory()
        with self._lock:
            if provider.credential_identity == self._credential:
                return provider
            self._generation += 1
            self._credential = provider.credential_identity
            self._retire(self._store)
            self._reset_catalog()
            self._retry_at = 0
            self._view = self._empty()
            self._view["connection_generation"] = self._generation
            generation, credential = self._generation, self._credential
            if self._thread is not None and threading.current_thread() is not self._thread:
                self._wake.set()
        if not provider.available:
            return provider
        store, snapshot = self._open_store(credential, None)
        revision = self._revision_of(store, snapshot)
        with self._lock:
            if generation != self._generation:
                self._retire(store)  # another credential took over while the database was read
                return provider
            self._store = store
            if snapshot is not _UNREADABLE:
                self._adopt_revision(store, revision)
            elif store is not None:
                # memory cannot know what is stored: the first pass's flush reconciles it
                self._store_dirty = True
            if snapshot not in (None, _UNREADABLE):
                if self._repos:
                    self._store_dirty = True  # a pass filled memory while the store was read: it wins
                else:
                    self._hydrate(snapshot)
            elif self._repos and store is not None:
                self._store_dirty = True
            self._seed_projects_sync()
        return provider

    # -- persistent store

    def _open_store(self, identity, viewer, *, rows=True):
        """A store for this credential (and viewer, once known) plus its loaded catalog.

        The catalog is a ``Snapshot``, ``None`` when nothing is stored for this
        tag (empty, or another tag's rows were just purged), or ``_UNREADABLE``
        when the store could not be built or read: the catalog must keep working
        in memory while Postgres is down or not migrated yet, and a transient
        error must never read as a purge.
        """
        if self._store_factory is None:
            return None, None
        try:
            store = self._store_factory(identity, viewer)
        except Exception:
            return None, _UNREADABLE
        try:
            return store, (store.load() if rows else store.load(rows=False))
        except Exception:
            return store, _UNREADABLE

    def _remember_revision(self, store, revision=None):
        """Cache the store's revision so serving a list never opens a store transaction.

        Read outside the lock, assigned under it and only for the current store:
        a superseded store's revision is never adopted, and a read that raced a
        newer write (or a purge) never moves the cached revision back.
        """
        if revision is None:
            try:
                revision = store.revision()
            except Exception:
                revision = None
        with self._lock:
            self._adopt_revision(store, revision)

    def _adopt_revision(self, store, revision):
        """Lock held, no database access: cache ``revision`` if ``store`` is still the current one."""
        if store is not self._store or store is self._purging:
            return
        if revision is None or self._revision is None:
            self._revision = revision
        else:
            self._revision = max(self._revision, revision)

    @staticmethod
    def _revision_of(store, snapshot):
        """The revision a freshly opened store reports (read outside the lock), ``None`` if unknown."""
        if store is None or snapshot is _UNREADABLE:
            return None
        if snapshot is not None:
            return snapshot.revision
        try:
            return store.revision()
        except Exception:
            return None

    def _allowlist(self):
        """The validated owner allowlist (empty = every owner); ``GitHubError`` when invalid."""
        from .config import github_organizations
        source = self._organizations
        try:
            value = _configured_organizations() if source is None else source() if callable(source) else list(source)
            return set(github_organizations(value))
        except InvalidError:
            raise GitHubError("invalid_config") from None

    def _hydrate(self, snapshot):
        """Serve a stored catalog (lock held) as ``stale`` until the first pass completes.

        Owners outside the allowlist are never served, even before the pass that purges them.
        """
        try:
            allow = self._allowlist()
        except GitHubError:
            return
        for rid, data in snapshot.repositories.items():
            if allow and _owner(data) not in allow:
                continue
            record = dict(data)
            synced = record.pop("synced", None)
            issues = dict(snapshot.issues.get(rid, {}))
            record.update(newest_updated_at=None, issues_loaded=len(issues), issues_complete=True, error=None)
            self._repos[rid] = record
            self._issues[rid] = issues
            if isinstance(synced, dict):
                self._marks[rid] = synced
            if rid in snapshot.watermarks:
                self._watermarks[rid] = snapshot.watermarks[rid]
            # not persisted: spread the next full reads over one interval after a restart
            offset = int(hashlib.sha256(rid.encode()).hexdigest()[:8], 16) % int(self._full_interval)
            self._full_at[rid] = self._now() - offset
        for pid, data in snapshot.projects.items():
            if allow and data.get("owner") not in allow:
                continue
            record = dict(data)
            synced = record.pop("synced", None)
            record.update(complete=True, error=None)
            self._projects[pid] = record
            self._project_items[pid] = dict(sorted(snapshot.project_items.get(pid, {}).items(),
                                                   key=lambda pair: pair[1].get("position", 0)))
            if isinstance(synced, dict):
                self._project_marks[pid] = synced
            if pid in snapshot.project_watermarks:
                self._project_watermarks[pid] = snapshot.project_watermarks[pid]
            offset = int(hashlib.sha256(pid.encode()).hexdigest()[:8], 16) % int(self._full_interval)
            self._project_full_at[pid] = self._now() - offset
        self._touch()
        if self._repos:
            self._view.update(available=True, status="stale", stale=True,
                              account=snapshot.viewer)

    def _bind_viewer(self, account):
        """Swap to a store tagged with the viewer login; a different stored viewer purges it.

        A store that cannot be read now is not swapped in: the pass keeps
        writing through the fingerprint store and memory stays as it is (only a
        real purge clears it); the next listing page or pass retries the bind.
        """
        with self._lock:
            self._check_generation()
            if self._store is None or self._store_viewer == account or self._bind_failed:
                return self._store
            generation, credential = self._generation, self._credential
        # the database is read outside the lock; only the revision is needed (the catalog is in memory)
        store, snapshot = self._open_store(credential, account, rows=False)
        revision = self._revision_of(store, snapshot)
        with self._lock:
            if generation != self._generation:
                self._retire(store)
                self._check_generation()
                return self._store
            if snapshot is _UNREADABLE:
                self._retire(store)
                self._bind_failed = True  # once per pass: each try may wait out a connect timeout
                return self._store
            self._retire(self._store)
            self._store, self._store_viewer = store, account
            self._adopt_revision(store, revision)
            if snapshot is None:
                # the stored catalog belonged to another viewer and was purged (or was empty)
                self._issues, self._marks, self._watermarks, self._issue_resume = {}, {}, {}, {}
                self._full_at = {}
                self._reset_projects()
                self._store_dirty = False
                self._touch()
            return store

    def _write(self, store, work):
        """Write through to the store; on any failure keep serving memory and flush later."""
        if store is None:
            return
        try:
            work(store)
        except Exception:
            with self._lock:
                if store is self._store:  # a refused superseded store says nothing about the current one
                    self._store_dirty = True
            return
        if store is self._store:
            self._remember_revision(store)

    def _stored_repository(self, record):
        row = {key: value for key, value in record.items() if key not in VOLATILE_REPOSITORY_FIELDS}
        row["synced"] = self._marks.get(record["repository_id"])
        return row

    def _flush(self, store):
        """Rewrite the whole in-memory catalog after earlier writes failed."""
        with self._lock:
            self._check_generation()
            if store is not self._store:
                return  # superseded: it may not write, and the current store stays dirty
            self._store_dirty = False
            repos = [self._stored_repository(record) for record in self._repos.values()]
            issues = {rid: dict(rows) for rid, rows in self._issues.items()}
            watermarks = dict(self._watermarks)

        def work(target):
            snapshot = target.load()
            # the listing first, without the synced probes: a flush cut short leaves
            # those repositories looking changed, never synced over stale issues
            target.upsert_repositories([dict(row, synced=None) for row in repos], None)
            for row in repos:
                rid = row["repository_id"]
                stale = set(snapshot.issues.get(rid, {}) if snapshot else ()) - set(issues.get(rid, {}))
                target.apply_repository(row, issues.get(rid, {}).values(), stale, watermarks.get(rid))
        self._write(store, work)

    @staticmethod
    @contextlib.contextmanager
    def _pass_lock(store):
        """The store's advisory lock around one pass (no store, or no database: run anyway)."""
        if store is None:
            yield True
            return
        with contextlib.ExitStack() as stack:
            try:
                acquired = stack.enter_context(store.advisory_lock())
            except Exception:
                acquired = True
            yield acquired

    def _check_generation(self):
        if getattr(self._local, "generation", self._generation) != self._generation:
            raise _Superseded()

    def _update(self, **fields):
        with self._lock:
            self._check_generation()
            self._view.update(fields)
            self._touch()

    def _progress(self, **fields):
        with self._lock:
            self._check_generation()
            self._view["progress"].update(fields)
            self._touch()

    def refresh(self):
        provider = self._sync_credentials()
        if not self._refresh_lock.acquire(blocking=False):
            return
        try:
            if self._now() >= self._retry_at:
                self._refresh(provider)
        finally:
            # every refresh closes what rotations retired, whether or not a pass ran
            # (paused, token removed); no pass can still hold their advisory lock here
            self._close_retired()
            self._refresh_lock.release()

    def _refresh(self, provider):
        with self._lock:
            generation = self._generation
            self._local.generation = generation
        try:
            self._collect(provider)
            with self._lock:
                if generation == self._generation and self._view.get("status") != "advisory_locked":
                    self._auth_failures = 0
        except _Superseded:
            pass
        except GitHubError as exc:
            if exc.reason == "authentication":
                self._revoke_catalog(generation)
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

    def _revoke_catalog(self, generation):
        """GitHub rejected the token (401): stop serving its catalog and purge the stored one.

        A revoked token keeps its fingerprint, so nothing else would ever drop
        the private rows it could read (ruling 2: revoked access never leaks).
        """
        with self._lock:
            if generation != self._generation:
                return
            self._auth_failures += 1
            if self._auth_failures < AUTH_FAILURES_BEFORE_PURGE:
                return  # one 401 can be a blip (or a proxy); the persisted catalog survives it
            store = self._store
            self._repos, self._issues, self._details = {}, {}, {}
            self._marks, self._watermarks, self._full_at, self._deferred = {}, {}, {}, {}
            self._resume_repos, self._issue_resume = None, {}
            self._reset_projects()
            self._revision = None  # until the purge lands, no since can be answered from memory
            self._purging = store
            self._view.update(available=False, status="auth_error", stale=False, partial=False, account=None,
                              error=str(GitHubError("authentication")))
            self._touch()
        if store is None:
            return
        try:
            store.purge()
        except Exception:
            with self._lock:
                if store is self._store:
                    self._store_dirty = True  # the next 401 purges again
            return
        finally:
            with self._lock:
                if self._purging is store:
                    self._purging = None
        self._remember_revision(store)

    def _collect(self, provider):
        with self._lock:
            self._check_generation()
            if provider.credential_identity != self._credential:
                raise _Superseded()
            store = self._store
        if not provider.available:
            with self._lock:
                self._view.update(refreshing=True, error=None, status="syncing", partial=True)
            raise GitHubError("not_configured")
        with self._pass_lock(store) as acquired:
            if acquired:
                self._collect_pass(provider)
            else:
                # another instance holds the catalog's advisory lock: say so instead of silently idling
                logging.getLogger(__name__).warning(
                    "GitHub catalog sync skipped: another herdr instance holds the advisory lock")
                with self._lock:
                    self._view.update(refreshing=False, checked_at=_stamp(), status="advisory_locked",
                                      error="Another instance is syncing the GitHub catalog; serving its stored copy.")
                    self._touch()

    def _collect_pass(self, provider):
        self._update(refreshing=True, error=None, status="syncing", partial=True, available=True)
        self._progress(phase="repositories", repositories_seen=0, repositories_total=None,
                       issues_seen=0, issues_total_known=None, issue_repositories_complete=0, current_repository=None)
        self._apply_sync_settings()
        projects = self._projects_settings()
        with self._lock:
            self._spent = self._detail_spent()  # issue_detail fetches draw on the same budget
        allow = self._allowlist()
        with self._lock:
            self._check_generation()
            store = self._store
            self._bind_failed = False
            if allow:
                # stop serving owners that left the allowlist now, not after the listing
                dropped = [rid for rid, record in self._repos.items() if _owner(record) not in allow]
                for rid in dropped:
                    for table in (self._repos, self._issues, self._marks, self._watermarks,
                                  self._issue_resume, self._full_at, self._deferred):
                        table.pop(rid, None)
                gone = [pid for pid, row in self._projects.items() if row.get("owner") not in allow]
                for pid in gone:
                    for table in self._project_tables():
                        table.pop(pid, None)
                if dropped or gone:
                    self._touch()  # no 304 may still describe the dropped owner's rows
                if self._resume_repos is not None:
                    self._resume_repos = {rid: r for rid, r in self._resume_repos.items() if _owner(r) in allow}
            relist = self._resume_repos is None or self._resume_passes >= RESUME_PASSES_BEFORE_RELIST
            self._resume_passes = 0 if relist else self._resume_passes + 1
        owners = None  # owners seen by this pass's listing (None: no listing this pass)
        if relist:
            discovered, cursor, cursors, excluded, owners = {}, None, set(), 0, set()
            while not self._stop.is_set():
                account, connection = provider.repositories_page(cursor)
                self._charge(provider)  # the listing is never cut short, but it counts
                store = self._bind_viewer(account)
                for row in connection["nodes"]:
                    record = _repository(row)
                    if allow and _owner(record) not in allow:
                        excluded += 1  # filtered before any issue work: no issue calls, not stored
                        continue
                    owners.add(_owner(record))
                    record["issues_loaded"] = len(self._issues.get(record["repository_id"], {}))
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
                for table in (self._issues, self._marks, self._watermarks, self._issue_resume, self._full_at,
                              self._deferred):
                    for key in [key for key in table if key not in discovered]:
                        del table[key]
                self._resume_repos = discovered
                # memory is ahead of a store that missed writes: no synced probe until the flush
                rows = [dict(self._stored_repository(record), **({"synced": None} if self._store_dirty else {}))
                        for record in discovered.values()]
            self._update(scope={"owners": sorted(allow or owners), "excluded_repositories": excluded,
                                "restricted": bool(allow)})

            def replace_listing(target):
                # the listing is complete: stored repositories missing from it are gone,
                # and so is every repository of an owner outside the allowlist
                target.upsert_repositories(rows, None)
                target.purge_owners_not_in(allow)
            self._write(store, replace_listing)
        else:
            discovered = self._resume_repos
            self._progress(repositories_seen=len(discovered), repositories_total=len(discovered))
        if self._store_dirty and store is not None:
            self._flush(store)
        with self._lock:
            deferred = dict(self._deferred)
        # repositories an earlier pass deferred go first, the longest-waiting first
        repos = sorted(discovered.values(), key=lambda r: (
            r["repository_id"] not in deferred, deferred.get(r["repository_id"], 0),
            -r["open_issue_count"], r["repository_id"]))
        due = self._due_reconciles(discovered)
        self._progress(phase="issues", issue_repositories_total=len(repos),
                       issues_total_known=sum(r["open_issue_count"] for r in repos))
        # progress floor: the longest-deferred repository gets one read even when the
        # listing spent the budget, so a repository too big for any pass still advances
        self._floor = next((r["repository_id"] for r in repos if r["repository_id"] in deferred), None)
        self._pass_reads = 0
        errors, over_budget, completed, limited = {}, [], 0, None
        for record in repos:
            if self._stop.is_set():
                return
            rid = record["repository_id"]
            if record["issues_complete"]:
                continue
            self._progress(current_repository=rid)
            try:
                if not self._sync_repository(provider, store, record, reconcile=rid in due):
                    return
                completed += 1
                with self._lock:
                    self._deferred.pop(rid, None)
                done = [r for r in repos if r["issues_complete"]]
                self._progress(issues_seen=sum(len(self._issues.get(r["repository_id"], {})) for r in done),
                               issue_repositories_complete=len(done))
            except _OverBudget:
                # costs nothing more: later repositories still run, unchanged ones complete for free
                over_budget.append(rid)
                self._defer([rid])
            except GitHubError as exc:
                with self._lock:
                    self._check_generation()
                    record["error"] = str(exc)
                    record["issues_loaded"] = len(self._issues.get(rid, {}))
                errors[rid] = str(exc)
                if exc.reason == "rate_limited":
                    # the same as a spent budget: no further read (and no floor), so only the
                    # repositories that still need one wait; unchanged ones complete for free
                    self._defer([rid])
                    limited, self._floor = exc, None
                    self._spent = max(self._spent, self._budget)
                    continue
                if exc.reason in {"authentication", "capacity"}:
                    raise
        # the owner set is complete when it is the allowlist or comes from this pass's full listing
        self._sync_projects(provider, store, projects, self._project_owners(allow, owners), limited=limited is not None,
                            authoritative=bool(allow) or owners is not None)
        if limited is not None:
            raise limited  # the resume state stays: the next pass continues with the deferred
        if over_budget:
            # keep the resume state: the next pass continues with the deferred
            # repositories before listing again (a partly read repository resumes at its cursor)
            if not relist and not completed and not self._pass_reads:
                with self._lock:
                    self._resume_passes = RESUME_PASSES_BEFORE_RELIST  # no progress: list next pass
            error = (f"The per-pass point budget ({self._budget}) was spent; "
                     f"{len(over_budget)} repositories wait for the next pass.")
            if errors:  # an error is not hidden by the budget note, nor counted as a success
                error = f"Some repository issues could not be refreshed. {error}"
            self._update(status="partial", partial=True, stale=bool(errors), error=error,
                         last_success_at=self._view["last_success_at"] if errors else _stamp())
            self._progress(phase="complete", current_repository=None)
            return
        with self._lock:
            self._check_generation()
            self._resume_repos, self._issue_resume, self._resume_passes = None, {}, 0
        self._update(status="partial" if errors else "ready", partial=bool(errors), stale=bool(errors),
                     error="Some repository issues could not be refreshed." if errors else None,
                     last_success_at=self._view["last_success_at"] if errors else _stamp())
        self._progress(phase="complete", current_repository=None)

    # -- Projects v2 (G9)

    def _projects_settings(self):
        """``github.projects`` for this pass; ``GitHubError("invalid_config")`` when invalid."""
        from .config import github_projects
        try:
            return github_projects(self._fixed_projects if self._fixed_projects is not None else _configured_projects())
        except InvalidError as exc:
            raise GitHubError("invalid_config", message=f"{exc}; fix the YAML configuration.") from None

    def _seed_projects_sync(self):
        """After a view reset (start, token change): say whether project sync is on before the
        first pass does, so hydrated projects are served (``stale``) and the page never claims
        that sync is off while it is configured on (lock held)."""
        try:
            enabled = self._projects_settings()["enabled"]
        except GitHubError:
            enabled = False
        if enabled:
            self._view["projects_sync"].update(enabled=True, status="stale" if self._projects else "waiting",
                                               projects=len(self._projects))

    def _project_owners(self, allow, listed):
        """Whose projects are read: the allowlist when set; otherwise the owners of the listed
        repositories (or, on a pass without a listing, the last scope) plus the viewer's account."""
        if allow:
            return sorted(allow)
        with self._lock:
            owners = set(listed if listed is not None else self._view["scope"]["owners"])
            account = self._view.get("account")
        if isinstance(account, str) and account and ":" not in account:  # not the App's account
            owners.add(account.lower())
        return sorted(owners)

    def _project_tables(self):
        return (self._projects, self._project_items, self._project_marks, self._project_watermarks,
                self._project_full_at, self._project_deferred, self._project_resume)

    def _projects_update(self, **fields):
        with self._lock:
            self._check_generation()
            self._view["projects_sync"].update(fields)
            self._view["projects_sync"]["projects"] = len(self._projects)
            self._touch()

    def _write_projects(self, store, work):
        """Write project rows through; a failure (e.g. migration 0008 not applied yet) leaves
        the issue catalog alone and marks only the projects unsaved, rewritten next pass."""
        if store is None:
            return
        try:
            work(store)
        except Exception:
            with self._lock:
                if store is self._store:
                    self._projects_unsaved = True
            return
        if store is self._store:
            self._remember_revision(store)

    def _stored_project(self, row):
        data = {key: value for key, value in row.items() if key not in ("complete", "error")}
        data["synced"] = self._project_marks.get(row["project_id"])
        return data

    def _flush_projects(self, store):
        """Rewrite every project in memory after earlier project writes failed."""
        with self._lock:
            if store is not self._store or not self._projects_unsaved:
                return
            self._projects_unsaved = False
            by_owner = {}
            for row in self._projects.values():
                by_owner.setdefault(row["owner"], []).append(self._stored_project(row))
            work = [(self._stored_project(row), list(self._project_items.get(pid, {}).values()),
                     self._project_watermarks.get(pid)) for pid, row in self._projects.items()]

        def flush(target):
            snapshot = target.load()
            for owner, rows in by_owner.items():
                target.replace_projects(rows, {owner})
            for row, items, watermark in work:
                stored = set(snapshot.project_items.get(row["project_id"], {}) if snapshot else ())
                target.apply_project(row, items, stored - {i["item_id"] for i in items}, watermark)
        self._write_projects(store, flush)

    def _sync_projects(self, provider, store, settings, owners, *, limited, authoritative=False):
        """List each in-scope owner's projects, then read the items of the projects whose
        (``updatedAt``, item count) probe changed or whose staggered full read is due.

        The listing of an owner always completes (it is one call per 50 projects); an item
        read is not started once the pass's point budget is spent, so that project keeps its
        old items and is read next pass. A failure here never fails the issue pass: only a
        401 (``authentication``) propagates.
        """
        if not settings["enabled"]:
            self._projects_update(enabled=False, status="disabled", error=None, deferred=0)
            return
        self._projects_update(enabled=True)
        if limited:  # GitHub is rate limiting this pass: nothing more is read
            self._projects_update(status="rate_limited", deferred=len(self._projects),
                                  error=str(GitHubError("rate_limited")))
            return
        if not hasattr(provider, "projects_page"):
            # a provider without Projects v2 support never fails the issue pass
            self._projects_update(status="error", error="This GitHub provider does not support Projects v2.")
            return
        if self._projects_unsaved:
            self._flush_projects(store)
        if authoritative:
            # an owner out of scope (no longer listed, or dropped from the allowlist) keeps no
            # projects: not in memory, not in the store
            with self._lock:
                self._check_generation()
                gone_owners = {row["owner"] for row in self._projects.values()} - set(owners)
                for pid in [pid for pid, row in self._projects.items() if row["owner"] in gone_owners]:
                    for table in self._project_tables():
                        table.pop(pid, None)
                if gone_owners:
                    self._touch()
            if gone_owners:
                self._write_projects(store, lambda target: target.replace_projects([], gone_owners))
        errors, listed, deferred, limit_hit = [], {}, [], None
        for owner in owners:
            if self._stop.is_set():
                return
            try:
                rows = self._list_projects(provider, owner)
            except GitHubError as exc:
                if exc.reason == "authentication":
                    raise
                if exc.reason == "rate_limited":
                    limit_hit = exc
                    break
                errors.append((owner, str(exc)))
                continue
            listed.update({row["project_id"]: row for row in rows})
            with self._lock:
                self._check_generation()
                gone = [pid for pid, row in self._projects.items() if row["owner"] == owner and pid not in listed]
                for pid in gone:
                    for table in self._project_tables():
                        table.pop(pid, None)
                for row in rows:
                    previous = self._projects.get(row["project_id"], {})
                    read = self._project_items.get(row["project_id"])
                    self._projects[row["project_id"]] = dict(row, complete=previous.get("complete", False),
                                                             error=previous.get("error"),
                                                             item_count=len(read) if read is not None else row["item_count"])
                stored_rows = [self._stored_project(self._projects[row["project_id"]]) for row in rows]
                self._touch()
            self._write_projects(store, lambda target, rows=stored_rows, owner=owner:
                                 target.replace_projects(rows, {owner}))
        now = self._now()
        # progress floor: the longest-deferred project may make one item page even when the
        # listings spent the budget, so a pass with a small budget still advances a project
        with self._lock:
            waiting = sorted((at, pid) for pid, at in self._project_deferred.items() if pid in listed)
        floor = waiting[0][1] if waiting else None
        order = [pair for pair in listed.items() if pair[0] == floor] + sorted(
            (pair for pair in listed.items() if pair[0] != floor),
            key=lambda pair: pair[1]["updated_at"] or "", reverse=True)
        for pid, row in order:
            if self._stop.is_set() or limit_hit is not None:
                break
            probe = {"updated_at": row["updated_at"], "item_count": row.get("listed_item_count", row["item_count"])}
            with self._lock:
                due = now - self._project_full_at.get(pid, now) >= self._full_interval
                unchanged = self._project_marks.get(pid) == probe and pid in self._project_items and not due
            if unchanged:
                with self._lock:
                    self._projects[pid].update(complete=True, error=None)
                continue
            if self._spent >= self._budget and pid != floor:
                deferred.append(pid)
                with self._lock:
                    self._project_deferred.setdefault(pid, now)
                continue
            try:
                self._read_project(provider, store, row, probe, settings["item_limit"], floor=pid == floor)
                with self._lock:
                    self._project_deferred.pop(pid, None)
            except _OverBudget:
                deferred.append(pid)
                with self._lock:
                    self._project_deferred.setdefault(pid, now)
            except GitHubError as exc:
                if exc.reason == "authentication":
                    raise
                if exc.reason == "rate_limited":
                    limit_hit = exc
                    deferred.append(pid)
                    continue
                with self._lock:
                    self._projects[pid].update(complete=False, error=str(exc))
                errors.append((row["title"] or pid, str(exc)))
        # one line per distinct message, naming every owner or project it applies to
        grouped = {}
        for who, message in errors:
            grouped.setdefault(message, []).append(who)
        problems = [f"{', '.join(who)}: {message}" for message, who in grouped.items()]
        if errors and not listed:
            status = "error"
        elif limit_hit is not None:
            status = "rate_limited"
            problems.append(str(limit_hit))
        elif errors or deferred:
            status = "partial"
        else:
            status = "ready"
        if deferred:
            problems.append(f"The per-pass point budget ({self._budget}) was spent; "
                            f"{len(deferred)} projects wait for the next pass.")
        error = " ".join(problems) or None
        if any("scope" in m or "denied" in m for m in grouped) and "read:project" not in error:
            error = f"{error} Projects need the token's read:project scope."
        self._projects_update(status=status, error=error, deferred=len(deferred),
                              **({"last_success_at": _stamp()} if status == "ready" else {}))

    def _list_projects(self, provider, owner):
        rows, cursor, cursors = [], None, set()
        while True:
            connection = provider.projects_page(owner, cursor)
            self._charge(provider)  # like the repository listing: never cut short, but it counts
            rows.extend(_project(node, owner) for node in connection["nodes"])
            if not connection["pageInfo"]["hasNextPage"]:
                return rows
            cursor = connection["pageInfo"]["endCursor"]
            if cursor in cursors:
                raise GitHubError("invalid_response")
            cursors.add(cursor)

    def _read_project(self, provider, store, row, probe, item_limit, *, floor=False):
        """Read one project's items in full (they cannot be ordered by update time) and
        reconcile: items missing from a complete read are removed. A read cut short (budget,
        item limit, error) changes nothing shown: the old items stay and the probe is not advanced.
        A read the budget cut short resumes at its cursor next pass while the project's probe is
        unchanged (a changed project starts again at page 1, since positions may have moved).
        ``floor``: the pass's progress floor, which may read one page over the budget."""
        pid = row["project_id"]
        with self._lock:
            resume = self._project_resume.pop(pid, None)
        if resume is not None and resume["probe"] == probe:
            items, cursor = dict(resume["items"]), resume["cursor"]
        else:
            items, cursor = {}, None
        cursors, pages = set(), 0
        while True:
            if self._spent >= self._budget and not (floor and pages == 0):
                if cursor is not None:
                    with self._lock:
                        self._project_resume[pid] = {"probe": probe, "cursor": cursor, "items": items}
                raise _OverBudget()
            pages += 1
            connection = provider.project_items_page(pid, cursor)
            self._charge(provider)
            for node in connection["nodes"]:
                item = _project_item(node, pid)
                if item is not None:
                    items[item["item_id"]] = dict(item, position=len(items))
            if len(items) > item_limit:
                raise GitHubError("capacity", message=(
                    f"The project has more than github.projects.item_limit ({item_limit}) items; "
                    "showing the items read earlier."))
            if not connection["pageInfo"]["hasNextPage"]:
                break
            cursor = connection["pageInfo"]["endCursor"]
            if cursor in cursors:
                raise GitHubError("invalid_response")
            cursors.add(cursor)
        watermark = _latest(*(item["updated_at"] for item in items.values()))
        with self._lock:
            self._check_generation()
            previous = set(self._project_items.get(pid, {}))
            self._project_items[pid] = items
            self._project_marks[pid] = probe
            self._project_watermarks[pid] = watermark
            self._project_full_at[pid] = self._now()
            self._projects[pid].update(complete=True, error=None, item_count=len(items))
            stored = self._stored_project(self._projects[pid])
            self._touch()
        removed = previous - set(items)
        self._write_projects(store, lambda target: target.apply_project(stored, items.values(), removed, watermark))

    def projects(self, *, start=True):
        """The in-scope Projects v2 with the sync state; empty while project sync is off."""
        view = self.snapshot(start=start)
        with self._lock:
            enabled = view["projects_sync"]["enabled"] or (self._fixed_projects or {}).get("enabled", False)
            rows = [copy.deepcopy(row) for row in self._projects.values()] if enabled else []
        view["projects"] = sorted(rows, key=lambda r: (r["owner"], r["number"]))
        return view

    def project_items(self, project_id, *, start=True):
        """One project and its items in board order; ``GitHubError("not_found")`` when unknown."""
        view = self.projects(start=start)
        project = next((row for row in view["projects"] if row["project_id"] == project_id), None)
        if project is None:
            raise GitHubError("not_found")
        with self._lock:
            items = [copy.deepcopy(item) for item in self._project_items.get(project_id, {}).values()]
        view.pop("projects")
        view.update(project=project, items=sorted(items, key=lambda i: i.get("position", 0)))
        return view

    def _defer(self, rids):
        now = self._now()
        with self._lock:
            self._check_generation()
            for rid in rids:
                self._deferred.setdefault(rid, now)

    def _afford(self, rid):
        """Before an issue read: stop when this pass's point budget is spent.

        The pass's progress floor (the longest-deferred repository) may still
        make the first issue read of the pass.
        """
        if self._spent >= self._budget and not (rid == self._floor and not self._pass_reads):
            raise _OverBudget()
        self._pass_reads += 1

    def _charge(self, provider):
        """Add the last call's GraphQL ``rateLimit.cost`` to this pass: at least 1 per call, and 1
        when GitHub did not report it for this call (never an earlier call's cost)."""
        self._spent += self._cost(provider)

    @staticmethod
    def _cost(provider):
        if hasattr(provider, "last_cost"):
            cost = provider.last_cost
        else:  # a provider without per-call costs: its current rate-limit report
            cost = provider.rate_limit.get("cost") if isinstance(provider.rate_limit, dict) else None
        return max(1, cost) if isinstance(cost, int) and not isinstance(cost, bool) else 1

    def _detail_spent(self):
        """Points issue_detail fetches spent in the current interval window (lock held)."""
        start, points = self._detail_window
        if self._now() - start >= self._interval:
            self._detail_window = [self._now(), 0]
            return 0
        return points

    def _due_reconciles(self, discovered):
        """Repositories whose scheduled full read is due, oldest first, at most N this pass."""
        now = self._now()
        with self._lock:
            due = sorted((at, rid) for rid, at in self._full_at.items()
                         if rid in discovered and now - at >= self._full_interval)
        limit = self._reconcile_per_pass or max(1, math.ceil(len(discovered) * self._interval / self._full_interval))
        return {rid for _, rid in due[:limit]}

    def _sync_repository(self, provider, store, record, *, reconcile=False):
        """Bring one repository's issues up to date; ``False`` when stopped mid-read."""
        rid = record["repository_id"]
        probe = {"open": record["open_issue_count"], "newest": record["newest_updated_at"]}
        with self._lock:
            self._check_generation()
            stored = dict(self._issues.get(rid, {}))
            watermark = self._watermarks.get(rid)
            resume = self._issue_resume.get(rid)
            unchanged = self._marks.get(rid) == probe and (probe["newest"] is not None or not probe["open"])
        if resume is None and unchanged and not reconcile:
            with self._lock:
                self._check_generation()
                record.update(issues_complete=True, issues_loaded=len(stored), error=None)
            return True
        if resume is None and not probe["open"]:
            # nothing is open: every stored issue left the open set, no read needed
            self._apply(store, record, probe, issues={}, upserts={}, removed=set(stored),
                        watermark=_latest(watermark, probe["newest"]), full=True)
            return True
        if resume is None:
            # An unfinished sync survives deferral and relisting. It keeps the probe of
            # the listing it started from: a later listing's newer probe would lift the
            # watermark (and the synced mark) past edits this sync never read.
            mode = "incremental" if watermark and _when(watermark) and not reconcile else "full"
            resume = self._resume(rid, phase="open", mode=mode, probe=probe, seen=[])
        probe = resume["probe"]
        if resume["phase"] == "open":
            if self._read_open(provider, record, stored, watermark, resume) is None:
                return False
            resume["seen"] += [issue["updated_at"] for issue in resume["fresh"].values()]
            if resume["mode"] == "incremental":
                with self._lock:
                    self._check_generation()
                    resume.update(phase="closed", closed={}, cursor=None, cursors=set(),
                                  since=_iso(_when(watermark) - WATERMARK_OVERLAP))
        fresh = resume["fresh"]
        if resume["mode"] == "full":
            issues, removed, upserts = dict(fresh), set(stored) - set(fresh), fresh
        else:
            closed = self._read_closed(provider, rid, resume)
            if closed is None:
                return False
            # closed-read timestamps never feed the watermark: a closed issue edited after
            # the open read would lift it past an open edit that read never saw
            issues = {gid: issue for gid, issue in {**stored, **fresh}.items() if gid not in closed}
            removed, upserts = set(closed) & (set(stored) | set(fresh)), {g: i for g, i in fresh.items() if g not in closed}
            if len(issues) != resume["total"]:
                # deletes and transfers never pass through CLOSED: re-read this repository only
                # (recorded first, so a deferral resumes the full read, not the closed one)
                resume = self._resume(rid, phase="open", mode="full", probe=probe, seen=resume["seen"])
                if self._read_open(provider, record, issues, watermark, resume) is None:
                    return False
                full = resume["fresh"]
                resume["seen"] += [issue["updated_at"] for issue in full.values()]
                removed = (set(stored) | set(fresh)) - set(full)
                issues, upserts = dict(full), full
        self._apply(store, record, probe, issues=issues, upserts=upserts, removed=removed,
                    watermark=_latest(watermark, probe["newest"], *resume["seen"]),
                    full=resume["mode"] == "full")
        return True

    def _resume(self, rid, **state):
        """Start (and record) one repository's sync: its open read comes first."""
        resume = dict(state, fresh={}, cursor=None, cursors=set(), total=None)
        with self._lock:
            self._check_generation()
            self._issue_resume[rid] = resume
        return resume

    def _read_open(self, provider, record, stored, watermark, resume):
        """Page open issues newest first: all of them (``full``) or down to the watermark overlap.

        Continues ``resume`` at its cursor and records each page in it (``fresh``,
        ``cursor``, the open ``total``); ``None`` when stopped.
        """
        rid = record["repository_id"]
        fresh = resume["fresh"]
        cutoff = _when(watermark) - WATERMARK_OVERLAP if resume["mode"] == "incremental" else None
        while True:
            if self._stop.is_set():
                return None
            self._afford(rid)
            connection = provider.issues_page(rid, resume["cursor"])
            self._charge(provider)
            below = False
            for row in connection["nodes"]:
                issue = _issue(row, rid)
                fresh[issue["github_id"]] = issue
                when = _when(issue["updated_at"])
                below = below or (cutoff is not None and when is not None and when < cutoff)
            with self._lock:
                self._check_generation()
                candidate = {**stored, **fresh}
                retained = sum(len(rows) for key, rows in self._issues.items() if key != rid)
                if retained + len(candidate) > self._issue_limit:
                    raise GitHubError("capacity")
                self._issues[rid] = copy.deepcopy(candidate)
                record["issues_loaded"] = len(candidate)
                if resume["total"] is None:
                    resume["total"] = connection["totalCount"]
            self._update(rate_limit=provider.rate_limit)
            if below or not connection["pageInfo"]["hasNextPage"]:
                return fresh
            self._advance(resume, connection)

    def _advance(self, resume, connection):
        """Record the next page's cursor in ``resume`` (a repeated cursor is an invalid response)."""
        cursor = connection["pageInfo"]["endCursor"]
        if cursor in resume["cursors"]:
            raise GitHubError("invalid_response")
        with self._lock:
            self._check_generation()
            resume["cursors"].add(cursor)
            resume["cursor"] = cursor

    def _read_closed(self, provider, rid, resume):
        """``{github_id: updatedAt}`` of issues closed (or edited while closed) since ``resume["since"]``.

        Pages from ``resume``'s cursor, so a window larger than one pass's budget
        is read once across passes, never again from page 1; ``None`` when stopped.
        """
        closed = resume["closed"]
        while True:
            if self._stop.is_set():
                return None
            self._afford(rid)
            connection = provider.closed_issues_page(rid, resume["since"], resume["cursor"])
            self._charge(provider)
            for row in connection["nodes"]:
                if not isinstance(row, dict) or not isinstance(row.get("id"), str):
                    raise GitHubError("invalid_response")
            with self._lock:
                self._check_generation()
                closed.update((row["id"], row.get("updatedAt")) for row in connection["nodes"])
            self._update(rate_limit=provider.rate_limit)
            if not connection["pageInfo"]["hasNextPage"]:
                return closed
            self._advance(resume, connection)

    def _apply(self, store, record, probe, *, issues, upserts, removed, watermark, full=False):
        """Commit one repository's completed sync to memory, then to the store.

        The probe values are recorded only here, after the issues are in, so a
        failed read leaves the repository looking changed on the next pass.
        """
        rid = record["repository_id"]
        with self._lock:
            self._check_generation()
            if len(issues) + sum(len(rows) for key, rows in self._issues.items() if key != rid) > self._issue_limit:
                raise GitHubError("capacity")
            self._issues[rid] = issues
            self._watermarks[rid] = watermark
            self._marks[rid] = probe
            if full:
                self._full_at[rid] = self._now()
            self._issue_resume.pop(rid, None)
            record.update(issues_complete=True, issues_loaded=len(issues), error=None)
            row = self._stored_repository(record)
            self._touch()

        # one transaction: a restart never sees the watermark or the synced probe
        # ahead of the issues and removals they describe
        self._write(store, lambda target: target.apply_repository(row, upserts.values(), removed, watermark))

    def _repository_rows(self, search):
        """Repositories matching ``search`` in list order (lock held)."""
        return sorted((copy.deepcopy(row) for row in self._repos.values() if search.lower() in row["name_with_owner"].lower()),
                      key=lambda r: (-r["open_issue_count"], r["name_with_owner"].lower()))

    def _issue_rows(self, repository_id, search):
        """Issues matching the repository and ``search`` filters, unsorted (lock held)."""
        return [copy.deepcopy(issue) for rid, issues in self._issues.items() if repository_id is None or rid == repository_id
                for issue in issues.values() if search.lower() in issue["title"].lower() or search in str(issue["number"])]

    @staticmethod
    def _issue_order(rows):
        return sorted(rows, key=lambda row: (row["updated_at"] or "", row["number"]), reverse=True)

    def repositories(self, *, page=1, per_page=50, search="", start=True):
        self._ready(start)
        with self._lock:
            view = self.snapshot(start=False, sync=False)
            rows = self._repository_rows(search)
        view["repositories"], view["pagination"] = _page(rows, page, per_page)
        return view

    def issues(self, *, repository_id=None, tab="all", page=1, per_page=50, search="", start=True):
        if tab not in TABS:
            raise ValueError("unknown issue tab")
        self._ready(start)
        with self._lock:
            view = self.snapshot(start=False, sync=False)
            rows = self._issue_rows(repository_id, search)
        view["counts"] = {name: sum(_tab(issue, name) for issue in rows) for name in sorted(TABS)}
        rows = self._issue_order(issue for issue in rows if _tab(issue, tab))
        view["issues"], view["pagination"] = _page(rows, page, per_page)
        return view

    def revision(self):
        """The catalog revision the lists are at, or ``None`` when it cannot be trusted
        (no store, or writes failing so memory is ahead of the store)."""
        with self._lock:
            return None if self._store is None or self._store_dirty else self._revision

    def list_tag(self, *, start=True):
        """What a list ETag is made of, taken *before* the body is built.

        ``{"revision", "fingerprint"}``: the revision plus the whole sync state
        (status, stale, progress, scope, deferral...) and the in-memory change
        counter, so a 304 never hides a state change. ``None`` when there is no
        trustworthy revision; the caller then hashes the body instead.
        """
        self._ready(start)
        with self._lock:
            view = self.snapshot(start=False, sync=False)
            revision = None if self._store is None or self._store_dirty else self._revision
            epoch = self._epoch
            # the view lists DEFERRED_SHOWN of them; a change past that still changes the tag
            deferred = hashlib.sha256(json.dumps(sorted(self._deferred.items())).encode()).hexdigest()
        if revision is None:
            return None
        return {"revision": revision,
                "fingerprint": json.dumps([view, epoch, self._generation, deferred], sort_keys=True, default=str)}

    def changes(self, *, kind, since, repository_id=None, tab="all", search="", project_id=None, start=True):
        """``since`` delta of one list: ``{revision, resync: false, upserted, removed}`` plus the sync state.

        Keys come from the store's revisioned rows and tombstones; the rows are
        the in-memory ones the full list serves. A changed row that no longer
        matches the filters (repository, search, tab) is reported as removed.
        A ``since`` the store can no longer answer (older than its pruned
        tombstones, newer than its revision) or an untrusted revision answers
        ``resync: true`` with the first ``RESYNC_PAGE`` rows only (``pagination`` says how many
        more; the client reloads the rest through the paginated list), never the whole catalog.
        """
        if kind not in {"repositories", "issues", "projects", "project_items"} or tab not in TABS:
            raise ValueError("unknown GitHub list")
        projects = kind in {"projects", "project_items"}
        if kind == "project_items":
            self.project_items(project_id, start=start)  # not_found before any store read
        self._ready(start)
        with self._lock:
            view = self.snapshot(start=False, sync=False)
            current_revision = self._revision
            # no trusted revision (writes failing, or a purge under way): every since resyncs;
            # project rows the store missed (_projects_unsaved) resync their lists too
            untrusted = self._store_dirty or current_revision is None or (projects and self._projects_unsaved)
            store = None if untrusted else self._store
        delta = None
        if store is not None and since == current_revision:
            # the common poll: nothing written since the client's revision, so no store read
            delta = {"revision": since, "resync": False, "upserted": [], "removed": []}
        elif store is not None:
            try:
                delta = store.changes_since(since, full=False)
            except Exception:
                delta = None
        field = {"projects": "projects", "project_items": "items"}.get(kind, kind)
        if projects:
            # the rows are read after the store delta, like the issue rows below: a row is never
            # older than the revision, so a change committed meanwhile is never reported removed
            listing = self.projects(start=start) if kind == "projects" else self.project_items(project_id, start=start)
        with self._lock:
            if kind == "projects":
                rows = listing["projects"]
                current = {(row["project_id"], row["project_id"]): row for row in rows}
            elif kind == "project_items":
                view["project"] = listing["project"]
                rows = listing["items"]
                current = {(project_id, row["item_id"]): row for row in rows}
            elif kind == "repositories":
                rows = self._repository_rows(search)
                current = {(row["repository_id"], row["repository_id"]): row for row in rows}
            else:
                every = self._issue_rows(repository_id, search)
                view["counts"] = {name: sum(_tab(issue, name) for issue in every) for name in sorted(TABS)}
                rows = self._issue_order(issue for issue in every if _tab(issue, tab))
                current = {(row["repository_id"], row["github_id"]): row for row in rows}
        if delta is None or delta["resync"]:
            page, pagination = _page(rows, 1, RESYNC_PAGE)
            view.update({"revision": delta["revision"] if delta else None, "resync": True, field: page,
                         "pagination": pagination})
            return view
        wanted = {"repositories": "repository", "issues": "issue", "projects": "project",
                  "project_items": "project_item"}[kind]
        scope = project_id if kind == "project_items" else repository_id
        touched = dict.fromkeys((item["repository_id"], item["key"]) for item in delta["upserted"] + delta["removed"]
                                if item["kind"] == wanted and (scope is None or item["repository_id"] == scope))
        position = {key: index for index, key in enumerate(current)}
        upserted = sorted((key for key in touched if key in current), key=position.get)
        owner = "project_id" if projects else "repository_id"
        view.update(revision=delta["revision"], resync=False, upserted=[current[key] for key in upserted],
                    removed=[{owner: rid, "key": key} for rid, key in touched if (rid, key) not in current])
        return view

    def issue_detail(self, repository_id, number):
        """One issue with its body, fetched once per ``updatedAt`` the catalog has seen.

        The stored detail (memory, then the store's detail table) is served with
        zero calls while its ``updated_at`` is not older than the catalog row's;
        once an incremental pass records a newer ``updatedAt`` the next request
        refetches. An issue missing from the open catalog (closed, or not synced
        yet) has nothing to compare against and falls back to a 60 s reuse.
        """
        provider = self._sync_credentials()
        with self._lock:
            if provider.credential_identity != self._credential:
                raise GitHubError("not_configured")
            generation = self._generation
            if repository_id not in self._repos:
                raise GitHubError("not_found")
            listed = next((issue for issue in self._issues.get(repository_id, {}).values()
                           if issue["number"] == number), None)
            cached = copy.deepcopy(self._details.get((repository_id, number)))
            store = self._store
        if cached is None and store is not None:
            try:
                found = store.stored_detail(repository_id, number)
            except Exception:
                found = None  # no detail table yet, or the database is down: fetch
            if found is not None:
                # as old as when it was fetched, not as fresh as this pass or this request
                stored, fetched_at = found
                cached = {"issue": stored, "stale": False, "error": None,
                          "checked_at": datetime.fromtimestamp(fetched_at, UTC).isoformat(),
                          "connection_generation": generation}
                with self._lock:
                    if generation == self._generation:
                        self._remember_detail(repository_id, number, cached)
        if cached and self._detail_fresh(cached, listed):
            return cached
        if self._now() < self._retry_at:
            if cached:
                return dict(cached, stale=True, error="GitHub requests are temporarily paused.")
            raise GitHubError("rate_limited", retry_at=self._retry_at)
        with self._lock:
            over = self._detail_spent() >= self._budget
            retry_at = self._detail_window[0] + self._interval
        if over:  # this interval's point budget is spent: no new body until the window rolls over
            if cached:
                return dict(cached, stale=True, error="The GitHub point budget for this interval is spent.")
            raise GitHubError("rate_limited", retry_at=retry_at)
        try:
            fetched_at = self._now()
            issue = _issue(provider.issue(repository_id, number), repository_id)
            with self._lock:
                cost = self._cost(provider)
                self._detail_window[1] += cost
                self._spent += cost
            result = {"issue": issue, "stale": False, "error": None,
                      "checked_at": datetime.fromtimestamp(fetched_at, UTC).isoformat(),
                      "connection_generation": generation}
            with self._lock:
                if generation != self._generation:
                    raise GitHubError("not_configured")
                self._auth_failures = 0
                self._remember_detail(repository_id, number, result)
            if store is not None:
                try:
                    store.put_detail(repository_id, issue, fetched_at=fetched_at)
                except Exception:
                    pass  # memory still serves it; the catalog itself is unaffected
            return result
        except GitHubError as exc:
            if exc.reason == "authentication":
                self._revoke_catalog(generation)
                raise  # never a cached private body for a revoked token
            with self._lock:
                if generation != self._generation:
                    raise GitHubError("not_configured") from None
                if exc.retry_at:
                    self._retry_at = max(self._retry_at, exc.retry_at)
            if cached:
                return dict(cached, stale=True, error=str(exc))
            raise

    def _detail_fresh(self, cached, listed):
        if listed is None:
            age = self._now() - datetime.fromisoformat(cached["checked_at"]).timestamp()
            return 0 <= age < 60  # a fetch "from the future" (clock change) is not trusted
        have, want = _when(cached["issue"].get("updated_at")), _when(listed.get("updated_at"))
        return have is not None and want is not None and have >= want

    def _remember_detail(self, repository_id, number, result):
        """Keep at most 200 opened details in memory (lock held); the store keeps the rest."""
        self._details.pop((repository_id, number), None)
        if len(self._details) >= 200:
            self._details.pop(next(iter(self._details)))
        self._details[(repository_id, number)] = copy.deepcopy(result)

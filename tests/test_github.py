"""GitHub catalog pagination, safe credentials and observable partial coverage."""
import copy
import io
import json
import threading
import urllib.error
from email.message import Message

import pytest

from herdr_engineering.github import GitHubError, GitHubMonitor, GitHubProvider, autospec_state


def connection(nodes, total=None, cursor=None):
    return {"nodes": nodes, "totalCount": len(nodes) if total is None else total,
            "pageInfo": {"hasNextPage": cursor is not None, "endCursor": cursor}}


def repository(name, count):
    return {"id": name, "nameWithOwner": f"owner/{name}", "url": f"https://github.com/owner/{name}",
            "issues": {"totalCount": count}, "isPrivate": True, "isArchived": False,
            "hasIssuesEnabled": True, "viewerPermission": "ADMIN"}


def issue(number, labels=(), **extra):
    return {"id": f"issue-{number}", "number": number, "title": f"Issue {number}",
            "url": f"https://github.com/owner/big/issues/{number}", "state": "OPEN",
            "updatedAt": "2026-10-20T12:00:00Z", "labels": {"nodes": [{"name": label} for label in labels], "pageInfo": {"hasNextPage": False}},
            "assignees": {"nodes": []}, **extra}


class Response(io.BytesIO):
    pass


class Client:
    def __init__(self, payload):
        self.payload = payload
        self.requests = []

    def open(self, request, **kwargs):
        self.requests.append(request)
        if isinstance(self.payload, Exception):
            raise self.payload
        return Response(json.dumps(self.payload).encode())


class Provider:
    available = True
    credential_identity = "private-fingerprint-not-in-response"
    rate_limit = {"remaining": 4000, "cost": 1, "resetAt": "2026-10-20T15:00:00Z"}

    def __init__(self):
        self.calls = []
        self.error = None
        self.detail_calls = 0

    def repositories_page(self, cursor=None):
        self.calls.append(("repos", cursor))
        if self.error:
            raise self.error
        if cursor is None:
            return "fixture-user", connection([repository("small", 0)], 2, "next")
        return "fixture-user", connection([repository("big", 2)], 2)

    def issues_page(self, repository_id, cursor=None):
        self.calls.append((repository_id, cursor))
        if cursor is None:
            return connection([issue(1, ["auto-implement", "ctx:64k"])], 2, "issues-next")
        return connection([issue(2, ["needs-classify"])], 2)

    def issue(self, repository_id, number):
        self.detail_calls += 1
        return issue(number, ["auto-implement"], body="Details password=private-secret")


@pytest.mark.parametrize("payload", [None, [], "bad", {"data": None}, {"errors": {}}, {"errors": [None]}, {"errors": "placeholder-token-value"}, {"data": {}, "errors": {"message": "placeholder-token-value"}}])
def test_invalid_graphql_payloads_are_safe_errors(payload):
    provider = GitHubProvider(token="placeholder-token-value", client=Client(payload))
    with pytest.raises(GitHubError) as error:
        provider.repositories_page()
    assert error.value.reason == "invalid_response"
    assert "placeholder-token-value" not in str(error.value)


def test_graphql_partial_errors_are_not_successful_empty_inventory():
    payload = {"data": {"viewer": {"login": "fixture", "repositories": connection([])}},
               "errors": [{"type": "FORBIDDEN", "message": "placeholder-token-value"}]}
    with pytest.raises(GitHubError, match="denied"):
        GitHubProvider(token="placeholder-token-value", client=Client(payload)).repositories_page()


def test_token_comes_from_host_config_and_is_never_serialized(fake_host_config):
    assert not GitHubProvider().available
    fake_host_config({"github": {"token": "placeholder-token-value"}})
    provider = GitHubProvider()
    assert provider.available
    monitor = GitHubMonitor(provider_factory=lambda: Provider())
    monitor.refresh()
    assert "fingerprint" not in json.dumps(monitor.repositories(start=False))
    assert "placeholder-token-value" not in json.dumps(monitor.repositories(start=False))


def test_graphql_uses_exact_issue_totals_and_cursor_variables():
    client = Client({"data": {"viewer": {"login": "fixture", "repositories": connection([repository("app", 4)])}}})
    provider = GitHubProvider(token="private", client=client)
    account, page = provider.repositories_page("cursor-value")
    request = client.requests[0]
    body = json.loads(request.data)
    assert body["variables"]["cursor"] == "cursor-value"
    assert "issues(states:OPEN) {totalCount}" in body["query"]
    assert "pullRequests" not in body["query"]
    assert request.full_url == "https://api.github.com/graphql"
    assert account == "fixture" and page["nodes"][0]["issues"]["totalCount"] == 4


def test_pagination_progress_global_order_tabs_and_lazy_detail():
    provider = Provider()
    monitor = GitHubMonitor(provider_factory=lambda: provider)
    monitor.refresh()
    view = monitor.repositories(per_page=1, start=False)
    assert view["status"] == "ready"
    assert view["repositories"][0]["repository_id"] == "github.com/owner/big"
    assert view["pagination"] == {"page": 1, "per_page": 1, "total": 2, "has_next": True}
    assert view["progress"]["issues_seen"] == 2
    assert view["progress"]["repositories_seen"] == 2
    assert view["progress"]["issue_repositories_complete"] == 2
    issues = monitor.issues(tab="preparation", start=False)
    assert [row["number"] for row in issues["issues"]] == [2]
    assert issues["counts"]["all"] == 2
    assert all("body" not in row for row in monitor.issues(start=False)["issues"])
    detail = monitor.issue_detail("github.com/owner/big", 1)
    assert "private-secret" not in detail["issue"]["body"]
    assert monitor.issue_detail("github.com/owner/big", 1) == detail
    assert provider.detail_calls == 1


def test_last_good_inventory_survives_failure_without_becoming_zero():
    provider = Provider()
    monitor = GitHubMonitor(provider_factory=lambda: provider)
    monitor.refresh()
    before = monitor.repositories(start=False)
    provider.error = GitHubError("unavailable")
    monitor.refresh()
    after = monitor.repositories(start=False)
    assert after["stale"] and after["partial"]
    assert after["repositories"] == before["repositories"]
    assert after["last_success_at"] == before["last_success_at"]


def test_rate_limited_issue_scan_resumes_cursor_after_backoff():
    provider = Provider()
    clock = [100.0]
    original = provider.issues_page
    fail = [True]
    def fetch(rid, cursor=None):
        if cursor == "issues-next" and fail[0]:
            fail[0] = False
            raise GitHubError("rate_limited", retry_at=150)
        return original(rid, cursor)
    provider.issues_page = fetch
    monitor = GitHubMonitor(provider_factory=lambda: provider, now=lambda: clock[0])
    monitor.refresh()
    assert monitor.snapshot(start=False)["status"] == "rate_limited"
    assert monitor.issues(start=False)["pagination"]["total"] == 1
    calls = len(provider.calls)
    monitor.refresh()
    assert len(provider.calls) == calls
    clock[0] = 151
    monitor.refresh()
    assert monitor.snapshot(start=False)["status"] == "ready"
    assert monitor.issues(start=False)["pagination"]["total"] == 2
    assert provider.calls.count(("github.com/owner/big", None)) == 1


@pytest.mark.parametrize("labels,state,managed", [
    (["auto-implement"], "candidate", True),
    (["needs-classify"], "preparation", True),
    (["needs-quality-bar"], "preparation", True),
    (["needs-autospec-template"], "preparation", True),
    (["auto-implement", "in-progress-by-bot"], "in_progress", True),
    (["auto-implement", "autospec:needs-human"], "blocked", True),
    (["auto-implement", "security:quarantined"], "blocked", True),
    (["auto-implement", "type:tracker"], "unmanaged", False),
    (["auto-implement", "no-auto"], "unmanaged", False),
])
def test_autospec_labels_never_claim_execution_readiness(labels, state, managed):
    value = autospec_state(labels)
    assert value["state"] == state and value["managed"] == managed
    assert value["eligibility"] in {"unknown", "blocked"}
    assert autospec_state(["auto-implement", "safety:reviewed"])["eligibility"] == "unknown"
    assert "labels_incomplete" in autospec_state(labels, complete=False)["reasons"]


def test_http_rate_limit_and_redirect_errors_never_include_headers_or_tokens():
    headers = Message()
    headers["Retry-After"] = "60"
    error = urllib.error.HTTPError("https://api.github.com/graphql", 429, "placeholder-token-value", headers, io.BytesIO(b"placeholder-token-value"))
    with pytest.raises(GitHubError) as caught:
        GitHubProvider(token="placeholder-token-value", client=Client(error)).repositories_page()
    assert caught.value.reason == "rate_limited" and caught.value.retry_at
    assert "placeholder-token-value" not in str(caught.value)
    redirect = urllib.error.HTTPError("https://evil.example", 302, "redirect", Message(), io.BytesIO())
    with pytest.raises(GitHubError) as caught:
        GitHubProvider(token="placeholder-token-value", client=Client(redirect)).repositories_page()
    assert caught.value.reason == "redirect"


def test_refresh_is_singleflight_and_exposes_progress_before_completion():
    provider = Provider()
    entered, release = threading.Event(), threading.Event()
    original = provider.issues_page
    def blocking(rid, cursor=None):
        entered.set()
        release.wait(timeout=3)
        return original(rid, cursor)
    provider.issues_page = blocking
    monitor = GitHubMonitor(provider_factory=lambda: provider)
    thread = threading.Thread(target=monitor.refresh)
    thread.start()
    try:
        assert entered.wait(timeout=2)
        snapshot = monitor.repositories(start=False)
        assert snapshot["refreshing"] and snapshot["progress"]["phase"] == "issues"
        assert snapshot["pagination"]["total"] == 2
        calls = copy.deepcopy(provider.calls)
        monitor.refresh()
        assert provider.calls == calls
    finally:
        release.set()
        thread.join(timeout=3)
    assert monitor.snapshot(start=False)["status"] == "ready"


def test_rotation_clears_private_cache_even_during_old_token_backoff():
    old, new = Provider(), Provider()
    new.credential_identity = "another-account"
    current = [old]
    monitor = GitHubMonitor(provider_factory=lambda: current[0], now=lambda: 10)
    monitor.refresh()
    assert monitor.repositories(start=False)["pagination"]["total"] == 2
    initial_generation = monitor.snapshot(start=False)["connection_generation"]
    monitor._retry_at = 10000
    current[0] = new
    assert monitor.repositories(start=False)["pagination"]["total"] == 0
    assert monitor.issues(start=False)["pagination"]["total"] == 0
    assert monitor._retry_at == 0
    assert monitor.snapshot(start=False)["connection_generation"] > initial_generation
    monitor.refresh()
    assert monitor.repositories(start=False)["pagination"]["total"] == 2
    new.available = False
    new.credential_identity = "no-credential"
    assert monitor.repositories(start=False)["pagination"]["total"] == 0


def test_inflight_old_credential_cannot_repopulate_after_rotation():
    old, new = Provider(), Provider()
    new.credential_identity = "another-account"
    current = [old]
    entered, release = threading.Event(), threading.Event()
    original = old.repositories_page
    def blocked(cursor=None):
        entered.set()
        release.wait(timeout=3)
        return original(cursor)
    old.repositories_page = blocked
    monitor = GitHubMonitor(provider_factory=lambda: current[0])
    thread = threading.Thread(target=monitor.refresh)
    thread.start()
    try:
        assert entered.wait(timeout=2)
        current[0] = new
        assert monitor.repositories(start=False)["pagination"]["total"] == 0
    finally:
        release.set()
        thread.join(timeout=3)
    assert monitor.repositories(start=False)["pagination"]["total"] == 0
    assert monitor.snapshot(start=False)["account"] is None


def test_first_background_start_does_not_immediately_repeat_full_scan():
    import time
    provider = Provider()
    monitor = GitHubMonitor(provider_factory=lambda: provider, interval=300)
    try:
        monitor.start()
        deadline = time.monotonic() + 2
        while monitor.snapshot(start=False)["status"] != "ready" and time.monotonic() < deadline:
            time.sleep(0.01)
        assert monitor.snapshot(start=False)["status"] == "ready"
        time.sleep(0.05)
        assert provider.calls.count(("repos", None)) == 1
    finally:
        monitor.close()


def test_issue_limit_counts_partial_and_retained_repository_caches():
    provider = Provider()
    provider.repositories_page = lambda cursor=None: ("fixture", connection([repository("a", 3), repository("b", 3)]))
    def incomplete(rid, cursor=None):
        if cursor:
            raise GitHubError("unavailable")
        offset = 0 if rid.endswith("/a") else 10
        return connection([issue(offset + 1), issue(offset + 2)], total=3, cursor="next")
    provider.issues_page = incomplete
    monitor = GitHubMonitor(provider_factory=lambda: provider, issue_limit=3)
    monitor.refresh()
    view = monitor.issues(start=False)
    assert view["status"] == "capacity"
    assert view["partial"]
    assert view["pagination"]["total"] == 2
    assert sum(len(rows) for rows in monitor._issues.values()) <= 3


def test_repository_limit_also_bounds_retained_partial_discovery():
    provider = Provider()
    monitor = GitHubMonitor(provider_factory=lambda: provider, repo_limit=2)
    monitor.refresh()
    assert monitor.repositories(start=False)["pagination"]["total"] == 2
    provider.repositories_page = lambda cursor=None: ("fixture", connection([repository("new", 0)], 2, "next"))
    monitor.refresh()
    view = monitor.repositories(start=False)
    assert view["status"] == "capacity" and view["partial"]
    assert view["pagination"]["total"] <= 2


def test_primary_languages_use_bulk_query_and_cached_reads_without_network():
    rows = [dict(repository('rust', 0), primaryLanguage={'name': 'Rust'}),
            dict(repository('go', 0), primaryLanguage={'name': 'Go'}), repository('unknown', 0)]
    client = Client({'data': {'viewer': {'login': 'fixture', 'repositories': connection(rows)}}})
    provider = GitHubProvider(token='private-token-value', client=client)
    monitor = GitHubMonitor(provider_factory=lambda: provider)
    monitor.refresh()
    assert monitor.repository_languages(start=False) == {
        'github.com/owner/rust': 'Rust', 'github.com/owner/go': 'Go', 'github.com/owner/unknown': None}
    assert len(client.requests) == 1
    assert 'primaryLanguage {name}' in json.loads(client.requests[0].data)['query']
    assert 'private-token-value' not in json.dumps(monitor.repository_languages(start=False))


@pytest.mark.parametrize('language', [None, [], 'Rust', {}, {'name': 3}, {'name': 'x' * 101}, {'name': 'Rust\nGo'}])
def test_malformed_primary_language_does_not_break_repository_catalog(language):
    row = dict(repository('app', 0), primaryLanguage=language)
    client = Client({'data': {'viewer': {'login': 'fixture', 'repositories': connection([row])}}})
    monitor = GitHubMonitor(provider_factory=lambda: GitHubProvider(token='private', client=client))
    monitor.refresh()
    assert monitor.repository_languages(start=False) == {'github.com/owner/app': None}
    assert monitor.snapshot(start=False)['status'] == 'ready'


def test_language_cache_retains_stale_metadata_but_clears_on_credential_rotation():
    row = dict(repository('app', 0), primaryLanguage={'name': 'Rust'})
    client = Client({'data': {'viewer': {'login': 'fixture', 'repositories': connection([row])}}})
    token = ['first-private-token']
    monitor = GitHubMonitor(provider_factory=lambda: GitHubProvider(token=token[0], client=client))
    monitor.refresh()
    client.payload = OSError('offline')
    monitor.refresh()
    assert monitor.snapshot(start=False)['stale']
    assert monitor.repository_languages(start=False) == {'github.com/owner/app': 'Rust'}
    token[0] = 'second-private-token'
    assert monitor.repository_languages(start=False) == {}
    assert len(client.requests) == 2


def test_language_cache_starts_existing_background_refresh_without_blocking():
    entered, release = threading.Event(), threading.Event()
    provider = Provider()
    original = provider.repositories_page

    def blocked(cursor=None):
        entered.set()
        assert release.wait(2)
        return original(cursor)

    provider.repositories_page = blocked
    monitor = GitHubMonitor(provider_factory=lambda: provider)
    try:
        assert monitor.repository_languages() == {}
        assert entered.wait(1)
    finally:
        release.set()
        monitor.close()


def test_partial_repository_discovery_publishes_primary_language_before_issues_finish():
    provider = Provider()

    def pages(cursor=None):
        if cursor:
            raise GitHubError('unavailable')
        return 'fixture', connection([dict(repository('rust', 0), primaryLanguage={'name': 'Rust'})], 2, 'next')

    provider.repositories_page = pages
    monitor = GitHubMonitor(provider_factory=lambda: provider)
    monitor.refresh()
    assert monitor.snapshot(start=False)['partial']
    assert monitor.repository_languages(start=False) == {'github.com/owner/rust': 'Rust'}


def test_primary_language_cannot_echo_configured_github_token():
    row = dict(repository('app', 0), primaryLanguage={'name': 'private-token-value'})
    client = Client({'data': {'viewer': {'login': 'fixture', 'repositories': connection([row])}}})
    monitor = GitHubMonitor(provider_factory=lambda: GitHubProvider(token='private-token-value', client=client))
    monitor.refresh()
    assert monitor.repository_languages(start=False) == {'github.com/owner/app': None}


# ---- G9 Projects v2: provider reads (response shape as documented; not validated live) ----

def test_projects_page_and_items_parse_status_and_mixed_content():
    from herdr_engineering.github import _project, _project_item
    listing = {"data": {"repositoryOwner": {"login": "acme", "projectsV2": {
        "totalCount": 1, "pageInfo": {"hasNextPage": False, "endCursor": None},
        "nodes": [{"id": "PVT_1", "number": 4, "title": "Roadmap", "url": "https://github.com/orgs/acme/projects/4",
                   "closed": False, "updatedAt": "2026-10-20T12:00:00Z", "items": {"totalCount": 4},
                   "field": {"id": "F", "options": [{"id": "o1", "name": "Todo"}, {"id": "o2", "name": "Done"}]}}]}},
        "rateLimit": {"remaining": 4000, "resetAt": "2026-10-20T15:00:00Z", "cost": 1}}}
    client = Client(listing)
    provider = GitHubProvider(token="placeholder-token-value", client=client)
    connection = provider.projects_page("acme")
    body = json.loads(client.requests[0].data)
    assert body["variables"] == {"login": "acme", "cursor": None}
    assert "orderBy:{field:UPDATED_AT,direction:DESC}" in body["query"]
    row = _project(connection["nodes"][0], "Acme")
    assert row["owner"] == "acme" and row["item_count"] == 4
    assert [o["name"] for o in row["status_options"]] == ["Todo", "Done"]
    assert _project(dict(connection["nodes"][0], field=None), "acme")["status_options"] is None
    items = {"data": {"node": {"items": {"totalCount": 4, "pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": [
        {"id": "I1", "type": "ISSUE", "isArchived": False, "updatedAt": "2026-10-20T12:00:00Z",
         "status": {"name": "Todo", "optionId": "o1"},
         "content": {"title": "Fix it", "number": 7, "url": "https://github.com/acme/app/issues/7", "issueState": "OPEN",
                     "repository": {"nameWithOwner": "acme/app"}}},
        {"id": "I5", "type": "PULL_REQUEST", "isArchived": False, "updatedAt": "2026-10-20T12:00:00Z", "status": None,
         "content": {"title": "Fix PR", "number": 8, "url": "https://github.com/acme/app/pull/8", "prState": "MERGED",
                     "repository": {"nameWithOwner": "acme/app"}}},
        {"id": "I2", "type": "DRAFT_ISSUE", "isArchived": False, "updatedAt": "2026-10-20T12:00:00Z", "status": None,
         "content": {"title": "An idea"}},
        {"id": "I3", "type": "REDACTED", "isArchived": False, "updatedAt": "2026-10-20T12:00:00Z", "status": {}, "content": None},
        {"id": "I4", "type": "ISSUE", "isArchived": True, "updatedAt": "2026-10-20T12:00:00Z", "status": None, "content": None}]}},
        "rateLimit": {"remaining": 3999, "resetAt": "2026-10-20T15:00:00Z", "cost": 1}}}
    nodes = GitHubProvider(token="placeholder-token-value", client=Client(items)).project_items_page("PVT_1")["nodes"]
    parsed = [_project_item(node, "PVT_1") for node in nodes]
    assert parsed[0]["status_option"] == "o1" and parsed[0]["repository"] == "acme/app" and parsed[0]["number"] == 7
    assert parsed[0]["state"] == "OPEN" and parsed[1]["state"] == "MERGED" and parsed[1]["type"] == "PULL_REQUEST"
    assert parsed[2]["status"] is None and parsed[2]["title"] == "An idea"
    assert parsed[3]["title"] is None and parsed[3]["type"] == "REDACTED"
    assert parsed[4] is None, "archived items are not shown"


def test_project_item_query_aliases_fields_whose_types_differ_across_content_types():
    """Issue.state (IssueState) and PullRequest.state (PullRequestState) under one response key fail
    GraphQL's SameResponseShape validation, so every item page would be rejected."""
    import re

    from herdr_engineering.github import PROJECT_ITEMS_QUERY
    issue = re.search(r"\.\.\. on Issue \{([^}]*)", PROJECT_ITEMS_QUERY).group(1)
    pull = re.search(r"\.\.\. on PullRequest \{([^}]*)", PROJECT_ITEMS_QUERY).group(1)
    assert "issueState: state" in issue and "prState: state" in pull
    unaliased = re.sub(r"\w+:\s*state\b", "", issue + pull)
    assert not re.search(r"\bstate\b", unaliased), "state must never appear under its own key"


def test_a_missing_read_project_scope_is_named():
    payload = {"data": None, "errors": [{"type": "INSUFFICIENT_SCOPES", "message": "needs read:project"}]}
    with pytest.raises(GitHubError) as caught:
        GitHubProvider(token="placeholder-token-value", client=Client(payload)).projects_page("acme")
    assert caught.value.reason == "insufficient_scope" and "read:project" in str(caught.value)

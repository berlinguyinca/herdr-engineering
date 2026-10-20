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


@pytest.mark.parametrize("payload", [None, [], "bad", {"data": None}, {"errors": {}}, {"errors": [None]}, {"errors": "secret-token-value"}, {"data": {}, "errors": {"message": "secret-token-value"}}])
def test_invalid_graphql_payloads_are_safe_errors(payload):
    provider = GitHubProvider(token="secret-token-value", client=Client(payload))
    with pytest.raises(GitHubError) as error:
        provider.repositories_page()
    assert error.value.reason == "invalid_response"
    assert "secret-token-value" not in str(error.value)


def test_graphql_partial_errors_are_not_successful_empty_inventory():
    payload = {"data": {"viewer": {"login": "fixture", "repositories": connection([])}},
               "errors": [{"type": "FORBIDDEN", "message": "secret-token-value"}]}
    with pytest.raises(GitHubError, match="denied"):
        GitHubProvider(token="secret-token-value", client=Client(payload)).repositories_page()


def test_private_token_file_is_required_and_never_serialized(tmp_path, monkeypatch):
    path = tmp_path / "token"
    path.write_text("secret-token-value")
    monkeypatch.setenv("HERDR_ENGINEERING_GITHUB_TOKEN_FILE", str(path))
    path.chmod(0o644)
    assert not GitHubProvider().available
    path.chmod(0o600)
    provider = GitHubProvider()
    assert provider.available
    monitor = GitHubMonitor(provider_factory=lambda: Provider())
    monitor.refresh()
    assert "fingerprint" not in json.dumps(monitor.repositories(start=False))
    assert "secret-token-value" not in json.dumps(monitor.repositories(start=False))


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
    error = urllib.error.HTTPError("https://api.github.com/graphql", 429, "secret-token-value", headers, io.BytesIO(b"secret-token-value"))
    with pytest.raises(GitHubError) as caught:
        GitHubProvider(token="secret-token-value", client=Client(error)).repositories_page()
    assert caught.value.reason == "rate_limited" and caught.value.retry_at
    assert "secret-token-value" not in str(caught.value)
    redirect = urllib.error.HTTPError("https://evil.example", 302, "redirect", Message(), io.BytesIO())
    with pytest.raises(GitHubError) as caught:
        GitHubProvider(token="secret-token-value", client=Client(redirect)).repositories_page()
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

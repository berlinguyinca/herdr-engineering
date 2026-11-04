"""Read-only GitHub routes validate and bound queries before using the monitor."""
import time
import json
import threading
import urllib.error
import urllib.request

import pytest

from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.repo import ControlPlaneRepo


class Monitor:
    def __init__(self):
        self.calls = []
        self.closed = False

    def repositories(self, **kwargs):
        self.calls.append(("repos", kwargs))
        return {"available": True, "repos": [], "total": 0}

    def issues(self, *, repository_id, **kwargs):
        self.calls.append(("issues", repository_id, kwargs))
        return {"available": True, "issues": [], "total": 0}

    def issue_detail(self, repository_id, number):
        self.calls.append(("detail", repository_id, number))
        return {"available": True, "issue": {"number": number}}

    def close(self):
        self.closed = True


@pytest.fixture
def server():
    monitor = Monitor()
    server = ControlPlaneServer(ControlPlaneRepo(),
                                api_keys={"read": "viewer", "write": "operator"},
                                github_monitor=monitor)
    thread = threading.Thread(target=server.serve, daemon=True)
    thread.start()
    server.wait_until_ready()
    yield server
    server.shutdown()
    thread.join(timeout=3)
    assert monitor.closed


def request(server, path, *, key="read", method="GET"):
    req = urllib.request.Request(f"http://127.0.0.1:{server.port}/api/v1/{path}",
                                 headers={"X-API-Key": key}, method=method)
    try:
        response = urllib.request.urlopen(req, timeout=3)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        return response.status, json.load(response)


def test_lists_use_injected_monitor_and_bounded_defaults(server):
    assert request(server, "github")[0] == 200
    assert server.github_monitor.calls == [("repos", {"page": 1, "per_page": 50, "search": ""})]
    assert request(server, "github/issues")[0] == 200
    assert server.github_monitor.calls[-1] == ("issues", None,
        {"page": 1, "per_page": 50, "search": "", "tab": "all"})
    assert "github_v1" in request(server, "health")[1]["capabilities"]


def test_encoded_filters_and_detail_remain_read_only(server):
    path = "github/issues?repository=github.com%2Fowner%2Frepo&tab=blocked&page=2&per_page=25&search=a%26b"
    assert request(server, path)[0] == 200
    assert server.github_monitor.calls[-1] == ("issues", "github.com/owner/repo",
        {"tab": "blocked", "page": 2, "per_page": 25, "search": "a&b"})
    assert request(server, "github/issue?repository=github.com%2Fowner%2Frepo&number=12")[1]["issue"]["number"] == 12
    assert server.github_monitor.calls[-1] == ("detail", "github.com/owner/repo", 12)


@pytest.mark.parametrize("query", ["page=0", "page=-1", "page=abc", "page=1000001",
    "per_page=0", "per_page=101", "page=1&page=2", "search=" + "x" * 301,
    "search=%00", "page=", "tab=unknown"])
def test_invalid_queries_do_not_reach_monitor(server, query):
    assert request(server, "github/issues?" + query)[0] == 400
    assert not server.github_monitor.calls


@pytest.mark.parametrize("path", ["github/issue", "github/issue?repository=owner/repo",
    "github/issue?repository=owner/repo&number=0", "github/issues?repository=../../etc/passwd",
    "github/issues?repository=https://other.example/owner/repo",
    "github/issue?repository=owner/repo&number=1&number=2"])
def test_invalid_repository_and_issue_identity_is_rejected(server, path):
    assert request(server, path)[0] == 400
    assert not server.github_monitor.calls


def test_auth_unknown_routes_and_posts_do_not_call_monitor(server):
    assert request(server, "github", key="wrong")[0] == 401
    assert request(server, "github/unknown")[0] == 404
    assert request(server, "github/issues/extra")[0] == 404
    assert request(server, "github", method="POST")[0] == 403
    assert request(server, "github", method="POST", key="write")[0] == 404
    assert not server.github_monitor.calls


@pytest.mark.parametrize("exception, status", [(KeyError, 404), (ValueError, 400),
    (RuntimeError, 503), (OSError, 500)])
def test_provider_errors_never_expose_credentials(server, exception, status):
    def fail(**kwargs):
        raise exception("private-token-value https://user:password@upstream.example")
    server.github_monitor.repositories = fail
    actual, body = request(server, "github")
    assert actual == status
    assert "private-token" not in json.dumps(body)
    assert "password" not in json.dumps(body)


def test_repository_names_are_canonicalized(server):
    assert request(server, "github/issues?repository=Owner%2FRepo")[0] == 200
    assert server.github_monitor.calls[-1][1] == "github.com/owner/repo"


@pytest.mark.parametrize("reason,status", [("not_found", 404), ("permission", 403),
    ("rate_limited", 429), ("authentication", 503)])
def test_typed_github_errors_have_actionable_safe_responses(server, reason, status):
    from herdr_engineering.github import GitHubError
    def fail(repository_id, number):
        raise GitHubError(reason)
    server.github_monitor.issue_detail = fail
    actual, body = request(server, "github/issue?repository=owner/repo&number=1")
    assert actual == status
    assert body["reason"] == reason


def test_real_monitor_without_credentials_serves_bounded_empty_pages(server):
    from herdr_engineering.github import GitHubMonitor

    class UnconfiguredProvider:
        credential_identity = "unconfigured-test"
        available = False

    original = server.github_monitor
    real = GitHubMonitor(provider_factory=UnconfiguredProvider)
    server.github_monitor = real
    try:
        assert real._thread is None  # API construction does not start upstream polling.
        status, result = request(server, "github")
        assert status == 200
        assert result["repositories"] == []
        assert result["pagination"]["per_page"] == 50
        status, result = request(server, "github/issues")
        assert status == 200
        assert result["issues"] == []
        assert result["counts"]["all"] == 0
    finally:
        real.close()
        server.github_monitor = original
    assert not real._thread.is_alive()


def test_real_monitor_surfaces_scope_and_invalid_config_in_the_list_bodies(server):
    import importlib.util
    from pathlib import Path

    from herdr_engineering.github import GitHubMonitor
    spec = importlib.util.spec_from_file_location(
        "_github_incremental_fakes", Path(__file__).parents[1] / "test_github_incremental.py")
    fakes = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fakes)
    FakeGitHub = fakes.FakeGitHub
    fake, allow = FakeGitHub(page_size=100), [["acme"]]
    fake.add_issue("acme/app")
    fake.add_issue("other/tool")
    real = GitHubMonitor(provider_factory=lambda: fake, organizations=lambda: allow[0])
    original, server.github_monitor = server.github_monitor, real
    try:
        real.refresh()
        for path in ("github", "github/issues"):
            # the first GET starts the monitor's background loop, whose own pass reports "syncing" for a moment
            deadline = time.monotonic() + 10
            status, body = request(server, path)
            while body.get("status") == "syncing" and time.monotonic() < deadline:
                time.sleep(0.05)
                status, body = request(server, path)
            assert status == 200 and body["status"] == "ready"
            assert body["scope"] == {"owners": ["acme"], "excluded_repositories": 1, "restricted": True}
        allow[0] = "acme"  # not a list
        real._retry_at = 0
        real.refresh()
        status, body = request(server, "github")
        assert status == 200  # the list shape is unchanged; the status says what is wrong
        assert body["status"] == "invalid_config" and "github.organizations" in body["error"]
        assert "repositories" in body and "scope" in body
    finally:
        real.close()
        server.github_monitor = original

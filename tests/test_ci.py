"""Woodpecker v3.18.1 provider contract and bounded transport regressions."""
import io
import json
import os
import sys
import urllib.parse

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import pytest

from herdr_engineering.ci import PipelineView, WoodpeckerProvider
from herdr_engineering.errors import PermissionError_Engineering

REPO = {"id": 17, "full_name": "berlinguyinca/autospec",
        "owner": "berlinguyinca", "name": "autospec",
        "default_branch": "main", "private": False, "active": True,
        "clone_url": "https://git.example/berlinguyinca/autospec",
        "forge_url": "https://forge.example/berlinguyinca/autospec"}


class FakeCI:
    """Route by exact URL path; unknown paths return ``default``."""

    def __init__(self, routes: dict, default: object = None):
        self._routes = routes
        self._default = default
        self.requests = []
        self.methods = []

    def urlopen(self, req, timeout=10):
        self.requests.append(req.full_url)
        self.methods.append(req.get_method())
        path = urllib.parse.urlparse(req.full_url).path
        body = self._routes.get(path, self._default)
        if body is None:
            body = ""
        if isinstance(body, (str, bytes)):
            data = body if isinstance(body, bytes) else body.encode()
            return io.BytesIO(data)
        return io.BytesIO(json.dumps(body).encode())


def _provider(routes, default=None, base="https://ci.example"):
    return WoodpeckerProvider(base_url=base, token="tok",
                              client=FakeCI(routes, default))


def test_offline_safe_stale_view(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))  # no token file exists there
    monkeypatch.delenv("HERDR_ENGINEERING_CI_TOKEN", raising=False)
    p = WoodpeckerProvider(base_url="https://ci.example")
    assert p.available is False
    assert p.pipelines("repo") == []  # never guessed success


def test_repos_parsed():
    p = _provider({"/api/user/repos": [REPO]})
    repos = p.repos()
    assert p._client.requests == ["https://ci.example/api/user/repos"]
    assert repos[0]["repository"] == "berlinguyinca/autospec"
    assert repos[0]["id"] == 17
    assert repos[0]["default_branch"] == "main"


def test_agents_listed_and_token_stripped():
    raw = [{"id": 1527, "name": "whiteale-kvm-node-5", "version": "3.18.1",
            "backend": "k8s", "platform": "linux/amd64", "capacity": 1,
            "last_contact": "2026-09-24T00:00:00Z", "last_work": "w1",
            "token": "SUPERSECRET_AGENT_TOKEN"}]
    p = _provider({"/api/agents": raw})
    agents = p.agents()
    assert p._client.requests == ["https://ci.example/api/agents"]
    assert agents[0]["name"] == "whiteale-kvm-node-5"
    assert agents[0]["version"] == "3.18.1"
    # the agent auth token is a secret: never surfaced in the view
    assert "token" not in agents[0]
    assert "SUPERSECRET_AGENT_TOKEN" not in str(agents)


def test_pipelines_resolves_numeric_repo_id():
    p = _provider({
        "/api/user/repos": [REPO],
        "/api/repos/17/pipelines": [
            {"id": 1104, "number": 12, "commit": "ac154", "branch": "main",
             "status": "failure", "event": "pull_request",
             "created": "t0", "finished": "t1"}],
    })
    views = p.pipelines("berlinguyinca/autospec")
    # owner/name resolved to the numeric id, then the id-based path is used
    assert p._client.requests == [
        "https://ci.example/api/user/repos",
        "https://ci.example/api/repos/17/pipelines"]
    assert len(views) == 1
    assert views[0].state == "failure"
    assert views[0].run_number == 12


def test_pipelines_accepts_numeric_id_directly():
    p = _provider({"/api/repos/17/pipelines": [
        {"id": 1104, "number": 12, "status": "success"}]})
    views = p.pipelines("17")
    # a bare numeric id skips the /api/repos lookup
    assert p._client.requests == ["https://ci.example/api/repos/17/pipelines"]
    assert views[0].state == "success"


def test_unknown_repo_is_unreachable_not_empty():
    from herdr_engineering.errors import UnreachableError
    p = _provider({"/api/user/repos": [REPO]})
    with pytest.raises(UnreachableError):
        p.pipelines("nobody/nope")


def test_pipeline_detail_flattens_workflows_into_tasks():
    detail = {"id": 1104, "number": 12, "status": "failure", "branch": "main",
              "event": "pull_request", "commit": "ac154", "author": "berlinguyinca",
              "message": "m", "title": "t", "created": "t0", "finished": "t1",
              "workflows": [{
                  "id": 795, "name": "woodpecker",
                  "children": [
                      {"id": 2584, "name": "checkout", "state": "success",
                       "exit_code": 0, "type": "commands"},
                      {"id": 2597, "name": "rust-validate", "state": "failure",
                       "exit_code": 101, "type": "commands"}]}]}
    p = _provider({
        "/api/user/repos": [REPO],
        "/api/repos/17/pipelines/12": detail,
    })
    got = p.pipeline("berlinguyinca/autospec", 12)
    assert "https://ci.example/api/repos/17/pipelines/12" in p._client.requests
    assert got["status"] == "failure"
    assert [t["name"] for t in got["tasks"]] == ["checkout", "rust-validate"]
    failing = [t for t in got["tasks"] if t["state"] == "failure"]
    assert failing[0]["name"] == "rust-validate"
    assert failing[0]["exit_code"] == 101


def test_step_log_decodes_stored_base64_entries():
    p = _provider({"/api/user/repos": [REPO],
                   "/api/repos/17/logs/12/2597": [
                       {"data": "bGluZTE=", "type": 0},
                       {"data": "bGluZTI=", "type": 1}]})
    assert p.step_log("berlinguyinca/autospec", 12, "2597") == "line1\nline2"


def test_html_response_is_an_error_not_empty_logs():
    from herdr_engineering.ci import CIProviderError
    p = _provider({"/api/repos/17/logs/12/2597": "<!doctype html>login"})
    with pytest.raises(CIProviderError) as exc:
        p.step_log("17", 12, "2597")
    assert exc.value.reason == "invalid_response"


def test_token_from_machine_local_file(tmp_path, monkeypatch):
    tok_file = tmp_path / ".config" / "herdr-engineering" / "ci-token"
    tok_file.parent.mkdir(parents=True)
    tok_file.write_text("sekrit\n")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("HERDR_ENGINEERING_CI_TOKEN", raising=False)
    p = WoodpeckerProvider(base_url="https://ci.example")
    assert p.token == "sekrit"
    assert p.available is True


def test_retry_requires_authorization_and_uses_id_path():
    repo = {"id": 1, "full_name": "acme/repo", "owner": "acme", "name": "repo"}
    p = _provider({
        "/api/user/repos": [repo],
        "/api/repos/1/pipelines/12": "",
    })
    with pytest.raises(PermissionError_Engineering):
        p.retry("acme/repo", "12", authorized=False)
    assert p.retry("acme/repo", "12", authorized=True) is True
    assert p._client.requests[-1] == "https://ci.example/api/repos/1/pipelines/12"
    assert p._client.methods[-1] == "POST"


def test_debug_with_pi_builds_bounded_handoff_from_failing_tasks():
    detail = {"id": 1104, "number": 12, "status": "failure", "branch": "main",
              "event": "pull_request", "commit": "ac154dc97e847ab30d865",
              "author": "berlinguyinca", "tasks": [
                  {"id": 2584, "name": "checkout", "state": "success", "exit_code": 0,
                   "workflow": "woodpecker"},
                  {"id": 2597, "name": "rust-validate", "state": "failure", "exit_code": 2,
                   "workflow": "woodpecker"}]}
    routes = {"/api/user/repos": [REPO], "/api/repos/17/pipelines/12": detail}
    # bypass provider.pipeline by monkeypatching is unnecessary: build provider
    p = _provider(routes)
    # stub pipeline() to avoid the second /api/repos call complexity
    p.pipeline = lambda repo, num: detail
    res = p.debug_with_pi("berlinguyinca/autospec", 12)
    assert res["ok"] is True
    h = res["handoff"]
    assert h["handoff_type"] == "debug-with-pi"
    assert h["target_owner"] == "pi-engineering"
    assert "rust-validate" in h["failing_step"]
    assert "exit 2" in h["log_summary"]
    assert len(h["log_summary"]) <= 4000
    assert res["view_in_web_ui"].endswith("/berlinguyinca/autospec/pipelines/12")


def test_debug_with_pi_handoff_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))  # hermetic: no token file
    monkeypatch.delenv("HERDR_ENGINEERING_CI_TOKEN", raising=False)
    p = WoodpeckerProvider()
    h = p.debug_with_pi_handoff(
        "repo", PipelineView(pipeline_id="p1", run_number=1,
                             repository="r", revision="abc"),
        "step1", "log" * 5000)
    assert len(h["log_summary"]) == 4000  # bounded
    assert h["target_owner"] == "pi-engineering"


def test_cancel_posts_cancel_route_and_never_deletes_pipeline():
    p = _provider({"/api/repos/17/pipelines/12/cancel": ""})
    with pytest.raises(PermissionError_Engineering):
        p.cancel("17", "12")
    assert p.cancel("17", "12", authorized=True)
    assert p._client.requests[-1].endswith("/api/repos/17/pipelines/12/cancel")
    assert p._client.methods[-1] == "POST"


def test_secret_file_override(tmp_path, monkeypatch):
    path = tmp_path / "secret"
    path.write_text("mounted-secret\n")
    monkeypatch.delenv("HERDR_ENGINEERING_CI_TOKEN", raising=False)
    monkeypatch.setenv("HERDR_ENGINEERING_CI_TOKEN_FILE", str(path))
    assert WoodpeckerProvider().token == "mounted-secret"


@pytest.mark.parametrize("status,reason", [(401,"authentication"),(403,"permission"),
                                           (429,"rate_limited"),(503,"unavailable")])
def test_http_errors_are_classified_without_response_or_token(status, reason):
    import urllib.error

    from herdr_engineering.ci import CIProviderError
    class Broken:
        def urlopen(self, request, timeout):
            raise urllib.error.HTTPError(request.full_url, status, "secret-error", {},
                                          io.BytesIO(b"secret-body"))
    p = WoodpeckerProvider(token="secret-token", client=Broken())
    with pytest.raises(CIProviderError) as exc:
        p.repos()
    assert exc.value.reason == reason
    assert exc.value.status_code == status
    assert "secret" not in str(exc.value.to_dict())


def test_transport_has_bounded_reads():
    from herdr_engineering.ci import CIProviderError
    class Body(io.BytesIO):
        def read(self, size=-1):
            assert 0 < size <= 2 * 1024 * 1024 + 1
            return super().read(size)
    class Large:
        def urlopen(self, request, timeout):
            assert timeout <= 15
            return Body(b"x" * (2 * 1024 * 1024 + 1))
    p = WoodpeckerProvider(token="tok", client=Large())
    with pytest.raises(CIProviderError) as exc:
        p.repos()
    assert exc.value.reason == "response_too_large"


def test_repository_names_must_be_unambiguous():
    p = _provider({"/api/user/repos": [REPO, dict(REPO, id=18, full_name="other/autospec")]})
    from herdr_engineering.errors import InvalidError
    with pytest.raises(InvalidError):
        p.pipelines("autospec")


def test_permissions_control_capabilities():
    p = _provider({"/api/repos/17/permissions": {"pull": True,"push": True}})
    assert p.capabilities("17", "running") == {"can_retry": False, "can_cancel": True}
    assert p.capabilities("17", "failure") == {"can_retry": True, "can_cancel": False}
    assert p.capabilities("17", "unknown") == {"can_retry": False, "can_cancel": False}


def test_timestamp_normalization_and_step_start_time():
    p = _provider({"/api/repos/17/pipelines/12": {
        "number": 12, "created": 1720000000, "started": 1720000001, "finished": 0,
        "workflows": [{"children": [{"id": 42,"state": "running", "started": 1720000001}]}]},
        "/api/repos/17/permissions": {"pull": True}})
    data = p.pipeline("17", 12)
    assert data["created_at"] == "2024-07-03T09:46:40Z"
    assert data["finished_at"] is None
    assert data["tasks"][0]["started_at"] == "2024-07-03T09:46:41Z"


def test_log_and_debug_output_redacts_configured_token():
    import base64
    p = _provider({"/api/repos/17/logs/12/1": [{"data": base64.b64encode(b"Bearer tok").decode()}]})
    assert "tok" not in p.step_log("17", 12, "1")
    assert "tok" not in p.debug_with_pi_handoff("repo", PipelineView("1",1,"repo","a"),"step","Bearer tok")["log_summary"]


def test_explicit_pagination_uses_woodpecker_parameters():
    p = _provider({"/api/user/repos": [REPO], "/api/repos/17/pipelines": []})
    p.repos()
    p.pipelines("17", page=2, per_page=25)
    assert p._client.requests == ["https://ci.example/api/user/repos",
                                  "https://ci.example/api/repos/17/pipelines?page=2&perPage=25"]


def test_redirects_are_rejected_before_authorization_can_leave_origin():
    import urllib.request

    from herdr_engineering.ci import CIProviderError, _NoRedirect
    with pytest.raises(CIProviderError) as exc:
        _NoRedirect().redirect_request(urllib.request.Request("https://ci.example/api/user/repos"),
                                       None, 302, "Found", {}, "https://login.example")
    assert exc.value.reason == "redirect"


def test_invalid_resource_identifiers_cannot_change_request_path():
    from herdr_engineering.errors import InvalidError
    p = _provider({})
    with pytest.raises(InvalidError):
        p.cancel("17", "../12", authorized=True)
    assert not p._client.requests


def test_failed_mutation_is_not_reported_successful():
    import urllib.error

    from herdr_engineering.ci import CIProviderError
    class Denied:
        def urlopen(self, req, timeout):
            raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, None)
    p = WoodpeckerProvider(token="tok", client=Denied())
    with pytest.raises(CIProviderError):
        p.cancel("17", "12", authorized=True)


def test_large_stored_log_is_truncated_and_marked():
    import base64
    text = base64.b64encode(b"x" * 80000).decode()
    p = _provider({"/api/repos/17/logs/12/1": [{"data": text}]})
    log = p.step_log("17", 12, "1")
    assert len(log) < 66000
    assert "log truncated" in log


def test_repository_discovery_preserves_complete_unpaginated_snapshot():
    rows = [dict(REPO, id=i+1, full_name=f"owner/repo{i}", name=f"repo{i}") for i in range(116)]
    p = _provider({"/api/user/repos": rows, "/api/repos/116/pipelines": []})
    assert len(p.repos()) == 116
    assert p.pipelines("owner/repo115") == []
    assert p._client.requests[-1] == "https://ci.example/api/repos/116/pipelines"


def test_discovery_caches_all_unambiguous_repository_ids():
    second = dict(REPO, id=18, full_name="other/repo", owner="other", name="repo")
    p = _provider({"/api/user/repos": [REPO, second],
                   "/api/repos/17/pipelines": [], "/api/repos/18/pipelines": []})
    p.repos()
    p.pipelines("berlinguyinca/autospec")
    p.pipelines("other/repo")
    assert sum(url.endswith("/api/user/repos") for url in p._client.requests) == 1


def test_discovery_does_not_cache_ambiguous_cross_forge_names():
    from herdr_engineering.errors import InvalidError
    p = _provider({"/api/user/repos": [REPO, dict(REPO, id=18)]})
    p.repos()
    with pytest.raises(InvalidError):
        p.pipelines("berlinguyinca/autospec")


def test_stored_log_null_data_entries_are_empty_output():
    p = _provider({"/api/repos/17/logs/12/1": [{"data": None}, {"data": "bGluZQ=="}]})
    assert p.step_log("17", 12, "1") == "\nline"


def test_oversized_json_logs_fall_back_to_bounded_download_preview():
    class BoundedBody(io.BytesIO):
        def __init__(self, value, max_read):
            super().__init__(value)
            self.max_read = max_read

        def read(self, size=-1):
            assert 0 < size <= self.max_read
            return super().read(size)

    class LargeLogs:
        def __init__(self):
            self.paths = []

        def urlopen(self, req, timeout):
            self.paths.append(urllib.parse.urlsplit(req.full_url).path)
            if self.paths[-1].endswith("/download"):
                return BoundedBody(b"readable output\n" + b"x" * (3 * 1024 * 1024), 65550)
            return BoundedBody(b"[" + b" " * (3 * 1024 * 1024), 2 * 1024 * 1024 + 1)

    client = LargeLogs()
    p = WoodpeckerProvider(token="tok", client=client)
    log = p.step_log("17", 12, "1")
    assert log.startswith("readable output\n")
    assert len(log) < 66000
    assert "log truncated" in log
    assert client.paths == ["/api/repos/17/logs/12/1", "/api/repos/17/logs/12/1/download"]


def test_log_auth_failure_never_attempts_download_fallback():
    import urllib.error

    from herdr_engineering.ci import CIProviderError

    class Denied:
        paths = []

        def urlopen(self, req, timeout):
            self.paths.append(req.full_url)
            raise urllib.error.HTTPError(req.full_url, 401, "Denied", {}, None)

    client = Denied()
    p = WoodpeckerProvider(token="tok", client=client)
    with pytest.raises(CIProviderError) as exc:
        p.step_log("17", 12, "1")
    assert exc.value.reason == "authentication"
    assert len(client.paths) == 1


def test_download_preview_redacts_token_across_preview_boundary():
    token = "a-private-credential-that-must-not-escape"
    p = WoodpeckerProvider(token=token, client=FakeCI({
        "/api/repos/17/logs/12/1": b"x" * (2 * 1024 * 1024 + 1),
        "/api/repos/17/logs/12/1/download": "x" * 65530 + token + "rest",
    }))
    log = p.step_log("17", 12, "1")
    assert "a-priv" not in log
    assert "log truncated" in log

"""Woodpecker/Pileated CI tests (spec 0130).

These lock the provider to the *real* Woodpecker 3.18.1 API contract, verified
live on 2026-09-24: the API is addressed by numeric repository id
(``/api/repos``, ``/api/repos/{id}/pipelines``,
``/api/repos/{id}/pipelines/{number}``) and the pipeline detail exposes
``workflows[].children[]`` (tasks). Logs are WebSocket-only, so ``step_log``
must return ``None`` (never guess) when the REST form is unavailable.
"""
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

    def urlopen(self, req, timeout=10):
        self.requests.append(req.full_url)
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
    p = _provider({"/api/repos": [REPO]})
    repos = p.repos()
    assert p._client.requests == ["https://ci.example/api/repos"]
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
        "/api/repos": [REPO],
        "/api/repos/17/pipelines": [
            {"id": 1104, "number": 12, "commit": "ac154", "branch": "main",
             "status": "failure", "event": "pull_request",
             "created": "t0", "finished": "t1"}],
    })
    views = p.pipelines("berlinguyinca/autospec")
    # owner/name resolved to the numeric id, then the id-based path is used
    assert p._client.requests == [
        "https://ci.example/api/repos",
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
    p = _provider({"/api/repos": [REPO]})
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
        "/api/repos": [REPO],
        "/api/repos/17/pipelines/12": detail,
    })
    got = p.pipeline("berlinguyinca/autospec", 12)
    assert p._client.requests[-1] == "https://ci.example/api/repos/17/pipelines/12"
    assert got["status"] == "failure"
    assert [t["name"] for t in got["tasks"]] == ["checkout", "rust-validate"]
    failing = [t for t in got["tasks"] if t["state"] == "failure"]
    assert failing[0]["name"] == "rust-validate"
    assert failing[0]["exit_code"] == 101


def test_step_log_returns_none_when_ws_only():
    # this build has no REST log endpoint: the route falls through to the SPA
    spa = "<!doctype html><html><head></head><body>app</body></html>"
    p = _provider({
        "/api/repos": [REPO],
        "/api/repos/17/pipelines/12/steps/rust-validate/log": spa,
    })
    assert p.step_log("berlinguyinca/autospec", 12, "rust-validate") is None


def test_step_log_returns_text_when_rest_available():
    p = _provider({
        "/api/repos": [REPO],
        "/api/repos/17/pipelines/12/steps/test/log": "line1\nline2\n",
    })
    assert p.step_log("berlinguyinca/autospec", 12, "test") == "line1\nline2\n"


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
        "/api/repos": [repo],
        "/api/repos/1/pipelines/p1/rebuild": "",
    })
    with pytest.raises(PermissionError_Engineering):
        p.retry("acme/repo", "p1", authorized=False)
    assert p.retry("acme/repo", "p1", authorized=True) is True
    assert p._client.requests[-1] == "https://ci.example/api/repos/1/pipelines/p1/rebuild"


def test_debug_with_pi_builds_bounded_handoff_from_failing_tasks():
    detail = {"id": 1104, "number": 12, "status": "failure", "branch": "main",
              "event": "pull_request", "commit": "ac154dc97e847ab30d865",
              "author": "berlinguyinca", "tasks": [
                  {"id": 2584, "name": "checkout", "state": "success", "exit_code": 0,
                   "workflow": "woodpecker"},
                  {"id": 2597, "name": "rust-validate", "state": "failure", "exit_code": 2,
                   "workflow": "woodpecker"}]}
    routes = {"/api/repos": [REPO], "/api/repos/17/pipelines/12": detail}
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

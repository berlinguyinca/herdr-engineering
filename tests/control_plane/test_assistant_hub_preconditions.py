"""Hub-side deploy preconditions against REAL local HTTP listeners standing in for GitHub, Woodpecker and the health
endpoint (no mocks).  GitHubAppClient runs its real installation-token exchange against the listener with a throwaway
RSA key.  Every doubt must end ``pending``; only a fully verified deploy is ``authorized``."""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from herdr_engineering.ci import WoodpeckerProvider
from herdr_engineering.control_plane.assistant import assistant_config
from herdr_engineering.control_plane.assistant_hub.canonical import RawAction
from herdr_engineering.control_plane.assistant_hub.deploy import DeployMatch, match_deploy
from herdr_engineering.control_plane.assistant_hub.policy import decide, record_owner_decision
from herdr_engineering.control_plane.assistant_hub.preconditions import (
    HealthClient,
    PreconditionClients,
    PreconditionResult,
    check_preconditions,
)
from herdr_engineering.review_gate.config import AppConfig
from herdr_engineering.review_gate.github_app import GitHubAppClient
from tests.control_plane.test_assistant_hub_deploy_grants import (
    CMD,
    NOW,
    REPO,
    SHA,
    DWorld,
)

OLD = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
TIP = SHA
SLUG = f"/repos/{REPO}"


# ------------------------------------------------------------------------------------------------ listeners
class Listener:
    """A real HTTP server whose answers come from ``handler(method, path) -> (status, body, delay)``."""

    def __init__(self, handler) -> None:
        outer = self
        self.handler, self.hits, self.closed = handler, [], False

        class H(BaseHTTPRequestHandler):
            def _serve(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)
                outer.hits.append((self.command, self.path, self.headers.get("Authorization")))
                status, body, delay = outer.handler(self.command, self.path)
                if delay:
                    time.sleep(delay)
                raw = body if isinstance(body, bytes) else json.dumps(body).encode()
                try:
                    self.send_response(status)
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                except OSError:
                    pass

            do_GET = do_POST = _serve

            def log_message(self, *a: Any) -> None:
                pass

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.srv.daemon_threads = True
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.srv.shutdown()
        self.srv.server_close()


class Remote:
    """The mutable state behind the three listeners."""

    def __init__(self) -> None:
        self.tip = TIP
        self.deployed: Any = OLD
        self.status_state, self.status_count = "success", 1
        self.runs: list[dict] = [{"name": "ci", "status": "completed", "conclusion": "success"}]
        self.wp: list[dict] = [{"id": 5, "number": 11, "commit": TIP, "branch": "main", "status": "success",
                                "event": "push"}]
        self.compare = "ahead"
        self.runs_total: Any = "auto"                          # "auto": len(runs); else the claimed total_count
        self.fail: dict[str, tuple[int, Any, float]] = {}      # path-fragment -> forced (status, body, delay)
        self.on_hit = None

    def forced(self, path: str):
        if self.on_hit:
            self.on_hit(path)
        for frag, answer in self.fail.items():
            if frag in path:
                return answer
        return None

    def github(self, method: str, path: str):
        forced = self.forced(path)
        if forced:
            return forced
        p = path.split("?", 1)[0]
        if method == "POST" and p.endswith("/access_tokens"):
            return 201, {"token": "inst-token", "expires_at": "2099-01-01T00:00:00Z"}, 0
        if p == SLUG:
            return 200, {"default_branch": "main"}, 0
        if p == f"{SLUG}/branches/main":
            return 200, {"commit": {"sha": self.tip}}, 0
        if p == f"{SLUG}/commits/{TIP}/status":
            return 200, {"state": self.status_state, "total_count": self.status_count,
                         "statuses": [{"state": self.status_state}] * min(self.status_count, 30)}, 0
        if p == f"{SLUG}/commits/{TIP}/check-runs":
            q = dict(kv.split("=", 1) for kv in path.split("?", 1)[1].split("&")) if "?" in path else {}
            page, per = int(q.get("page", 1)), min(int(q.get("per_page", 30)), 100)
            doc: dict[str, Any] = {"check_runs": self.runs[(page - 1) * per: page * per]}
            if self.runs_total == "auto":
                doc["total_count"] = len(self.runs)
            elif self.runs_total is not None:
                doc["total_count"] = self.runs_total
            return 200, doc, 0
        if "/compare/" in p:
            commits = [{"sha": "b" * 40, "commit": {"message": "one"}}, {"sha": TIP, "commit": {"message": "two"}}]
            shapes = {"ahead": ("ahead", 2, 0, commits), "behind": ("behind", 0, 3, []),
                      "identical": ("identical", 0, 0, []), "diverged": ("diverged", 2, 1, commits)}
            status, ahead, behind, cs = shapes[self.compare]
            return 200, {"status": status, "ahead_by": ahead, "behind_by": behind, "total_commits": len(cs),
                         "commits": cs}, 0
        return 404, {"message": "Not Found"}, 0

    def woodpecker(self, method: str, path: str):
        forced = self.forced(path)
        if forced:
            return forced
        p = path.split("?", 1)[0]
        if p == f"/api/repos/lookup/{REPO}":
            return 200, {"id": 7, "full_name": REPO}, 0
        if p == "/api/repos/7/pipelines":
            return 200, self.wp, 0
        return 404, {}, 0

    def health(self, method: str, path: str):
        forced = self.forced(path)
        if forced:
            return forced
        if path != "/api/v1/health?probe=1":
            return 404, {}, 0
        return 200, {"status": "ok", "version": "1", "commit": self.deployed}, 0


class Stack:
    def __init__(self, tmp_path: Path) -> None:
        self.r = Remote()
        self.gh_l, self.wp_l = Listener(self.r.github), Listener(self.r.woodpecker)
        self.health_l = Listener(self.r.health)
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = tmp_path / "app.pem"
        pem.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                          serialization.NoEncryption()))
        self.pem = pem
        self.gh = GitHubAppClient(AppConfig(app_id=1, installation_id=2, private_key_file=str(pem)),
                                  api_base=self.gh_l.url, timeout=5)
        self.ci = WoodpeckerProvider(base_url=self.wp_l.url, allow_loopback_http=True)
        self.health = HealthClient(timeout=5)
        self.health_url = self.health_l.url + "/api/v1/health"

    def clients(self, **kw) -> PreconditionClients:
        return PreconditionClients(gh=kw.get("gh", self.gh), ci=kw.get("ci", self.ci),
                                   health=kw.get("health", self.health))

    def close(self) -> None:
        for lst in (self.gh_l, self.wp_l, self.health_l):
            lst.close()


@pytest.fixture
def stack(tmp_path):
    s = Stack(tmp_path)
    try:
        yield s
    finally:
        s.close()


def deploy_cfg(stack: Stack, *, require=None, health_url="default", timeout=None, mode="auto") -> dict:
    scope: dict[str, Any] = {"command": CMD, "mode": mode}
    if require is not None:
        scope["require"] = require
    url = stack.health_url if health_url == "default" else health_url
    if url is not None:
        scope["health_url"] = url
    section: dict[str, Any] = {"projects": {"herdr-engineering": {"repo": REPO, "scopes": {"control-plane": scope}}}}
    if timeout is not None:
        section["precondition_timeout_seconds"] = timeout
    return assistant_config({"personal_assistant": {"deploy": section}})["deploy"]


def the_match(sha: str = SHA) -> DeployMatch:
    m = match_deploy(RawAction(tool="Bash", command=f"{CMD} --ref {sha}", repo=REPO),
                     assistant_config({"personal_assistant": {"deploy": {"projects": {"herdr-engineering": {
                         "repo": REPO, "scopes": {"control-plane": {"command": CMD}}}}}}})["deploy"])
    assert m is not None
    return m


def scope_of(cfg: dict) -> dict:
    return cfg["projects"]["herdr-engineering"]["scopes"]["control-plane"]


def check(stack: Stack, cfg: dict | None = None, **kw) -> PreconditionResult:
    cfg = cfg or deploy_cfg(stack)
    return check_preconditions(the_match(), scope_of(cfg), kw.get("gh", stack.gh), kw.get("ci", stack.ci),
                               kw.get("health", stack.health))


# ------------------------------------------------------------------------------------------------ config
def test_health_url_is_validated_like_a_judge_endpoint(stack):
    for good in ("http://127.0.0.1:9/api/v1/health", "https://cp.example.test/api/v1/health", "http://localhost/h"):
        assert scope_of(deploy_cfg(stack, health_url=good))["health_url"] == good
    for bad in ("http://example.com/h", "ftp://127.0.0.1/h", "https://u:p@cp.example.test/h", "http://127.0.0.1/h?x=1",
                "http://127.0.0.1/h#f", "", 5, "http://127.0.0.1/h x"):
        with pytest.raises(ValueError, match="health_url"):
            deploy_cfg(stack, health_url=bad)


def test_health_url_is_required_when_descends_is_explicitly_required(stack):
    with pytest.raises(ValueError, match="health_url"):
        deploy_cfg(stack, require=["on_main", "descends_from_deployed"], health_url=None)
    assert deploy_cfg(stack, require=["on_main", "ci_green"], health_url=None)


@pytest.mark.parametrize("bad", [0, 61, -1, 1.5, "10", True, None])
def test_timeout_is_one_to_sixty_seconds(stack, bad):
    with pytest.raises(ValueError, match="precondition_timeout_seconds"):
        deploy_cfg(stack, timeout=bad) if bad is not None else assistant_config(
            {"personal_assistant": {"deploy": {"precondition_timeout_seconds": None, "projects": {}}}})
    assert deploy_cfg(stack, timeout=1)["precondition_timeout_seconds"] == 1
    assert deploy_cfg(stack, timeout=60)["precondition_timeout_seconds"] == 60


# ------------------------------------------------------------------------------------------------ evaluator
def test_all_green_is_ok(stack):
    res = check(stack)
    assert res == PreconditionResult(True, (), ())
    # the installation-token exchange really happened, over HTTP, to the listener
    assert any(m == "POST" and p.endswith("/access_tokens") for m, p, _ in stack.gh_l.hits)
    assert all(auth is None for _, _, auth in stack.wp_l.hits)           # Woodpecker read is tokenless


def test_tip_moved_fails_on_main(stack):
    stack.r.tip = "c" * 40
    res = check(stack)
    assert not res.ok and res.failed == ("on_main",) and res.unchecked == ()


@pytest.mark.parametrize("state,count,runs,failed", [
    ("failure", 1, [], True), ("error", 1, [], True), ("pending", 1, [], True),
    ("success", 0, [], True),                                                # nothing at all is not green
    ("pending", 0, [], True),
    ("success", 1, [{"name": "a", "status": "in_progress", "conclusion": None}], True),
    ("success", 1, [{"name": "a", "status": "queued", "conclusion": None}], True),
    ("success", 1, [{"name": "a", "status": "completed", "conclusion": "failure"}], True),
    ("success", 1, [{"name": "a", "status": "completed", "conclusion": "cancelled"}], True),
    ("success", 1, [{"name": "a", "status": "completed", "conclusion": "timed_out"}], True),
    ("success", 1, [{"name": "a", "status": "completed", "conclusion": "action_required"}], True),
    ("success", 1, [{"name": "a", "status": "completed", "conclusion": "success"},
                    {"name": "b", "status": "completed", "conclusion": "neutral"},
                    {"name": "c", "status": "completed", "conclusion": "skipped"}], False),
    ("pending", 0, [{"name": "a", "status": "completed", "conclusion": "success"}], False),   # check runs only
])
def test_github_ci_rules(stack, state, count, runs, failed):
    stack.r.status_state, stack.r.status_count, stack.r.runs = state, count, runs
    res = check(stack)
    assert res.ok is (not failed)
    assert (res.failed == ("ci_green",)) is failed and res.unchecked == ()


@pytest.mark.parametrize("pipes,failed", [
    ([], True),
    ([{"number": 1, "commit": TIP, "branch": "main", "status": "failure", "event": "push"}], True),
    ([{"number": 1, "commit": TIP, "branch": "main", "status": "running", "event": "push"}], True),
    ([{"number": 1, "commit": TIP, "branch": "main", "status": "pending", "event": "push"}], True),
    ([{"number": 1, "commit": TIP, "branch": "main", "status": "killed", "event": "push"}], True),
    ([{"number": 1, "commit": "d" * 40, "branch": "main", "status": "success", "event": "push"}], True),
    ([{"number": 1, "commit": TIP, "branch": "feature", "status": "success", "event": "push"}], True),
    ([{"number": 1, "commit": TIP, "branch": "main", "status": "success", "event": "pull_request"}], True),
    ([{"number": 1, "commit": TIP, "branch": "main", "status": "success", "event": "push"},
      {"number": 2, "commit": TIP, "branch": "main", "status": "failure", "event": "push"}], True),   # latest rules
    ([{"number": 1, "commit": TIP, "branch": "main", "status": "failure", "event": "push"},
      {"number": 2, "commit": TIP, "branch": "main", "status": "success", "event": "push"}], False),
])
def test_woodpecker_rules(stack, pipes, failed):
    stack.r.wp = pipes
    res = check(stack)
    assert res.ok is (not failed)
    assert (res.failed == ("ci_green",)) is failed


def test_github_green_but_woodpecker_red_and_vice_versa(stack):
    stack.r.wp[0]["status"] = "failure"
    assert check(stack).failed == ("ci_green",)
    stack.r.wp[0]["status"] = "success"
    stack.r.status_state = "failure"
    assert check(stack).failed == ("ci_green",)


@pytest.mark.parametrize("mode", ["behind", "identical", "diverged"])
def test_sha_not_strictly_ahead_of_deployed_fails(stack, mode):
    stack.r.compare = mode
    res = check(stack)
    assert not res.ok and res.failed == ("descends_from_deployed",) and res.unchecked == ()


def test_deployed_equal_to_sha_is_not_a_new_deploy(stack):
    stack.r.deployed = SHA
    res = check(stack)
    assert not res.ok and res.failed == ("descends_from_deployed",)
    assert not any("/compare/" in p for _, p, _ in stack.gh_l.hits)


@pytest.mark.parametrize("commit", ["abc1234", "0123456", OLD.upper(), "", None, 5, ["x"], OLD + "0", "zz" * 20])
def test_unusable_deployed_commit_is_unchecked(stack, commit):
    stack.r.deployed = commit
    res = check(stack)
    assert not res.ok and res.unchecked == ("descends_from_deployed",) and res.failed == ()


FORCED = [
    ("down", None),
    ("500", (500, {"x": 1}, 0)),
    ("garbage", (200, b"{not json", 0)),
    ("wrong_type", (200, [1, 2], 0)),
    ("oversized", (200, b" " * (5 * 1024 * 1024) + b"{}", 0)),
    ("slow", (200, {}, 2.5)),
]


def _force(stack: Stack, where: str, kind: str, answer) -> GitHubAppClient | None:
    """Break one endpoint family; returns a replacement client when the endpoint is simply down."""
    frag = {"gh_tip": "/branches/", "gh_status": "/status", "gh_runs": "/check-runs", "gh_compare": "/compare/",
            "wp": "/api/repos/", "health": "/api/v1/health"}[where]
    if answer is None:
        {"wp": stack.wp_l, "health": stack.health_l}.get(where, stack.gh_l).close()
    else:
        stack.r.fail[frag] = answer


BROKEN_WHERE = [
    ("gh_tip", {"on_main", "ci_green"}),       # default_branch_head is also used to find the CI branch
    ("gh_status", {"ci_green"}), ("gh_runs", {"ci_green"}), ("gh_compare", {"descends_from_deployed"}),
    ("wp", {"ci_green"}), ("health", {"descends_from_deployed"}),
]
# One GitHub path cannot be "down" alone: that case is test_github_down_makes_every_github_name_unchecked.
BROKEN_CASES = [(where, names, kind, answer) for where, names in BROKEN_WHERE for kind, answer in FORCED
                if not (answer is None and where.startswith("gh"))]


@pytest.mark.parametrize("where,names,kind,answer", BROKEN_CASES,
                         ids=[f"{w}-{k}" for w, _, k, _ in BROKEN_CASES])
def test_a_broken_endpoint_is_unchecked_never_ok(tmp_path, where, names, kind, answer):
    stack = Stack(tmp_path)
    try:
        if kind == "slow":
            stack.gh = GitHubAppClient(AppConfig(1, 2, str(stack.pem)), api_base=stack.gh_l.url, timeout=1)
            stack.ci = WoodpeckerProvider(base_url=stack.wp_l.url, allow_loopback_http=True)
            stack.ci.public_timeout = 1
            stack.health = HealthClient(timeout=1)
        _force(stack, where, kind, answer)
        res = check(stack)
        assert not res.ok
        assert set(res.unchecked) == names and res.failed == ()
    finally:
        stack.close()


def test_github_down_makes_every_github_name_unchecked(stack):
    stack.gh_l.close()
    res = check(stack)
    assert not res.ok and set(res.unchecked) == {"on_main", "ci_green", "descends_from_deployed"}


def test_no_clients_configured_is_all_unchecked(stack):
    res = check_preconditions(the_match(), scope_of(deploy_cfg(stack)), None, None, None)
    assert not res.ok and set(res.unchecked) == {"on_main", "ci_green", "descends_from_deployed"}


def test_ci_url_that_is_not_https_leaves_ci_green_unchecked(stack):
    ci = WoodpeckerProvider(base_url="http://ci.example.test")
    res = check(stack, ci=ci)
    assert res.unchecked == ("ci_green",) and not res.ok
    ci2 = WoodpeckerProvider(base_url=stack.wp_l.url)          # loopback http without the explicit test switch
    assert check(stack, ci=ci2).unchecked == ("ci_green",)


def test_missing_health_url_leaves_descends_unchecked(stack):
    cfg = deploy_cfg(stack, health_url=None)
    res = check(stack, cfg)
    assert res.unchecked == ("descends_from_deployed",) and not res.ok


def test_require_subset_is_honoured(stack):
    stack.r.status_state, stack.r.status_count, stack.r.runs = "failure", 1, []
    stack.r.compare = "behind"
    stack.r.wp = []
    cfg = deploy_cfg(stack, require=["on_main"], health_url=None)
    res = check(stack, cfg)
    assert res == PreconditionResult(True, (), ())
    assert not any("/status" in p or "check-runs" in p for _, p, _ in stack.gh_l.hits)
    assert stack.wp_l.hits == [] and stack.health_l.hits == []


def test_a_redirect_is_not_followed(stack):
    other = Listener(lambda m, p: (200, {"commit": OLD}, 0))
    try:
        stack.r.fail["/api/v1/health"] = (302, b"", 0)
        # the 302 has no Location the opener would follow; the listener sees only one hit and the result is unchecked
        res = check(stack)
        assert res.unchecked == ("descends_from_deployed",) and other.hits == []
    finally:
        other.close()


def test_health_commit_is_only_read_from_the_commit_key(stack):
    stack.r.health = None
    h = Listener(lambda m, p: (200, {"status": "ok", "sha": TIP, "commit": 7}, 0))
    try:
        cfg = deploy_cfg(stack, health_url=h.url + "/api/v1/health")
        assert check(stack, cfg).unchecked == ("descends_from_deployed",)
        assert h.hits[0][1] == "/api/v1/health?probe=1"
    finally:
        h.close()


# ------------------------------------------------------------------------------------------------ check-run paging
def _runs(n: int, bad_at: int | None = None) -> list[dict]:
    return [{"name": f"r{i}", "status": "completed", "conclusion": "failure" if i == bad_at else "success"}
            for i in range(n)]


def test_a_failing_run_on_page_two_is_seen(stack):
    stack.r.runs = _runs(150, bad_at=120)
    res = check(stack)
    assert not res.ok and res.failed == ("ci_green",) and res.unchecked == ()
    pages = [p for _, p, _ in stack.gh_l.hits if "check-runs" in p]
    assert any("page=2" in p for p in pages)


@pytest.mark.parametrize("n", [100, 101, 250, 500])
def test_all_green_runs_up_to_the_five_page_cap_pass(stack, n):
    stack.r.runs = _runs(n)
    assert check(stack) == PreconditionResult(True, (), ())


@pytest.mark.parametrize("total", [501, 600, 10_000])
def test_more_runs_than_the_cap_can_read_is_unchecked(stack, total):
    stack.r.runs = _runs(500)
    stack.r.runs_total = total
    res = check(stack)
    assert not res.ok and res.unchecked == ("ci_green",) and res.failed == ()


def test_total_above_what_was_served_is_unchecked(stack):
    stack.r.runs = _runs(3)
    stack.r.runs_total = 7                          # the listener serves 3 and claims 7: an incomplete list
    res = check(stack)
    assert not res.ok and res.unchecked == ("ci_green",)


@pytest.mark.parametrize("total", [None, "many", -1, 1.5, True])
def test_malformed_check_run_page_is_unchecked(stack, total):
    stack.r.runs_total = total
    res = check(stack)
    assert not res.ok and res.unchecked == ("ci_green",) and res.failed == ()


# ------------------------------------------------------------------------------------------------ woodpecker events
def _pipe(number, status, event, branch="main", commit=TIP):
    return {"number": number, "commit": commit, "branch": branch, "status": status, "event": event}


def test_a_later_cron_or_tag_success_cannot_mask_a_failed_push(stack):
    for event in ("cron", "tag", "release", "deployment", "weird"):
        stack.r.wp = [_pipe(1, "failure", "push"), _pipe(2, "success", event)]
        res = check(stack)
        assert not res.ok and res.failed == ("ci_green",), event


def test_a_later_manual_success_counts_as_the_latest(stack):
    stack.r.wp = [_pipe(1, "failure", "push"), _pipe(2, "success", "manual")]
    assert check(stack) == PreconditionResult(True, (), ())
    stack.r.wp = [_pipe(1, "success", "push"), _pipe(2, "failure", "manual")]
    assert check(stack).failed == ("ci_green",)


def test_only_cron_or_tag_pipelines_is_not_green(stack):
    stack.r.wp = [_pipe(1, "success", "cron"), _pipe(2, "success", "tag")]
    assert check(stack).failed == ("ci_green",)


# ------------------------------------------------------------------------------------------------ default clients
def test_default_clients_without_a_configured_ci_url_has_no_ci(fake_host_config):
    from herdr_engineering.control_plane.assistant_hub.preconditions import default_clients
    for values in ({}, {"ci": {}}, {"ci": {"url": ""}}, {"ci": {"url": "  "}}):
        fake_host_config(values)
        assert default_clients().ci is None
    fake_host_config({"ci": {"url": "https://ci.example.test"}})
    ci = default_clients().ci
    assert ci is not None and ci.base_url == "https://ci.example.test"


# ------------------------------------------------------------------------------------------------ decide
class DeployWorld(DWorld):
    def go(self, stack: Stack, cfg: dict, *, clients=None, command=None, **kw):
        return decide(self.store, self.cfg, self.worker,
                      RawAction(tool="Bash", command=command or f"{CMD} --ref {SHA}", repo=REPO,
                                **kw), "s1", "t1", self.clock.now, judge=self.judge, deploy_cfg=cfg,
                      preconditions=clients if clients is not None else stack.clients())


@pytest.fixture
def dwrld(tmp_path):
    w = DeployWorld(tmp_path)
    w.gate()
    w.stand()
    try:
        yield w
    finally:
        w.listener.close()


def test_all_green_deploy_is_authorized_with_a_deploy_audit(dwrld, stack):
    d = dwrld.go(stack, deploy_cfg(stack))
    assert d.outcome == "authorized" and d.source == "grant" and d.grant_id
    (a,) = dwrld.audits("assistant.deploy.authorized")
    assert "herdr-engineering" in a["detail"] and "control-plane" in a["detail"] and SHA[:12] in a["detail"]
    assert SHA not in a["detail"] and OLD not in a["detail"]
    assert dwrld.store.list_action_requests("acme", "owner", "pending") == []


@pytest.mark.parametrize("mutate,name", [
    (lambda r: setattr(r, "tip", "c" * 40), "on_main"),
    (lambda r: setattr(r, "status_state", "failure"), "ci_green"),
    (lambda r: setattr(r, "wp", []), "ci_green"),
    (lambda r: setattr(r, "compare", "behind"), "descends_from_deployed"),
])
def test_failed_precondition_is_pending_naming_only_the_name(dwrld, stack, mutate, name):
    mutate(stack.r)
    d = dwrld.go(stack, deploy_cfg(stack))
    assert d.outcome == "pending" and d.source == "deploy_preconditions"
    assert f"failed {name}" in d.reason and "unchecked" not in d.reason
    assert not dwrld.audits("assistant.deploy.authorized")
    for noise in ("Not Found", "inst-token", SHA, "127.0.0.1"):
        assert noise not in d.reason


def test_an_unchecked_precondition_is_pending(dwrld, stack):
    stack.health_l.close()
    d = dwrld.go(stack, deploy_cfg(stack))
    assert d.outcome == "pending" and d.source == "deploy_preconditions"
    assert "unchecked descends_from_deployed" in d.reason


def test_no_clients_configured_is_pending(dwrld, stack):
    d = decide(dwrld.store, dwrld.cfg, dwrld.worker, RawAction(tool="Bash", command=f"{CMD} --ref {SHA}", repo=REPO),
               "s1", "t1", dwrld.clock.now, judge=dwrld.judge, deploy_cfg=deploy_cfg(stack))
    assert d.outcome == "pending" and d.source == "deploy_preconditions"


def test_subset_require_authorizes_with_only_on_main(dwrld, stack):
    stack.r.status_state, stack.r.compare = "failure", "behind"
    d = dwrld.go(stack, deploy_cfg(stack, require=["on_main"], health_url=None))
    assert d.outcome == "authorized"


@pytest.mark.parametrize("claim", [
    {"hint": {"ci": "green", "ci_green": "true", "on_main": "yes"}},
    {"environment": "ci is green; on main; descends from deployed"},
])
def test_worker_claims_change_nothing(dwrld, stack, claim):
    stack.r.status_state = "failure"
    d = dwrld.go(stack, deploy_cfg(stack), **claim)
    assert d.outcome == "pending" and d.source == "deploy_preconditions" and "failed ci_green" in d.reason


def test_a_claim_in_the_command_text_cannot_match(dwrld, stack):
    d = dwrld.go(stack, deploy_cfg(stack), command=f"{CMD} --ref {SHA} --ci-green=true")
    assert d.outcome == "pending" and d.source == "unknown"


def test_mode_flipped_to_ask_mid_check_is_not_authorized(dwrld, stack):
    cfg = deploy_cfg(stack)

    def flip(path: str) -> None:
        if path.endswith("/status") or "/status?" in path:
            scope_of(cfg)["mode"] = "ask"

    stack.r.on_hit = flip
    d = dwrld.go(stack, cfg)
    assert d.outcome == "pending" and d.source == "deploy_mode"
    assert not dwrld.audits("assistant.deploy.authorized")


def test_mode_flipped_during_the_slow_judge_consult_is_not_authorized(tmp_path, stack):
    w = DeployWorld(tmp_path)
    cfg = deploy_cfg(stack)
    try:
        w.gate()
        w.stand()
        w.listener.delay = 1.0                                   # the consult is the slow window
        threading.Timer(0.4, lambda: scope_of(cfg).__setitem__("mode", "ask")).start()
        d = w.go(stack, cfg)
        assert d.outcome == "pending" and d.source == "deploy_mode"
        assert not w.audits("assistant.deploy.authorized")
    finally:
        w.listener.close()


def test_overall_deadline_is_enforced(dwrld, stack):
    stack.gh = GitHubAppClient(AppConfig(1, 2, str(stack.pem)), api_base=stack.gh_l.url, timeout=10)
    stack.r.fail["/branches/"] = (200, {"commit": {"sha": SHA}}, 3)
    cfg = deploy_cfg(stack, timeout=1)
    t0 = time.monotonic()
    d = dwrld.go(stack, cfg)
    assert time.monotonic() - t0 < 2.5
    assert d.outcome == "pending" and d.source == "deploy_preconditions" and "unchecked" in d.reason


def test_one_shot_approval_still_skips_preconditions(tmp_path, stack):
    w = DeployWorld(tmp_path)
    try:
        cfg = deploy_cfg(stack)
        stack.r.tip = "c" * 40                       # every precondition would fail
        stack.health_l.close()
        first = w.go(stack, cfg)
        assert first.outcome == "pending"
        record_owner_decision(w.store, w.cfg, NOW, "acme", "owner", first.request_id, True, "owner")
        again = w.go(stack, cfg)
        assert again.outcome == "authorized" and again.source == "owner"
        assert stack.gh_l.hits == []
    finally:
        w.listener.close()


def test_vetoes_still_win_over_green_preconditions(dwrld, stack):
    cfg = deploy_cfg(stack)
    d = decide(dwrld.store, {**dwrld.cfg, "extra_vetoes": [
        {"code": "no_deploys", "reason": "frozen", "verb": "deploy.prod"}]}, dwrld.worker,
        RawAction(tool="Bash", command=f"{CMD} --ref {SHA}", repo=REPO), "s1", "t1", dwrld.clock.now,
        judge=dwrld.judge, deploy_cfg=cfg, preconditions=stack.clients())
    assert d.outcome == "denied"


@pytest.mark.parametrize("frag,name", [("/branches/", "on_main"), ("/status", "ci_green"),
                                       ("/check-runs", "ci_green"), ("/compare/", "descends_from_deployed")])
@pytest.mark.parametrize("answer", [(500, {}, 0), (200, b"{nope", 0), (200, [1], 0)], ids=["500", "garbage", "type"])
def test_each_github_endpoint_broken_is_exactly_pending_preconditions(dwrld, stack, frag, name, answer):
    stack.r.fail[frag] = answer
    d = dwrld.go(stack, deploy_cfg(stack))
    assert (d.outcome, d.source) == ("pending", "deploy_preconditions")
    assert f"unchecked {name}" in d.reason
    assert not dwrld.audits("assistant.deploy.authorized")


def test_callable_deploy_cfg_flipped_to_ask_mid_check_is_deploy_mode(dwrld, stack):
    cfg = deploy_cfg(stack)

    def flip(path: str) -> None:
        if "/status" in path:
            scope_of(cfg)["mode"] = "ask"

    stack.r.on_hit = flip
    d = decide(dwrld.store, dwrld.cfg, dwrld.worker, RawAction(tool="Bash", command=f"{CMD} --ref {SHA}", repo=REPO),
               "s1", "t1", dwrld.clock.now, judge=dwrld.judge, deploy_cfg=lambda: cfg,
               preconditions=stack.clients())
    assert (d.outcome, d.source) == ("pending", "deploy_mode")


def test_callable_deploy_cfg_replaced_with_a_new_dict_mid_check_is_deploy_mode(dwrld, stack):
    holder = {"cfg": deploy_cfg(stack)}
    ask = deploy_cfg(stack, mode="ask")

    def swap(path: str) -> None:
        if "/status" in path:
            holder["cfg"] = ask                       # a config reload replaces the whole object

    stack.r.on_hit = swap
    d = decide(dwrld.store, dwrld.cfg, dwrld.worker, RawAction(tool="Bash", command=f"{CMD} --ref {SHA}", repo=REPO),
               "s1", "t1", dwrld.clock.now, judge=dwrld.judge, deploy_cfg=lambda: holder["cfg"],
               preconditions=stack.clients())
    assert (d.outcome, d.source) == ("pending", "deploy_mode")


def test_default_require_scope_without_health_url_is_pending_never_authorized(dwrld, stack):
    cfg = deploy_cfg(stack, health_url=None)
    assert "require" in scope_of(cfg) and "health_url" not in scope_of(cfg)
    d = dwrld.go(stack, cfg)
    assert (d.outcome, d.source) == ("pending", "deploy_preconditions")
    assert "unchecked descends_from_deployed" in d.reason
    assert not dwrld.audits("assistant.deploy.authorized")

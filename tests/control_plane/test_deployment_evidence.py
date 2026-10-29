"""Release evidence from GitHub (deploy-4 follow-up A), against a fake GitHub server.

``published_commit`` is the ``<asset>.commit`` asset of the definitions' release tag;
``on_main`` is "the target is an ancestor of (or equal to) the branch tip and not behind
the plan commit", from the compare API. Every failure is unknown, never confirmed.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from herdr_engineering.control_plane import deployment_evidence as de
from herdr_engineering.control_plane.deployment_coordinator import ReleaseEvidence
from herdr_engineering.control_plane.deployment_definitions import Definition
from herdr_engineering.control_plane.deployment_evidence import GitHubReleaseEvidence

REPO = "InferWeave/inferweave"
TRACK = (REPO, "main", "ci/woodpecker/push/rust")
T = "a" * 40
P = "b" * 40          # an earlier plan commit
OTHER = "c" * 40
TOKEN = "ghp_" + "S3CR3T" * 6
UNKNOWN = ReleaseEvidence()


def _defn(component="hive-gateway", tag="node-ci-latest", asset="inferweave", track=TRACK):
    return Definition(
        component=component, stage=1, order=0, host="hive", updater="u",
        follows={"repo": track[0], "tag": tag, "asset": asset, "branch": track[1],
                 "ci_context": track[2]},
        restart="gateway-cycle", gpu=False,
        health={"probe": "served-request", "timeout_s": 600, "hold_s": 600},
        rollback={"strategy": "previous-version"})


class FakeGitHub:
    """api.github.com + the release-asset download host on one loopback server.
    ``routes[path] = (status, headers, body)`` or a callable(handler) -> same."""

    def __init__(self):
        self.routes = {}
        self.requests = []       # (path, Authorization header or None)
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                fake.requests.append((self.path, self.headers.get("Authorization")))
                route = fake.routes.get(self.path.split("?")[0])
                if route is None:
                    route = (404, {}, b'{"message":"Not Found"}')
                if callable(route):
                    route = route(self)
                    if route is None:
                        return
                status, headers, body = route
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    # ---- canned GitHub answers
    def release(self, tag="node-ci-latest", assets=(("inferweave.commit", 7),), repo=REPO):
        body = {"tag_name": tag, "draft": False, "assets": [
            {"id": i, "name": n, "url": f"{self.base}/repos/{repo}/releases/assets/{i}"}
            for n, i in assets]}
        self.routes[f"/repos/{repo}/releases/tags/{tag}"] = (200, {}, json.dumps(body).encode())

    def asset(self, commit_text, asset_id=7, repo=REPO, redirect=True):
        dl = f"/download/{asset_id}"
        if redirect:
            self.routes[f"/repos/{repo}/releases/assets/{asset_id}"] = (
                302, {"Location": self.base + dl}, b"")
            self.routes[dl] = (200, {}, commit_text.encode())
        else:
            self.routes[f"/repos/{repo}/releases/assets/{asset_id}"] = (200, {}, commit_text.encode())

    def compare(self, base, head, status, ahead_by=0, behind_by=0, repo=REPO):
        self.routes[f"/repos/{repo}/compare/{base}...{head}"] = (200, {}, json.dumps(
            {"status": status, "ahead_by": ahead_by, "behind_by": behind_by,
             "commits": [], "files": []}).encode())

    def confirmed(self, commit=T):
        self.release()
        self.asset(commit + "\n")
        self.compare("main", T, "behind", behind_by=3)


@pytest.fixture()
def gh():
    fake = FakeGitHub()
    yield fake
    fake.close()


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _inline(fn):
    fn()


def _source(gh, defs=None, *, token=TOKEN, clock=None, executor=_inline, **kw):
    defs = defs if defs is not None else {"hive-gateway": _defn()}
    return GitHubReleaseEvidence(
        lambda: defs, token=lambda: token, api=gh.base,
        download_hosts={"127.0.0.1"}, clock=clock or Clock(), executor=executor, **kw)


# --------------------------------------------------------------- answers
def test_a_published_release_on_main_is_confirmed_and_only_the_api_gets_the_token(gh):
    gh.confirmed()
    assert _source(gh)(TRACK, T, None) == ReleaseEvidence(T, True)
    api = [(p, a) for p, a in gh.requests if p.startswith("/repos/")]
    downloads = [(p, a) for p, a in gh.requests if p.startswith("/download/")]
    assert api and all(a == f"Bearer {TOKEN}" for _, a in api)
    assert downloads == [("/download/7", None)]          # credentials never follow the redirect


def test_a_direct_asset_answer_without_redirect_works(gh):
    gh.release()
    gh.asset(T, redirect=False)
    gh.compare("main", T, "identical")
    assert _source(gh)(TRACK, T, None) == ReleaseEvidence(T, True)


def test_a_release_carrying_another_commit_reports_that_commit(gh):
    gh.confirmed(commit=OTHER)
    assert _source(gh)(TRACK, T, None) == ReleaseEvidence(OTHER, True)


@pytest.mark.parametrize("status,ahead", [("diverged", 2), ("ahead", 1)])
def test_a_target_not_on_main_is_false(gh, status, ahead):
    gh.release()
    gh.asset(T)
    gh.compare("main", T, status, ahead_by=ahead)
    assert _source(gh)(TRACK, T, None) == ReleaseEvidence(T, False)


def test_behind_with_commits_of_its_own_is_not_on_main(gh):
    gh.release()
    gh.asset(T)
    gh.compare("main", T, "behind", ahead_by=1, behind_by=2)      # inconsistent: refuse it
    assert _source(gh)(TRACK, T, None) == UNKNOWN


@pytest.mark.parametrize("status,expected", [("behind", True), ("identical", True),
                                             ("ahead", False), ("diverged", True)])
def test_the_target_must_not_be_behind_the_plan_commit(gh, status, expected):
    """compare/{target}...{plan}: the plan behind the target is the normal case; the plan
    ahead (the target is older) is False; a diverged plan (force-pushed away) is no reason
    to halt a target that is on main."""
    gh.confirmed()
    gh.compare(T, P, status, ahead_by=1 if status in ("ahead", "diverged") else 0)
    assert _source(gh)(TRACK, T, P) == ReleaseEvidence(T, expected)


def test_the_plan_commit_equal_to_the_target_needs_no_second_compare(gh):
    gh.confirmed()
    assert _source(gh)(TRACK, T, T) == ReleaseEvidence(T, True)
    assert not any(p.startswith(f"/repos/{REPO}/compare/{T}...") for p, _ in gh.requests)


def test_several_assets_on_a_track_must_all_carry_the_same_commit(gh):
    defs = {"hive-gateway": _defn(), "hive-relay": _defn("hive-relay", asset="iw-relay")}
    gh.release(assets=(("inferweave.commit", 7), ("iw-relay.commit", 8)))
    gh.asset(T, 7)
    gh.asset(T, 8)
    gh.compare("main", T, "behind")
    assert _source(gh, defs)(TRACK, T, None) == ReleaseEvidence(T, True)
    gh.asset(OTHER, 8)
    assert _source(gh, defs)(TRACK, T, None) == ReleaseEvidence(None, True)


# --------------------------------------------------------------- failures
@pytest.mark.parametrize("breakage", [
    "release-500", "release-401", "release-404", "asset-missing", "compare-404", "compare-500",
    "garbage-release", "garbage-compare", "release-not-object", "assets-not-list",
    "asset-url-elsewhere", "asset-id-not-int", "commit-not-hex", "commit-short",
    "commit-oversize", "release-oversize", "compare-oversize", "status-unknown",
    "counts-not-int", "redirect-elsewhere", "redirect-loop", "download-500"])
def test_any_failure_is_unknown(gh, breakage):
    gh.confirmed()
    rel = f"/repos/{REPO}/releases/tags/node-ci-latest"
    cmp_ = f"/repos/{REPO}/compare/main...{T}"
    asset = f"/repos/{REPO}/releases/assets/7"
    if breakage.startswith("release-") and breakage[8:].isdigit():
        gh.routes[rel] = (int(breakage[8:]), {}, b'{"message":"x"}')
    elif breakage.startswith("compare-") and breakage[8:].isdigit():
        gh.routes[cmp_] = (int(breakage[8:]), {}, b"{}")
    elif breakage == "asset-missing":
        gh.release(assets=(("inferweave.sha256", 7),))
    elif breakage == "garbage-release":
        gh.routes[rel] = (200, {}, b"{not json")
    elif breakage == "garbage-compare":
        gh.routes[cmp_] = (200, {}, b"\xff\xfe")
    elif breakage == "release-not-object":
        gh.routes[rel] = (200, {}, b"[1,2]")
    elif breakage == "assets-not-list":
        gh.routes[rel] = (200, {}, b'{"assets": {"a": 1}}')
    elif breakage == "asset-url-elsewhere":
        gh.routes[rel] = (200, {}, json.dumps({"assets": [
            {"id": 7, "name": "inferweave.commit", "url": "https://evil.example/x"}]}).encode())
    elif breakage == "asset-id-not-int":
        gh.routes[rel] = (200, {}, json.dumps({"assets": [
            {"id": "7", "name": "inferweave.commit",
             "url": f"{gh.base}/repos/{REPO}/releases/assets/7"}]}).encode())
    elif breakage == "commit-not-hex":
        gh.asset("z" * 40)
    elif breakage == "commit-short":
        gh.asset(T[:12])
    elif breakage == "commit-oversize":
        gh.asset(T + " " * 300)
    elif breakage == "release-oversize":
        gh.routes[rel] = (200, {}, b'{"pad":"' + b"x" * (de.MAX_JSON + 10) + b'"}')
    elif breakage == "compare-oversize":
        gh.routes[cmp_] = (200, {}, b'{"pad":"' + b"x" * (de.MAX_JSON + 10) + b'"}')
    elif breakage == "status-unknown":
        gh.compare("main", T, "weird")
    elif breakage == "counts-not-int":
        gh.routes[cmp_] = (200, {}, b'{"status":"behind","ahead_by":"0","behind_by":1}')
    elif breakage == "redirect-elsewhere":
        gh.routes[asset] = (302, {"Location": "https://evil.example/steal"}, b"")
    elif breakage == "redirect-loop":
        gh.routes[asset] = (302, {"Location": gh.base + "/download/7"}, b"")
        gh.routes["/download/7"] = (302, {"Location": gh.base + "/download/7"}, b"")
    elif breakage == "download-500":
        gh.routes["/download/7"] = (500, {}, b"")
    src = _source(gh)
    assert src(TRACK, T, None) == UNKNOWN
    assert src.status()["state"] == "unknown" and src.status()["error"]


def test_no_token_makes_no_request_and_is_unknown(gh):
    gh.confirmed()
    src = _source(gh, token=None)
    assert src(TRACK, T, None) == UNKNOWN
    assert gh.requests == []
    assert src.status() == {"state": "unknown", "error": "no GitHub token configured"}


@pytest.mark.parametrize("target", ["", "not-a-sha", "A" * 40 + "0", T[:12]])
def test_a_target_that_is_not_a_full_sha_is_unknown_without_a_request(gh, target):
    gh.confirmed()
    assert _source(gh)(TRACK, target, None) == UNKNOWN
    assert gh.requests == []


def test_a_track_without_definitions_is_unknown(gh):
    gh.confirmed()
    assert _source(gh, {})(TRACK, T, None) == UNKNOWN
    assert gh.requests == []


def test_a_non_https_api_off_loopback_is_refused():
    with pytest.raises(ValueError):
        GitHubReleaseEvidence(lambda: {}, api="http://api.github.example")


def test_a_slow_drip_server_hits_the_hard_timeout(gh):
    def drip(handler):
        handler.send_response(200)
        handler.send_header("Content-Length", "100000")
        handler.end_headers()
        try:
            for _ in range(100):
                handler.wfile.write(b" ")
                handler.wfile.flush()
                time.sleep(0.3)
        except OSError:
            pass
        return None

    gh.confirmed()
    gh.routes[f"/repos/{REPO}/releases/tags/node-ci-latest"] = drip
    src = _source(gh, timeout=1.0)
    started = time.monotonic()
    assert src(TRACK, T, None) == UNKNOWN
    assert time.monotonic() - started < 3.0
    assert src.status()["error"] == "timeout"


def test_the_call_never_waits_on_github(gh):
    """The default executor runs the fetch on a worker: the tick reads the cache only."""
    release = threading.Event()

    def slow(handler):
        release.wait(5)
        return (200, {}, b"{}")

    gh.confirmed()
    gh.routes[f"/repos/{REPO}/releases/tags/node-ci-latest"] = slow
    src = GitHubReleaseEvidence(lambda: {"hive-gateway": _defn()}, token=lambda: TOKEN,
                                api=gh.base, download_hosts={"127.0.0.1"})
    started = time.monotonic()
    for _ in range(5):
        assert src(TRACK, T, None) == UNKNOWN
    assert time.monotonic() - started < 0.5
    release.set()


def test_the_worker_confirms_on_a_later_call(gh):
    gh.confirmed()
    src = GitHubReleaseEvidence(lambda: {"hive-gateway": _defn()}, token=lambda: TOKEN,
                                api=gh.base, download_hosts={"127.0.0.1"})
    assert src(TRACK, T, None) == UNKNOWN
    deadline = time.monotonic() + 5
    while src(TRACK, T, None) == UNKNOWN and time.monotonic() < deadline:
        time.sleep(0.02)
    assert src(TRACK, T, None) == ReleaseEvidence(T, True)


# ------------------------------------------------------------------ cache
def _count(gh):
    return sum(1 for p, _ in gh.requests if p.endswith("/releases/tags/node-ci-latest"))


def test_a_success_is_cached_for_the_success_ttl(gh):
    gh.confirmed()
    clock = Clock()
    src = _source(gh, clock=clock)
    assert src(TRACK, T, None) == ReleaseEvidence(T, True)
    clock.t += de.SUCCESS_TTL_S - 1
    assert src(TRACK, T, None) == ReleaseEvidence(T, True)
    assert _count(gh) == 1
    clock.t += 2
    assert src(TRACK, T, None) == ReleaseEvidence(T, True)
    assert _count(gh) == 2


def test_the_cache_is_per_target_and_plan(gh):
    gh.confirmed()
    gh.compare("main", OTHER, "behind")
    gh.compare(T, P, "behind")
    src = _source(gh)
    assert src(TRACK, T, None) == ReleaseEvidence(T, True)
    assert src(TRACK, OTHER, None) == ReleaseEvidence(T, True)     # release still carries T
    assert src(TRACK, T, P) == ReleaseEvidence(T, True)
    assert _count(gh) == 3


def test_a_failure_is_retried_only_after_the_failure_backoff(gh):
    gh.routes[f"/repos/{REPO}/releases/tags/node-ci-latest"] = (500, {}, b"")
    clock = Clock()
    src = _source(gh, clock=clock)
    assert src(TRACK, T, None) == UNKNOWN
    assert src(TRACK, T, None) == UNKNOWN
    assert _count(gh) == 1
    clock.t += de.FAILURE_RETRY_S + 1
    gh.confirmed()
    assert src(TRACK, T, None) == ReleaseEvidence(T, True)
    assert _count(gh) == 2
    assert de.FAILURE_RETRY_S < de.SUCCESS_TTL_S


def test_a_failed_refresh_keeps_the_old_success_only_up_to_the_max_age(gh):
    gh.confirmed()
    clock = Clock()
    src = _source(gh, clock=clock)
    assert src(TRACK, T, None) == ReleaseEvidence(T, True)
    gh.routes[f"/repos/{REPO}/releases/tags/node-ci-latest"] = (503, {}, b"")
    clock.t += de.SUCCESS_TTL_S + 1
    assert src(TRACK, T, None) == ReleaseEvidence(T, True)      # stale, within the max age
    assert src.status()["error"]
    clock.t = 1000.0 + de.MAX_AGE_S + 1
    assert src(TRACK, T, None) == UNKNOWN                       # the failure did not refresh it
    assert de.MAX_AGE_S <= 180


def test_a_definitive_not_on_main_replaces_a_cached_confirmation(gh):
    gh.confirmed()
    clock = Clock()
    src = _source(gh, clock=clock)
    assert src(TRACK, T, None) == ReleaseEvidence(T, True)
    gh.compare("main", T, "diverged", ahead_by=1)
    clock.t += de.SUCCESS_TTL_S + 1
    assert src(TRACK, T, None) == ReleaseEvidence(T, False)


def test_the_cache_is_bounded(gh):
    gh.confirmed()
    src = _source(gh)
    for i in range(de.CACHE_ENTRIES + 20):
        src(TRACK, f"{i:040x}", None)
    assert len(src._cache) <= de.CACHE_ENTRIES


# ------------------------------------------------------------------ token
def test_the_token_never_appears_in_status_logs_or_answers(gh, caplog):
    caplog.set_level(logging.DEBUG)
    echo = json.dumps({"message": f"bad credentials {TOKEN}"}).encode()
    src = _source(gh)
    clock = src._clock
    seen = []
    for route in [(401, {}, echo), (200, {}, echo), (500, {}, echo),
                  (302, {"Location": f"https://evil.example/{TOKEN}"}, b"")]:
        gh.routes[f"/repos/{REPO}/releases/tags/node-ci-latest"] = route
        clock.t += de.FAILURE_RETRY_S + 1
        seen.append(repr(src(TRACK, T, None)))
        seen.append(json.dumps(src.status()))
    gh.confirmed()
    gh.routes[f"/repos/{REPO}/releases/assets/7"] = (302, {"Location": f"https://x.example/{TOKEN}"}, b"")
    clock.t += de.FAILURE_RETRY_S + 1
    seen.append(repr(src(TRACK, T, None)))
    seen.append(json.dumps(src.status()))
    text = "\n".join(seen) + "\n" + caplog.text
    assert TOKEN not in text and "S3CR3T" not in text


# ----------------------------------------------------------- the server
def test_the_server_wires_the_github_source_and_confirms_through_it(tmp_path, gh):
    from tests.control_plane.test_deployment_definitions import C1, CI, STAGE1, _write
    from tests.control_plane.test_deployments_api import VIEW, _get, _server

    d = _write(tmp_path / "pipelines", [doc for doc in STAGE1 if doc["host"] == "hive"])
    ci = CI()
    ci.add(10, C1)
    svr, _repo = _server(tmp_path, {"deployment_definitions": str(d)}, ci=ci)
    try:
        assert isinstance(svr.deployment_evidence, GitHubReleaseEvidence)
        svr.deployments_tick()                                  # no token on a test host
        deadline = time.monotonic() + 5                         # the worker answers async
        while svr.deployment_evidence.status()["error"] is None and time.monotonic() < deadline:
            time.sleep(0.02)
        body = _get(svr.port, headers=VIEW)[1]
        assert body["coordinator"]["evidence"] == "unknown"
        assert body["coordinator"]["evidence_error"] == "no GitHub token configured"
        assert body["plans"][0]["current"]["reason"] == "release not confirmed"

        gh.release()
        gh.asset(C1)
        gh.compare("main", C1, "behind", behind_by=1)
        svr.deployment_evidence = GitHubReleaseEvidence(
            svr.deployment_definitions.definitions, token=lambda: TOKEN, api=gh.base,
            download_hosts={"127.0.0.1"}, executor=_inline)
        svr.deployments_tick()
        body = _get(svr.port, headers=VIEW)[1]
        [plan] = body["plans"]
        assert plan["release_confirmed"] is True and plan["target_on_main"] is True
        assert plan["current"]["type"] == "Start" and plan["permitted"] == ["hive-gateway"]
        assert body["coordinator"]["evidence"] == "configured"
        assert TOKEN not in json.dumps(body)
    finally:
        svr.shutdown()

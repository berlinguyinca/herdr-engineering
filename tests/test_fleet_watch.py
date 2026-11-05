"""Fleet rollout watcher core (spec docs/specs/herdr-fleet-rollout-watcher-20261104, phase 2).

No mocks: a real local HTTP server speaks the minimal GitHub branch endpoint (ETag, 304, 403/429
rate-limit headers) over real bare git repos, which also serve as the git remote.
"""

from __future__ import annotations

import json
import os
import random
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from herdr_engineering.fleet_watch import core, plan, poll, state, verify

REPO = "acme/widget"


def git(cwd: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
    out = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, env=env)
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


class Forge:
    """Bare remotes under <root>/<owner>/<repo>.git plus a work clone per repo."""

    def __init__(self, root: Path):
        self.root = root
        self.work: dict[str, Path] = {}

    def create(self, repo: str) -> Path:
        bare = self.root / f"{repo}.git"
        bare.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
        work = self.root / "work" / repo
        work.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
        git(work, "remote", "add", "origin", str(bare))
        self.work[repo] = work
        self.commit(repo, "one")
        return bare

    def commit(self, repo: str, name: str, push: bool = True) -> str:
        work = self.work[repo]
        (work / name).write_text(name)
        git(work, "add", name)
        git(work, "commit", "-q", "-m", name)
        if push:
            git(work, "push", "-q", "-f", "origin", "main")
        return git(work, "rev-parse", "HEAD")

    def head(self, repo: str) -> str:
        return git(self.root / f"{repo}.git", "rev-parse", "refs/heads/main")


class FakeGitHub:
    """Protocol-faithful GET /repos/{o}/{r}/branches/main with ETag, 304 and rate limits."""

    def __init__(self, forge: Forge):
        self.forge = forge
        self.mode = "ok"            # ok | retry_after | reset | secondary | down
        self.remaining = 4000
        self.head_override: dict[str, str] = {}   # what the API claims, when it differs from git
        self.requests: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                parts = self.path.strip("/").split("/")
                outer.requests.append({"path": self.path, "inm": self.headers.get("If-None-Match"),
                                       "auth": self.headers.get("Authorization")})
                if outer.mode == "down":
                    return self._send(500, b"{}")
                if outer.mode == "retry_after":
                    return self._send(403, b"{}", {"Retry-After": "42"})
                if outer.mode == "reset":
                    return self._send(403, b"{}", {"X-RateLimit-Remaining": "0",
                                                   "X-RateLimit-Reset": str(int(time.time()) + 300)})
                if outer.mode == "secondary":
                    return self._send(429, b"{}")
                if len(parts) != 5 or parts[0] != "repos" or parts[3:] != ["branches", "main"]:
                    return self._send(404, b"{}")
                try:
                    name = f"{parts[1]}/{parts[2]}"
                    sha = outer.head_override.get(name) or outer.forge.head(name)
                except AssertionError:
                    return self._send(404, b"{}")
                tag = f'"{sha[:16]}"'
                extra = {"ETag": tag, "X-RateLimit-Remaining": str(outer.remaining)}
                if self.headers.get("If-None-Match") == tag:
                    return self._send(304, b"", extra)
                self._send(200, json.dumps({"name": "main", "commit": {"sha": sha}}).encode(), extra)

            def _send(self, code, body, headers=None):
                self.send_response(code)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def forge(tmp_path):
    f = Forge(tmp_path / "forge")
    f.create(REPO)
    return f


@pytest.fixture
def gh(forge, monkeypatch):
    server = FakeGitHub(forge)
    monkeypatch.setenv("HERDR_FLEET_WATCH_API", server.url)
    monkeypatch.setenv("HERDR_FLEET_WATCH_GIT_BASE", str(forge.root))
    yield server
    server.close()


@pytest.fixture
def sdir(tmp_path):
    return tmp_path / "state"


# ---------------------------------------------------------------- unit: backoff, plan, state


def test_backoff_prefers_retry_after_then_reset_then_exponential_with_cap():
    assert poll.backoff_seconds(failures=1, headers={"retry-after": "42"}, now=1000) == 42
    assert poll.backoff_seconds(failures=1, headers={"retry-after": "99999"}, now=1000) == 900
    assert poll.backoff_seconds(failures=1, headers={"x-ratelimit-reset": "1300"}, now=1000) == 300
    assert poll.backoff_seconds(failures=1, headers={"x-ratelimit-reset": "50"}, now=1000) == 5
    assert poll.backoff_seconds(failures=1, now=0) == 120
    assert poll.backoff_seconds(failures=2, now=0) == 240
    assert poll.backoff_seconds(failures=3, now=0) == 480
    assert poll.backoff_seconds(failures=4, now=0) == 900
    assert poll.backoff_seconds(failures=30, now=0, jitter=1.0) == 900


def test_interval_has_jitter_and_stretches_when_budget_is_low():
    rng = random.Random(1)
    vals = {poll.next_interval({}, rng) for _ in range(200)}
    assert min(vals) >= 100 and max(vals) <= 140 and len(vals) > 5
    assert poll.next_interval({"x-ratelimit-remaining": "99"}, rng) == 600
    assert poll.next_interval({"x-ratelimit-remaining": "100"}, rng) != 600


def test_rollout_id_is_deterministic_and_order_independent():
    a = plan.rollout_id({"x": "a" * 40, "y": "b" * 40})
    assert a == plan.rollout_id({"y": "b" * 40, "x": "a" * 40})
    assert len(a) == 16
    assert a != plan.rollout_id({"x": "a" * 40, "y": "c" * 40})


def test_plan_coalesces_changed_repos_and_is_idempotent_once_succeeded():
    heads = {"a": "1" * 40, "b": "2" * 40, "c": "3" * 40}
    converged = {"a": "1" * 40, "b": "9" * 40}
    p = plan.plan_rollout(heads, converged, {})
    assert p["targets"] == {"b": "2" * 40, "c": "3" * 40}
    assert p["before"] == {"b": "9" * 40, "c": None}
    assert [h["host"] for h in p["hosts"]] == ["beast", "bender", "macbook-pro"]
    assert [h["optional"] for h in p["hosts"]] == [False, False, True]
    assert plan.plan_rollout(heads, converged, {p["rollout_id"]: {"status": "succeeded"}}) is None
    assert plan.plan_rollout(heads, converged, {p["rollout_id"]: {"status": "halted"}}) is not None
    assert plan.plan_rollout(heads, heads, {}) is None


def test_state_round_trip_is_private_and_corrupt_file_is_set_aside(sdir):
    s = state.load(sdir)
    s["repos"]["a/b"] = {"etag": "x"}
    path = state.save(s, sdir)
    assert (path.stat().st_mode & 0o777) == 0o600 and (sdir.stat().st_mode & 0o777) == 0o700
    assert state.load(sdir)["repos"]["a/b"] == {"etag": "x"}
    path.write_text("{not json")
    assert state.load(sdir) == state.empty_state()
    assert list(sdir.glob("state.json.corrupt-*"))


def test_single_instance_lock_is_exclusive(sdir):
    with state.single_instance(sdir), pytest.raises(state.WatcherBusy), state.single_instance(sdir):
        pass
    with state.single_instance(sdir):
        pass


# ---------------------------------------------------------------- integration: poller on real HTTP


def test_poll_sends_etag_and_gets_304_without_changing_head(gh, forge):
    first = poll.poll_repo(REPO, {}, now=0)
    assert first.outcome == "new" and first.head == forge.head(REPO) and first.etag
    entry = poll.apply_result({}, first, now=0)
    second = poll.poll_repo(REPO, entry, now=1)
    assert second.outcome == "unchanged"
    assert gh.requests[1]["inm"] == first.etag
    forge.commit(REPO, "two")
    third = poll.poll_repo(REPO, entry, now=2)
    assert third.outcome == "changed" and third.head == forge.head(REPO) and third.etag != first.etag


def test_poll_sends_the_token_only_when_given(gh):
    poll.poll_repo(REPO, {}, now=0)
    poll.poll_repo(REPO, {}, now=0, auth="tok")
    assert gh.requests[0]["auth"] is None and gh.requests[1]["auth"] == "Bearer tok"


@pytest.mark.parametrize(("mode", "wait"), [("retry_after", 42), ("reset", 300), ("secondary", 120)])
def test_poll_rate_limit_backs_off_per_headers(gh, mode, wait):
    gh.mode = mode
    r = poll.poll_repo(REPO, {}, now=time.time())
    assert r.outcome == "rate_limited"
    assert abs(r.wait_s - wait) <= (2 if mode == "reset" else 30)


def test_poll_server_error_and_dead_server_are_errors_with_growing_backoff(gh):
    gh.mode = "down"
    r = poll.poll_repo(REPO, {"failures": 2}, now=0, rng=random.Random(0))
    assert r.outcome == "error" and r.wait_s >= 480
    gh.close()
    dead = poll.poll_repo(REPO, {}, now=0)
    assert dead.outcome == "error" and dead.detail.startswith("network")


def test_unknown_repo_is_an_error_not_a_crash(gh):
    r = poll.poll_repo("nobody/nothing", {}, now=0)
    assert r.outcome == "error" and r.detail == "http 404"


def test_apply_result_tracks_degraded_and_failure_count():
    bad = poll.PollResult("r", "error", None, None, 120, "x")
    e = poll.apply_result({}, bad, now=10)
    e = poll.apply_result(e, bad, now=20)
    assert e["failures"] == 2 and e["degraded"] and e["next_poll_at"] == 140
    e = poll.apply_result(e, poll.PollResult("r", "unchanged", "a" * 40, "t", 120), now=30)
    assert e["failures"] == 0 and not e["degraded"]


# ---------------------------------------------------------------- integration: D3 on real git


def cache_for(sdir):
    return verify.cache_path(sdir, REPO)


def test_d3_accepts_published_head(forge, sdir):
    v = verify.verify_published(forge.head(REPO), api_head=forge.head(REPO), cache=cache_for(sdir),
                                url=str(forge.root / f"{REPO}.git"))
    assert v.ok


def test_d3_refuses_stray_unpushed_commit(forge, sdir):
    stray = forge.commit(REPO, "stray", push=False)
    url = str(forge.root / f"{REPO}.git")
    v = verify.verify_published(stray, api_head=stray, cache=cache_for(sdir), url=url)
    assert not v.ok and v.reason == "unknown_commit"


def test_d3_refuses_sha_that_a_force_push_removed_from_main(forge, sdir):
    url = str(forge.root / f"{REPO}.git")
    old = forge.commit(REPO, "doomed")
    assert verify.verify_published(old, api_head=old, cache=cache_for(sdir), url=url).ok
    git(forge.work[REPO], "reset", "-q", "--hard", "HEAD~1")
    forge.commit(REPO, "replacement")        # force-pushes main away from `old`
    v = verify.verify_published(old, api_head=old, cache=cache_for(sdir), url=url)
    assert not v.ok and v.reason == "not_on_origin_main"


def test_d3_accepts_ancestor_but_refuses_non_api_head_and_malformed(forge, sdir):
    url = str(forge.root / f"{REPO}.git")
    first = forge.head(REPO)
    newer = forge.commit(REPO, "two")
    assert verify.verify_published(first, api_head=first, cache=cache_for(sdir), url=url).ok
    assert verify.verify_published(first, api_head=newer, cache=cache_for(sdir), url=url).reason == "not_api_head"
    assert verify.verify_published("abc", api_head=None, cache=cache_for(sdir), url=url).reason == "malformed_sha"


def test_d3_fetch_failure_is_a_refusal_not_an_exception(sdir, tmp_path):
    v = verify.verify_published("a" * 40, api_head="a" * 40, cache=cache_for(sdir), url=str(tmp_path / "nope.git"))
    assert not v.ok and v.reason == "fetch_failed"


# ---------------------------------------------------------------- integration: a whole pass


def test_run_once_plans_a_rollout_then_goes_quiet_when_converged(gh, forge, sdir, tmp_path):
    lock = tmp_path / "lock.yaml"
    lock.write_text("# keep\nrepos:\n  widget: " + forge.head(REPO) + "\nnode: 22\n")
    quiet = core.run_once([REPO], directory=sdir, lock_path=lock)
    assert quiet["rollout"] is None and quiet["repos"][REPO]["outcome"] == "new"
    newest = forge.commit(REPO, "two")
    rep = core.run_once([REPO], directory=sdir, lock_path=lock)
    assert rep["repos"][REPO]["outcome"] == "changed"
    assert rep["rollout"]["targets"] == {REPO: newest}
    assert rep["rollout"]["rollout_id"] == plan.rollout_id({REPO: newest})
    again = core.run_once([REPO], directory=sdir, lock_path=lock)
    assert again["repos"][REPO]["outcome"] == "unchanged"
    assert again["rollout"]["rollout_id"] == rep["rollout"]["rollout_id"]
    assert lock.read_text().startswith("# keep")          # the lock is never written


def test_run_once_refuses_head_that_is_not_published(gh, forge, sdir, tmp_path):
    # The API claims a head that the remote's main does not contain (a stray commit).
    stray = forge.commit(REPO, "stray", push=False)
    gh.head_override[REPO] = stray           # the API view only; the git remote is unchanged
    rep = core.run_once([REPO], directory=sdir, lock_path=tmp_path / "none.yaml")
    assert rep["refused"][REPO]["reason"] == "unknown_commit" and rep["rollout"] is None


def test_run_once_skips_a_repo_in_backoff_and_resumes_after(gh, forge, sdir, tmp_path):
    gh.mode = "retry_after"
    t0 = time.time()
    r1 = core.run_once([REPO], directory=sdir, now=t0, lock_path=tmp_path / "n")
    assert r1["repos"][REPO]["outcome"] == "rate_limited" and r1["rollout"] is None
    n = len(gh.requests)
    gh.mode = "ok"
    r2 = core.run_once([REPO], directory=sdir, now=t0 + 5, lock_path=tmp_path / "n")
    assert r2["repos"][REPO]["outcome"] == "backoff" and len(gh.requests) == n   # no request sent
    r3 = core.run_once([REPO], directory=sdir, now=t0 + 60, lock_path=tmp_path / "n")
    assert r3["repos"][REPO]["outcome"] == "new"


def test_run_once_rejects_a_concurrent_pass(gh, sdir, tmp_path):
    with state.single_instance(sdir), pytest.raises(state.WatcherBusy):
        core.run_once([REPO], directory=sdir, lock_path=tmp_path / "n")


# ---------------------------------------------------------------- smoke: the real CLI


def cli(tmp_path, gh, forge, *args):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {**os.environ, "HOME": str(home), "XDG_STATE_HOME": str(home / "state"), "GH_TOKEN": "t",
           "HERDR_FLEET_WATCH_API": gh.url, "HERDR_FLEET_WATCH_GIT_BASE": str(forge.root)}
    return subprocess.run([sys.executable, "-c", "import sys; from herdr_engineering.cli import main; sys.exit(main())",
                           "fleet-watch", *args], capture_output=True, text=True, env=env, timeout=120)


def test_cli_once_dry_run_prints_plan_and_json(gh, forge, tmp_path):
    out = cli(tmp_path, gh, forge, "once", "--dry-run", "--repo", REPO, "--lock", str(tmp_path / "none"))
    assert out.returncode == 0, out.stderr
    assert f"{REPO}: new head={forge.head(REPO)[:12]}" in out.stdout
    assert "(dry-run, nothing executed)" in out.stdout and "beast -> bender -> macbook-pro?" in out.stdout
    js = json.loads(cli(tmp_path, gh, forge, "once", "--dry-run", "--repo", REPO, "--json",
                        "--lock", str(tmp_path / "none")).stdout)
    assert js["repos"][REPO]["outcome"] == "unchanged" and js["dry_run"] is True
    assert (tmp_path / "home/state/herdr-engineering/fleet-watch/state.json").exists()
    assert gh.requests[1]["inm"]                           # the second process reused the stored ETag
    assert gh.requests[0]["auth"] == "Bearer t"


def test_cli_without_dry_run_refuses_and_touches_nothing(gh, forge, tmp_path):
    out = cli(tmp_path, gh, forge, "once", "--repo", REPO)
    assert out.returncode == 2 and "--dry-run" in out.stderr
    assert gh.requests == [] and not (tmp_path / "home/state").exists()


# ---------------------------------------------------------------- review fixes


@pytest.mark.parametrize("bad", [
    {"schema_version": 1, "repos": [], "converged": {}, "rollouts": {}},
    {"schema_version": 1, "repos": {}, "converged": [], "rollouts": {}},
    {"schema_version": 1, "repos": {}, "converged": {}, "rollouts": 3},
    {"schema_version": 1, "repos": {"o/x": 5}, "converged": {}, "rollouts": {}},
    {"schema_version": 1, "repos": {"o/x": {"failures": "a"}}, "converged": {}, "rollouts": {}},
    {"schema_version": 1, "repos": {"o/x": {"next_poll_at": "soon"}}, "converged": {}, "rollouts": {}},
    {"schema_version": 1, "repos": {}, "converged": {"o/x": 7}, "rollouts": {}},
    {"schema_version": 1, "repos": {}, "converged": {}, "rollouts": {"id": "x"}},
    [1, 2],
])
def test_state_load_sets_aside_any_wrong_shape(sdir, bad):
    sdir.mkdir(parents=True)
    (sdir / "state.json").write_text(json.dumps(bad))
    assert state.load(sdir) == state.empty_state()
    assert list(sdir.glob("state.json.corrupt-*"))


def test_run_once_survives_a_wrong_shaped_state_file(gh, sdir, tmp_path):
    sdir.mkdir(parents=True)
    (sdir / "state.json").write_text('{"schema_version":1,"repos":{"acme/widget":{"failures":"a"}}}')
    assert core.run_once([REPO], directory=sdir, lock_path=tmp_path / "n")["repos"][REPO]["outcome"] == "new"


def test_http_protocol_errors_are_degraded_polls_not_crashes():
    import http.client

    def short_read(url, headers):
        raise http.client.IncompleteRead(b"x")

    r = poll.poll_repo(REPO, {}, now=0, fetch=short_read)
    assert r.outcome == "error" and r.detail.startswith("network")


def test_permissions_are_enforced_on_pre_existing_paths(sdir):
    sdir.mkdir(parents=True, mode=0o755)
    os.chmod(sdir, 0o755)
    (sdir / "state.json.tmp").write_text("x")
    os.chmod(sdir / "state.json.tmp", 0o644)
    state.save(state.empty_state(), sdir)
    with state.single_instance(sdir):
        pass
    assert (sdir.stat().st_mode & 0o777) == 0o700
    assert ((sdir / "state.json").stat().st_mode & 0o777) == 0o600
    assert ((sdir / "lock").stat().st_mode & 0o777) == 0o600


def test_new_parents_are_private_whatever_the_umask(tmp_path):
    old = os.umask(0)
    try:
        d = tmp_path / "a" / "b" / "fleet-watch"
        state.save(state.empty_state(), d)
        assert (d.stat().st_mode & 0o777) == 0o700
        assert ((tmp_path / "a" / "b").stat().st_mode & 0o777) == 0o700
    finally:
        os.umask(old)


def test_d3_requires_an_api_head(forge, sdir):
    url = str(forge.root / f"{REPO}.git")
    v = verify.verify_published(forge.head(REPO), api_head=None, cache=cache_for(sdir), url=url)
    assert not v.ok and v.reason == "not_api_head"


def test_d3_refuses_when_main_moved_since_the_poll(forge, sdir):
    url = str(forge.root / f"{REPO}.git")
    polled = forge.head(REPO)
    forge.commit(REPO, "raced")
    v = verify.verify_published(polled, api_head=polled, cache=cache_for(sdir), url=url,
                                require_tip=True)
    assert not v.ok and v.reason == "not_api_head"


def test_retry_after_zero_cannot_hot_loop():
    assert poll.backoff_seconds(failures=1, headers={"retry-after": "0"}, now=0) >= 5
    assert poll.backoff_seconds(failures=1, headers={"x-ratelimit-reset": "10"}, now=1000) >= 5


def test_sha_with_trailing_newline_is_malformed(forge, sdir):
    v = verify.verify_published(forge.head(REPO) + "\n", api_head=forge.head(REPO) + "\n",
                                cache=cache_for(sdir), url=str(forge.root / f"{REPO}.git"))
    assert v.reason == "malformed_sha"
    bad = json.dumps({"commit": {"sha": "a" * 40 + "\n"}}).encode()
    r = poll.poll_repo(REPO, {}, now=0, fetch=lambda u, h: poll.Response(200, {}, bad))
    assert r.outcome == "error"


def test_no_partial_rollout_when_any_watched_repo_is_unverified(gh, forge, sdir, tmp_path):
    forge.create("acme/gadget")
    repos = [REPO, "acme/gadget", "acme/missing"]       # the third 404s: no verified head
    rep = core.run_once(repos, directory=sdir, lock_path=tmp_path / "n")
    assert rep["rollout"] is None
    assert rep["held"] == {"acme/missing": "error"}
    ok = core.run_once(repos[:2], directory=sdir, lock_path=tmp_path / "n")
    assert set(ok["rollout"]["targets"]) == set(repos[:2]) and ok["held"] == {}

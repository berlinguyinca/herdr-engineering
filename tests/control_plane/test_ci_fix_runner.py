"""The CI fix runner skeleton: real git against a local "forge", the real control-plane state machine in
process (and once over HTTP), fakes for the gateway and the agent launcher."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from herdr_engineering.control_plane.api import ControlPlaneServer
from herdr_engineering.control_plane.ci_fix import CIFixService, ci_fix_config
from herdr_engineering.control_plane.repo import ControlPlaneRepo

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("ci_fix_runner", ROOT / "deploy/control-plane/ci-fix-runner.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)

CONFIG = {"enabled": True, "hosts": ["bender", "beast"], "models": ["qwen-a", "qwen-b"],
          "max_concurrent": {"global": 4, "per_host": 1}, "deadline_minutes": 30}
pytestmark = pytest.mark.skipif(not runner.shutil.which("git"), reason="git is required")


def git(*args, cwd=None):
    done = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.test", "-c", "commit.gpgsign=false",
                           *args], cwd=cwd, capture_output=True, text=True, check=True,
                          env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"})
    return done.stdout.strip()


@pytest.fixture
def forge(tmp_path):
    """A bare 'remote' with three commits on main; the failing one is the first."""
    work = tmp_path / "forge-work"
    work.mkdir()
    git("init", "-q", "-b", "main", cwd=work)
    shas = []
    for index in range(3):
        (work / "app.txt").write_text(f"version {index}\n")
        git("add", "-A", cwd=work)
        git("commit", "-q", "-m", f"commit {index}", cwd=work)
        shas.append(git("rev-parse", "HEAD", cwd=work))
    bare = tmp_path / "forge.git"
    git("clone", "-q", "--bare", str(work), str(bare))
    return SimpleNamespace(path=bare, shas=shas, work=work)


class LocalForge:
    """A bare repository on disk standing in for the forge; pull requests are recorded, not opened."""

    def __init__(self, path):
        self.path, self.prs, self.pr_error, self.labelled = path, [], None, True

    def url(self, context):
        return str(self.path)

    def credential_file(self):
        return None

    def open_pr(self, context, head, base, title, body, draft, label):
        if self.pr_error:
            raise runner.Failure(self.pr_error)
        self.prs.append({"head": head, "base": base, "title": title, "body": body, "draft": draft, "label": label})
        number = len(self.prs) + 8
        return {"url": f"https://github.com/org/frontend/pull/{number}", "number": number, "labelled": self.labelled}


class Gateway:
    def __init__(self, models=("qwen-b",)):
        self._models = list(models)
        self.fail = False

    def models(self):
        if self.fail:
            raise runner.Failure("gateway unreachable: refused")
        return list(self._models)


class Launcher:
    """Starts nothing real; records what the runner asked."""

    def __init__(self, unavailable=None):
        self.unavailable, self.started, self.stopped, self.running = unavailable, [], [], {}
        self.screen = {}                                  # fix id -> what the "screen" shows (None: unreadable)

    def available(self):
        return self.unavailable

    def start(self, fix, config=None):
        self.started.append(fix["id"])
        self.running[fix["id"]] = True
        return {"session_id": f"herdr:bender:eng:{len(self.started)}"}

    def alive(self, fix):
        return self.running.get(fix["id"], False)

    def fingerprint(self, fix):
        return self.screen.get(fix["id"])

    def stop(self, fix):
        self.stopped.append(fix["id"])
        self.running[fix["id"]] = False


class InProcess:
    """The control plane, called directly, with the refusals mapped the way HTTP maps them."""

    def __init__(self, service):
        self.service, self.down = service, False

    def _guard(self):
        if self.down:
            raise runner.Failure("control plane unavailable: refused")

    def _call(self, function, *args, **kwargs):
        self._guard()
        try:
            return function(*args, **kwargs)
        except ValueError as exc:
            raise runner.Rejected(400, str(exc)) from None

    def config(self):
        self._guard()
        return self.service.runner_config()

    def claim(self, host, defer=None):
        return self._call(self.service.claim, host, defer)

    def state(self, fix_id, state, **fields):
        return self._call(self.service.set_state, fix_id, state, **fields)

    def result(self, fix_id, body):
        return self._call(self.service.record_result, fix_id, body)

    def get(self, fix_id):
        self._guard()
        try:
            return self.service.get(fix_id)
        except KeyError:
            return None

    def active(self, host):
        self._guard()
        return self.service.list(host=host, active=True)["fixes"]


class Provider:
    available = True
    token = "t"
    base_url = "https://ci.example"
    revision = "a" * 40

    def repos(self):
        return [{"id": 17, "repository": "org/frontend", "forge_url": "https://github.com/org/frontend",
                 "clone_url": "https://github.com/org/frontend.git"}]

    def debug_with_pi(self, repository, number):
        return {"ok": True, "view_in_web_ui": f"https://ci.example/repos/17/pipeline/{number}",
                "handoff": {"pipeline_id": str(number), "revision": self.revision, "failing_step": "build",
                            "log_summary": "FAILED"}}


class Env:
    def __init__(self, tmp_path, forge, config=None, models=("qwen-b",)):
        self.repo = ControlPlaneRepo()
        self.clock = [1_000_000.0]
        self.service = CIFixService(self.repo, ci_fix_config(config or CONFIG))
        self.cp = InProcess(self.service)
        self.forge, self.gateway, self.launcher = LocalForge(forge.path), Gateway(models), Launcher()
        self.shas = forge.shas
        self.tmp = tmp_path
        self.said = []

    def runner(self, host="bender", **changes):
        parts = dict(host_id=host, state_dir=self.tmp / "state", work_root=self.tmp / "work",
                     mirror_root=self.tmp / "mirrors", control_plane=self.cp, git=runner.Git(), forge=self.forge,
                     gateway=self.gateway, launcher=self.launcher, clock=lambda: self.clock[0],
                     say=self.said.append)
        parts.update(changes)
        return runner.Runner(**parts)

    def ask(self, number=12, sha=None, op=None):
        class P(Provider):
            revision = sha or self.shas[0]
        view, _ = self.service.request(SimpleNamespace(_factory=P), "17", number, op or f"op-{number}")
        return view["id"]

    def leftovers(self):
        found = []
        for base in (self.tmp / "state" / "fixes", self.tmp / "work" / "ci-fix"):
            if base.is_dir():
                found += [str(entry) for entry in base.iterdir()]
        for mirror in (self.tmp / "mirrors").glob("*.git") if (self.tmp / "mirrors").is_dir() else []:
            found += runner.Git().pins(mirror)
        return found


@pytest.fixture
def env(tmp_path, forge):
    return Env(tmp_path, forge)


# ------------------------------------------------------------------ the happy dry run
def test_prepare_makes_a_standalone_copy_at_the_exact_commit_and_cleans_up_when_the_agent_quits(env):
    fix = env.ask(sha=env.shas[0])                          # not the tip of main: an older failing commit
    r = env.runner()
    r.once()
    view = env.service.get(fix)
    assert view["state"] == "running" and view["host"] == "bender" and view["model"] == "qwen-b"
    assert view["branch"] == f"herdr-ci-fix/12-{env.shas[0][:7]}" and view["session_id"] == "herdr:bender:eng:1"
    repo = env.tmp / "work" / "ci-fix" / fix / "repo"
    assert git("rev-parse", "HEAD", cwd=repo) == env.shas[0]
    assert git("rev-parse", "--abbrev-ref", "HEAD", cwd=repo) == view["branch"]
    assert (repo / "app.txt").read_text() == "version 0\n"
    assert git("remote", cwd=repo) == ""                                      # no origin, no credentials
    assert not (repo / ".git/objects/info/alternates").exists()               # standalone, not borrowing the mirror
    assert git("config", "--local", "user.email", cwd=repo) == "ci-fix@herdr.invalid"
    assert oct((env.tmp / "state" / "fixes" / fix / "state.json").stat().st_mode & 0o777) == "0o600"
    env.launcher.running[fix] = False                                         # the agent quits, without a commit
    env.runner().once()
    final = env.service.get(fix)
    assert final["state"] == "no_change" and final["ended_at"] and "without a commit" in final["reason"]
    assert env.launcher.stopped == [fix]
    assert env.leftovers() == []                                              # no record, work copy or pin remains


def test_a_running_fix_is_left_alone_while_its_agent_lives(env):
    fix = env.ask()
    env.runner().once()
    env.runner().once()                                                       # a restarted runner, agent alive
    assert env.service.get(fix)["state"] == "running" and len(env.launcher.started) == 1
    assert (env.tmp / "work" / "ci-fix" / fix / "repo").is_dir()


# ------------------------------------------------------------------ failures while preparing
def test_a_commit_the_forge_does_not_have_fails_with_that_reason_and_leaves_nothing(env):
    fix = env.ask(sha="c" * 40)
    env.runner().once()
    view = env.service.get(fix)
    assert view["state"] == "failed" and "not available from the repository" in view["reason"]
    assert env.leftovers() == [] and env.launcher.started == []


def test_the_launcher_failing_ends_the_fix_and_cleans_up(env):
    class Broken(Launcher):
        def start(self, fix, config=None):
            raise runner.Failure("pi is not installed")
    env.launcher = Broken()
    fix = env.ask()
    env.runner().once()
    assert env.service.get(fix)["state"] == "failed" and "pi is not installed" in env.service.get(fix)["reason"]
    assert env.leftovers() == []


@pytest.mark.parametrize("change", ["bad-sha", "bad-number"])
def test_a_malformed_request_is_failed_not_run(env, change):
    fix = env.ask()
    real = env.cp.claim

    def forged(host, defer=None):
        out = real(host, defer)
        if out["request"]:
            out["request"] = {**out["request"], **({"revision": "--upload-pack=x"} if change == "bad-sha" else {"number": "12; rm"})}
        return out
    env.cp.claim = forged
    env.runner().once()
    assert env.service.get(fix)["state"] == "failed" and env.leftovers() == []


def test_a_forged_fix_id_is_refused_before_anything_touches_a_path(env):
    r = env.runner()
    r.config = env.cp.config()
    with pytest.raises(runner.Failure, match="invalid fix id"):
        r.prepare({"id": "../../etc", "revision": "a" * 40, "number": 1, "context": {}}, "m")
    assert not (env.tmp / "work").exists() or list((env.tmp / "work").rglob("*")) == []


def test_the_forge_must_be_a_configured_host_and_a_plain_repository_path():
    forge = runner.HttpsForge(("github.com",))
    assert forge.url({"repository_id": "github.com/org/app"}) == "https://github.com/org/app.git"
    for bad in ("evil.example/org/app", "github.com/../etc/passwd", "github.com/org/a b", "github.com", None, "github.com/-x"):
        with pytest.raises(runner.Failure):
            forge.url({"repository_id": bad})


def test_a_mirror_over_the_size_cap_fails_the_request(env, monkeypatch):
    fix = env.ask()
    monkeypatch.setattr(runner.Git, "size_bytes", lambda self, path: 6 * 1024 ** 3)
    env.runner().once()
    assert "over 5 GB" in env.service.get(fix)["reason"] and env.leftovers() == []


# ------------------------------------------------------------------ choosing a model and a host
def test_the_first_configured_model_the_gateway_lists_wins(tmp_path, forge):
    env = Env(tmp_path, forge, models=("qwen-b", "qwen-a"))
    fix = env.ask()
    env.runner().once()
    assert env.service.get(fix)["model"] == "qwen-a"                          # config order, not gateway order


@pytest.mark.parametrize("setup,expect", [
    (lambda e: e.gateway._models.clear(), "none of the configured models is listed"),
    (lambda e: setattr(e.gateway, "fail", True), "gateway unreachable"),
])
def test_no_model_keeps_the_request_queued_with_a_reason(env, setup, expect):
    setup(env)
    fix = env.ask()
    env.runner().once()
    view = env.service.get(fix)
    assert view["state"] == "queued" and expect in view["reason"] and env.leftovers() == []
    assert env.launcher.started == []


def test_without_a_configured_model_nothing_runs(tmp_path, forge):
    env = Env(tmp_path, forge, config={**CONFIG, "models": []})
    fix = env.ask()
    env.runner().once()
    assert env.service.get(fix)["state"] == "queued" and "none configured" in env.service.get(fix)["reason"]


def test_a_host_that_cannot_run_an_agent_claims_nothing(env):
    env.launcher.unavailable = "bwrap is not installed"
    fix = env.ask()
    env.runner().once()
    assert env.service.get(fix)["state"] == "queued" and env.launcher.started == []
    assert any("bwrap is not installed" in line for line in env.said)


def test_a_host_below_the_disk_floor_claims_nothing(env, monkeypatch):
    monkeypatch.setattr(runner.shutil, "disk_usage", lambda path: SimpleNamespace(free=1024))
    fix = env.ask()
    env.runner().once()
    assert env.service.get(fix)["state"] == "queued" and any("free on this host" in line for line in env.said)


def test_per_host_capacity_is_respected_and_the_other_host_takes_the_next(env):
    first, second = env.ask(number=1), env.ask(number=2)
    env.runner("bender").once()
    assert env.service.get(first)["state"] == "running" and env.service.get(second)["state"] == "queued"
    env.runner("bender").once()
    assert env.service.get(second)["state"] == "queued"                       # bender is full
    env.runner("beast", state_dir=env.tmp / "state-beast", work_root=env.tmp / "work-beast",
               mirror_root=env.tmp / "mirrors-beast").once()                  # its own host, its own disk
    assert env.service.get(second)["state"] == "running" and env.service.get(second)["host"] == "beast"


def test_disabled_or_unlisted_hosts_do_nothing(tmp_path, forge):
    env = Env(tmp_path, forge, config={"enabled": True, "hosts": ["beast"], "models": ["qwen-b"]})
    fix = env.ask()
    env.runner("bender").once()                                               # not a listed host
    assert env.service.get(fix)["state"] == "queued"
    off = Env(tmp_path / "off", forge)
    off.service = CIFixService(off.repo, ci_fix_config(None))
    off.cp = InProcess(off.service)
    off.runner().once()                                                       # disabled
    assert off.launcher.started == []


# ------------------------------------------------------------------ cancel, deadline
def test_cancel_stops_the_agent_and_cleans_everything(env):
    fix = env.ask()
    env.runner().once()
    env.service.cancel(fix)
    env.runner().once()
    final = env.service.get(fix)
    assert final["state"] == "cancelled" and "stopped and cleaned up" in final["reason"]
    assert env.launcher.stopped == [fix] and env.leftovers() == []


def test_the_deadline_expires_a_running_fix(env):
    fix = env.ask()
    env.runner().once()
    env.clock[0] += 31 * 60
    env.runner().once()
    assert env.service.get(fix)["state"] == "expired" and "30 minutes" in env.service.get(fix)["reason"]
    assert env.leftovers() == []


def test_a_fix_ended_on_the_control_plane_is_cleaned_without_reporting(env):
    fix = env.ask()
    env.runner().once()
    env.service.set_state(fix, "failed", reason="operator override")           # ended elsewhere
    env.runner().once()
    assert env.service.get(fix)["reason"] == "operator override" and env.leftovers() == []


# ------------------------------------------------------------------ crash reconcile
def test_a_runner_that_died_while_preparing_is_reconciled(env):
    fix = env.ask()
    env.runner().once()
    record = json.loads((env.tmp / "state" / "fixes" / fix / "state.json").read_text())
    record["phase"] = "preparing"                                              # as if it died before launching
    (env.tmp / "state" / "fixes" / fix / "state.json").write_text(json.dumps(record))
    env.service.set_state(fix, "failed", reason="x") if False else None
    env.launcher.running[fix] = False
    env.runner().once()
    assert env.service.get(fix)["state"] == "failed"
    assert "restarted while the fix was being prepared" in env.service.get(fix)["reason"]
    assert env.leftovers() == []


def test_a_claim_with_no_local_record_is_failed(env):
    fix = env.ask()
    assert env.service.claim("bender")["request"]["id"] == fix                 # claimed, then the runner died
    env.runner().once()
    assert env.service.get(fix)["state"] == "failed" and "lost this fix" in env.service.get(fix)["reason"]


def test_orphans_are_removed(env, forge):
    fix = env.ask()
    env.runner().once()
    env.launcher.running[fix] = False
    env.runner().once()                                                        # finished and cleaned
    orphan = env.tmp / "work" / "ci-fix" / ("cifix_" + "1" * 26)
    (orphan / "repo").mkdir(parents=True)
    (orphan / "repo" / "f").write_text("x")
    stranger = env.tmp / "work" / "ci-fix" / "not-a-fix-id"
    stranger.mkdir()
    mirror = next((env.tmp / "mirrors").glob("*.git"))
    runner.Git().pin(mirror, runner.PIN_PREFIX + "cifix_" + "2" * 26, forge.shas[0])
    env.runner().once()
    assert not orphan.exists() and runner.Git().pins(mirror) == []
    assert stranger.exists()                                                   # only validated ids are ever removed


def test_an_unreadable_record_is_cleaned_up(env):
    bad = env.tmp / "state" / "fixes" / ("cifix_" + "3" * 26)
    bad.mkdir(parents=True)
    (bad / "state.json").write_text("{not json")
    work = env.tmp / "work" / "ci-fix" / bad.name
    work.mkdir(parents=True)
    env.runner().once()
    assert not bad.exists() and not work.exists()


def test_the_final_report_is_retried_until_the_control_plane_answers(env):
    fix = env.ask()
    env.runner().once()
    env.launcher.running[fix] = False
    real_state = env.cp.state

    def flaky(fix_id, state, **fields):
        if state == "no_change":
            raise runner.Failure("control plane unavailable: refused")
        return real_state(fix_id, state, **fields)
    env.cp.state = flaky
    env.runner().once()
    record = env.tmp / "state" / "fixes" / fix / "state.json"
    assert record.exists() and json.loads(record.read_text())["phase"] == "ended"
    assert not (env.tmp / "work" / "ci-fix" / fix).exists()                    # resources are freed regardless
    assert env.service.get(fix)["state"] == "finishing"                          # the final state is not out yet
    env.cp.state = real_state
    env.runner().once()
    assert env.service.get(fix)["state"] == "no_change" and not record.exists()


def test_a_final_report_the_control_plane_refuses_is_dropped(env):
    fix = env.ask()
    env.runner().once()
    env.launcher.running[fix] = False
    env.service.cancel(fix)                                                    # flagged
    env.service.set_state(fix, "cancelled")                                    # ended before the runner reports
    env.runner().once()
    assert env.service.get(fix)["state"] == "cancelled" and env.leftovers() == []


# ------------------------------------------------------------------ git and secrets
def test_git_runs_without_user_config_prompts_or_a_token_on_the_command_line(tmp_path, monkeypatch):
    token = tmp_path / "token"
    token.write_text("ghp_SECRET_VALUE\n")
    token.chmod(0o600)
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"], seen["env"] = argv, kwargs["env"]
        return subprocess.CompletedProcess(argv, 0, "", "")
    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    runner.Git().run(["fetch", "https://github.com/org/app.git"], credential_file=token)
    joined = " ".join(seen["argv"])
    assert "ghp_SECRET_VALUE" not in joined and str(token) in joined          # the helper reads the FILE
    assert seen["env"]["GIT_CONFIG_GLOBAL"] == "/dev/null" and seen["env"]["GIT_CONFIG_SYSTEM"] == "/dev/null"
    assert seen["env"]["GIT_TERMINAL_PROMPT"] == "0"
    assert not any(key.startswith("GIT_") and key not in {
        "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_TERMINAL_PROMPT", "GIT_ASKPASS"} for key in seen["env"])


def test_a_global_url_rewrite_cannot_redirect_the_fetch(tmp_path, forge, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".gitconfig").write_text(f'[url "ssh://git@nowhere.invalid/"]\n\tinsteadOf = {forge.path}\n')
    monkeypatch.setenv("HOME", str(home))
    mirror = tmp_path / "m.git"
    git_port = runner.Git()
    git_port.ensure_mirror(mirror)
    git_port.fetch(mirror, str(forge.path), ["+refs/heads/*:refs/heads/*"])
    assert git_port.has_commit(mirror, forge.shas[0])


def test_the_api_key_file_must_be_private(tmp_path):
    key = tmp_path / "key"
    key.write_text("secret\n")
    key.chmod(0o644)
    plane = runner.HttpControlPlane("http://127.0.0.1:1", key)
    with pytest.raises(runner.Failure, match="must not be readable"):
        plane.config()
    key.chmod(0o600)
    with pytest.raises(runner.Failure, match="control plane unavailable"):
        plane.config()


# ------------------------------------------------------------------ over HTTP, end to end
def test_the_runner_works_against_the_real_http_api(tmp_path, forge):
    server = ControlPlaneServer(ControlPlaneRepo(), api_keys={"op": "operator", "runner-key": "agent"},
                                ci_fix={**CONFIG, "hosts": ["bender"]})
    server.ci_monitor._factory = lambda: type("P", (Provider,), {"revision": forge.shas[0]})()
    thread = threading.Thread(target=server.serve, daemon=True)
    thread.start()
    server.wait_until_ready()
    try:
        key = tmp_path / "agent.key"
        key.write_text("runner-key\n")
        key.chmod(0o600)
        import urllib.request
        base = f"http://127.0.0.1:{server.port}"
        request = urllib.request.Request(
            f"{base}/api/v1/ci/fix", method="POST", headers={"X-API-Key": "op", "Content-Type": "application/json"},
            data=json.dumps({"repository": "17", "number": 12, "operation_id": "o"}).encode())
        with urllib.request.urlopen(request, timeout=5) as response:
            fix = json.load(response)["id"]
        launcher = Launcher()
        parts = dict(host_id="bender", state_dir=tmp_path / "state", work_root=tmp_path / "work",
                     mirror_root=tmp_path / "mirrors", control_plane=runner.HttpControlPlane(base, key),
                     git=runner.Git(), forge=LocalForge(forge.path), gateway=Gateway(), launcher=launcher,
                     say=lambda message: None)
        runner.Runner(**parts).once()
        assert server.ci_fix.get(fix)["state"] == "running" and server.ci_fix.get(fix)["branch"].startswith("herdr-ci-fix/12-")
        server.ci_fix.cancel(fix)
        runner.Runner(**parts).once()
        assert server.ci_fix.get(fix)["state"] == "cancelled" and launcher.stopped == [fix]
        assert not (tmp_path / "work" / "ci-fix" / fix).exists()
        assert not (tmp_path / "state" / "fixes" / fix).exists()
    finally:
        server.shutdown()


def test_the_command_line_requires_a_key_file_and_refuses_a_bad_host(capsys):
    with pytest.raises(SystemExit):
        runner.main([])
    with pytest.raises(SystemExit):
        runner.main(["--api-key-file", "/nonexistent", "--url", "http://x", "--host-id", "bad host"])

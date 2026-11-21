"""Per-PR `Opus agent review` through Woodpecker: .woodpecker/opus-review.yaml and scripts/ci/opus-review.sh.

The wrapper is run for real (bash, flock, a real process boundary). Its collaborators sit at the process boundary as
stand-in executables that record what they were called with: a `gh` that answers the fork question, and a stand-in for
always-on-review.py (the script itself is covered by test_always_on_review.py).
"""
from __future__ import annotations

import json
import os
import pwd
import shutil
import stat
import subprocess
import time
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WRAPPER = ROOT / "scripts/ci/opus-review.sh"
WORKFLOW = ROOT / ".woodpecker/opus-review.yaml"
TOKEN = "ghp_OPUSREVIEWTOKEN0123456789"

STAND_IN_REVIEW = r'''#!/usr/bin/env python3
import json, os, sys, time
d = os.path.dirname(os.path.abspath(__file__))
t0 = time.time()
cfg = json.load(open(os.path.join(d, "review.json")))
time.sleep(cfg.get("sleep", 0))
open(os.path.join(d, "review.log"), "a").write(json.dumps({
    "argv": sys.argv[1:], "gh_token": os.environ.get("GH_TOKEN"), "secret_var": os.environ.get("OPUS_REVIEW_GITHUB_TOKEN"),
    "github_token": os.environ.get("GITHUB_TOKEN"), "start": t0, "end": time.time()}) + "\n")
sys.exit(cfg.get("exit", 0))
'''
FAKE_GH = r'''#!/usr/bin/env python3
import json, os, sys
d = os.path.dirname(os.path.abspath(__file__))
open(os.path.join(d, "gh.log"), "a").write(json.dumps({"argv": sys.argv[1:], "gh_token": os.environ.get("GH_TOKEN")}) + "\n")
cfg = json.load(open(os.path.join(d, "gh.json")))
if cfg.get("fail"):
    sys.stderr.write("gh: boom\n"); sys.exit(1)
val = cfg.get("cross", False)
if "--jq" in sys.argv:
    print(val if isinstance(val, str) else str(val).lower())
else:
    print(json.dumps({"isCrossRepository": val}))
'''


class Host:
    """A fake review host: an install tree whose layout mirrors the repo, a fake gh first on PATH, a HOME."""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.tree = tmp / "tree"
        (self.tree / "scripts/ci").mkdir(parents=True)
        (self.tree / "deploy/ci-review").mkdir(parents=True)
        shutil.copy(WRAPPER, self.tree / "scripts/ci/opus-review.sh")
        self.review = self.tree / "deploy/ci-review/always-on-review.py"
        self.review.write_text(STAND_IN_REVIEW)
        self.bin = tmp / "bin"
        self.bin.mkdir()
        (self.bin / "gh").write_text(FAKE_GH)
        (self.bin / "gh").chmod(0o755)
        self.home = tmp / "home"
        (self.home / ".config/herdr-engineering").mkdir(parents=True)
        self.config = self.home / ".config/herdr-engineering/always-on-review.yaml"
        self.config.write_text("post: true\n")
        self.token_file = self.home / ".config/herdr-engineering/opus-review.token"
        self.token_file.write_text(TOKEN + "\n")
        self.token_file.chmod(0o600)
        self.set_review()
        self.set_gh()
        # the installed command is a symlink to the script, as the operator steps say
        self.installed = self.home / ".local/bin/herdr-opus-review"
        self.installed.parent.mkdir(parents=True)
        self.installed.symlink_to(self.tree / "scripts/ci/opus-review.sh")

    def set_review(self, **kw):
        (self.tree / "deploy/ci-review/review.json").write_text(json.dumps(kw))

    def set_gh(self, **kw):
        (self.bin / "gh.json").write_text(json.dumps(kw))

    def env(self, **over):
        e = {"PATH": f"{self.bin}:{os.environ['PATH']}", "HOME": str(self.home), "CI_PIPELINE_EVENT": "pull_request",
             "CI_REPO": "berlinguyinca/other-repo", "CI_COMMIT_PULL_REQUEST": "42"}
        e.update(over)
        return {k: v for k, v in e.items() if v is not None}

    def popen(self, *args, **over):
        home = [] if "--home" in args else ["--home", str(self.home)]
        return subprocess.Popen(["bash", str(self.installed), *home, *args], env=self.env(**over), stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True)

    def run(self, *args, **over):
        p = self.popen(*args, **over)
        out, _ = p.communicate(timeout=60)
        return p.returncode, out

    def calls(self):
        p = self.tree / "deploy/ci-review/review.log"
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


@pytest.fixture
def host(tmp_path):
    return Host(tmp_path)


# ---------------- the wrapper ----------------

def test_the_wrapper_is_executable_bash_and_syntactically_valid():
    assert WRAPPER.stat().st_mode & stat.S_IXUSR
    assert subprocess.run(["bash", "-n", str(WRAPPER)], capture_output=True).returncode == 0


def test_it_reviews_the_pr_of_ci_repo_and_posts(host):
    rc, out = host.run()
    assert rc == 0, out
    [call] = host.calls()
    assert call["argv"] == ["review", "--repo", "berlinguyinca/other-repo", "--pr", "42", "--config", str(host.config),
                            "--host-lock-wait", "1800", "--post"]


def test_the_token_file_becomes_gh_token_for_the_review_and_nothing_else_carries_it(host):
    rc, out = host.run(GITHUB_TOKEN="ghp_AMBIENT", OPUS_REVIEW_GITHUB_TOKEN="ghp_FROMENV")
    assert rc == 0, out
    [call] = host.calls()
    assert call["gh_token"] == TOKEN and call["secret_var"] is None and call["github_token"] is None
    assert TOKEN not in out and "ghp_FROMENV" not in out
    argvs = json.dumps([call["argv"]] + [json.loads(x)["argv"] for x in (host.bin / "gh.log").read_text().splitlines()])
    assert TOKEN not in argvs


def test_whitespace_around_the_token_is_stripped(host):
    host.token_file.write_text(f"  {TOKEN} \r\n\n")
    assert host.run()[0] == 0
    assert host.calls()[0]["gh_token"] == TOKEN


def test_a_group_or_world_readable_token_file_is_refused(host):
    for mode in (0o640, 0o604, 0o644, 0o660, 0o666):
        host.token_file.chmod(mode)
        rc, out = host.run()
        assert rc == 1 and host.calls() == [] and "0600" in out and TOKEN not in out, (oct(mode), out)


def test_a_token_file_owned_by_someone_else_is_refused(host):
    if os.getuid() == 0:
        pytest.skip("cannot construct a foreign owner check as root")
    other = host.tmp / "other.token"
    other.write_text(TOKEN)
    other.chmod(0o600)
    fake = host.bin / "stat"                      # stands in at the process boundary: reports another owner
    real = shutil.which("stat")
    fake.write_text(f'#!/bin/sh\nout=$({real} "$@") || exit $?\ncase "$*" in *%u*) echo 0;; *) echo "$out";; esac\n')
    fake.chmod(0o755)
    rc, out = host.run()
    assert rc == 1 and host.calls() == [] and "not owned" in out.lower() and TOKEN not in out


def test_a_token_file_path_starting_with_a_dash_is_refused(host):
    rc, out = host.run("--token-file", "-evil")
    assert rc == 2 and host.calls() == []


def test_the_token_file_is_checked_and_read_through_one_descriptor():
    text = WRAPPER.read_text()
    assert "exec {tfd}<" in text and "/dev/fd/$tfd" in text and "<&$tfd" in text, "open once, check and read the same fd"


def test_a_token_file_swapped_after_the_check_is_not_read(host):
    """A `stat` shim on PATH replaces the token file with another (0600) one right after reporting its mode. The token
    must still be the one that was opened and checked: reading by path would return the impostor."""
    impostor = host.tmp / "impostor.token"
    impostor.write_text("ghp_IMPOSTOR\n")
    impostor.chmod(0o600)
    real = shutil.which("stat")
    shim = host.bin / "stat"
    shim.write_text(f'#!/bin/sh\n{real} "$@" || exit $?\ncase "$*" in *%a*) mv -f {impostor} {host.token_file};; esac\n')
    shim.chmod(0o755)
    rc, out = host.run()
    assert rc == 0, out
    assert host.token_file.read_text().strip() == "ghp_IMPOSTOR", "the swap really happened"
    assert host.calls()[0]["gh_token"] == TOKEN and "IMPOSTOR" not in out


def test_a_missing_token_file_fails_the_step_with_instructions(host):
    host.token_file.unlink()
    rc, out = host.run()
    assert rc == 1 and host.calls() == [] and "install -m 600" in out and str(host.token_file) in out


def test_an_empty_token_file_is_an_error(host):
    host.token_file.write_text(" \n\t\n")
    rc, out = host.run()
    assert rc == 1 and host.calls() == [] and "empty" in out.lower()


def test_the_token_file_path_can_be_set_by_flag_or_agent_env(host):
    elsewhere = host.tmp / "elsewhere.token"
    elsewhere.write_text("ghp_ELSEWHERE\n")
    elsewhere.chmod(0o600)
    host.token_file.unlink()
    assert host.run("--token-file", str(elsewhere))[0] == 0
    assert host.calls()[-1]["gh_token"] == "ghp_ELSEWHERE"
    assert host.run(OPUS_REVIEW_TOKEN_FILE=str(elsewhere))[0] == 0


def test_the_fork_check_uses_the_token_and_the_pr(host):
    assert host.run()[0] == 0
    gh = [json.loads(x) for x in (host.bin / "gh.log").read_text().splitlines()]
    assert gh[0]["argv"][:3] == ["pr", "view", "42"] and "--repo" in gh[0]["argv"]
    assert gh[0]["argv"][-2:] == ["--jq", ".isCrossRepository"]
    assert "berlinguyinca/other-repo" in gh[0]["argv"] and gh[0]["gh_token"] == TOKEN


def test_a_pr_from_a_fork_is_skipped_cleanly(host):
    host.set_gh(cross=True)
    rc, out = host.run()
    assert rc == 0 and host.calls() == [] and "fork" in out.lower()


def test_when_the_fork_question_cannot_be_answered_nothing_is_reviewed(host):
    host.set_gh(fail=True)
    rc, out = host.run()
    assert rc != 0 and host.calls() == []


def test_an_ambient_github_token_is_never_used_instead_of_the_file(host):
    host.token_file.unlink()
    rc, out = host.run(GH_TOKEN="ghp_AMBIENT", GITHUB_TOKEN="ghp_AMBIENT2")
    assert rc == 1 and host.calls() == []


@pytest.mark.parametrize("answer", ["", "maybe", "True", "null", "false\ntrue", "{}"])
def test_only_the_exact_answer_false_lets_the_review_start(host, answer):
    host.set_gh(cross=answer)
    rc, out = host.run()
    assert rc != 0 and host.calls() == [], out


@pytest.mark.parametrize("over", [{"CI_PIPELINE_EVENT": "push"}, {"CI_PIPELINE_EVENT": "manual"},
                                  {"CI_PIPELINE_EVENT": "cron"}, {"CI_PIPELINE_EVENT": "pull_request_closed"}])
def test_only_a_pull_request_event_is_reviewed(host, over):
    rc, out = host.run(**over)
    assert rc == 0 and host.calls() == []


def test_a_pull_request_metadata_event_is_reviewed_too(host):
    """A draft marked ready arrives as pull_request_metadata, not pull_request."""
    assert host.run(CI_PIPELINE_EVENT="pull_request_metadata")[0] == 0 and len(host.calls()) == 1


@pytest.mark.parametrize("over", [{"CI_REPO": "no-slash"}, {"CI_REPO": "a/b/c"}, {"CI_REPO": "--evil/x"},
                                  {"CI_REPO": "a/b; rm -rf /"}, {"CI_REPO": None}, {"CI_COMMIT_PULL_REQUEST": "x"},
                                  {"CI_COMMIT_PULL_REQUEST": "4 2"}, {"CI_COMMIT_PULL_REQUEST": "-1"},
                                  {"CI_COMMIT_PULL_REQUEST": ""}, {"CI_COMMIT_PULL_REQUEST": None}])
def test_a_malformed_repo_or_pr_number_is_refused(host, over):
    rc, out = host.run(**over)
    assert rc == 2 and host.calls() == []


def test_a_missing_config_is_an_error(host):
    host.config.unlink()
    rc, out = host.run()
    assert rc == 2 and host.calls() == [] and "config" in out.lower()


def test_a_config_can_be_named_on_the_command_line(host, tmp_path):
    other = tmp_path / "other.yaml"
    other.write_text("post: true\n")
    assert host.run("--config", str(other))[0] == 0
    assert host.calls()[0]["argv"][host.calls()[0]["argv"].index("--config") + 1] == str(other)


@pytest.mark.parametrize("code,expected", [(0, 0), (4, 0), (3, 0), (1, 1), (2, 2)])
def test_review_exit_codes(host, code, expected):
    """4 = another run holds the PR or the head is already reviewed; 3 = the head moved and the new push reviews it."""
    host.set_review(exit=code)
    assert host.run()[0] == expected


def test_the_lock_wait_is_passed_on_and_bounded(host):
    assert host.run("--lock-wait", "90")[0] == 0
    argv = host.calls()[0]["argv"]
    assert argv[argv.index("--host-lock-wait") + 1] == "90"
    assert host.run("--lock-wait", "soon")[0] == 2


def test_a_home_replaced_by_the_local_backend_is_not_trusted(host, tmp_path):
    """Woodpecker's local backend sets HOME to a throwaway directory: the config must still be found."""
    isolated = tmp_path / "isolated-home"
    isolated.mkdir()
    rc, out = host.run(HOME=str(isolated), XDG_CONFIG_HOME=str(isolated / ".config"))   # --home stands for the passwd entry
    assert rc == 0, out
    [call] = host.calls()
    assert call["argv"][call["argv"].index("--config") + 1] == str(host.config)


def test_without_an_override_the_home_comes_from_the_passwd_entry(host, tmp_path):
    isolated = tmp_path / "isolated-home"
    isolated.mkdir()
    real = pwd.getpwuid(os.getuid()).pw_dir
    if (Path(real) / ".config/herdr-engineering/always-on-review.yaml").exists():
        pytest.skip("this account has a real config")
    p = subprocess.run(["bash", str(host.installed)], env=host.env(HOME=str(isolated)), capture_output=True, text=True,
                       timeout=60)
    assert p.returncode == 2 and f"{real}/.config/herdr-engineering/always-on-review.yaml" in p.stderr
    assert str(isolated) not in p.stderr and host.calls() == []


def test_the_review_runs_with_the_real_home_so_the_logins_resolve(host, tmp_path):
    host_home_probe = host.tree / "deploy/ci-review/always-on-review.py"
    host_home_probe.write_text(STAND_IN_REVIEW.replace('"github_token":', '"home": os.environ.get("HOME"), "github_token":'))
    isolated = tmp_path / "isolated-home"
    isolated.mkdir()
    assert host.run(HOME=str(isolated))[0] == 0
    assert host.calls()[0]["home"] == str(host.home)


def test_the_script_is_found_next_to_the_install_not_in_the_working_directory(host, tmp_path):
    cwd = tmp_path / "elsewhere"
    cwd.mkdir()
    (cwd / "deploy/ci-review").mkdir(parents=True)
    (cwd / "deploy/ci-review/always-on-review.py").write_text("raise SystemExit(99)\n")
    p = subprocess.run(["bash", str(host.installed), "--token-file", str(host.token_file)], cwd=cwd, env=host.env(), capture_output=True, text=True, timeout=60)
    assert p.returncode == 0 and len(host.calls()) == 1


# ---------------- the workflow ----------------

@pytest.fixture(scope="module")
def wf():
    return yaml.safe_load(WORKFLOW.read_text())


def test_it_triggers_only_on_pull_requests(wf):
    assert wf["when"] == [{"event": ["pull_request", "pull_request_metadata"]}]


def test_it_runs_on_the_review_host_never_the_hive_heavy_pool(wf):
    assert wf["labels"] == {"review-host": "true"}
    assert "hive-heavy" not in WORKFLOW.read_text().replace("never the hive-heavy pool", "")


def test_it_is_capped_to_one_review_at_a_time(wf):
    assert wf["concurrency"]["limit"] == 1 and wf["concurrency"]["group"]
    assert "{{" not in wf["concurrency"]["group"]


def test_it_is_one_step_that_calls_the_installed_wrapper_and_checks_nothing_out(wf):
    assert wf["skip_clone"] is True
    [step] = wf["steps"]
    assert step["image"] == "bash"
    assert step["commands"] == ["/usr/local/bin/herdr-opus-review"], "absolute: the local backend replaces $HOME"
    assert "$HOME" not in WORKFLOW.read_text().replace("replaces $HOME", "")
    assert not any("git " in c or "checkout" in c for c in step["commands"]), "never fetch or run the PR's code"


def test_the_workflow_references_no_secret_so_a_missing_one_cannot_break_pipelines(wf):
    text = WORKFLOW.read_text()
    assert "from_secret" not in text and "secrets" not in wf["steps"][0] and "environment" not in wf["steps"][0]
    assert "ghp_" not in text and "gho_" not in text


def test_the_workflow_stays_tiny(wf):
    assert len([x for x in WORKFLOW.read_text().splitlines() if x.strip() and not x.strip().startswith("#")]) <= 20


def test_the_template_in_the_docs_is_exactly_the_workflow_file():
    doc = (ROOT / "docs/operations/per-pr-opus-review.md").read_text()
    blocks = [b.split("```", 1)[0] for b in doc.split("```yaml\n")[1:]]
    body = "\n".join(x for x in WORKFLOW.read_text().splitlines() if not x.strip().startswith("#")).strip()
    assert any("\n".join(x for x in b.splitlines() if not x.strip().startswith("#")).strip() == body for b in blocks), \
        "docs/operations/per-pr-opus-review.md must show the exact YAML other repos copy"


def test_the_backup_timer_is_hourly_and_the_docs_say_why():
    timer = (ROOT / "deploy/ci-review/herdr-always-on-review.timer").read_text()
    assert "OnUnitActiveSec=1h" in timer and "OnUnitActiveSec=5min" not in timer
    doc = (ROOT / "docs/operations/independent-ai-review.md").read_text()
    assert "missed webhook" in doc and "by hand with" not in doc.split("## Current requirement")[1].split("---")[0]


WOODPECKER_CLI = next((c for c in (shutil.which("woodpecker-cli"), "/tmp/claude-1000/rev910/wp/wcli") if c and Path(c).is_file()), None)


@pytest.mark.skipif(WOODPECKER_CLI is None, reason="woodpecker-cli is not installed")
def test_woodpecker_cli_lints_the_workflow_clean():
    p = subprocess.run([WOODPECKER_CLI, "lint", "--strict", str(WORKFLOW)], capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stdout + p.stderr

"""Tests for the PR freshness guard, the concurrency group and the shared-cache trust decision (PR #712 review).

Drives the real shell scripts with env vars against a real throwaway git remote; nothing under test is mocked.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
FRESHNESS = ROOT / "scripts" / "ci-pipeline-freshness.sh"
TRUST = ROOT / "scripts" / "ci" / "cache-trust.sh"
UNIT_GATES = ROOT / "scripts" / "ci" / "unit-gates.sh"

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e", "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@e", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
}


def git(cwd: Path, *args: str) -> str:
    out = subprocess.run(["git", *args], cwd=cwd, env={**os.environ, **GIT_ENV}, capture_output=True, text=True)
    assert out.returncode == 0, f"git {' '.join(args)}: {out.stderr}"
    return out.stdout.strip()


@pytest.mark.parametrize("name", ["unit-gates.yml", "ui-gates.yml"])
def test_concurrency_group_uses_woodpecker_variables(name: str) -> None:
    text = (ROOT / ".woodpecker" / name).read_text()
    group = yaml.safe_load(text)["concurrency"]["group"]
    assert "{{" not in group, "Woodpecker only substitutes ${VAR}; {{branch}} is a literal shared by every PR"
    assert "${CI_COMMIT_SOURCE_BRANCH}" in group and "${CI_WORKFLOW_NAME}" in group


@pytest.fixture
def remote(tmp_path: Path) -> dict[str, str]:
    """A bare origin with main, a PR branch `feat` (two commits) and refs/pull/7/head at the newer one."""
    bare = tmp_path / "origin.git"
    work = tmp_path / "work"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(bare))
    git(tmp_path, "clone", "-q", str(bare), str(work))
    (work / "a").write_text("1")
    git(work, "add", "a")
    git(work, "commit", "-qm", "one")
    git(work, "push", "-q", "origin", "HEAD:main")
    old = git(work, "rev-parse", "HEAD")
    (work / "a").write_text("2")
    git(work, "commit", "-qam", "two")
    new = git(work, "rev-parse", "HEAD")
    git(work, "push", "-q", "origin", "HEAD:refs/heads/feat", "HEAD:refs/pull/7/head")
    return {"work": str(work), "old": old, "new": new}


def freshness(remote: dict[str, str], **env: str) -> subprocess.CompletedProcess[str]:
    base = {k: v for k, v in os.environ.items() if not k.startswith("CI_")}
    return subprocess.run(["bash", str(FRESHNESS)], cwd=remote["work"], env={**base, **GIT_ENV, **env}, capture_output=True, text=True)


def pr_env(sha: str, **extra: str) -> dict[str, str]:
    return {"CI_PIPELINE_EVENT": "pull_request", "CI_COMMIT_SHA": sha, "CI_COMMIT_PULL_REQUEST": "7",
            "CI_COMMIT_SOURCE_BRANCH": "feat", "CI_COMMIT_BRANCH": "main", **extra}


def test_head_commit_of_pr_runs(remote: dict[str, str]) -> None:
    assert freshness(remote, **pr_env(remote["new"])).returncode == 0


def test_head_commit_runs_even_when_target_branch_is_main(remote: dict[str, str]) -> None:
    # CI_COMMIT_BRANCH=main for a PR; the old script compared against main's head and skipped every PR.
    r = freshness(remote, **pr_env(remote["new"]))
    assert r.returncode == 0, r.stdout + r.stderr


def test_older_commit_is_superseded(remote: dict[str, str]) -> None:
    r = freshness(remote, **pr_env(remote["old"]))
    assert r.returncode == 1 and "superseded" in r.stdout


def test_source_branch_fallback_without_pr_number(remote: dict[str, str]) -> None:
    env = pr_env(remote["old"])
    del env["CI_COMMIT_PULL_REQUEST"]
    assert freshness(remote, **env).returncode == 1
    env["CI_COMMIT_SHA"] = remote["new"]
    assert freshness(remote, **env).returncode == 0


def test_fork_pr_from_branch_named_main_is_not_skipped(remote: dict[str, str]) -> None:
    env = pr_env(remote["new"], CI_COMMIT_SOURCE_BRANCH="main")
    assert freshness(remote, **env).returncode == 0


def test_api_failure_runs(remote: dict[str, str]) -> None:
    git(Path(remote["work"]), "remote", "set-url", "origin", "/nonexistent/origin.git")
    r = freshness(remote, **pr_env(remote["old"]))
    assert r.returncode == 0 and "running the pipeline" in r.stderr


@pytest.mark.parametrize("event", ["cron", "manual", "push"])
def test_other_events_always_run(remote: dict[str, str], event: str) -> None:
    assert freshness(remote, **pr_env(remote["old"], CI_PIPELINE_EVENT=event)).returncode == 0


def trusted(**env: str) -> bool:
    base = {k: v for k, v in os.environ.items() if not k.startswith("CI_")}
    r = subprocess.run(["bash", "-c", f". {TRUST}; herdr_ci_cache_trusted"], env={**base, **env}, capture_output=True, text=True)
    return r.returncode == 0


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({"CI_PIPELINE_EVENT": "push", "CI_COMMIT_BRANCH": "main", "CI_REPO": "o/r"}, True),
        ({"CI_PIPELINE_EVENT": "cron", "CI_COMMIT_BRANCH": "main", "CI_REPO": "o/r"}, True),
        ({"CI_PIPELINE_EVENT": "manual", "CI_COMMIT_BRANCH": "main", "CI_REPO": "o/r"}, True),
        ({"CI_PIPELINE_EVENT": "manual", "CI_COMMIT_BRANCH": "feat", "CI_REPO": "o/r"}, False),
        ({"CI_PIPELINE_EVENT": "pull_request", "CI_COMMIT_BRANCH": "main", "CI_REPO": "o/r"}, False),
        ({"CI_PIPELINE_EVENT": "push", "CI_COMMIT_BRANCH": "feat", "CI_REPO": "o/r"}, False),
        ({"CI_PIPELINE_EVENT": "push", "CI_COMMIT_BRANCH": "main"}, False),
        ({}, False),
    ],
)
def test_cache_trust_decision(env: dict[str, str], expected: bool) -> None:
    assert trusted(**env) is expected


@pytest.mark.parametrize("script", [UNIT_GATES, ROOT / "scripts" / "ci" / "ui-gates.sh"])
def test_gates_use_the_trust_aware_cache_and_flock(script: Path) -> None:
    text = script.read_text()
    assert "need flock" in text
    assert "herdr_ci_cache_setup" in text
    assert "cp -a" not in text and "|| true" not in text.split("herdr_ci_cache_setup")[1].split("venv=")[0]


def cache_setup(tmp_path: Path, **env: str) -> dict[str, str]:
    shared = tmp_path / "shared"
    base = {k: v for k, v in os.environ.items() if not k.startswith("CI_")}
    script = f'. {TRUST}; herdr_ci_cache_setup t 2>/dev/null; echo "$PIP_CACHE_DIR|$PLAYWRIGHT_BROWSERS_PATH|$PLAYWRIGHT_SKIP_BROWSER_GC|$HERDR_CI_PRIVATE_CACHE"'
    out = subprocess.run(["bash", "-c", script], env={**base, "HERDR_CI_CACHE": str(shared), "TMPDIR": str(tmp_path), **env},
                         capture_output=True, text=True, check=True).stdout.strip()
    pip, pw, gc, private = out.split("|")
    assert gc == "1"
    return {"pip": pip, "pw": pw, "private": private, "shared": str(shared)}


def test_pull_request_never_touches_the_shared_cache(tmp_path: Path) -> None:
    got = cache_setup(tmp_path, CI_PIPELINE_EVENT="pull_request", CI_COMMIT_BRANCH="main", CI_REPO="o/r")
    assert got["private"] and got["pw"].startswith(got["private"])
    assert not got["pip"].startswith(got["shared"]) and not Path(got["shared"]).exists()


def test_trusted_event_uses_the_shared_cache(tmp_path: Path) -> None:
    got = cache_setup(tmp_path, CI_PIPELINE_EVENT="push", CI_COMMIT_BRANCH="main", CI_REPO="o/r")
    assert got["private"] == "" and got["pw"] == got["shared"] + "/playwright"

"""Tests for templates/ci-merge-defaults: the repo-agnostic "CI does the merging" template.

Real git repositories and the real script; nothing is mocked. The template script must also stay identical to the reference
implementation in scripts/ci/integrate-main.sh so the two never drift.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "templates" / "ci-merge-defaults"
SCRIPT = TEMPLATE / "integrate-main.sh"
GIT_ENV = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
IDENT = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e"}


def git(cwd: Path, *args: str) -> str:
    out = subprocess.run(["git", *args], cwd=cwd, env={**os.environ, **GIT_ENV, **IDENT}, capture_output=True, text=True)
    assert out.returncode == 0, f"git {' '.join(args)}: {out.stderr}"
    return out.stdout.strip()


def commit(work: Path, path: str, text: str) -> str:
    (work / path).write_text(text)
    git(work, "add", "-A")
    git(work, "commit", "-qm", f"change {path}")
    return git(work, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> dict[str, Path]:
    """Bare origin with `trunk` (not `main`, to prove the target branch is read from CI variables) and a workspace clone."""
    bare, seed = tmp_path / "o.git", tmp_path / "seed"
    git(tmp_path, "init", "-q", "--bare", "-b", "trunk", str(bare))
    git(tmp_path, "clone", "-q", str(bare), str(seed))
    git(seed, "checkout", "-q", "-b", "trunk")
    commit(seed, "a.txt", "a\n")
    git(seed, "push", "-q", "origin", "trunk")
    git(seed, "checkout", "-q", "-b", "feat")
    head = commit(seed, "b.txt", "b\n")
    git(seed, "push", "-q", "origin", "feat")
    git(seed, "checkout", "-q", "trunk")
    ws = tmp_path / "ws"
    ws.mkdir()
    git(ws, "init", "-q", "-b", "feat")
    git(ws, "remote", "add", "origin", f"file://{bare}")
    git(ws, "fetch", "-q", "--no-tags", "origin", head)
    git(ws, "checkout", "-q", "FETCH_HEAD")
    return {"seed": seed, "ws": ws, "head": Path(head)}


def run(ws: Path, head: Path, **env: str) -> subprocess.CompletedProcess[str]:
    base = {k: v for k, v in os.environ.items() if not k.startswith("CI_")}
    e = {**base, **GIT_ENV, "CI_PIPELINE_EVENT": "pull_request", "CI_COMMIT_SHA": str(head), "CI_COMMIT_TARGET_BRANCH": "trunk", **env}
    return subprocess.run(["bash", str(SCRIPT)], cwd=ws, env=e, capture_output=True, text=True)


def test_script_matches_reference_implementation():
    assert SCRIPT.read_text() == (ROOT / "scripts" / "ci" / "integrate-main.sh").read_text()


def test_merges_current_target_branch(repo):
    commit(repo["seed"], "c.txt", "c\n")
    git(repo["seed"], "push", "-q", "origin", "trunk")
    r = run(repo["ws"], repo["head"])
    assert r.returncode == 0, r.stderr
    assert (repo["ws"] / "b.txt").exists() and (repo["ws"] / "c.txt").exists()


def test_conflict_fails_and_names_file(repo):
    seed = repo["seed"]
    commit(seed, "b.txt", "other\n")
    git(seed, "push", "-q", "origin", "trunk")
    r = run(repo["ws"], repo["head"])
    assert r.returncode == 1 and "b.txt" in r.stderr
    assert git(repo["ws"], "rev-parse", "HEAD") == str(repo["head"])


def test_push_event_is_a_no_op(repo):
    commit(repo["seed"], "c.txt", "c\n")
    git(repo["seed"], "push", "-q", "origin", "trunk")
    r = run(repo["ws"], repo["head"], CI_PIPELINE_EVENT="push")
    assert r.returncode == 0 and not (repo["ws"] / "c.txt").exists()


def test_workflow_snippets_are_valid_yaml_with_expected_triggers():
    pr = yaml.safe_load((TEMPLATE / "woodpecker" / "pr-gate.yml").read_text())
    main = yaml.safe_load((TEMPLATE / "woodpecker" / "main-gate.yml").read_text())
    assert any(w.get("event") == "pull_request" for w in pr["when"])
    assert "integrate-main" in [s["name"] for s in pr["steps"]]
    assert main["when"] == [{"event": "push", "branch": "main"}]
    assert main["concurrency"]["limit"] == 1
    assert "integrate-main" not in [s["name"] for s in main["steps"]]


def test_readme_covers_github_settings():
    text = (TEMPLATE / "README.md").read_text()
    for needle in ("strict", "gh pr merge --auto --squash", "allow_auto_merge", "merge=union", "required_status_checks"):
        assert needle in text

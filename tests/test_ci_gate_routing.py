"""Tests for CI gate routing: when the unit gates run, when they may be skipped, and what proves `main`.

Both halves exist because of issue #719. `main` ran no tests at all - unit-gates triggered on pull_request only, and with a
`when: {path: ...}` filter the pipeline is never even created, so the context could never be required and a PR whose unit-gates
failed merged anyway. The path decision now lives in scripts/ci/unit-gates-applicable.sh (which always reports), and
scripts/ci/main-cron-gates.sh proves main on a schedule without paying a suite per quiet interval.

These drive the real shell scripts against real throwaway git repositories, because the thing being guaranteed is a routing
decision made from git state - a Python-level mock would test the mock.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
APPLICABLE = ROOT / "scripts" / "ci" / "unit-gates-applicable.sh"
CRON = ROOT / "scripts" / "ci" / "main-cron-gates.sh"
UNIT_GATES_YML = (ROOT / ".woodpecker" / "unit-gates.yml").read_text()
CRON_YML = (ROOT / ".woodpecker" / "main-cron.yml").read_text()

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e", "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@e", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
}


def git(repo: Path, *args: str) -> str:
    env = {**os.environ, **GIT_ENV}
    out = subprocess.run(["git", *args], cwd=repo, env=env, capture_output=True, text=True)
    assert out.returncode == 0, f"git {' '.join(args)}: {out.stderr}"
    return out.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repo with a `main` branch and one commit touching every interesting area."""
    r = tmp_path / "repo"
    (r / "herdr_engineering").mkdir(parents=True)
    (r / "tests").mkdir()
    (r / "docs").mkdir()
    (r / "scripts" / "ci").mkdir(parents=True)
    (r / "herdr_engineering" / "cli.py").write_text("x = 1\n")
    (r / "tests" / "test_cli.py").write_text("def test_x():\n    assert True\n")
    (r / "docs" / "readme.md").write_text("hello\n")
    (r / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    git(r, "init", "-q", "-b", "main")
    git(r, "add", "-A")
    git(r, "commit", "-qm", "base")
    return r


def change(repo: Path, path: str, branch: str = "feature") -> str:
    """Commit a one-line change to `path` on `branch`, and return that branch's head sha."""
    if branch != git(repo, "rev-parse", "--abbrev-ref", "HEAD"):
        git(repo, "checkout", "-q", "-b", branch)
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("changed\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", f"touch {path}")
    return git(repo, "rev-parse", "HEAD")


def applicability(repo: Path, sha: str, event: str = "pull_request", base: str = "main") -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "CI_EVENT": event, "CI_COMMIT_SHA": sha, "CI_TARGET_BRANCH": "main",
           "HERDR_APPLICABILITY_BASE": base}
    return subprocess.run(["bash", str(APPLICABLE)], cwd=repo, env=env, capture_output=True, text=True)


# ------------------------------------------------------------------ applicability: what must run ------------------------------------

@pytest.mark.parametrize("path", [
    "herdr_engineering/new_module.py",       # product code
    "herdr_engineering/deep/nested/thing.py",
    "tests/test_new.py",                     # a test nobody has run yet
    "packages/herdr-web-client/app.js",      # client
    "scripts/ci/unit-gates.sh",              # the gate itself
    "scripts/ci/anything-new.sh",            # a new helper must not slip through unproven
    "deploy/control-plane/deploy.sh",        # the deploy scripts the suite exercises
    "pyproject.toml",                        # dependency and pytest config
    ".woodpecker/unit-gates.yml",            # the routing config
])
def test_a_change_that_can_affect_the_suite_runs_it(repo, path):
    result = applicability(repo, change(repo, path))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "running the suite" in result.stdout


# ------------------------------------------------------------------ applicability: what may be skipped --------------------------------

@pytest.mark.parametrize("path", [
    "docs/operations/ci-gates.md",
    "CHANGELOG.md",
    "README.md",
    "docs/herdr_engineering_notes.md",   # contains a covered prefix; must not match on substring
])
def test_a_change_that_cannot_affect_the_suite_is_skipped(repo, path):
    result = applicability(repo, change(repo, path))
    assert result.returncode == 3, (result.stdout, result.stderr)
    assert "skipping the suite" in result.stdout


def test_a_branch_identical_to_its_base_has_nothing_to_prove(repo):
    git(repo, "checkout", "-q", "-b", "no-op")
    result = applicability(repo, git(repo, "rev-parse", "HEAD"))
    assert result.returncode == 3
    assert "identical to the merge base" in result.stdout


# ------------------------------------------------------------------ applicability: never silently green -------------------------

@pytest.mark.parametrize("event", ["push", "cron", "tag", "deployment"])
def test_events_that_are_already_a_request_to_prove_something_always_run(repo, event):
    # A docs-only push to main still has to be proven: that is the whole point of #719.
    result = applicability(repo, change(repo, "docs/whatever.md"), event=event)
    assert result.returncode == 0, result.stdout + result.stderr


def test_a_base_that_cannot_be_resolved_runs_the_suite_instead_of_guessing(repo):
    result = applicability(repo, git(repo, "rev-parse", "HEAD"), base="origin/no-such-branch")
    assert result.returncode == 0, "a guard that cannot work out the diff must not hand back a green"
    assert "fail open" in result.stderr


def test_the_decision_uses_the_merge_base_not_the_branch_tip(repo):
    """main moved after the branch was cut; that must not make an unrelated branch look applicable."""
    change(repo, "docs/only-docs.md", branch="feature")
    tip = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", "main")
    change(repo, "herdr_engineering/from_main.py", branch="main")   # main moved, on a covered path
    git(repo, "checkout", "-q", "feature")
    result = applicability(repo, tip)
    assert result.returncode == 3, "the base's own changes are not this PR's responsibility"


# ------------------------------------------------------------------ the cron proof ------------------------------------------

@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A minimal checkout whose scripts/ci/unit-gates.sh is a stub, so the cron logic can be tested without the suite."""
    root = tmp_path / "tree"
    (root / "scripts" / "ci").mkdir(parents=True)
    shutil.copy(CRON, root / "scripts" / "ci" / "main-cron-gates.sh")
    stub = root / "scripts" / "ci" / "unit-gates.sh"
    stub.write_text('#!/bin/bash\necho ran >> "$RUN_LOG"\nexit "${FAKE_GATE_EXIT:-0}"\n')
    stub.chmod(0o755)
    (root / "README.md").write_text("x\n")
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "base")
    return root


def run_cron(tree: Path, state: Path, ran: Path, gate_exit: str = "0") -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "HERDR_CI_STATE": str(state), "RUN_LOG": str(ran), "FAKE_GATE_EXIT": gate_exit}
    return subprocess.run(["bash", str(tree / "scripts" / "ci" / "main-cron-gates.sh")],
                          cwd=tree, env=env, capture_output=True, text=True)


def test_an_unproven_commit_on_main_is_proven(tree, tmp_path):
    state, ran = tmp_path / "state", tmp_path / "ran"
    result = run_cron(tree, state, ran)
    assert result.returncode == 0, result.stderr
    assert ran.exists(), "the gate must actually run for a commit with no proof"
    assert "proven green" in result.stdout


def test_a_commit_already_proven_green_is_not_re_run(tree, tmp_path):
    state, ran = tmp_path / "state", tmp_path / "ran"
    assert run_cron(tree, state, ran).returncode == 0
    before = ran.read_text()
    result = run_cron(tree, state, ran)
    assert result.returncode == 0, result.stderr
    assert ran.read_text() == before, "a quiet schedule must not pay for the suite again"
    assert "already proven green" in result.stdout


def test_a_new_commit_on_main_is_proven_even_when_an_older_one_was(tree, tmp_path):
    state, ran = tmp_path / "state", tmp_path / "ran"
    run_cron(tree, state, ran)
    (tree / "README.md").write_text("new commit\n")
    git(tree, "add", "-A")
    git(tree, "commit", "-qm", "second")
    result = run_cron(tree, state, ran)
    assert result.returncode == 0, result.stderr
    assert ran.read_text().count("ran") == 2


def test_a_red_gate_earns_no_memo(tree, tmp_path):
    """Otherwise one failure buys a free pass for every later interval at the same commit."""
    state, ran = tmp_path / "state", tmp_path / "ran"
    result = run_cron(tree, state, ran, gate_exit="1")
    assert result.returncode != 0
    assert not (state / "main-unit-gates-green.txt").exists()


def test_a_red_gate_leaves_the_previous_memo_alone(tree, tmp_path):
    """A failure at a new commit must not discard the proof of the commit before it."""
    state, ran = tmp_path / "state", tmp_path / "ran"
    run_cron(tree, state, ran)
    proven = (state / "main-unit-gates-green.txt").read_text()
    (tree / "README.md").write_text("new commit\n")
    git(tree, "add", "-A")
    git(tree, "commit", "-qm", "second")
    result = run_cron(tree, state, ran, gate_exit="1")
    assert result.returncode != 0
    assert (state / "main-unit-gates-green.txt").read_text() == proven


def test_a_commit_already_proven_green_short_circuits_before_the_gate(tree, tmp_path):
    """The memo is the whole economy of the cron, so it has to be consulted before any install work."""
    state, ran = tmp_path / "state", tmp_path / "ran"
    run_cron(tree, state, ran)
    assert run_cron(tree, state, ran, gate_exit="1").returncode == 0, "a proven commit must not even reach the gate"


def test_a_state_dir_that_cannot_be_written_still_runs_the_gate(tree, tmp_path):
    """Fail-open: no memo means no idea what is proven, and 'unknown' must never mean 'green'."""
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    blocked.chmod(0o500)
    try:
        result = run_cron(tree, blocked / "state", tmp_path / "ran")
    finally:
        blocked.chmod(0o700)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "ran").exists(), "an unreadable memo must not skip the suite"
    assert "not writable" in result.stderr


# ------------------------------------------------------------------ the wiring that makes it count ------------------------------

def test_unit_gates_is_no_longer_path_filtered():
    """The trap this whole change exists to spring: a filtered pipeline never reports, so it can never be required."""
    triggers = yaml.safe_load(UNIT_GATES_YML)["when"]
    assert [t for t in triggers if t.get("event") == "pull_request"], "unit-gates must still trigger on PRs"
    offenders = [t for t in triggers if "path" in t or "include" in t]
    assert not offenders, f"a path filter here re-creates the never-reports problem: {offenders}"


def test_the_skip_is_reported_rather_than_silently_absent():
    assert "unit-gates-applicable.sh" in UNIT_GATES_YML
    assert ".ci-unit-gates-not-applicable" in UNIT_GATES_YML
    assert "nothing to run" in UNIT_GATES_YML, "a skipped run must say why, in the log"


def test_the_gate_script_is_still_reproducible_from_a_clean_checkout():
    assert "bash scripts/ci/unit-gates.sh" in UNIT_GATES_YML


def test_the_cron_pipeline_triggers_on_cron_only():
    events = {t.get("event") for t in yaml.safe_load(CRON_YML)["when"]}
    assert events == {"cron"}, f"a cron pipeline must not double-run PRs or pushes: {events}"
    assert "main-cron-gates.sh" in CRON_YML


def test_both_scripts_are_valid_bash():
    for script in (APPLICABLE, CRON):
        out = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
        assert out.returncode == 0, f"{script.name}: {out.stderr}"

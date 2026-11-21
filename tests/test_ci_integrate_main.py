"""Tests for PR-plus-main CI: scripts/ci/integrate-main.sh, the main-push pipeline, the superseded-main guard, the CHANGELOG union driver.

Real shell scripts against real throwaway git repositories (a bare origin plus clones); nothing under test is mocked.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
INTEGRATE = ROOT / "scripts" / "ci" / "integrate-main.sh"
FRESHNESS = ROOT / "scripts" / "ci-pipeline-freshness.sh"
APPLICABLE = ROOT / "scripts" / "ci" / "unit-gates-applicable.sh"
CRON = ROOT / "scripts" / "ci" / "main-cron-gates.sh"
WOODPECKER = ROOT / ".woodpecker"

# No identity at all in the environment: the script must supply its own throwaway one, and never write global config.
GIT_ENV = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
IDENT = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e"}


def git(cwd: Path, *args: str) -> str:
    out = subprocess.run(["git", *args], cwd=cwd, env={**os.environ, **GIT_ENV, **IDENT}, capture_output=True, text=True)
    assert out.returncode == 0, f"git {' '.join(args)}: {out.stderr}"
    return out.stdout.strip()


def commit(work: Path, path: str, text: str, msg: str) -> str:
    target = work / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    git(work, "add", "-A")
    git(work, "commit", "-qm", msg)
    return git(work, "rev-parse", "HEAD")


@pytest.fixture
def origin(tmp_path: Path) -> dict[str, Path]:
    """A bare origin whose main has two files; `ci` is a clone that plays the Woodpecker workspace."""
    bare = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(bare))
    git(tmp_path, "clone", "-q", str(bare), str(seed))
    git(seed, "checkout", "-q", "-b", "main")
    commit(seed, "a.txt", "a\n", "base a")
    commit(seed, "docs/readme.md", "r\n", "base docs")
    git(seed, "push", "-q", "origin", "main")
    return {"bare": bare, "seed": seed, "tmp": tmp_path}


def workspace(origin: dict[str, Path], sha: str, *extra: str) -> Path:
    """What the checkout step does: init, add origin, fetch only the pipeline commit, check it out."""
    ws = origin["tmp"] / "ws"
    ws.mkdir()
    git(ws, "init", "-q", "-b", "feat")
    git(ws, "remote", "add", "origin", f"file://{origin['bare']}")
    git(ws, "fetch", "-q", "--no-tags", *extra, "origin", sha)
    git(ws, "checkout", "-q", "FETCH_HEAD")
    return ws


def pr_branch(origin: dict[str, Path], path: str, text: str) -> str:
    seed = origin["seed"]
    git(seed, "checkout", "-q", "-b", "feat", "main")
    sha = commit(seed, path, text, "pr change")
    git(seed, "push", "-q", "origin", "feat")
    git(seed, "checkout", "-q", "main")
    return sha


def main_moves(origin: dict[str, Path], path: str, text: str) -> str:
    seed = origin["seed"]
    git(seed, "checkout", "-q", "main")
    sha = commit(seed, path, text, "main moves")
    git(seed, "push", "-q", "origin", "main")
    return sha


def run(script: Path, cwd: Path, **env: str) -> subprocess.CompletedProcess[str]:
    base = {k: v for k, v in os.environ.items() if not k.startswith(("CI_", "HERDR_"))}
    return subprocess.run(["bash", str(script)], cwd=cwd, env={**base, **GIT_ENV, **env}, capture_output=True, text=True)


def pr_env(sha: str, **extra: str) -> dict[str, str]:
    return {"CI_PIPELINE_EVENT": "pull_request", "CI_COMMIT_SHA": sha, "CI_COMMIT_SOURCE_BRANCH": "feat",
            "CI_COMMIT_BRANCH": "main", **extra}


# ------------------------------------------------------------------ integrate-main.sh ---------------------------------------------

def test_pr_head_is_merged_with_current_main(origin):
    head = pr_branch(origin, "b.txt", "b\n")
    new_main = main_moves(origin, "c.txt", "c\n")
    ws = workspace(origin, head)
    r = run(INTEGRATE, ws, **pr_env(head))
    assert r.returncode == 0, r.stdout + r.stderr
    assert (ws / "b.txt").exists() and (ws / "c.txt").exists(), "both the PR change and main's newer change must be present"
    assert git(ws, "rev-parse", "HEAD") != head
    assert git(ws, "rev-parse", "HEAD^1") == head and git(ws, "rev-parse", "HEAD^2") == new_main


def test_merge_uses_a_local_identity_and_writes_no_global_config(origin):
    head = pr_branch(origin, "b.txt", "b\n")
    main_moves(origin, "c.txt", "c\n")
    ws = workspace(origin, head)
    home = origin["tmp"] / "home"
    home.mkdir()
    r = run(INTEGRATE, ws, **pr_env(head), HOME=str(home), GIT_CONFIG_GLOBAL=str(home / ".gitconfig"))
    assert r.returncode == 0, r.stdout + r.stderr
    assert not (home / ".gitconfig").exists()
    assert subprocess.run(["git", "config", "--local", "user.name"], cwd=ws, capture_output=True).returncode != 0


def test_already_up_to_date_is_a_no_op(origin):
    head = pr_branch(origin, "b.txt", "b\n")
    ws = workspace(origin, head)
    r = run(INTEGRATE, ws, **pr_env(head))
    assert r.returncode == 0, r.stdout + r.stderr
    assert git(ws, "rev-parse", "HEAD") == head


@pytest.mark.parametrize("event", ["push", "cron", "manual", ""])
def test_other_events_are_a_no_op(origin, event):
    head = pr_branch(origin, "b.txt", "b\n")
    main_moves(origin, "c.txt", "c\n")
    ws = workspace(origin, head)
    r = run(INTEGRATE, ws, **pr_env(head, CI_PIPELINE_EVENT=event))
    assert r.returncode == 0, r.stdout + r.stderr
    assert git(ws, "rev-parse", "HEAD") == head and not (ws / "c.txt").exists()


def test_legacy_ci_event_variable_is_honoured(origin):
    head = pr_branch(origin, "b.txt", "b\n")
    main_moves(origin, "c.txt", "c\n")
    ws = workspace(origin, head)
    env = pr_env(head)
    del env["CI_PIPELINE_EVENT"]
    assert run(INTEGRATE, ws, **env, CI_EVENT="pull_request").returncode == 0
    assert (ws / "c.txt").exists()


def test_conflict_aborts_names_files_and_exits_1(origin):
    head = pr_branch(origin, "a.txt", "pr side\n")
    main_moves(origin, "a.txt", "main side\n")
    ws = workspace(origin, head)
    r = run(INTEGRATE, ws, **pr_env(head))
    assert r.returncode == 1
    out = r.stdout + r.stderr
    assert "a.txt" in out and "rebase or merge main into your branch" in out
    assert git(ws, "rev-parse", "HEAD") == head, "the merge must be aborted, leaving the PR head checked out"
    assert git(ws, "status", "--porcelain") == ""


def test_conflict_message_names_the_actual_target_branch(origin):
    seed = origin["seed"]
    git(seed, "checkout", "-q", "-b", "release", "main")
    commit(seed, "a.txt", "release side\n", "release moves")
    git(seed, "push", "-q", "origin", "release")
    head = pr_branch(origin, "a.txt", "pr side\n")
    ws = workspace(origin, head)
    r = run(INTEGRATE, ws, **pr_env(head, CI_COMMIT_TARGET_BRANCH="release"))
    out = r.stdout + r.stderr
    assert r.returncode == 1
    assert "rebase or merge release into your branch" in out and "merge main" not in out


def test_shallow_checkout_is_deepened_so_the_merge_can_find_a_base(origin):
    pr_branch(origin, "b.txt", "b\n")
    git(origin["seed"], "checkout", "-q", "feat")
    head = commit(origin["seed"], "b2.txt", "b2\n", "pr second")
    git(origin["seed"], "push", "-q", "origin", "feat")
    main_moves(origin, "c.txt", "c\n")
    ws = workspace(origin, head, "--depth=1")
    r = run(INTEGRATE, ws, **pr_env(head))
    assert r.returncode == 0, r.stdout + r.stderr
    assert (ws / "b2.txt").exists() and (ws / "c.txt").exists()


def test_unreachable_origin_fails_the_pipeline(origin):
    head = pr_branch(origin, "b.txt", "b\n")
    ws = workspace(origin, head)
    git(ws, "remote", "set-url", "origin", "/nonexistent/origin.git")
    r = run(INTEGRATE, ws, **pr_env(head))
    assert r.returncode == 1, "proving 'PR + main' is impossible without main; do not hand back a green for the PR alone"


# --------------------------------------------- downstream behaviour with a merge commit as HEAD ---------------------------------

def test_applicability_still_sees_exactly_the_prs_changes_after_the_merge(origin):
    """A docs-only PR stays 'not applicable' even though main meanwhile changed covered paths."""
    head = pr_branch(origin, "docs/new.md", "d\n")
    main_moves(origin, "tests/test_new.py", "def test_x():\n    pass\n")
    ws = workspace(origin, head)
    assert run(INTEGRATE, ws, **pr_env(head)).returncode == 0
    for drop in (False, True):      # Woodpecker's PR head, and a local run that falls back to HEAD (the merge)
        env = pr_env(head)
        if drop:
            env.pop("CI_COMMIT_SHA")
        r = run(APPLICABLE, ws, **env)
        assert r.returncode == 3, r.stdout + r.stderr


def test_applicability_runs_the_suite_for_a_covered_pr_change_after_the_merge(origin):
    head = pr_branch(origin, "tests/test_pr.py", "x\n")
    main_moves(origin, "docs/other.md", "o\n")
    ws = workspace(origin, head)
    assert run(INTEGRATE, ws, **pr_env(head)).returncode == 0
    for drop in (False, True):
        env = pr_env(head)
        if drop:
            env.pop("CI_COMMIT_SHA")
        assert run(APPLICABLE, ws, **env).returncode == 0


def test_freshness_is_independent_of_the_merge(origin):
    """CI_COMMIT_SHA stays the PR head after the merge, so the freshness comparison is the same before and after it."""
    head = pr_branch(origin, "b.txt", "b\n")
    main_moves(origin, "c.txt", "c\n")
    git(origin["seed"], "push", "-q", "origin", f"{head}:refs/pull/7/head")
    ws = workspace(origin, head)
    env = pr_env(head, CI_COMMIT_PULL_REQUEST="7")
    before = run(FRESHNESS, ws, **env)
    assert run(INTEGRATE, ws, **env).returncode == 0
    after = run(FRESHNESS, ws, **env)
    assert before.returncode == after.returncode == 0


# ------------------------------------------------------------------ pipelines ------------------------------------------------------

def steps(name: str) -> list[dict]:
    return yaml.safe_load((WOODPECKER / name).read_text())["steps"]


@pytest.mark.parametrize("name", ["unit-gates.yml", "ui-gates.yml", "artifact-handoff.yml", "structural-gates.yml"])
def test_pr_pipelines_integrate_main_right_after_checkout_and_freshness(name):
    steps_ = steps(name)
    names = [s["name"] for s in steps_]
    assert names[0] == "checkout"
    text = "\n".join(c for s in steps_ if s["name"] == "integrate-main" for c in s["commands"])
    assert "scripts/ci/integrate-main.sh" in text
    after_checkout = names.index("integrate-main")
    if "freshness" in names:
        assert names.index("freshness") < after_checkout, "a superseded pipeline must stop before it spends a fetch and a merge"
    for later in names[after_checkout + 1:]:
        assert later not in ("checkout", "freshness")
    assert after_checkout <= 2


def test_artifact_handoff_and_ui_gates_fetch_full_history_for_the_merge():
    for name in ("artifact-handoff.yml", "ui-gates.yml"):
        checkout = "\n".join(next(s for s in steps(name) if s["name"] == "checkout")["commands"])
        assert "--depth" not in checkout, f"{name}: a shallow checkout has no merge base with main"


def test_main_push_pipeline_runs_the_full_suite():
    doc = yaml.safe_load((WOODPECKER / "unit-gates-main.yml").read_text())
    assert doc["when"] == [{"event": "push", "branch": "main"}]
    assert doc["labels"] == {"cluster": "hive-heavy"}
    assert doc["concurrency"]["limit"] == 1 and doc["concurrency"]["group"] == "unit-gates-main"
    text = "\n".join(c for s in doc["steps"] for c in s["commands"])
    assert "scripts/ci-pipeline-freshness.sh" in text and "scripts/ci/unit-gates.sh" in text
    assert "integrate-main" not in text and "applicability" not in text, "main is tested as pushed, never skipped"
    names = [s["name"] for s in doc["steps"]]
    assert names.index("freshness") < names.index("unit-gates")


# ------------------------------------------------------------------ superseded main push --------------------------------------------

SUPERSEDED_MARKER = ".ci-main-superseded"


def test_superseded_main_push_pipeline_exits_0_with_a_marker(origin):
    """Exit 0, not 1: a red superseded run on main would page 'CI failed - main' until the newer head's run finishes."""
    old = git(origin["seed"], "rev-parse", "main")
    new = main_moves(origin, "c.txt", "c\n")
    ws = workspace(origin, old)
    r = run(FRESHNESS, ws, CI_PIPELINE_EVENT="push", CI_COMMIT_SHA=old, CI_COMMIT_BRANCH="main")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "superseded by newer push" in r.stdout and new in r.stdout
    assert (ws / SUPERSEDED_MARKER).exists()


def test_current_main_push_leaves_no_marker_and_clears_a_stale_one(origin):
    head = git(origin["seed"], "rev-parse", "main")
    ws = workspace(origin, head)
    (ws / SUPERSEDED_MARKER).write_text("stale")
    assert run(FRESHNESS, ws, CI_PIPELINE_EVENT="push", CI_COMMIT_SHA=head, CI_COMMIT_BRANCH="main").returncode == 0
    assert not (ws / SUPERSEDED_MARKER).exists()


def test_superseded_pull_request_still_exits_1_without_a_marker(origin):
    old = git(origin["seed"], "rev-parse", "main")
    ws = workspace(origin, old)
    git(origin["seed"], "checkout", "-q", "-b", "feat2", "main")
    new = commit(origin["seed"], "z.txt", "z\n", "newer")
    git(origin["seed"], "push", "-q", "origin", "feat2")
    r = run(FRESHNESS, ws, CI_PIPELINE_EVENT="pull_request", CI_COMMIT_SHA=old, CI_COMMIT_SOURCE_BRANCH="feat2", CI_COMMIT_BRANCH="main")
    assert r.returncode == 1 and new in r.stdout
    assert not (ws / SUPERSEDED_MARKER).exists()


def test_unit_gates_main_skips_and_never_records_green_on_the_marker(tmp_path):
    steps_by_name = {s["name"]: s for s in steps("unit-gates-main.yml")}
    for marked, expect_run in ((True, False), (False, True)):
        root = tmp_path / f"ws{marked}"
        (root / "scripts" / "ci").mkdir(parents=True)
        log = tmp_path / f"log{marked}"
        for name in ("unit-gates.sh", "main-cron-gates.sh"):
            (root / "scripts" / "ci" / name).write_text(f'#!/bin/bash\necho "{name} $*" >> "{log}"\n')
        if marked:
            (root / SUPERSEDED_MARKER).write_text("x")
        script = "\n".join(steps_by_name["unit-gates"]["commands"])
        r = subprocess.run(["bash", "-c", script], cwd=root, capture_output=True, text=True)
        assert r.returncode == 0, r.stdout + r.stderr
        ran = log.read_text() if log.exists() else ""
        assert ("unit-gates.sh" in ran) is expect_run
        assert ("--record-green" in ran) is expect_run, "a superseded run must never record green"


def test_current_main_push_pipeline_runs(origin):
    head = git(origin["seed"], "rev-parse", "main")
    ws = workspace(origin, head)
    assert run(FRESHNESS, ws, CI_PIPELINE_EVENT="push", CI_COMMIT_SHA=head, CI_COMMIT_BRANCH="main").returncode == 0


def test_push_to_another_branch_is_never_superseded(origin):
    old = git(origin["seed"], "rev-parse", "main")
    main_moves(origin, "c.txt", "c\n")
    ws = workspace(origin, old)
    assert run(FRESHNESS, ws, CI_PIPELINE_EVENT="push", CI_COMMIT_SHA=old, CI_COMMIT_BRANCH="release").returncode == 0


def test_main_push_guard_fails_open_without_a_remote(origin):
    old = git(origin["seed"], "rev-parse", "main")
    ws = workspace(origin, old)
    git(ws, "remote", "set-url", "origin", "/nonexistent/origin.git")
    r = run(FRESHNESS, ws, CI_PIPELINE_EVENT="push", CI_COMMIT_SHA=old, CI_COMMIT_BRANCH="main")
    assert r.returncode == 0 and "running the pipeline" in r.stderr


# ------------------------------------------------------------------ cron memo interplay ---------------------------------------------

def test_a_green_main_push_run_is_recorded_so_the_cron_skips_it(tmp_path):
    root = tmp_path / "tree"
    (root / "scripts" / "ci").mkdir(parents=True)
    shutil.copy(CRON, root / "scripts" / "ci" / "main-cron-gates.sh")
    stub = root / "scripts" / "ci" / "unit-gates.sh"
    stub.write_text('#!/bin/bash\necho ran >> "$RUN_LOG"\n')
    (root / "README.md").write_text("x\n")
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "base")
    state, ran = tmp_path / "state", tmp_path / "ran"
    env = {**os.environ, **GIT_ENV, "HERDR_CI_STATE": str(state), "RUN_LOG": str(ran)}
    rec = subprocess.run(["bash", str(root / "scripts/ci/main-cron-gates.sh"), "--record-green"], cwd=root, env=env, capture_output=True, text=True)
    assert rec.returncode == 0, rec.stderr
    assert not ran.exists(), "recording must never run the suite"
    cron = subprocess.run(["bash", str(root / "scripts/ci/main-cron-gates.sh")], cwd=root, env=env, capture_output=True, text=True)
    assert cron.returncode == 0 and "already proven green" in cron.stdout and not ran.exists()


def test_main_push_pipeline_records_green_only_after_the_suite():
    cmds = [c for s in steps("unit-gates-main.yml") for c in s["commands"]]
    gate = next(i for i, c in enumerate(cmds) if "scripts/ci/unit-gates.sh" in c)
    rec = next(i for i, c in enumerate(cmds) if "--record-green" in c)
    assert rec > gate


# ------------------------------------------------------------------ CHANGELOG union driver ------------------------------------------

def test_gitattributes_keeps_existing_entries_and_adds_the_union_driver():
    text = (ROOT / ".gitattributes").read_text()
    assert "CHANGELOG.md merge=union" in text
    assert "docs/assets/screenshots/*.png binary" in text and "tests/playwright/fonts/*.ttf binary" in text


def test_two_branches_adding_changelog_bullets_merge_cleanly_keeping_both(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    shutil.copy(ROOT / ".gitattributes", repo / ".gitattributes")
    commit(repo, "CHANGELOG.md", "# Changelog\n\n## [Unreleased]\n\n- existing\n\n## [0.1]\n\n- old\n", "base")
    git(repo, "checkout", "-q", "-b", "one")
    commit(repo, "CHANGELOG.md", "# Changelog\n\n## [Unreleased]\n\n- existing\n- from one\n\n## [0.1]\n\n- old\n", "one")
    git(repo, "checkout", "-q", "main")
    git(repo, "checkout", "-q", "-b", "two")
    commit(repo, "CHANGELOG.md", "# Changelog\n\n## [Unreleased]\n\n- existing\n- from two\n\n## [0.1]\n\n- old\n", "two")
    git(repo, "merge", "--no-edit", "one")
    merged = (repo / "CHANGELOG.md").read_text()
    assert "- from one" in merged and "- from two" in merged and "<<<<<<<" not in merged

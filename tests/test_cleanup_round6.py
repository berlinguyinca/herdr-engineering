"""Review round 6 for ``herdr-eng cleanup`` / the reaper. Real git, real ``gh`` (read-only, against
stable old PRs of this repository's public GitHub remote). No mocks, stubs or fakes."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from herdr_engineering.cleanup_command import (
    Refusal,
    cleanup,
    gh_pr_state,
    pr_excuses_unpushed,
    select_pr,
    settle_worktree,
    worktree_quick_facts,
)

from .test_cleanup_command import Env, git
from .test_cleanup_round2 import _user_config, home  # noqa: F401
from .test_cleanup_round5 import _ignoring


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


# --- 1: built-in OS junk is disposable, untracked or ignored ------------------------------------------------

JUNK = (".DS_Store", "._resource", "Thumbs.db", "desktop.ini", "Icon\r")


def test_a_finder_browsed_worktree_is_reapable_with_no_config(env: Env, home: Path) -> None:  # noqa: F811
    wt = env.worktree("browsed")
    for name in JUNK:
        (wt / name).write_text("x")
    (wt / "sub").mkdir()
    (wt / "sub" / ".DS_Store").write_text("x")
    dry = cleanup("worktree", wt, roots=env.roots, dry_run=True)
    assert dry["action"] == "would_remove" and (wt / ".DS_Store").exists()
    assert cleanup("worktree", wt, roots=env.roots)["action"] == "removed"
    assert not wt.exists()


def test_ignored_junk_is_disposable_too(env: Env, home: Path) -> None:  # noqa: F811
    wt = _ignoring(env, "ignoredjunk", ".DS_Store\nThumbs.db\n")
    (wt / ".DS_Store").write_text("x")
    (wt / "Thumbs.db").write_text("x")
    assert cleanup("worktree", wt, roots=env.roots)["action"] == "removed"


def test_junk_never_hides_another_untracked_file(env: Env, home: Path) -> None:  # noqa: F811
    wt = env.worktree("mixed")
    (wt / ".DS_Store").write_text("x")
    (wt / "notes.txt").write_text("keep me")
    with pytest.raises(Refusal, match="dirty"):
        cleanup("worktree", wt, roots=env.roots)
    assert (wt / "notes.txt").exists() and (wt / ".DS_Store").exists()


def test_a_tracked_junk_file_with_edits_is_still_dirty(env: Env, home: Path) -> None:  # noqa: F811
    wt = env.worktree("trackedjunk")
    (wt / ".DS_Store").write_text("v1")
    git(wt, "add", "-f", ".DS_Store")
    git(wt, "commit", "-qm", "oops")
    git(wt, "push", "-q", "origin", "HEAD")
    (wt / ".DS_Store").write_text("v2")
    with pytest.raises(Refusal, match="dirty"):
        cleanup("worktree", wt, roots=env.roots)


def test_junk_named_like_a_directory_prefix_is_not_junk(env: Env, home: Path) -> None:  # noqa: F811
    wt = env.worktree("lookalike")
    (wt / "Thumbs.db.bak").write_text("x")
    with pytest.raises(Refusal, match="dirty"):
        cleanup("worktree", wt, roots=env.roots)


# --- 3: core.excludesFile set in an included config file -----------------------------------------------------

def test_excludes_file_from_an_included_gitconfig_is_honoured(env: Env, home: Path) -> None:  # noqa: F811
    (home / ".gitignore_global").write_text("local.notes\n")
    (home / ".gitconfig.local").write_text("[core]\n\texcludesFile = ~/.gitignore_global\n")
    (home / ".gitconfig").write_text("[include]\n\tpath = ~/.gitconfig.local\n")
    wt = env.worktree("included")
    (wt / "local.notes").write_text("x")
    with pytest.raises(Refusal, match="ignored_files"):  # ignored, as the user's own git says
        cleanup("worktree", wt, roots=env.roots)
    _user_config(home, 'disk_hygiene:\n  rebuildable: ["local.notes"]\n')
    assert cleanup("worktree", wt, roots=env.roots)["action"] == "removed"


# --- 4: porcelain is parsed with -z --------------------------------------------------------------------------

def test_a_non_ascii_rebuildable_directory_is_recognised(env: Env, home: Path) -> None:  # noqa: F811
    wt = _ignoring(env, "accent", "*.egg-info/\n")
    (wt / "café.egg-info").mkdir()
    (wt / "café.egg-info" / "PKG-INFO").write_text("x")
    assert cleanup("worktree", wt, roots=env.roots)["action"] == "removed"


def test_a_nested_repository_in_a_non_ascii_ignored_directory_is_found(env: Env, home: Path) -> None:  # noqa: F811
    _user_config(home, 'disk_hygiene:\n  rebuildable: ["*"]\n')
    wt = _ignoring(env, "vendored", "café-cache/\n")
    clone = wt / "café-cache"
    clone.mkdir()
    subprocess.run(["git", "init", "-q", str(clone)], check=True)
    (clone / "f").write_text("local work")
    git(clone, "add", "f")
    git(clone, "-c", "user.email=a@b", "-c", "user.name=n", "commit", "-qm", "only here")
    with pytest.raises(Refusal, match="ignored_files"):
        cleanup("worktree", wt, roots=env.roots)
    assert (clone / "f").exists()


def test_a_file_name_containing_a_carriage_return_is_exact_not_junk(env: Env, home: Path) -> None:  # noqa: F811
    wt = env.worktree("carriage")
    (wt / "Icon\r.txt").write_text("x")  # not Finder's `Icon\r`
    with pytest.raises(Refusal, match="dirty"):
        cleanup("worktree", wt, roots=env.roots)
    assert (wt / "Icon\r.txt").exists()


def test_untracked_names_with_spaces_and_newlines_make_the_tree_dirty(env: Env, home: Path) -> None:  # noqa: F811
    wt = env.worktree("odd")
    (wt / "a b\nc.txt").write_text("x")
    with pytest.raises(Refusal, match="dirty"):
        cleanup("worktree", wt, roots=env.roots)


# --- 5: git's default XDG ignore file is honoured exactly as the user's git does -------------------------------

def test_the_xdg_ignore_file_is_honoured_like_the_users_git(env: Env, home: Path) -> None:  # noqa: F811
    (home / ".config" / "git").mkdir(parents=True)
    (home / ".config" / "git" / "ignore").write_text("scratch.out\n")
    wt = env.worktree("xdg")
    (wt / "scratch.out").write_text("x")
    with pytest.raises(Refusal, match="ignored_files"):
        cleanup("worktree", wt, roots=env.roots)
    _user_config(home, 'disk_hygiene:\n  rebuildable: ["scratch.out"]\n')
    assert cleanup("worktree", wt, roots=env.roots)["action"] == "removed"


# --- efficiency: local refusals come before any network lookup ------------------------------------------------

def test_ignored_files_are_refused_before_the_pr_lookup_and_unpushed_check(env: Env, home: Path) -> None:  # noqa: F811
    wt = _ignoring(env, "early", ".env\n")
    env.commit(wt)  # unpushed
    (wt / ".env").write_text("S=1\n")
    with pytest.raises(Refusal, match="ignored_files"):
        cleanup("worktree", wt, roots=env.roots, merged_ok=True)
    with pytest.raises(Refusal, match="ignored_files"):
        worktree_quick_facts(wt, ())


def test_the_final_recheck_still_catches_what_changed_after_the_inspection(env: Env, home: Path) -> None:  # noqa: F811
    wt = _ignoring(env, "late", ".env\n")
    facts = worktree_quick_facts(wt, ())
    (wt / ".env").write_text("S=1\n")
    with pytest.raises(Refusal, match="ignored_files"):
        settle_worktree(wt, facts, ())
    (wt / ".env").unlink()
    env.commit(wt, "later.txt")
    with pytest.raises(Refusal, match="changed"):
        settle_worktree(wt, facts, ())


# --- 6: real gh, real HTTP, real stable PRs of this repository's public remote ----------------------------------

REMOTE = "https://github.com/berlinguyinca/herdr-engineering"
MERGED_BRANCH, MERGED_HEAD = "feat/control-plane-live-e2e-test", "6350474fb2a3c05b6ea8449c61a5eeb48d5fa03b"
CLOSED_BRANCH, CLOSED_HEAD = "fix/checkpoint-lock-holder-diagnostics", "ddc8e14399172ea335dd43c6103669943ead7546"

# What `gh pr list --head <branch> --state all --json state,headRefOid,headRefName,headRepositoryOwner,number`
# answered for those two branches (captured from the real calls).
CAPTURED_MERGED = json.loads(
    '[{"headRefName":"feat/control-plane-live-e2e-test","headRefOid":"6350474fb2a3c05b6ea8449c61a5eeb48d5fa03b",'
    '"headRepositoryOwner":{"id":"MDQ6VXNlcjE2MzY0","name":"Gert Wohlgemuth","login":"berlinguyinca"},'
    '"number":1,"state":"MERGED"}]')
CAPTURED_CLOSED = json.loads(
    '[{"headRefName":"fix/checkpoint-lock-holder-diagnostics","headRefOid":"ddc8e14399172ea335dd43c6103669943ead7546",'
    '"headRepositoryOwner":{"id":"MDQ6VXNlcjE2MzY0","name":"Gert Wohlgemuth","login":"berlinguyinca"},'
    '"number":693,"state":"CLOSED"}]')


@pytest.fixture(scope="session")
def real_gh() -> None:
    if shutil.which("gh") is None:
        pytest.skip("gh is not installed: real-gh tests need the GitHub CLI")
    done = subprocess.run(["gh", "api", "repos/berlinguyinca/herdr-engineering", "--jq", ".full_name"],
                          capture_output=True, text=True, timeout=60, check=False)
    if done.returncode != 0:
        pytest.skip(f"gh is not authenticated or github.com is unreachable: {done.stderr.strip()[:120]}")


@pytest.fixture
def github_clone(tmp_path: Path) -> Path:
    """A local git repository whose origin is the real public GitHub remote (gh resolves it from here)."""
    repo = tmp_path / "ghclone"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    git(repo, "remote", "add", "origin", REMOTE)
    return repo


def test_real_gh_still_answers_in_the_shape_the_captured_json_has(real_gh: None, github_clone: Path) -> None:
    for branch, captured in ((MERGED_BRANCH, CAPTURED_MERGED), (CLOSED_BRANCH, CAPTURED_CLOSED)):
        live = subprocess.run(["gh", "pr", "list", "--head", branch, "--state", "all", "--json",
                               "state,headRefOid,headRefName,headRepositoryOwner,number", "--limit", "30"],
                              cwd=github_clone, capture_output=True, text=True, timeout=60, check=True)
        assert json.loads(live.stdout) == captured


def test_gh_pr_state_against_a_real_merged_pr(real_gh: None, github_clone: Path) -> None:
    assert gh_pr_state(github_clone, MERGED_BRANCH, MERGED_HEAD, "berlinguyinca") == \
        {"state": "MERGED", "head": MERGED_HEAD}


def test_gh_pr_state_against_a_real_closed_pr(real_gh: None, github_clone: Path) -> None:
    assert gh_pr_state(github_clone, CLOSED_BRANCH, CLOSED_HEAD, "berlinguyinca") == \
        {"state": "CLOSED", "head": CLOSED_HEAD}


def test_gh_pr_state_ignores_a_pr_whose_head_moved_or_owner_differs(real_gh: None, github_clone: Path) -> None:
    assert gh_pr_state(github_clone, MERGED_BRANCH, "a" * 40, "berlinguyinca") == {"state": "NONE", "head": ""}
    assert gh_pr_state(github_clone, MERGED_BRANCH, MERGED_HEAD, "someone-else") == {"state": "NONE", "head": ""}


def test_real_gh_no_pr_for_the_branch_keeps_unpushed_commits(real_gh: None, env: Env, home: Path) -> None:  # noqa: F811
    wt = env.worktree("nopr")
    env.commit(wt)
    git(env.repo, "remote", "set-url", "origin", REMOTE)  # tracking refs stay; gh now asks real GitHub
    with pytest.raises(Refusal, match="unpushed_commits"):
        cleanup("worktree", wt, roots=env.roots, merged_ok=True)
    assert wt.is_dir()


def test_selection_over_the_captured_json() -> None:
    merged = select_pr(CAPTURED_MERGED, MERGED_BRANCH, MERGED_HEAD, "berlinguyinca")
    closed = select_pr(CAPTURED_CLOSED, CLOSED_BRANCH, CLOSED_HEAD, "berlinguyinca")
    assert merged == {"state": "MERGED", "head": MERGED_HEAD}
    assert closed == {"state": "CLOSED", "head": CLOSED_HEAD}
    assert select_pr(CAPTURED_MERGED, MERGED_BRANCH, "b" * 40, "berlinguyinca")["state"] == "NONE"
    assert select_pr(CAPTURED_MERGED, "other", MERGED_HEAD, "berlinguyinca")["state"] == "NONE"
    assert select_pr(CAPTURED_MERGED, MERGED_BRANCH, MERGED_HEAD, "fork-owner")["state"] == "NONE"


def test_only_a_merged_pr_at_this_head_excuses_unpushed_commits() -> None:
    merged = select_pr(CAPTURED_MERGED, MERGED_BRANCH, MERGED_HEAD, "berlinguyinca")
    closed = select_pr(CAPTURED_CLOSED, CLOSED_BRANCH, CLOSED_HEAD, "berlinguyinca")
    assert pr_excuses_unpushed(merged, MERGED_HEAD)
    assert not pr_excuses_unpushed(merged, "c" * 40)  # HEAD moved since
    assert not pr_excuses_unpushed(closed, CLOSED_HEAD)
    assert not pr_excuses_unpushed({"state": "NONE", "head": ""}, MERGED_HEAD)
    assert not pr_excuses_unpushed(None, MERGED_HEAD)  # gh unknown


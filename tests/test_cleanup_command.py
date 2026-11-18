"""``herdr-eng cleanup``: real git repos, real worktrees, real files. No mocks."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from herdr_engineering.cleanup_command import Refusal, cleanup, default_roots, main


def git(cwd: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True,
                          text=True, env=env).stdout


class Env:
    def __init__(self, base: Path) -> None:
        self.base = base.resolve()
        self.roots = [self.base / "work"]
        self.origin = self.base / "origin.git"
        self.repo = self.roots[0] / "repo"
        self.roots[0].mkdir()
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.origin)], check=True)
        subprocess.run(["git", "clone", "-q", str(self.origin), str(self.repo)], check=True,
                       capture_output=True)
        (self.repo / "a.txt").write_text("a\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "init")
        git(self.repo, "push", "-q", "origin", "HEAD:main")
        git(self.repo, "fetch", "-q", "origin")
        git(self.repo, "branch", "-q", "--set-upstream-to=origin/main")

    def worktree(self, name: str, *, push: bool = True) -> Path:
        path = self.roots[0] / name
        git(self.repo, "worktree", "add", "-q", "-b", name, str(path), "origin/main")
        if push:
            git(path, "push", "-q", "origin", name)
        return path

    def commit(self, wt: Path, name: str = "b.txt") -> None:
        (wt / name).write_text("x\n")
        git(wt, "add", name)
        git(wt, "commit", "-qm", "work")


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


def test_default_roots_cover_agent_owned_dirs(tmp_path: Path) -> None:
    roots = {str(p) for p in default_roots(tmp_path)}
    assert str(tmp_path / "work") in roots
    assert str(tmp_path / "IdeaProjects") in roots
    assert str(tmp_path / ".cache/herdr-engineering") in roots


def test_clean_pushed_worktree_is_removed(env: Env) -> None:
    wt = env.worktree("feat1")
    out = cleanup("worktree", wt, roots=env.roots)
    assert out["action"] == "removed"
    assert not wt.exists()
    assert "feat1" not in git(env.repo, "worktree", "list")


def test_dry_run_changes_nothing(env: Env) -> None:
    wt = env.worktree("feat1")
    out = cleanup("worktree", wt, roots=env.roots, dry_run=True)
    assert out["action"] == "would_remove"
    assert wt.is_dir()


def test_dirty_worktree_refused(env: Env) -> None:
    wt = env.worktree("feat1")
    (wt / "scratch.txt").write_text("unsaved")
    with pytest.raises(Refusal, match="dirty"):
        cleanup("worktree", wt, roots=env.roots)
    assert wt.is_dir()


def test_unpushed_commit_refused(env: Env) -> None:
    wt = env.worktree("feat1")
    env.commit(wt)
    with pytest.raises(Refusal, match="unpushed"):
        cleanup("worktree", wt, roots=env.roots)
    # the flag alone is not enough: the commit is not merged anywhere
    with pytest.raises(Refusal, match="unpushed"):
        cleanup("worktree", wt, roots=env.roots, merged_ok=True)
    assert wt.is_dir()


def test_unpushed_but_merged_elsewhere_needs_flag(env: Env) -> None:
    """A squash/cherry-pick merge leaves local commits off every remote ref."""
    wt = env.worktree("feat1")
    env.commit(wt)
    other = env.base / "other"
    subprocess.run(["git", "clone", "-q", str(env.origin), str(other)], check=True,
                   capture_output=True)
    git(env.repo, "fetch", "-q", "origin")
    git(other, "fetch", "-q", str(wt), "feat1")
    git(other, "cherry-pick", "-x", "FETCH_HEAD")  # -x: distinct sha even within one second
    git(other, "push", "-q", "origin", "HEAD:main")
    git(env.repo, "fetch", "-q", "origin")
    with pytest.raises(Refusal, match="unpushed"):
        cleanup("worktree", wt, roots=env.roots)
    assert cleanup("worktree", wt, roots=env.roots, merged_ok=True)["action"] == "removed"


def test_main_checkout_refused(env: Env) -> None:
    with pytest.raises(Refusal, match="main checkout"):
        cleanup("worktree", env.repo, roots=env.roots)
    assert env.repo.is_dir()


def test_non_worktree_directory_refused(env: Env) -> None:
    plain = env.roots[0] / "plain"
    plain.mkdir()
    with pytest.raises(Refusal, match="not a git worktree"):
        cleanup("worktree", plain, roots=env.roots)


def test_path_outside_roots_refused(env: Env, tmp_path: Path) -> None:
    outside = env.base / "elsewhere"
    outside.mkdir()
    for scope in ("tmp", "target", "worktree"):
        with pytest.raises(Refusal, match="outside"):
            cleanup(scope, outside, roots=env.roots)
    assert outside.is_dir()


def test_root_itself_refused(env: Env) -> None:
    with pytest.raises(Refusal, match="root"):
        cleanup("tmp", env.roots[0], roots=env.roots)


def test_symlink_path_refused_and_target_untouched(env: Env) -> None:
    precious = env.base / "precious"
    precious.mkdir()
    (precious / "keep").write_text("k")
    link = env.roots[0] / "link"
    link.symlink_to(precious)
    with pytest.raises(Refusal, match="symlink"):
        cleanup("tmp", link, roots=env.roots)
    assert (precious / "keep").exists()


def test_symlinked_parent_component_refused(env: Env) -> None:
    precious = env.base / "precious"
    (precious / "inner").mkdir(parents=True)
    (env.roots[0] / "hop").symlink_to(precious)
    with pytest.raises(Refusal, match="symlink"):
        cleanup("tmp", env.roots[0] / "hop" / "inner", roots=env.roots)
    assert (precious / "inner").is_dir()


def test_dotdot_escape_refused(env: Env) -> None:
    victim = env.base / "victim"
    victim.mkdir()
    with pytest.raises(Refusal):
        cleanup("tmp", env.roots[0] / ".." / "victim", roots=env.roots)
    assert victim.is_dir()


def test_foreign_owner_refused() -> None:
    # /usr/bin is root-owned: inside a root, but not ours
    with pytest.raises(Refusal, match="owned"):
        cleanup("tmp", Path("/usr/bin"), roots=[Path("/usr")], dry_run=True)


def test_tmp_scope_removes_tree_with_inner_symlink_not_followed(env: Env) -> None:
    precious = env.base / "precious"
    precious.mkdir()
    (precious / "keep").write_text("k")
    scratch = env.roots[0] / "scratch"
    (scratch / "sub").mkdir(parents=True)
    (scratch / "sub" / "f").write_text("f")
    (scratch / "sub" / "l").symlink_to(precious)
    assert cleanup("tmp", scratch, roots=env.roots)["action"] == "removed"
    assert not scratch.exists()
    assert (precious / "keep").exists()


def test_target_scope_requires_cargo_target(env: Env) -> None:
    proj = env.roots[0] / "proj"
    (proj / "target" / "debug" / ".fingerprint").mkdir(parents=True)
    (proj / "Cargo.toml").write_text("[package]\n")
    (proj / "target" / "debug" / "x.o").write_text("o" * 100)
    assert cleanup("target", proj / "target", roots=env.roots)["action"] == "removed"
    assert not (proj / "target").exists()
    assert (proj / "Cargo.toml").exists()

    notcargo = env.roots[0] / "other" / "target"
    notcargo.mkdir(parents=True)
    with pytest.raises(Refusal, match="cargo target"):
        cleanup("target", notcargo, roots=env.roots)


def test_target_scope_refuses_tracked_target(env: Env) -> None:
    (env.repo / "Cargo.toml").write_text("[package]\n")
    (env.repo / "target" / "debug" / ".fingerprint").mkdir(parents=True)
    (env.repo / "target" / "tracked.txt").write_text("t")
    git(env.repo, "add", "-f", "target/tracked.txt")
    with pytest.raises(Refusal, match="tracked"):
        cleanup("target", env.repo / "target", roots=env.roots)


def test_cli_json_and_exit_codes(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    wt = env.worktree("feat1")
    rc = main(["--scope", "worktree", "--path", str(wt), "--root", str(env.roots[0]),
               "--dry-run", "--json"])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["action"] == "would_remove"
    (wt / "dirty").write_text("x")
    rc = main(["--scope", "worktree", "--path", str(wt), "--root", str(env.roots[0]), "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 1 and out["refused"] == "dirty"


def test_cli_requires_path_for_filesystem_scopes(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--scope", "worktree", "--root", str(env.roots[0]), "--json"]) == 2

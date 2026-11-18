"""``herdr-eng cleanup``: real git repos, real worktrees, real files. No mocks."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from herdr_engineering.cleanup_command import (
    Refusal,
    cleanup,
    configured_roots,
    default_roots,
    main,
)


def git(cwd: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True,
                          text=True, env=env).stdout


class Env:
    def __init__(self, base: Path, root: Path | None = None) -> None:
        self.base = base.resolve()
        self.roots = [root if root is not None else self.base / "work"]
        self.origin = self.base / "origin.git"
        self.repo = self.roots[0] / "repo"
        self.roots[0].mkdir(parents=True)
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


def test_default_roots_are_agent_created_locations_only(tmp_path: Path) -> None:
    roots = {str(p) for p in default_roots(tmp_path)}
    assert str(tmp_path / ".local/share/worktrees") in roots
    assert str(tmp_path / ".cache/herdr-engineering") in roots
    assert not any(r.startswith((str(tmp_path / "IdeaProjects"), str(tmp_path / "work"))) for r in roots)


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
            cleanup(scope, outside, roots=env.roots, temp_roots=env.roots)
    assert outside.is_dir()


def test_root_itself_refused(env: Env) -> None:
    with pytest.raises(Refusal, match="root"):
        cleanup("tmp", env.roots[0], roots=env.roots, temp_roots=env.roots)


def test_symlink_path_refused_and_target_untouched(env: Env) -> None:
    precious = env.base / "precious"
    precious.mkdir()
    (precious / "keep").write_text("k")
    link = env.roots[0] / "link"
    link.symlink_to(precious)
    with pytest.raises(Refusal, match="symlink"):
        cleanup("tmp", link, roots=env.roots, temp_roots=env.roots)
    assert (precious / "keep").exists()


def test_symlinked_parent_component_refused(env: Env) -> None:
    precious = env.base / "precious"
    (precious / "inner").mkdir(parents=True)
    (env.roots[0] / "hop").symlink_to(precious)
    with pytest.raises(Refusal, match="symlink"):
        cleanup("tmp", env.roots[0] / "hop" / "inner", roots=env.roots, temp_roots=env.roots)
    assert (precious / "inner").is_dir()


def test_dotdot_escape_refused(env: Env) -> None:
    victim = env.base / "victim"
    victim.mkdir()
    with pytest.raises(Refusal):
        cleanup("tmp", env.roots[0] / ".." / "victim", roots=env.roots, temp_roots=env.roots)
    assert victim.is_dir()


def test_foreign_owner_refused() -> None:
    # /usr/bin is root-owned: inside a root, but not ours
    with pytest.raises(Refusal, match="owned"):
        cleanup("tmp", Path("/usr/bin"), roots=[], temp_roots=[Path("/usr")], dry_run=True)


def test_tmp_scope_removes_tree_with_inner_symlink_not_followed(env: Env) -> None:
    precious = env.base / "precious"
    precious.mkdir()
    (precious / "keep").write_text("k")
    scratch = env.roots[0] / "scratch"
    (scratch / "sub").mkdir(parents=True)
    (scratch / "sub" / "f").write_text("f")
    (scratch / "sub" / "l").symlink_to(precious)
    assert cleanup("tmp", scratch, roots=env.roots, temp_roots=env.roots)["action"] == "removed"
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


@pytest.fixture
def home_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Env:
    """An Env whose worktrees live in the real default root of a throwaway HOME."""
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(tmp_path)
    return Env(tmp_path, root=home / ".local/share/worktrees")


def test_cli_json_and_exit_codes(home_env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    wt = home_env.worktree("feat1")
    rc = main(["--scope", "worktree", "--path", str(wt), "--dry-run", "--json"])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["action"] == "would_remove"
    (wt / "dirty").write_text("x")
    rc = main(["--scope", "worktree", "--path", str(wt), "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 1 and out["refused"] == "dirty"


def test_cli_requires_path_for_filesystem_scopes(home_env: Env) -> None:
    assert main(["--scope", "worktree", "--json"]) == 2


# --- review round 1 -----------------------------------------------------------------------------

def test_cli_has_no_root_flag(home_env: Env, tmp_path: Path) -> None:
    victim = tmp_path / "Documents" / "keep"
    victim.mkdir(parents=True)
    with pytest.raises(SystemExit):
        main(["--scope", "tmp", "--root", str(tmp_path), "--path", str(victim)])
    assert victim.is_dir()


def test_roots_ignore_env_var_and_repo_local_config(home_env: Env, tmp_path: Path,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    repo_cfg = tmp_path / "herdr-engineering.yaml"
    repo_cfg.write_text(f"disk_hygiene:\n  roots: ['{tmp_path}']\n")
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "herdr-engineering.yaml").write_text(f"disk_hygiene:\n  roots: ['{tmp_path}']\n")
    monkeypatch.setenv("HERDR_CLEANUP_ROOTS", str(tmp_path))
    monkeypatch.setenv("HERDR_ENGINEERING_CONFIG", str(repo_cfg))
    assert tmp_path not in configured_roots()
    assert {str(p) for p in configured_roots()} == {str(p) for p in default_roots()}


def test_roots_come_from_user_config(home_env: Env, tmp_path: Path) -> None:
    cfg = tmp_path / "home" / ".config" / "herdr-engineering" / "config.yaml"
    cfg.parent.mkdir(parents=True)
    extra = tmp_path / "extra-root"
    cfg.write_text(f"disk_hygiene:\n  roots: ['{extra}']\n")
    assert extra in configured_roots()


def test_tmp_scope_refused_under_worktree_roots(env: Env) -> None:
    scratch = env.roots[0] / "scratch"
    scratch.mkdir()
    temp = env.base / "tmproot"
    temp.mkdir()
    with pytest.raises(Refusal, match="outside"):
        cleanup("tmp", scratch, roots=env.roots, temp_roots=[temp])
    assert scratch.is_dir()


def test_tmp_scope_refuses_repo_worktree_and_containers_of_them(env: Env) -> None:
    wt = env.worktree("feat1")
    for victim in (env.repo, wt):
        with pytest.raises(Refusal, match="git repo"):
            cleanup("tmp", victim, roots=env.roots, temp_roots=env.roots)
        assert victim.is_dir()
    holder = env.roots[0] / "holder"
    (holder / "deep").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(holder / "deep" / "r")], check=True)
    with pytest.raises(Refusal, match="git repo"):
        cleanup("tmp", holder, roots=env.roots, temp_roots=env.roots)
    inside = wt / "sub"
    inside.mkdir()
    with pytest.raises(Refusal, match="git repo"):
        cleanup("tmp", inside, roots=env.roots, temp_roots=env.roots)
    assert holder.is_dir() and inside.is_dir()


def test_default_temp_roots_exclude_home_and_worktrees(tmp_path: Path) -> None:
    from herdr_engineering.cleanup_command import default_temp_roots
    assert Path.home() not in default_temp_roots()
    assert all(not str(p).startswith(str(Path.home() / ".local")) for p in default_temp_roots())


def test_target_scope_refused_while_cli_sees_a_held_lease(home_env: Env, tmp_path: Path,
                                                         monkeypatch: pytest.MonkeyPatch,
                                                         capsys: pytest.CaptureFixture[str]) -> None:
    from .test_build_cleanup_leases import hold_lease
    state = tmp_path / "state"
    monkeypatch.setenv("HERDR_ENGINEERING_STATE", str(state))
    proj = home_env.roots[0] / "proj"
    (proj / "target" / "debug" / ".fingerprint").mkdir(parents=True)
    (proj / "Cargo.toml").write_text("[package]\n")
    owner, _, _, _ = hold_lease(state / "build-gates", proj)
    try:
        rc = main(["--scope", "target", "--path", str(proj / "target"), "--json"])
        assert rc == 1 and json.loads(capsys.readouterr().out)["refused"] == "active_lease"
        assert (proj / "target").is_dir()
    finally:
        owner.kill()
        owner.wait()


def _opener(path: Path, mode: str) -> subprocess.Popen[str]:
    """A real process that holds ``path`` open (mode 'file') or has it as cwd (mode 'cwd')."""
    if mode == "cwd":
        proc = subprocess.Popen(["sleep", "60"], cwd=path, stdout=subprocess.DEVNULL, text=True)
        time.sleep(0.5)
        return proc
    code = "import sys,time\nf=open(sys.argv[1]);print('ready',flush=True);time.sleep(60)"
    proc = subprocess.Popen([sys.executable, "-c", code, str(path)], stdout=subprocess.PIPE, text=True)
    assert proc.stdout is not None and proc.stdout.readline().strip() == "ready"
    return proc


@pytest.mark.parametrize("mode", ["cwd", "file"])
def test_busy_check_sees_cwd_and_open_files(env: Env, mode: str) -> None:
    scratch = env.roots[0] / "scratch"
    scratch.mkdir()
    held = scratch / "data.bin"
    held.write_text("x")
    proc = _opener(scratch if mode == "cwd" else held, mode)
    try:
        with pytest.raises(Refusal, match="in_use"):
            cleanup("tmp", scratch, roots=env.roots, temp_roots=env.roots)
        assert held.exists()
    finally:
        proc.kill()
        proc.wait()
    assert cleanup("tmp", scratch, roots=env.roots, temp_roots=env.roots)["action"] == "removed"


def test_busy_check_fails_closed_without_lsof(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from herdr_engineering.cleanup_command import in_use_snapshot
    monkeypatch.setenv("PATH", str(tmp_path))  # no ps/lsof on PATH
    with pytest.raises(Refusal, match="in_use"):
        in_use_snapshot()


def test_select_pr_requires_branch_sha_and_owner() -> None:
    from herdr_engineering.cleanup_command import select_pr
    sha = "a" * 40
    pr = {"number": 7, "state": "MERGED", "headRefName": "feat", "headRefOid": sha,
          "headRepositoryOwner": {"login": "Me"}}
    assert select_pr([pr], "feat", sha, "me")["state"] == "MERGED"
    assert select_pr([pr], "feat", "b" * 40, "me")["state"] == "NONE"   # reused branch name
    assert select_pr([{**pr, "headRepositoryOwner": {"login": "fork"}}], "feat", sha, "me")["state"] == "NONE"
    assert select_pr([pr], "other", sha, "me")["state"] == "NONE"
    assert select_pr([pr], "feat", sha, "")["state"] == "NONE"
    newer = {**pr, "number": 9, "state": "OPEN"}
    assert select_pr([pr, newer], "feat", sha, "me")["state"] == "OPEN"


def test_gh_failure_or_absence_is_unknown_not_none(env: Env, monkeypatch: pytest.MonkeyPatch,
                                                   tmp_path: Path) -> None:
    from herdr_engineering.cleanup_command import gh_pr_state
    monkeypatch.setenv("PATH", str(tmp_path))  # no gh
    assert gh_pr_state(env.repo, "main", "a" * 40, "me") is None
    assert gh_pr_state(env.repo, "main", "a" * 40, "me") is None


def test_ignored_secret_file_blocks_worktree_removal(env: Env) -> None:
    wt = env.worktree("feat1")
    (wt / ".gitignore").write_text(".env\ntarget/\nnode_modules/\n")
    git(wt, "add", ".gitignore")
    git(wt, "commit", "-qm", "ignore")
    git(wt, "push", "-q", "origin", "feat1")
    (wt / "target" / "debug").mkdir(parents=True)
    (wt / "target" / "debug" / "x.o").write_text("o")
    (wt / "node_modules").mkdir()
    (wt / "node_modules" / "m.js").write_text("m")
    assert cleanup("worktree", wt, roots=env.roots, dry_run=True)["action"] == "would_remove"
    (wt / ".env").write_text("SECRET=1\n")
    with pytest.raises(Refusal, match="ignored_files"):
        cleanup("worktree", wt, roots=env.roots)
    assert (wt / ".env").read_text() == "SECRET=1\n"

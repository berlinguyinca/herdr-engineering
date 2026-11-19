"""Review round 2 for ``herdr-eng cleanup`` / the reaper: real git (worktrees, bare repos,
separate git dirs, skip-worktree), real processes, real scratch-directory structures. No mocks."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from herdr_engineering.cleanup_command import (
    Refusal,
    cleanup,
    default_roots,
    default_temp_roots,
    main,
)
from herdr_engineering.disk_hygiene import Policy, find_repos, load_policy, reap_worktrees, run

from .test_cleanup_command import Env, git

SESSION = "0b2c3d4e-1111-4222-8333-444455556666"
UID = os.getuid()


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "home"
    (h / ".config" / "herdr-engineering").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(h))
    return h


def _wt(env: Env, path: Path, branch: str, *, push: bool = True) -> Path:
    git(env.repo, "worktree", "add", "-q", "-b", branch, str(path), "origin/main")
    if push:
        git(path, "push", "-q", "origin", branch)
    return path


def _holder(*args: str, cwd: Path | None = None) -> subprocess.Popen[str]:
    code = "import sys,time;print('ready',flush=True);time.sleep(60)"
    proc = subprocess.Popen([sys.executable, "-c", code, *args], cwd=cwd, stdout=subprocess.PIPE, text=True)
    assert proc.stdout is not None and proc.stdout.readline().strip() == "ready"
    return proc


# --- 1: autospec interop (/tmp/wt-*) ---------------------------------------------------------------

def test_agent_temp_worktrees_are_worktree_roots(env: Env, tmp_path: Path) -> None:
    parent = tmp_path / "tmpparent"
    parent.mkdir()
    wt = _wt(env, parent / "wt-fix-x", "fix-x")
    other = _wt(env, parent / "keepme", "keepme")
    with pytest.raises(Refusal, match="outside_roots"):
        cleanup("worktree", other, roots=env.roots, temp_worktree_parents=[parent])
    assert other.is_dir()
    out = cleanup("worktree", wt, roots=env.roots, temp_worktree_parents=[parent])
    assert out["action"] == "removed" and not wt.exists()


def test_agent_temp_worktree_still_needs_clean_pushed_registered(env: Env, tmp_path: Path) -> None:
    parent = tmp_path / "tmpparent"
    parent.mkdir()
    dirty = _wt(env, parent / "wt-dirty", "dirty")
    (dirty / "x").write_text("x")
    with pytest.raises(Refusal, match="dirty"):
        cleanup("worktree", dirty, roots=env.roots, temp_worktree_parents=[parent])
    unpushed = _wt(env, parent / "wt-unpushed", "unpushed", push=False)
    env.commit(unpushed)
    with pytest.raises(Refusal, match="unpushed"):
        cleanup("worktree", unpushed, roots=env.roots, temp_worktree_parents=[parent])
    plain = parent / "wt-plain"
    plain.mkdir()
    with pytest.raises(Refusal, match="not_a_worktree"):
        cleanup("worktree", plain, roots=env.roots, temp_worktree_parents=[parent])


def test_default_temp_worktree_parent_covers_real_tmp_and_cli_after_cd_slash(
        env: Env, home: Path) -> None:
    """autospec: ``cd / && herdr-eng cleanup --scope worktree --path /tmp/wt-...``. The invoking
    shell's command line names the path and its cwd is ``/``: neither may count as in use."""
    name = f"wt-r2-{uuid.uuid4().hex[:8]}"
    wt = _wt(env, Path("/tmp") / name, name)
    try:
        cmd = (f"cd / && exec {sys.executable} -m herdr_engineering.cli cleanup --scope worktree "
               f"--path {wt} --json")
        done = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, timeout=300,
                              env={**os.environ, "HOME": str(home)})
        assert done.returncode == 0, done.stdout + done.stderr
        assert json.loads(done.stdout)["action"] == "removed" and not wt.exists()
    finally:
        if wt.exists():
            git(env.repo, "worktree", "remove", "--force", str(wt))


def test_worktree_with_a_process_inside_is_in_use(env: Env, tmp_path: Path) -> None:
    parent = tmp_path / "tmpparent"
    parent.mkdir()
    wt = _wt(env, parent / "wt-busy", "busy")
    proc = _holder(cwd=wt)
    try:
        with pytest.raises(Refusal, match="in_use"):
            cleanup("worktree", wt, roots=env.roots, temp_worktree_parents=[parent])
    finally:
        proc.kill()
        proc.wait()


def test_reaper_discovers_agent_temp_worktrees(env: Env, tmp_path: Path) -> None:
    parent = tmp_path / "tmpparent"
    parent.mkdir()
    wt = _wt(env, parent / "wt-reap", "reap")
    out = reap_worktrees(Policy(pr_source="git", min_idle_hours=0, ttl_days=0), extra_repos=[env.repo],
                         extra_roots=[str(env.roots[0])], temp_worktree_parents=[parent],
                         now=time.time() + 86400)
    rows = {Path(w["path"]).name: w for r in out["repos"] for w in r["worktrees"]}
    assert rows["wt-reap"]["action"] == "removed" and not wt.exists()


def _install_state(home: Path):
    from herdr_engineering.workflow_hooks import WorkflowInstaller
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    return WorkflowInstaller(home, command=[sys.executable, "-m", "herdr_engineering.cli"])


def _allow(home: Path) -> list[str]:
    data = json.loads((home / ".claude" / "settings.json").read_text())
    return data.get("permissions", {}).get("allow", [])


def test_allow_rule_added_once_to_a_pre_existing_host_then_removal_is_respected(tmp_path: Path) -> None:
    h = tmp_path / "h"
    inst = _install_state(h)
    inst.install()
    # a host whose hooks were installed before the rule existed: no rule, no marker
    settings = h / ".claude" / "settings.json"
    data = json.loads(settings.read_text())
    del data["permissions"]
    settings.write_text(json.dumps(data))
    for marker in (h / ".local/share/herdr-engineering/workflow").glob("*allow*"):
        marker.unlink()
    assert inst.status().state == "current"
    inst.install()
    assert _allow(h) == ["Bash(herdr-eng cleanup:*)"]
    data = json.loads(settings.read_text())
    del data["permissions"]
    settings.write_text(json.dumps(data))
    inst.install()
    inst.repair()
    assert _allow(h) == []


# --- 2: rebuildable output -------------------------------------------------------------------------

REBUILD = ["target", "node_modules", ".venv", "venv", "__pycache__", ".pytest_cache", ".ruff_cache",
           ".mypy_cache", ".tox", ".nox", "pkg.egg-info", "dist", "build", "coverage", ".coverage",
           "htmlcov", ".gradle", ".next", ".turbo", ".cache"]


def _ignore_and_push(wt: Path, names: list[str]) -> None:
    (wt / ".gitignore").write_text("\n".join(names) + "\n")
    git(wt, "add", ".gitignore")
    git(wt, "commit", "-qm", "ignore")
    git(wt, "push", "-q", "origin", "HEAD")


def test_common_rebuildable_outputs_do_not_block_removal(env: Env) -> None:
    wt = env.worktree("built")
    _ignore_and_push(wt, REBUILD)
    for name in REBUILD:
        if name == ".coverage":
            (wt / name).write_text("x")
        else:
            (wt / name / "sub").mkdir(parents=True)
            (wt / name / "sub" / "f").write_text("x")
    assert cleanup("worktree", wt, roots=env.roots)["action"] == "removed"


@pytest.mark.parametrize("secret", [".env", "data.sqlite", "app.db", "secrets.json"])
def test_other_ignored_files_still_refused(env: Env, secret: str) -> None:
    wt = env.worktree("sec")
    _ignore_and_push(wt, [*REBUILD, secret])
    (wt / "dist").mkdir()
    (wt / "dist" / "f").write_text("x")
    (wt / secret).write_text("s")
    with pytest.raises(Refusal, match="ignored_files"):
        cleanup("worktree", wt, roots=env.roots)
    assert (wt / secret).exists()


# --- 3: hidden index state -------------------------------------------------------------------------

@pytest.mark.parametrize("flag", ["--skip-worktree", "--assume-unchanged"])
def test_skip_worktree_edits_refuse_removal(env: Env, flag: str) -> None:
    wt = env.worktree("hidden")
    (wt / "local.yml").write_text("a: 1\n")
    git(wt, "add", "local.yml")
    git(wt, "commit", "-qm", "cfg")
    git(wt, "push", "-q", "origin", "HEAD")
    git(wt, "update-index", flag, "local.yml")
    (wt / "local.yml").write_text("a: 2  # my local override\n")
    with pytest.raises(Refusal, match="hidden_index"):
        cleanup("worktree", wt, roots=env.roots)
    assert "local override" in (wt / "local.yml").read_text()


# --- 4: Claude scratch root ------------------------------------------------------------------------

def test_default_roots_use_the_scratch_reapers_claude_root(tmp_path: Path) -> None:
    expected = Path(os.path.realpath(f"/tmp/claude-{UID}"))
    assert expected in default_roots(tmp_path)
    assert expected in default_temp_roots()


def _scratch(tmp_path: Path) -> tuple[Path, Path, Path]:
    root = tmp_path / "claude-501"
    session = root / "-Users-me-proj" / SESSION
    (session / "scratchpad").mkdir(parents=True)
    (session / "scratchpad" / "a.txt").write_text("a")
    return root, root / "-Users-me-proj", session


def _scratch_cleanup(tmp_path: Path, target: Path, root: Path, live: Path | None = None) -> dict:
    live_dir = live or tmp_path / "no-sessions"
    live_dir.mkdir(exist_ok=True)
    return cleanup("tmp", target, roots=[], temp_roots=[tmp_path], scratch_root=root, live_roots=[live_dir])


@pytest.mark.parametrize("level", ["root", "project", "session"])
def test_scratch_structure_is_never_deleted(tmp_path: Path, level: str) -> None:
    root, project, session = _scratch(tmp_path)
    target = {"root": root, "project": project, "session": session}[level]
    with pytest.raises(Refusal, match="scratch_structure|is_root"):
        _scratch_cleanup(tmp_path, target, root)
    assert session.is_dir()


def test_scratch_inside_own_session_is_removable(tmp_path: Path, home: Path) -> None:
    from .test_cleanup_round3 import claim_session
    claim_session(home, SESSION)  # an agent cleans up its own session
    root, _, session = _scratch(tmp_path)
    assert _scratch_cleanup(tmp_path, session / "scratchpad" / "a.txt", root)["action"] == "removed"
    assert _scratch_cleanup(tmp_path, session / "scratchpad", root)["action"] == "removed"
    assert session.is_dir()


def test_scratch_live_session_and_keep_marker_honoured(tmp_path: Path) -> None:
    root, _, session = _scratch(tmp_path)
    live = tmp_path / "projects"
    live.mkdir()
    (live / f"{SESSION}.jsonl").write_text("{}")  # fresh transcript: the session is live
    with pytest.raises(Refusal, match="live_session"):
        _scratch_cleanup(tmp_path, session / "scratchpad", root, live)
    old = time.time() - 3 * 86400
    os.utime(live / f"{SESSION}.jsonl", (old, old))
    (session / "scratchpad" / ".keep").write_text("")
    with pytest.raises(Refusal, match="keep"):
        _scratch_cleanup(tmp_path, session / "scratchpad", root, live)
    assert (session / "scratchpad" / "a.txt").exists()


# --- 5: bare repos / separate git dirs -------------------------------------------------------------

def test_tmp_scope_refuses_bare_repo_separate_gitdir_and_containers(tmp_path: Path) -> None:
    bare = tmp_path / "proj.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    sep_work, sep_dir = tmp_path / "sep", tmp_path / "sepgit"
    subprocess.run(["git", "init", "-q", f"--separate-git-dir={sep_dir}", str(sep_work)], check=True)
    box = tmp_path / "box"
    box.mkdir()
    subprocess.run(["git", "init", "-q", "--bare", str(box / "inner.git")], check=True)
    for victim in (bare, sep_work, sep_dir, box):
        with pytest.raises(Refusal, match="git_repository"):
            cleanup("tmp", victim, roots=[], temp_roots=[tmp_path])
        assert victim.exists()
    (bare / "objects" / "marker").write_text("m")
    assert (bare / "objects" / "marker").exists()


def test_tmp_scope_refuses_a_dir_inside_a_bare_repo(tmp_path: Path) -> None:
    bare = tmp_path / "proj.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    with pytest.raises(Refusal, match="git_repository"):
        cleanup("tmp", bare / "refs", roots=[], temp_roots=[tmp_path])


# --- 6: no repo-wide prune -------------------------------------------------------------------------

def _prunable_sibling(env: Env, tmp_path: Path) -> Path:
    away = tmp_path / "external-volume" / "wt"
    git(env.repo, "worktree", "add", "-q", "-b", "external", str(away), "origin/main")
    subprocess.run(["rm", "-rf", str(away)], check=True)  # volume unmounted
    return env.repo / ".git" / "worktrees" / "wt"


def test_cleanup_does_not_prune_unrelated_registrations(env: Env, tmp_path: Path) -> None:
    entry = _prunable_sibling(env, tmp_path)
    assert entry.is_dir()
    cleanup("worktree", env.worktree("mine"), roots=env.roots)
    assert entry.is_dir()


def test_reaper_does_not_prune_registrations_outside_agent_roots(env: Env, tmp_path: Path) -> None:
    entry = _prunable_sibling(env, tmp_path)
    reap_worktrees(Policy(pr_source="git", min_idle_hours=0), extra_repos=[env.repo],
                   extra_roots=[str(env.roots[0])])
    assert entry.is_dir()


# --- 7: robustness ---------------------------------------------------------------------------------

def git_shim(directory: Path, monkeypatch: pytest.MonkeyPatch, before_status: str) -> None:
    """A real ``git`` pass-through earlier on PATH that first runs ``before_status`` (shell) when
    git is asked for ``status``. The hardened runner ignores repo/user config such as
    ``core.fsmonitor``, so slow or racing git is staged at the executable instead."""
    real = shutil.which("git")
    assert real
    directory.mkdir(exist_ok=True)
    shim = directory / "git"
    shim.write_text(f'#!/bin/sh\nfor a in "$@"; do if [ "$a" = status ]; then {before_status}; break; fi; done\n'
                    f'exec {real} "$@"\n')
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{directory}{os.pathsep}{os.environ['PATH']}")


def _hang_on_status(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    git_shim(repo.parent / "slow-shim", monkeypatch, "sleep 4")


def _user_config(home: Path, text: str) -> None:
    (home / ".config" / "herdr-engineering" / "config.yaml").write_text(text)


def test_reaper_survives_git_timeouts_and_still_reports(
        env: Env, home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import herdr_engineering.cleanup_command as cc
    wt = env.worktree("slow")
    ok = env.worktree("fine")
    _hang_on_status(env.repo, monkeypatch)  # every `git status` of the repo's worktrees now takes 4 s
    monkeypatch.setattr(cc, "GIT_TIMEOUT", 1)
    monkeypatch.setenv("HERDR_ENGINEERING_STATE", str(home / "state"))
    _user_config(home, f"disk_hygiene:\n  roots: ['{env.roots[0]}']\n  repos: ['{env.repo}']\n"
                       "  pr_source: git\n  min_idle_hours: 0\n  worktree_ttl_days: 0\n  docker: {enabled: false}\n")
    from herdr_engineering.disk_hygiene import main_args

    class A:
        disk_hygiene_command = "run"
        only = "worktrees"
        dry_run = False
        repo: list[str] = []
        ttl_days = None
        pr_source = None
        json = True
    assert main_args(A()) == 0  # type: ignore[arg-type]
    status = json.loads((home / "state" / "build-gates" / "disk-hygiene.json").read_text())
    rows = [w for r in status["jobs"][0]["repos"] for w in r["worktrees"]]
    assert {Path(w["path"]).name for w in rows} == {wt.name, ok.name}
    assert all(w["action"] == "kept" for w in rows) and wt.exists() and ok.exists()


def test_cli_turns_timeout_into_a_clean_refusal(env: Env, home: Path, monkeypatch: pytest.MonkeyPatch,
                                                capsys: pytest.CaptureFixture[str]) -> None:
    import herdr_engineering.cleanup_command as cc
    wt = env.worktree("slowcli")
    _hang_on_status(env.repo, monkeypatch)
    monkeypatch.setattr(cc, "GIT_TIMEOUT", 1)
    _user_config(home, f"disk_hygiene:\n  roots: ['{env.roots[0]}']\n")
    rc = main(["--scope", "worktree", "--path", str(wt), "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 1 and out["refused"] == "timeout" and wt.exists()


@pytest.mark.skipif(Path("/proc").is_dir(), reason="Linux falls back to the real /proc (tests/test_cleanup_round4.py)")
def test_reaper_survives_missing_process_inspection(env: Env, tmp_path: Path,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    git_bin = Path(subprocess.run(["which", "git"], capture_output=True, text=True).stdout.strip())
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "git").symlink_to(git_bin)
    wt = env.worktree("noinspect")
    monkeypatch.setenv("PATH", str(bindir))  # real git, no ps/lsof: inspection is unavailable
    out = reap_worktrees(Policy(pr_source="git", min_idle_hours=0, ttl_days=0), extra_repos=[env.repo],
                         extra_roots=[str(env.roots[0])], now=time.time() + 86400)
    rows = [w for r in out["repos"] for w in r["worktrees"]]
    assert rows and all(w["action"] == "kept" and w["reason"] == "in_use" for w in rows)
    assert wt.exists()


# --- 8: unreadable config fails closed -------------------------------------------------------------

@pytest.mark.parametrize("text", ["disk_hygiene: [unclosed\n", "version: 99\ndisk_hygiene: {enabled: false}\n"])
def test_unreadable_user_config_disables_destructive_jobs(home: Path, text: str,
                                                          capsys: pytest.CaptureFixture[str]) -> None:
    _user_config(home, text)
    out = run(load_policy())
    assert out["status"] == "config_unreadable"
    rc = main(["--scope", "docker", "--path", "x", "--json"])
    assert rc == 1 and json.loads(capsys.readouterr().out)["refused"] == "config_unreadable"


# --- 9: bare-repo-backed worktrees -----------------------------------------------------------------

def _bare_backed(env: Env, tmp_path: Path) -> tuple[Path, Path]:
    bare = tmp_path / "src" / "proj.git"
    bare.parent.mkdir()
    subprocess.run(["git", "clone", "-q", "--bare", str(env.origin), str(bare)], check=True,
                   capture_output=True)
    git(bare, "config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*")
    wt = env.roots[0] / "bare-wt"
    git(bare, "worktree", "add", "-q", "-b", "feat", str(wt), "main")
    git(wt, "push", "-q", "origin", "feat")
    git(bare, "fetch", "-q", "origin")
    return bare, wt


def test_worktree_of_a_bare_repo_is_removed(env: Env, tmp_path: Path) -> None:
    bare, wt = _bare_backed(env, tmp_path)
    assert cleanup("worktree", wt, roots=env.roots)["action"] == "removed"
    assert not wt.exists() and "bare-wt" not in git(bare, "worktree", "list")


def test_reaper_finds_and_reaps_bare_backed_worktrees(env: Env, tmp_path: Path) -> None:
    bare, wt = _bare_backed(env, tmp_path)
    assert bare in find_repos(env.roots)
    out = reap_worktrees(Policy(pr_source="git", min_idle_hours=0, ttl_days=0), extra_roots=[str(env.roots[0])],
                         now=time.time() + 86400)
    rows = {Path(w["path"]).name: w for r in out["repos"] for w in r["worktrees"]}
    assert rows["bare-wt"]["action"] == "removed" and not wt.exists()


# --- 10: one safety contract (scratch_safety) --------------------------------------------------------

def test_process_whose_arguments_name_the_path_blocks_tmp_removal(tmp_path: Path) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    target = scratch / "data.bin"
    target.write_text("x")
    proc = _holder(str(target))  # path only in argv: not cwd, not an open file
    try:
        with pytest.raises(Refusal, match="in_use"):
            cleanup("tmp", target, roots=[], temp_roots=[tmp_path])
        assert target.exists()
    finally:
        proc.kill()
        proc.wait()

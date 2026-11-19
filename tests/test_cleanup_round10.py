"""Review round 10: HOME is an environment variable too, nested claude-<uid> dirs, unresolved excludes,
reparented ancestry, and a resolved scratch root. Real git, real files, real processes."""
from __future__ import annotations

import json
import os
import pwd
import subprocess
import time
from pathlib import Path

import pytest

from herdr_engineering.build_cleanup import _managed_process
from herdr_engineering.cleanup_command import (
    Refusal,
    _git,
    _guard_scratch,
    _process_started,
    configured_roots,
    default_roots,
    own_sessions,
    reset_excludes_cache,
    user_config_path,
    user_roots,
)
from herdr_engineering.disk_hygiene import Policy, reap_worktrees
from herdr_engineering.scratch_reaper import default_live_roots, default_root, system_temp_roots

from .test_cleanup_command import Env

UID = os.getuid()
SESSION = "11111111-2222-3333-4444-555555555555"


def _passwd_home() -> Path:
    return Path(pwd.getpwuid(UID).pw_dir)


def _attacker_home(tmp_path: Path) -> Path:
    home = tmp_path / "evil-home"
    home.mkdir()
    return home


# --- 1: HOME never decides the config, the roots or the git excludes -------------------------------------

def test_config_and_roots_come_from_the_passwd_home_not_from_home(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, account_home: Path) -> None:
    evil = _attacker_home(tmp_path)
    (evil / ".config" / "herdr-engineering").mkdir(parents=True)
    (evil / ".config" / "herdr-engineering" / "config.yaml").write_text(
        f"disk_hygiene:\n  roots: ['{tmp_path}/victim']\n")
    monkeypatch.setenv("HOME", str(evil))
    assert account_home == _passwd_home()
    assert user_config_path() == _passwd_home() / ".config" / "herdr-engineering" / "config.yaml"
    assert default_roots()[0] == _passwd_home() / ".local" / "share" / "worktrees"
    assert not any(str(root).startswith(str(evil)) for root in default_roots())
    assert tmp_path / "victim" not in configured_roots(user_roots())


def test_git_excludes_never_come_from_home_or_xdg(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, account_home: Path) -> None:
    evil = _attacker_home(tmp_path)
    (evil / ".config" / "git").mkdir(parents=True)
    (evil / ".config" / "git" / "ignore").write_text("r10-secret.dat\n")  # git's default when nothing is set
    (evil / "ignore-all").write_text("r10-secret.dat\n")
    (evil / ".gitconfig").write_text(f"[core]\n\texcludesFile = {evil}/ignore-all\n")
    monkeypatch.setenv("HOME", str(evil))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(evil / ".config"))
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "r10-secret.dat").write_text("real work")
    reset_excludes_cache()
    out = _git(repo, "status", "--porcelain", "--untracked-files=all").stdout
    assert "r10-secret.dat" in out  # the attacker's ignore files did not hide it


# --- 2: HOME never names the sessions or the live-session roots ------------------------------------------

def test_a_forged_session_file_under_home_is_not_ours(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, account_home: Path) -> None:
    evil = _attacker_home(tmp_path)
    (evil / ".claude" / "sessions").mkdir(parents=True)
    parent = os.getppid()
    started = _process_started(parent)
    assert started is not None
    (evil / ".claude" / "sessions" / f"{parent}.json").write_text(
        json.dumps({"pid": parent, "sessionId": SESSION, "startedAt": started * 1000}))
    monkeypatch.setenv("HOME", str(evil))
    assert SESSION not in own_sessions()


def test_live_session_roots_come_from_the_passwd_home(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, account_home: Path) -> None:
    monkeypatch.setenv("HOME", str(_attacker_home(tmp_path)))
    assert all(root.is_relative_to(_passwd_home()) for root in default_live_roots())


# --- 3: any claude-<uid> component under a fixed temp root is guarded -------------------------------------

def _under_fixed_temp(tmp_path: Path) -> Path:
    if not any(tmp_path.is_relative_to(os.path.realpath(root)) for root in system_temp_roots()):
        pytest.skip("pytest's tmp_path is not under a fixed temp root here")
    return tmp_path


def _nested_session(tmp_path: Path) -> Path:
    session = _under_fixed_temp(tmp_path) / "elsewhere" / f"claude-{UID}" / "-proj" / SESSION
    (session / "work").mkdir(parents=True)
    (session / "work" / "f").write_text("x")
    return session


def test_a_foreign_session_below_a_nested_claude_dir_keeps_the_session_rules(tmp_path: Path) -> None:
    session = _nested_session(tmp_path)
    with pytest.raises(Refusal) as refusal:
        _guard_scratch(session / "work" / "f", [Path(f"/tmp/claude-{UID}")], [], time.time())
    assert refusal.value.code == "session_recent"


def test_the_nested_claude_dir_and_its_session_dir_are_not_removable(tmp_path: Path) -> None:
    session = _nested_session(tmp_path)
    for path in (session, session.parents[1]):
        with pytest.raises(Refusal) as refusal:
            _guard_scratch(path, [Path(f"/tmp/claude-{UID}")], [], time.time())
        assert refusal.value.code == "scratch_structure"


# --- 4: an unresolvable core.excludesFile fails closed ---------------------------------------------------

def _repo_with_leftover_xdg_ignore(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, setting: str) -> Path:
    home = tmp_path / "home"
    (home / ".config" / "git").mkdir(parents=True)
    (home / ".config" / "git" / "ignore").write_text("hidden.dat\n")
    (home / ".gitconfig").write_text(f"[core]\n\texcludesFile = {setting}\n")
    monkeypatch.setenv("HOME", str(home))
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "hidden.dat").write_text("work")
    reset_excludes_cache()
    return repo


@pytest.mark.parametrize("setting", ["relative/ignore", "{home}/does-not-exist"])
def test_an_unusable_excludes_file_is_refused_not_replaced_by_the_xdg_one(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, setting: str) -> None:
    repo = _repo_with_leftover_xdg_ignore(tmp_path, monkeypatch, setting.format(home=tmp_path / "home"))
    with pytest.raises(Refusal) as refusal:
        _git(repo, "status", "--porcelain", "--untracked-files=all")
    assert refusal.value.code == "excludes_unresolved"


def test_the_reaper_keeps_a_worktree_whose_excludes_file_is_unresolved(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    (home / ".config" / "git").mkdir(parents=True)
    (home / ".config" / "git" / "ignore").write_text("*.dat\n")
    (home / ".gitconfig").write_text("[core]\n\texcludesFile = relative/ignore\n")
    monkeypatch.setenv("HOME", str(home))
    env = Env(tmp_path, root=home / ".local/share/worktrees")
    wt = env.worktree("done")
    out = reap_worktrees(Policy(pr_source="git", min_idle_hours=0, ttl_days=0), now=time.time() + 86400,
                         temp_worktree_parents=[], scratch_root=tmp_path / "scratch")
    assert [(w["action"], w["reason"]) for r in out["repos"] for w in r["worktrees"]] == \
        [("kept", "excludes_unresolved")]
    assert wt.exists()


# --- 5: an ancestor that vanishes and a retry that finds only init fails closed --------------------------

class _Reparenting(type(Path())):  # type: ignore[misc]
    """A /proc directory whose pid 500 names a parent that has exited on the first read of its
    ``status`` and is a child of init on every later one: the moment Herdr dies under its build."""
    reads = 0

    def read_text(self, *args: object, **kwargs: object) -> str:
        if self.name == "status" and self.parent.name == "500":
            type(self).reads += 1
            return "PPid:\t400\n" if type(self).reads == 1 else "PPid:\t1\n"
        return super().read_text(*args, **kwargs)  # type: ignore[arg-type]


def _proc_with_orphan(tmp_path: Path) -> tuple[Path, Path]:
    proc = tmp_path / "proc"
    for pid, parent in (("500", "400"), ("1", "0")):
        (proc / pid).mkdir(parents=True)
        (proc / pid / "status").write_text(f"PPid:\t{parent}\n")
        (proc / pid / "cmdline").write_bytes(b"sh\0")
    reparenting = _Reparenting(proc)
    return reparenting, reparenting / "500"  # pid 400 never existed: it is the ancestor that exited


def test_a_vanished_ancestor_whose_child_was_reparented_is_unverifiable(tmp_path: Path) -> None:
    proc, child = _proc_with_orphan(tmp_path)
    with pytest.raises(OSError, match="ancestry changed"):
        _managed_process(child, proc)


def test_a_starting_pid_that_is_itself_gone_is_still_not_managed(tmp_path: Path) -> None:
    assert _managed_process(tmp_path / "proc" / "424242", tmp_path / "proc") is False


# --- 6: the scratch root is the validated real path ------------------------------------------------------

def test_the_scratch_root_is_the_real_path_of_what_was_validated(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    base = _under_fixed_temp(tmp_path) / "base"
    (base / f"claude-{UID}").mkdir(parents=True)
    spelled = str(base).removeprefix("/private")  # macOS: /var/... is a symlink ABOVE the temp dir
    monkeypatch.setenv("CLAUDE_CODE_TMPDIR", spelled)
    assert default_root() == Path(os.path.realpath(base)) / f"claude-{UID}"

"""Review round 11: the threat model is accidents plus other local users. Real git, real files."""
from __future__ import annotations

import logging
import os
import subprocess
import time
from pathlib import Path

import pytest

from herdr_engineering.cleanup_command import Refusal, _status, checked_path, cleanup
from herdr_engineering.scratch_reaper import system_temp_roots

UID = os.getuid()
SESSION = "11111111-2222-3333-4444-555555555555"


def _under_fixed_temp(tmp_path: Path) -> Path:
    if not any(tmp_path.is_relative_to(os.path.realpath(root)) for root in system_temp_roots()):
        pytest.skip("pytest's tmp_path is not under a fixed temp root here")
    return tmp_path


# --- 1: a target that CONTAINS a claude-<uid> directory gets the session rules for every session in it -----

def test_a_target_containing_a_recent_foreign_session_is_refused(tmp_path: Path) -> None:
    agents = _under_fixed_temp(tmp_path) / "r11-agents"
    session = agents / f"claude-{UID}" / "-proj" / SESSION
    (session / "work").mkdir(parents=True)
    (session / "work" / "f").write_text("live work")
    with pytest.raises(Refusal) as refusal:
        cleanup("tmp", agents, roots=[], temp_roots=[tmp_path], scratch_root=Path(f"/tmp/claude-{UID}"),
                live_roots=[], now=time.time())
    assert refusal.value.code == "scratch_protected"
    assert (session / "work" / "f").exists()


def test_a_target_containing_only_old_sessions_is_still_refused(tmp_path: Path) -> None:
    agents = _under_fixed_temp(tmp_path) / "r11-old"
    session = agents / f"claude-{UID}" / "-proj" / SESSION
    (session / "work").mkdir(parents=True)
    (session / "work" / "f").write_text("old")
    with pytest.raises(Refusal, match="scratch_protected"):  # one session at a time, never its container
        cleanup("tmp", agents, roots=[], temp_roots=[tmp_path], scratch_root=Path(f"/tmp/claude-{UID}"),
                live_roots=[], now=time.time() + 30 * 86400)
    assert agents.exists()


# --- 3: a symlinked claude-<uid> never becomes a deletion root ---------------------------------------------

def test_a_symlinked_scratch_root_is_dropped_with_a_warning(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    victim = tmp_path / "victim"
    (victim / "precious").mkdir(parents=True)
    root = tmp_path / f"claude-{UID}"
    root.symlink_to(victim)
    with caplog.at_level(logging.WARNING), pytest.raises(Refusal) as refusal:
        checked_path(victim / "precious", [root])
    assert refusal.value.code == "outside_roots"
    assert "ignoring" in caplog.text
    assert (victim / "precious").exists()


# --- 7/9: ignored and untracked are classified exactly as the user's own `git status` does -------------------

def _git_status(repo: Path) -> set[tuple[str, str]]:
    out = subprocess.run(["git", "-C", str(repo), "status", "--porcelain", "-z", "--untracked-files=normal",
                          "--ignored=matching"], capture_output=True, text=True, check=True).stdout
    return {(field[:2], field[3:]) for field in out.split("\0") if field}


@pytest.mark.parametrize("case", ["xdg", "default", "missing", "dev_null", "unset", "git_config_global"])
def test_the_ignored_set_equals_git_status_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str) -> None:
    home = tmp_path / "home"
    (home / ".config" / "git").mkdir(parents=True)
    (home / "xdg" / "git").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("GIT_CONFIG_GLOBAL", raising=False)
    if case == "xdg":
        monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "xdg"))
        (home / "xdg" / "git" / "ignore").write_text("*.dat\n")
    elif case == "default":
        (home / ".config" / "git" / "ignore").write_text("*.dat\n")
    elif case == "git_config_global":  # a dotfiles config with no excludesFile, over a stale ~/.gitconfig
        (home / ".gitconfig").write_text(f"[core]\n\texcludesFile = {home}/stale-ignore\n")
        (home / "stale-ignore").write_text("*.dat\n")
        (home / "dotfiles").write_text("[user]\n\tname = x\n")
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(home / "dotfiles"))
    elif case in ("missing", "dev_null"):
        target = "/dev/null" if case == "dev_null" else f"{home}/no-such-file"
        (home / ".gitconfig").write_text(f"[core]\n\texcludesFile = {target}\n")
        (home / ".config" / "git" / "ignore").write_text("*.dat\n")  # not git's default once excludesFile is set
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    for name in ("a.dat", "b.txt"):
        (repo / name).write_text("x")
    expected = _git_status(repo)
    assert {code for code, _ in expected} <= {"??", "!!"}
    assert set(_status(repo, "--untracked-files=normal", "--ignored=matching") or []) == expected

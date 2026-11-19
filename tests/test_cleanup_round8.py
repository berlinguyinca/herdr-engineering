"""Review round 8 for ``herdr-eng cleanup``: zombies, env-derived roots, per-repo excludes. Real everything."""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from herdr_engineering.build_cleanup import _process_paths
from herdr_engineering.cleanup_command import (
    Refusal,
    _guard_scratch,
    _user_excludes_file,
    default_roots,
    guard_roots,
    reset_excludes_cache,
    scratch_root,
)
from herdr_engineering.scratch_reaper import default_root

UID = os.getuid()
DEFAULT = Path(os.path.realpath(f"/tmp/claude-{UID}"))


# --- 1 + 7: a zombie (exited, not reaped) must not fail the /proc scan --------------------------------------

@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="needs a real Linux /proc")
def test_a_zombie_under_a_herdr_named_parent_does_not_fail_the_scan(tmp_path: Path) -> None:
    herdr = tmp_path / "herdr"
    herdr.symlink_to(sys.executable)  # a process named herdr that never reaps its child
    code = ("import subprocess,time;c=subprocess.Popen(['sleep','60']);c.kill();"
            "print(c.pid,flush=True);time.sleep(60)")
    parent = subprocess.Popen([str(herdr), "-c", code], stdout=subprocess.PIPE, text=True)
    try:
        assert parent.stdout is not None
        child = int(parent.stdout.readline())
        deadline = time.time() + 10
        while time.time() < deadline:
            state = Path(f"/proc/{child}/stat").read_text().rsplit(")", 1)[1].split()[0]
            if state == "Z":
                break
            time.sleep(0.05)
        assert state == "Z"
        _process_paths()  # must skip the zombie, not raise
    finally:
        parent.kill()
        parent.wait()


# --- 2 + 3: an environment variable never widens the deletion roots ---------------------------------------

def _env_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    base = tmp_path / "e"
    base.mkdir()
    monkeypatch.setenv("CLAUDE_CODE_TMPDIR", str(base))
    return base / f"claude-{UID}"


def test_a_symlinked_scratch_root_from_the_environment_is_ignored(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    victim = tmp_path / "victim"
    victim.mkdir()
    _env_root(tmp_path, monkeypatch).symlink_to(victim)
    with caplog.at_level(logging.WARNING):
        assert default_root() == DEFAULT
    assert "CLAUDE_CODE_TMPDIR" in caplog.text
    assert victim not in default_roots(tmp_path)


def test_a_real_owned_root_under_a_temp_dir_is_accepted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _env_root(tmp_path, monkeypatch)
    root.mkdir()
    assert default_root() == root


def test_a_relative_or_missing_environment_root_is_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_CODE_TMPDIR", "relative/dir")
    assert default_root() == DEFAULT
    _env_root(tmp_path, monkeypatch)  # exists as a base, but claude-<uid> was never created
    assert default_root() == DEFAULT


def test_the_guard_covers_the_default_root_and_the_environment_root(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _env_root(tmp_path, monkeypatch)
    root.mkdir()
    roots = guard_roots(scratch_root())
    assert root in roots and DEFAULT in roots


def test_the_guard_checks_every_root(tmp_path: Path) -> None:
    first, second = tmp_path / "a", tmp_path / "b"
    (second / "proj").mkdir(parents=True)
    with pytest.raises(Refusal, match="scratch_structure"):
        _guard_scratch(second / "proj", [first, second], [], time.time())


# --- 4 + 5 + 6: core.excludesFile is resolved per repository, through includes and includeIf ------------

def _repo(path: Path) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return path


def test_excludes_file_is_per_repo_through_an_includeif_in_an_included_file(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    work, other = _repo(tmp_path / "work" / "r1"), _repo(tmp_path / "other" / "r2")
    (home / "work-ignore").write_text("x\n")
    (home / "work.cfg").write_text(f"[core]\n\texcludesFile = {home}/work-ignore\n")
    (home / "inc.cfg").write_text(f'[includeIf "gitdir:{tmp_path}/work/"]\n\tpath = {home}/work.cfg\n')
    (home / ".gitconfig").write_text(f"[include]\n\tpath = {home}/inc.cfg\n")
    reset_excludes_cache()
    assert _user_excludes_file(str(home), str(work)) == f"{home}/work-ignore"
    assert _user_excludes_file(str(home), str(other)) == "/dev/null"


def test_the_xdg_global_config_is_honoured_and_reset_forgets(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    (home / "xdg" / "git").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "xdg"))  # a caller's variable is not consulted
    (home / ".config" / "git").mkdir(parents=True)
    repo = _repo(tmp_path / "r")
    (home / "x-ignore").write_text("x\n")
    (home / ".config" / "git" / "config").write_text(f"[core]\n\texcludesFile = {home}/x-ignore\n")
    reset_excludes_cache()
    assert _user_excludes_file(str(home), str(repo)) == f"{home}/x-ignore"
    (home / ".config" / "git" / "config").write_text("")
    assert _user_excludes_file(str(home), str(repo)) == f"{home}/x-ignore"  # once per run
    reset_excludes_cache()
    assert _user_excludes_file(str(home), str(repo)) == "/dev/null"


def test_no_excludes_anywhere_resolves_to_dev_null(tmp_path: Path) -> None:
    reset_excludes_cache()
    assert _user_excludes_file(str(tmp_path), str(_repo(tmp_path / "r"))) == "/dev/null"
    assert _user_excludes_file(str(tmp_path), str(tmp_path / "r")) == "/dev/null"

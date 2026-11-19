"""Review round 9: fixed temp roots, validated env root, thread-leader zombies, local excludes. Real everything."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from herdr_engineering.build_cleanup import _managed_process, _process_paths
from herdr_engineering.cleanup_command import (
    default_temp_roots,
    guard_roots,
    is_dirty,
)
from herdr_engineering.scratch_reaper import default_root, system_temp_roots

UID = os.getuid()
DEFAULT = Path(os.path.realpath(f"/tmp/claude-{UID}"))


@pytest.fixture
def non_temp() -> Path:
    # /var/tmp is not one of the fixed system temp roots; the checkout cannot serve (CI checks out under /tmp)
    if not os.access("/var/tmp", os.W_OK):
        pytest.skip("needs a writable directory outside the fixed system temp roots")
    base = Path(tempfile.mkdtemp(dir="/var/tmp", prefix="_r9_not_a_temp_dir"))
    (base / f"claude-{UID}").mkdir()
    try:
        yield base
    finally:
        shutil.rmtree(base, ignore_errors=True)


# --- 1: TMPDIR is never a deletion root ------------------------------------------------------------------

def test_tmpdir_does_not_become_a_temp_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TMPDIR", str(tmp_path / "elsewhere"))
    roots = default_temp_roots(Path("/x/claude-1"))
    assert tmp_path / "elsewhere" not in roots
    assert Path("/tmp") in roots and Path("/x/claude-1") in roots
    assert Path("/tmp") in system_temp_roots()


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS per-user temp dir")
def test_macos_user_temp_dir_is_a_fixed_root() -> None:
    expected = subprocess.run(["getconf", "DARWIN_USER_TEMP_DIR"], capture_output=True, text=True,
                              check=True).stdout.strip()
    assert Path(expected) in system_temp_roots()


# --- 2: CLAUDE_CODE_TMPDIR is judged on its resolved path, never against $TMPDIR --------------------------

def test_dotdot_cannot_escape_the_temp_dirs(non_temp: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_CODE_TMPDIR", f"/tmp/../{os.path.relpath(non_temp, '/')}")
    assert default_root() == DEFAULT


def test_tmpdir_cannot_vouch_for_the_claude_root(non_temp: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_CODE_TMPDIR", str(non_temp))
    monkeypatch.setenv("TMPDIR", str(non_temp))
    assert default_root() == DEFAULT


# --- 4: the guard covers claude-<uid> under every fixed temp root ------------------------------------------

def test_guard_roots_cover_every_temp_root() -> None:
    roots = guard_roots(DEFAULT)
    for temp in system_temp_roots():
        assert temp / f"claude-{UID}" in roots


# --- 3 + 6: thread-group leaders and vanishing processes --------------------------------------------------

@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="needs a real Linux /proc")
def test_a_zombie_leader_with_a_live_thread_still_counts_its_cwd(tmp_path: Path) -> None:
    target = tmp_path / "busy"
    target.mkdir()
    code = ("import ctypes,threading,time\n"
            "threading.Thread(target=time.sleep,args=(60,),daemon=False).start()\n"
            "import os;print(os.getpid(),flush=True)\n"
            "ctypes.CDLL(None).pthread_exit(None)\n")
    proc = subprocess.Popen([sys.executable, "-c", code], cwd=target, stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout is not None
        pid = int(proc.stdout.readline())
        deadline = time.time() + 10
        state = ""
        while time.time() < deadline:
            state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
            if state == "Z":
                break
            time.sleep(0.05)
        assert state == "Z"  # leader exited, the other thread still runs
        assert target in {p.resolve() for p in _process_paths()}
    finally:
        proc.kill()
        proc.wait()


def test_a_starting_pid_that_is_already_gone_is_not_managed(tmp_path: Path) -> None:
    assert _managed_process(tmp_path / "424242", tmp_path) is False


# --- 5: a repository's own core.excludesFile outranks the global one -----------------------------------------

def test_a_local_excludes_file_outranks_the_global_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    (home / "global-ignore").write_text("build/\n")
    (home / ".gitconfig").write_text(f"[core]\n\texcludesFile = {home}/global-ignore\n")
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "build").mkdir()
    (repo / "build" / "x").write_text("unversioned work")
    assert not is_dirty(repo)  # the global file ignores build/
    (home / "local-ignore").write_text("")
    subprocess.run(["git", "-C", str(repo), "config", "core.excludesFile", str(home / "local-ignore")], check=True)
    assert is_dirty(repo)  # the repo's own, narrower file wins, as in the user's git


# --- 8: no warning for a root the run does not use -----------------------------------------------------------

def test_an_explicit_root_does_not_resolve_the_environment(non_temp: Path, tmp_path: Path,
                                                           monkeypatch: pytest.MonkeyPatch,
                                                           caplog: pytest.LogCaptureFixture) -> None:
    from herdr_engineering.scratch_reaper import main
    monkeypatch.setenv("CLAUDE_CODE_TMPDIR", str(non_temp))
    monkeypatch.setenv("HOME", str(tmp_path))
    root = tmp_path / f"claude-{UID}"
    root.mkdir()
    main(["--root", str(root), "--dry-run"])
    assert "ignoring CLAUDE_CODE_TMPDIR" not in caplog.text

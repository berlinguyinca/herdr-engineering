"""Review round 8 for ``herdr-eng cleanup``: zombies and env-derived roots. Real everything."""
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
    default_roots,
    guard_roots,
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
    with pytest.raises(Refusal, match="scratch_protected"):
        _guard_scratch(second / "proj", [first, second], [], time.time())

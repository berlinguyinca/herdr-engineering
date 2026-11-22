"""Review round 10 (kept parts): nested claude-<uid> dirs, ancestry that changed, a resolved scratch root.
Real git, real files, real processes."""
from __future__ import annotations

import os
import time
import uuid
from pathlib import Path

import pytest

from herdr_engineering.build_cleanup import _managed_process
from herdr_engineering.cleanup_command import Refusal, _guard_scratch
from herdr_engineering.scratch_reaper import default_root, system_temp_roots

from .test_cleanup_round2 import home  # noqa: F401

UID = os.getuid()
SESSION = str(uuid.uuid4())  # unique per run: a literal id would be found in a real transcript


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


def test_a_foreign_session_below_a_nested_claude_dir_keeps_the_session_rules(tmp_path: Path, home: Path) -> None:  # noqa: F811
    session = _nested_session(tmp_path)
    with pytest.raises(Refusal) as refusal:
        _guard_scratch(session / "work" / "f", [Path(f"/tmp/claude-{UID}")], [], time.time())
    assert refusal.value.code == "session_recent"


def test_the_nested_claude_dir_is_protected_and_its_session_dir_follows_the_session_rules(
        tmp_path: Path, home: Path) -> None:  # noqa: F811
    session = _nested_session(tmp_path)
    for path, code in ((session, "session_recent"), (session.parents[1], "scratch_protected")):
        with pytest.raises(Refusal) as refusal:
            _guard_scratch(path, [Path(f"/tmp/claude-{UID}")], [], time.time())
        assert refusal.value.code == code


# --- ancestry --------------------------------------------------------------------------------------------

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

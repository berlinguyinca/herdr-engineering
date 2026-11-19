"""Review round 12: one rule for --scope tmp. Real directories under a fixed temp root, real ancestor sessions."""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from herdr_engineering.cleanup_command import Refusal, cleanup
from herdr_engineering.scratch_reaper import system_temp_roots

from .test_cleanup_round2 import home  # noqa: F401
from .test_cleanup_round3 import claim_session

UID = os.getuid()
OWN = "11111111-2222-3333-4444-555555555555"
OTHER = "66666666-7777-8888-9999-000000000000"


def _fixed(tmp_path: Path) -> Path:
    if not any(tmp_path.is_relative_to(os.path.realpath(r)) for r in system_temp_roots()):
        pytest.skip("pytest's tmp_path is not under a fixed temp root here")
    return tmp_path


def _tmp(tmp_path: Path, target: Path, **kw: object) -> dict:
    return cleanup("tmp", target, roots=[], temp_roots=[tmp_path], live_roots=[], now=time.time(),
                   scratch_root=kw.pop("scratch_root", Path(f"/tmp/claude-{UID}")), dry_run=True, **kw)


def _refused(tmp_path: Path, target: Path, code: str, **kw: object) -> None:
    with pytest.raises(Refusal) as refusal:
        _tmp(tmp_path, target, **kw)
    assert refusal.value.code == code, refusal.value


def test_1_a_symlinked_claude_dir_is_a_guard_root_not_a_deletion_root(tmp_path: Path) -> None:
    base = _fixed(tmp_path) / "x"
    session = base / "bobs-dir" / "-proj" / OTHER
    (session / "work").mkdir(parents=True)
    (session / "work" / "f").write_text("alice's live work")
    link = base / f"claude-{UID}"
    link.symlink_to(base / "bobs-dir")
    for target in (session / "work", session, session.parent):
        with pytest.raises(Refusal):
            _tmp(tmp_path, target, scratch_root=link)


def test_2_a_target_containing_the_scratch_root_with_an_own_session_is_refused(tmp_path: Path, home: Path) -> None:  # noqa: F811
    agents = _fixed(tmp_path) / "agents"
    session = agents / f"claude-{UID}" / "-proj" / OWN
    (session / "tasks").mkdir(parents=True)
    (session / "tasks" / "bg.output").write_text("running")
    claim_session(home, OWN)
    _refused(tmp_path, session, "scratch_protected")
    _refused(tmp_path, agents, "scratch_protected")


def test_3_a_claude_dir_nested_inside_the_own_session_is_refused(tmp_path: Path, home: Path) -> None:  # noqa: F811
    base = _fixed(tmp_path) / "n"
    own = base / f"claude-{UID}" / "-proj" / OWN
    child = own / "scratchpad" / "child" / f"claude-{UID}" / "-p" / OTHER
    (child / "tasks").mkdir(parents=True)
    (child / "tasks" / "out").write_text("live child")
    claim_session(home, OWN)
    _refused(tmp_path, own / "scratchpad", "scratch_protected", scratch_root=base / f"claude-{UID}")
    # strictly inside the nested session: it is still below the caller's own session
    _refused(tmp_path, child / "tasks", "scratch_protected", scratch_root=base / f"claude-{UID}")


def test_4_a_claude_dir_deeper_than_six_levels_is_found(tmp_path: Path) -> None:
    top = _fixed(tmp_path) / "d"
    session = top.joinpath(*"1234567") / f"claude-{UID}" / "-p" / OTHER
    (session / "w").mkdir(parents=True)
    (session / "w" / "f").write_text("live")
    _refused(tmp_path, top, "scratch_protected")


def test_a_single_old_foreign_session_is_removable(tmp_path: Path) -> None:
    session = _fixed(tmp_path) / "ok" / f"claude-{UID}" / "-p" / OTHER
    (session / "w").mkdir(parents=True)
    assert cleanup("tmp", session / "w", roots=[], temp_roots=[tmp_path], live_roots=[],
                   scratch_root=Path(f"/tmp/claude-{UID}"), dry_run=True,
                   now=time.time() + 30 * 86400)["action"] == "would_remove"


# --- 5: the timers carry XDG_CONFIG_HOME -----------------------------------------------------------------

def test_5_installer_writes_xdg_config_home_into_the_units_when_set(tmp_path: Path) -> None:
    script = Path(__file__).resolve().parent.parent / "scripts" / "install-disk-hygiene.sh"
    base = {k: v for k, v in os.environ.items() if k != "XDG_CONFIG_HOME"}
    base |= {"HERDR_ENG": "/bin/echo", "HOME": str(tmp_path)}
    render = lambda env: subprocess.run(  # noqa: E731
        ["bash", str(script), "install", "--dry-run"], capture_output=True, text=True, check=True, env=env).stdout
    set_out = render({**base, "XDG_CONFIG_HOME": "/home/u/.xdg"})
    assert "XDG_CONFIG_HOME" in set_out and "/home/u/.xdg" in set_out
    assert "XDG_CONFIG_HOME" not in render(base)

"""Review round 14: other Claude accounts of the same uid, and enclosing sessions. Real processes."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from herdr_engineering.cleanup_command import Refusal, cleanup

from .test_cleanup_round2 import home  # noqa: F401
from .test_cleanup_round3 import process_started
from .test_cleanup_round12 import OTHER, OWN, UID, _fixed

DAY = 86400


def _old(base: Path, session: Path) -> Path:
    (session / "scratchpad").mkdir(parents=True, exist_ok=True)
    (session / "scratchpad" / "notes.md").write_text("n")
    old = time.time() - 3 * DAY
    for d, dirs, files in os.walk(base, topdown=False):
        for name in (*files, *dirs):
            os.utime(Path(d) / name, (old, old), follow_symlinks=False)
        os.utime(d, (old, old))
    return session


def _claude_binary(tmp_path: Path, *, protected: bool = False) -> Path:
    """An executable named ``claude`` that just sleeps. ``protected``: the system ``sleep`` itself
    (a symlink), whose environment macOS hides from ``ps -E``."""
    binary = tmp_path / "bin" / "claude"
    binary.parent.mkdir(exist_ok=True)
    source = shutil.which("sleep")
    assert source
    if protected:
        binary.symlink_to(source)
    else:
        shutil.copy(source, binary)
        if sys.platform == "darwin":
            subprocess.run(["codesign", "--force", "-s", "-", str(binary)], check=True, capture_output=True)
    return binary


@pytest.fixture
def running(tmp_path: Path) -> Iterator[list[subprocess.Popen[bytes]]]:
    procs: list[subprocess.Popen[bytes]] = []
    yield procs
    for proc in procs:
        proc.kill()
        proc.wait()


def _start(tmp_path: Path, running: list[subprocess.Popen[bytes]], env: dict[str, str] | None = None,
           *, protected: bool = False) -> subprocess.Popen[bytes]:
    proc = subprocess.Popen([str(_claude_binary(tmp_path, protected=protected)), "120"],
                            env=env if env is not None else {**os.environ})
    running.append(proc)
    time.sleep(0.3)
    return proc


def _record(config: Path, proc: subprocess.Popen[bytes], session: str) -> None:
    (config / "sessions").mkdir(parents=True, exist_ok=True)
    (config / "projects").mkdir(parents=True, exist_ok=True)
    (config / "sessions" / f"{proc.pid}.json").write_text(json.dumps(
        {"pid": proc.pid, "sessionId": session, "startedAt": process_started(proc.pid) * 1000 + 1500}))


def _clean(tmp_path: Path, target: Path) -> dict:
    return cleanup("tmp", target, roots=[], temp_roots=[tmp_path], scratch_root=Path(f"/tmp/claude-{UID}"),
                   dry_run=True, now=time.time() + 5 * DAY)


def _refused(tmp_path: Path, target: Path, code: str) -> None:
    with pytest.raises(Refusal) as refusal:
        _clean(tmp_path, target)
    assert refusal.value.code == code, refusal.value


def test_1_a_running_session_of_another_account_is_live(
        tmp_path: Path, home: Path, running: list[subprocess.Popen[bytes]],  # noqa: F811
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)  # the caller is the default account
    work = home / ".claude-work"
    session = _old(_fixed(tmp_path) / "a", _fixed(tmp_path) / "a" / f"claude-{UID}" / "-proj" / OTHER)
    proc = _start(tmp_path, running, {**os.environ, "CLAUDE_CONFIG_DIR": str(work)})
    _record(work, proc, OTHER)
    _refused(tmp_path, session, "live_session")
    proc.kill()
    proc.wait()
    assert _clean(tmp_path, session)["action"] == "would_remove"  # nothing runs any more


@pytest.mark.skipif(sys.platform != "darwin", reason="Linux exposes every same-uid environment in /proc")
def test_1_a_running_claude_process_with_an_unreadable_environment_fails_closed(
        tmp_path: Path, home: Path, running: list[subprocess.Popen[bytes]]) -> None:  # noqa: F811
    session = _old(_fixed(tmp_path) / "u", _fixed(tmp_path) / "u" / f"claude-{UID}" / "-proj" / OTHER)
    _start(tmp_path, running, protected=True)  # ps -E shows no environment for a protected system binary
    _refused(tmp_path, session, "live_session_unknown")


def test_2_an_enclosing_running_session_protects_a_nested_old_session(
        tmp_path: Path, home: Path, running: list[subprocess.Popen[bytes]]) -> None:  # noqa: F811
    base = _fixed(tmp_path) / "b"
    outer = base / f"claude-{UID}" / "-proj" / OWN
    inner = outer / "scratchpad" / "child" / f"claude-{UID}" / "-p" / OTHER
    (inner / "w").mkdir(parents=True)
    (inner / "w" / "result.md").write_text("child output")
    (outer / "other").mkdir()
    _old(base, outer)
    proc = _start(tmp_path, running)
    _record(home / ".claude", proc, OWN)
    cleanup_args = dict(roots=[], temp_roots=[tmp_path], scratch_root=base / f"claude-{UID}", dry_run=True,
                        now=time.time() + 5 * DAY)
    for target in (outer / "other", inner / "w"):
        with pytest.raises(Refusal) as refusal:
            cleanup("tmp", target, **cleanup_args)  # type: ignore[arg-type]
        assert refusal.value.code == "live_session", (target, refusal.value)


def test_2_an_enclosing_own_session_protects_a_nested_old_session(tmp_path: Path, home: Path) -> None:  # noqa: F811
    from .test_cleanup_round3 import claim_session
    base = _fixed(tmp_path) / "c"
    outer = base / f"claude-{UID}" / "-proj" / OWN
    inner = outer / "scratchpad" / "child" / f"claude-{UID}" / "-p" / OTHER
    (inner / "w").mkdir(parents=True)
    _old(base, outer)
    claim_session(home, OWN)
    with pytest.raises(Refusal) as refusal:
        cleanup("tmp", inner / "w", roots=[], temp_roots=[tmp_path], scratch_root=base / f"claude-{UID}",
                dry_run=True, now=time.time() + 5 * DAY)
    assert refusal.value.code == "scratch_protected"

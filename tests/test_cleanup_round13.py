"""Review round 13: CLAUDE_CONFIG_DIR, running sessions, .keep above a nested root, installer variables.
Real directories, real processes, real files."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from herdr_engineering.cleanup_command import Refusal, cleanup

from .test_cleanup_round2 import home  # noqa: F401
from .test_cleanup_round3 import claim_session, process_started
from .test_cleanup_round12 import OTHER, OWN, UID, _fixed

DAY = 86400


def _old_session(base: Path, name: str = OTHER) -> Path:
    session = base / f"claude-{UID}" / "-proj" / name
    (session / "scratchpad").mkdir(parents=True)
    note = session / "scratchpad" / "notes.md"
    note.write_text("n")
    old = time.time() - 3 * DAY
    for item in (note, session / "scratchpad", session):
        os.utime(item, (old, old))
    return session


def _clean(tmp_path: Path, target: Path, *, live_roots: list[Path] | None = None, fresh: bool = False) -> dict:
    return cleanup("tmp", target, roots=[], temp_roots=[tmp_path], scratch_root=Path(f"/tmp/claude-{UID}"),
                   live_roots=live_roots, dry_run=True, now=time.time() + (0 if fresh else 30 * DAY))


def _refused(tmp_path: Path, target: Path, code: str, **kw: object) -> None:
    with pytest.raises(Refusal) as refusal:
        _clean(tmp_path, target, **kw)  # type: ignore[arg-type]
    assert refusal.value.code == code, refusal.value


# --- 1: CLAUDE_CONFIG_DIR ----------------------------------------------------------------------------------

def test_1_a_fresh_transcript_under_claude_config_dir_makes_the_session_live(
        tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    session = _old_session(_fixed(tmp_path) / "a")
    config = tmp_path / "claude-work"
    (config / "projects" / "-proj").mkdir(parents=True)
    (config / "projects" / "-proj" / f"{OTHER}.jsonl").write_text("{}")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    _refused(tmp_path, session, "live_session", fresh=True)


def test_1_the_callers_own_session_is_found_under_claude_config_dir(
        tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    session = _old_session(_fixed(tmp_path) / "b", OWN)
    config = tmp_path / "claude-work"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    claim_session(config.parent, OWN)  # writes <dir>/.claude/sessions; move it under the config dir
    (config / "sessions").mkdir(parents=True)
    for file in (config.parent / ".claude" / "sessions").glob("*.json"):
        file.rename(config / "sessions" / file.name)
    _refused(tmp_path, session, "scratch_protected")


def test_1_an_unreadable_claude_config_dir_refuses(tmp_path: Path, home: Path,  # noqa: F811
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    session = _old_session(_fixed(tmp_path) / "c")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "does-not-exist"))
    _refused(tmp_path, session, "scratch_protected")


# --- 4: a running Claude process keeps its session live --------------------------------------------------------

def test_4_a_running_claude_process_for_the_session_makes_it_live(tmp_path: Path, home: Path) -> None:  # noqa: F811
    session = _old_session(_fixed(tmp_path) / "d")  # idle for 3 days, no transcript
    (home / ".claude" / "projects").mkdir(parents=True)
    stranger = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(60)"])
    try:
        sessions = home / ".claude" / "sessions"
        sessions.mkdir(parents=True)
        record = {"pid": stranger.pid, "sessionId": OTHER, "startedAt": process_started(stranger.pid) * 1000 + 2000}
        (sessions / f"{stranger.pid}.json").write_text(json.dumps(record))
        _refused(tmp_path, session, "live_session")
        record["startedAt"] -= 10 * DAY * 1000  # a recycled pid: the start time no longer matches
        (sessions / f"{stranger.pid}.json").write_text(json.dumps(record))
        assert _clean(tmp_path, session)["action"] == "would_remove"
    finally:
        stranger.kill()
        stranger.wait()
    assert _clean(tmp_path, session)["action"] == "would_remove"  # the process is gone


# --- 3: .keep anywhere up to the outermost claude-<uid> --------------------------------------------------------

def test_3_a_keep_in_the_enclosing_session_protects_a_nested_session(tmp_path: Path, home: Path) -> None:  # noqa: F811
    base = _fixed(tmp_path) / "k"
    outer = base / f"claude-{UID}" / "-proj" / OWN
    inner = outer / "scratchpad" / "child" / f"claude-{UID}" / "-p" / OTHER
    (inner / "w").mkdir(parents=True)
    (outer / ".keep").write_text("")
    old = time.time() - 3 * DAY
    for item in (inner / "w", inner):
        os.utime(item, (old, old))
    _refused(tmp_path, inner / "w", "keep_marker")


# --- 1/2: the installer carries the user's variables ------------------------------------------------------------

@pytest.mark.parametrize("name", ["CLAUDE_CONFIG_DIR", "GIT_CONFIG_GLOBAL"])
def test_installer_writes_the_variable_into_the_units_when_set(tmp_path: Path, name: str) -> None:
    script = Path(__file__).resolve().parent.parent / "scripts" / "install-disk-hygiene.sh"
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDE_CONFIG_DIR", "GIT_CONFIG_GLOBAL")}
    env |= {"HERDR_ENG": "/bin/echo", "HOME": str(tmp_path)}

    def render(extra: dict[str, str]) -> str:
        return subprocess.run(["bash", str(script), "install", "--dry-run"], capture_output=True, text=True,
                              check=True, env={**env, **extra}).stdout
    assert f"{name}" in render({name: "/home/u/cfg"}) and "/home/u/cfg" in render({name: "/home/u/cfg"})
    assert name not in render({})

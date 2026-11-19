"""`herdr-eng handoff context`, run as a real subprocess against fixture records laid out under a temporary HOME."""
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).parent / "fixtures" / "handoff_adapters"
CLAUDE_SESSION = "11111111-1111-4111-8111-111111111111"
OPENCODE_SESSION = "ses_fixture0001"


def _run(home: Path, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
    base = {"PATH": os.environ["PATH"], "PYTHONPATH": str(ROOT), "HOME": str(home), **env}
    return subprocess.run([sys.executable, "-m", "herdr_engineering.cli", "handoff", "context", *args],
                          capture_output=True, text=True, env=base, timeout=60)


def _install_claude(home: Path, cwd: Path) -> None:
    directory = home / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(cwd))
    directory.mkdir(parents=True)
    shutil.copy(FIXTURES / "claude" / "main_chain.jsonl", directory / f"{CLAUDE_SESSION}.jsonl")


def _install_opencode(home: Path) -> None:
    db = home / ".local" / "share" / "opencode" / "opencode.db"
    db.parent.mkdir(parents=True)
    con = sqlite3.connect(db)
    try:
        con.executescript((FIXTURES / "opencode" / "opencode.sql").read_text(encoding="utf-8"))
    finally:
        con.close()
    (home / ".config" / "opencode").mkdir(parents=True)
    shutil.copy(FIXTURES / "opencode" / "opencode.json", home / ".config" / "opencode" / "opencode.json")


def test_claude_session_prints_the_reading_as_json(tmp_path):
    cwd = tmp_path / "work"
    _install_claude(tmp_path, cwd)
    done = _run(tmp_path, "--harness", "claude", "--session-id", CLAUDE_SESSION, "--cwd", str(cwd), "--json")
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == {"harness": "claude", "used_tokens": 5702, "window_tokens": 200_000,
                                       "percent": 2.9, "source": "claude-transcript", "window_source": "model_table"}


def test_opencode_session_prints_the_reading_as_json(tmp_path):
    _install_opencode(tmp_path)
    done = _run(tmp_path, "--harness", "opencode", "--session-id", OPENCODE_SESSION, "--json")
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == {"harness": "opencode", "used_tokens": 54542, "window_tokens": 131_072,
                                       "percent": 41.6, "source": "opencode-db", "window_source": "recorded"}


def test_without_json_prints_one_human_line(tmp_path):
    cwd = tmp_path / "work"
    _install_claude(tmp_path, cwd)
    done = _run(tmp_path, "--harness", "claude", "--session-id", CLAUDE_SESSION, "--cwd", str(cwd))
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == ["claude: 5702 of 200000 tokens (2.9%)"]


def test_session_without_a_record_is_unknown_with_exit_0(tmp_path):
    done = _run(tmp_path, "--harness", "claude", "--session-id", "33333333-3333-4333-8333-333333333333",
                "--cwd", str(tmp_path / "work"), "--json")
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == {"unknown": True}


def test_unknown_without_json_says_unknown(tmp_path):
    done = _run(tmp_path, "--harness", "codex", "--session-id", "x", "--cwd", str(tmp_path))
    assert (done.returncode, done.stdout.strip()) == (0, "unknown")


def test_pane_that_cannot_be_resolved_is_unknown_with_exit_0(tmp_path):
    done = _run(tmp_path, "--json")  # no pane, no harness flags, no Herdr here
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == {"unknown": True}


@pytest.mark.parametrize("args", [
    ["--harness", "nonsense", "--session-id", "x"],
    ["--session-id", "x"],
    ["--harness", "claude"],
])
def test_bad_flag_combinations_are_argparse_errors(tmp_path, args):
    done = _run(tmp_path, *args)
    assert done.returncode == 2 and "usage:" in done.stderr

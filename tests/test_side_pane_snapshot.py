"""CI smoke test of the side pane without Herdr: `side-pane run --snapshot` in a real subprocess, on a real event log and
real handoff records written to a temporary store. It renders one frame to stdout and exits."""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from tests.test_side_pane_model import three_session_store

REPO = Path(__file__).resolve().parents[1]
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def run_snapshot(store: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "HERDR_HANDOFF_STORE": str(store), "PYTHONPATH": str(REPO)}
    env.pop("HERDR_PANE_ID", None)
    return subprocess.run([sys.executable, "-m", "herdr_engineering.cli", "side-pane", "run", "--snapshot", *args],
                          env=env, cwd=REPO, capture_output=True, text=True, timeout=60, check=False)


def plain(text: str) -> str:
    return _ANSI.sub("", text)


@pytest.mark.parametrize("view,expected", [
    ("summary", "Summary"),
    ("handoffs", "guard PR and CI"),
    ("ci", "CI"),
])
def test_each_view_renders_one_frame_and_exits_zero(tmp_path: Path, view: str, expected: str) -> None:
    three_session_store(tmp_path)
    done = run_snapshot(tmp_path, "--tab", "w1:t1", "--session-pane", "p-3", "--session-id", "s3", "--tab-view", view,
                        "--width", "60")
    assert done.returncode == 0, done.stderr
    frame = plain(done.stdout)
    assert expected in frame, frame
    assert all(len(row) <= 60 for row in frame.splitlines()), frame


def test_the_handoffs_view_shows_the_whole_chain(tmp_path: Path) -> None:
    three_session_store(tmp_path)
    done = run_snapshot(tmp_path, "--tab", "w1:t1", "--session-pane", "p-3", "--session-id", "s3", "--tab-view", "handoffs")
    frame = plain(done.stdout)
    assert done.returncode == 0 and "brainstorm the guard now" in frame and "guard PR and CI" in frame, frame


def test_a_snapshot_without_a_pane_or_tab_exits_two(tmp_path: Path) -> None:
    done = run_snapshot(tmp_path, "--tab-view", "summary")
    assert done.returncode == 2 and done.stdout == "" and "HERDR_PANE_ID" in done.stderr, done.stderr


def test_a_snapshot_without_a_session_says_so(tmp_path: Path) -> None:
    done = run_snapshot(tmp_path, "--tab", "w1:t1")
    assert done.returncode == 0 and "no session" in plain(done.stdout), done.stdout

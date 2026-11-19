"""`side-pane run` never turns a session pane, or a pipe, into a side pane."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.handoff_controller_herdr import isolated_herdr
from tests.test_side_pane_ensure import REPO, session, side_of

herdr_server = pytest.mark.herdr_server


def run_viewer_cli(env: dict[str, str], cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-m", "herdr_engineering.cli", "side-pane", "run", "--no-ci", *args],
                          env={**os.environ, **env, "PYTHONPATH": str(REPO)}, cwd=cwd, stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, timeout=60, check=False)


def test_it_refuses_when_stdin_is_not_a_terminal(tmp_path: Path) -> None:
    env = {"HOME": str(tmp_path), "PATH": str(tmp_path), "HERDR_ENGINEERING_STATE": str(tmp_path / "state"), "HERDR_PANE_ID": "w1:p9"}
    done = run_viewer_cli(env, tmp_path)
    assert done.returncode == 2 and done.stdout == ""
    assert len(done.stderr.strip().splitlines()) == 1 and "not a terminal" in done.stderr, done.stderr
    assert not (tmp_path / "state").exists(), "a marker was registered"


@herdr_server
def test_it_refuses_in_a_pane_that_hosts_a_managed_agent() -> None:
    with isolated_herdr("sp-refuse") as h:
        side = side_of(h)
        pane, tab = session(h, side)
        done = run_viewer_cli({**h.env, "HOME": str(h.root / "home")}, h.root, "--pane", pane, "--tab", tab)
        assert done.returncode == 2 and "hosts a managed agent" in done.stderr, done.stderr
        assert len(done.stderr.strip().splitlines()) == 1
        assert not list((h.root / "home").rglob("*.marker.json"))

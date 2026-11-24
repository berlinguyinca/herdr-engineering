"""Default-target detection for /handoff, against real processes, a real herdr server and a real env."""
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from herdr_engineering import handoff_harness as hh

ROOT = Path(hh.__file__).resolve().parents[1]
EMPTY_PROC = Path("/nonexistent-proc")


def test_explicit_wins_and_is_validated():
    assert hh.detect(explicit="codex", env={}).method == "explicit"
    with pytest.raises(hh.Undetectable, match="unknown harness"):
        hh.detect(explicit="emacs", env={})


@pytest.mark.parametrize("harness,var", [("claude", "CLAUDECODE"), ("pi", "PI_CODING_AGENT"),
                                         ("codex", "CODEX_THREAD_ID"), ("opencode", "OPENCODE")])
def test_environment_marker_identifies_one_harness(harness, var):
    found = hh.detect(env={var: "1"}, proc=EMPTY_PROC)
    assert (found.harness, found.method) == (harness, "environment")


def test_conflicting_markers_are_undetectable_not_guessed():
    with pytest.raises(hh.Undetectable, match="disagree"):
        hh.detect(env={"CLAUDECODE": "1", "OPENCODE": "1"}, proc=EMPTY_PROC)


def test_nothing_known_fails_clearly_and_names_the_options():
    with pytest.raises(hh.Undetectable) as err:
        hh.detect(env={"PATH": "/usr/bin"}, proc=EMPTY_PROC)
    assert "name one of: claude, pi, codex, opencode" in str(err.value)


def _wrapper(tmp_path: Path, name: str) -> Path:
    """A real executable called <name> that runs the detector as its child process."""
    path = tmp_path / name
    path.write_text(f"#!{sys.executable}\nimport subprocess, sys\n"
                    f"sys.exit(subprocess.call([sys.executable, '-m', 'herdr_engineering.cli', 'handoff', 'harness']))\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


@pytest.mark.parametrize("harness", hh.HARNESSES)
def test_real_ancestor_process_is_detected_through_the_cli(tmp_path, harness):
    env = {"PATH": os.environ["PATH"], "PYTHONPATH": str(ROOT), "HOME": str(tmp_path)}
    done = subprocess.run([str(_wrapper(tmp_path, harness))], capture_output=True, text=True, env=env, timeout=60)
    out = json.loads(done.stdout)
    assert done.returncode == 0 and out["harness"] == harness and out["method"] == "process-ancestry"


def test_cli_fails_with_exit_2_when_undetectable(tmp_path):
    env = {"PATH": os.environ["PATH"], "PYTHONPATH": str(ROOT), "HOME": str(tmp_path)}
    done = subprocess.run([sys.executable, "-m", "herdr_engineering.cli", "handoff", "harness", "--proc-root",
                           str(EMPTY_PROC)], capture_output=True, text=True, env=env, timeout=60)
    out = json.loads(done.stdout)
    assert done.returncode == 2 and out["ok"] is False and "name one of" in out["error"]


def test_cli_accepts_an_explicit_harness(tmp_path):
    env = {"PATH": os.environ["PATH"], "PYTHONPATH": str(ROOT), "HOME": str(tmp_path)}
    done = subprocess.run([sys.executable, "-m", "herdr_engineering.cli", "handoff", "harness", "--harness",
                           "opencode"], capture_output=True, text=True, env=env, timeout=60)
    assert json.loads(done.stdout)["harness"] == "opencode"


def test_pane_agent_from_a_real_herdr_server():
    herdr = shutil.which("herdr")
    if not herdr:
        pytest.skip("herdr binary not installed")
    home = Path(tempfile.mkdtemp(prefix="hd."))
    env = {**os.environ, "HOME": str(home), "HERDR_SOCKET_PATH": str(home / "t.sock")}
    env = {k: v for k, v in env.items() if k != "HERDR_PANE_ID"}
    proc = subprocess.Popen([herdr, "server"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=home)
    try:
        for _ in range(60):
            if subprocess.run([herdr, "workspace", "list"], env=env, capture_output=True).returncode == 0:
                break
            time.sleep(0.25)
        else:
            pytest.skip("could not start an isolated herdr server")
        made = json.loads(subprocess.run([herdr, "workspace", "create", "--cwd", str(home)], env=env,
                                         capture_output=True, text=True).stdout)
        pane = made["result"]["root_pane"]["pane_id"]
        # A fresh shell pane has no agent: the detector must not invent one from the pane.
        assert hh._from_pane({**env, "HERDR_PANE_ID": pane}, herdr) is None
        reported = subprocess.run([herdr, "pane", "report-agent-session", pane, "--source", "t", "--agent", "codex",
                                   "--agent-session-id", "s1", "--session-start-source", "t"], env=env,
                                  capture_output=True, text=True)
        if reported.returncode == 0:
            found = hh._from_pane({**env, "HERDR_PANE_ID": pane}, herdr)
            if found:  # herdr may keep agent=None until a process is recognised; only assert when it reports one
                assert (found.harness, found.method) == ("codex", "herdr-pane")
    finally:
        subprocess.run([herdr, "server", "stop"], env=env, capture_output=True, timeout=10)
        proc.wait(timeout=10)
        shutil.rmtree(home, ignore_errors=True)

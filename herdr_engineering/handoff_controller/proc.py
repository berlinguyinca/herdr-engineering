"""Run an external command in its own process group, with a timeout and a cleaned environment. Never raises."""
from __future__ import annotations

import os
import signal
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

TIMED_OUT = 124
NOT_RUNNABLE = 127
_AMBIENT_GIT_VARS = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_NAMESPACE")


def clean_env(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment minus ambient git variables (a hook's GIT_DIR must not redirect us), prompts disabled."""
    env = {k: v for k, v in (os.environ if base is None else base).items() if k not in _AMBIENT_GIT_VARS}
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def run_group(cmd: Sequence[str], timeout_s: float, *, env: Mapping[str, str] | None = None,
              cwd: Path | None = None, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    """A timeout is returncode 124 (the whole process group is killed), a command that cannot start 127."""
    argv = list(cmd)
    try:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                stdin=subprocess.DEVNULL if input_text is None else subprocess.PIPE,
                                text=True, env=clean_env(env), cwd=cwd, start_new_session=True)
    except (OSError, ValueError) as exc:
        return subprocess.CompletedProcess(argv, NOT_RUNNABLE, "", str(exc))
    try:
        out, err = proc.communicate(input_text, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)  # hooks and ssh are children of git: kill them all
        except ProcessLookupError:
            pass
        try:
            proc.communicate(timeout=5)  # reap
        except subprocess.TimeoutExpired:
            pass
        return subprocess.CompletedProcess(argv, TIMED_OUT, "", f"timed out after {timeout_s}s")
    except (OSError, ValueError) as exc:  # e.g. the child closed stdin early with input still to send
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()
        return subprocess.CompletedProcess(argv, NOT_RUNNABLE, "", str(exc))
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)

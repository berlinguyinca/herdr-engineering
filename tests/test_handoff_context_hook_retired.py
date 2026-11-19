"""The 50% context reminder hook is gone; the old command a settings.json may still call is a silent no-op."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from herdr_engineering.handoff_controller.switch import is_enabled, set_enabled

ROOT = Path(__file__).resolve().parents[1]
CODE = "import sys; from herdr_engineering import cli; sys.exit(cli.main(sys.argv[1:]))"
HARNESSES = ("claude", "codex", "pi", "opencode")


def run(args: list[str], stdin: str | bytes, home: Path) -> subprocess.CompletedProcess:
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home), "PYTHONPATH": str(ROOT),
           "HERDR_ENGINEERING_STATE": str(home / "state")}
    return subprocess.run([sys.executable, "-c", CODE, "handoff", "context-hook", *args], input=stdin.encode() if isinstance(stdin, str) else stdin, env=env,
                          capture_output=True, timeout=60)


def over_fifty_percent(tmp_path: Path) -> str:
    line = {"type": "assistant", "message": {"role": "assistant", "usage": {
        "input_tokens": 150_000, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "output_tokens": 5}}}
    path = tmp_path / "t.jsonl"
    path.write_text(json.dumps(line) + "\n")
    return json.dumps({"session_id": "s1", "transcript_path": str(path), "hook_event_name": "UserPromptSubmit"})


def test_module_gone() -> None:
    assert importlib.util.find_spec("herdr_engineering.handoff_context_hook") is None


@pytest.mark.parametrize("harness", HARNESSES)
@pytest.mark.parametrize("kind", ["over_50_percent", "empty", "garbage", "binary_garbage"])
def test_legacy_context_hook_command_exits_zero_silently(tmp_path: Path, harness: str, kind: str) -> None:
    stdin = {"over_50_percent": over_fifty_percent(tmp_path), "empty": "", "garbage": "{not json\n",
             "binary_garbage": b"\xff\xfe\x00\x80"}[kind]
    done = run([harness], stdin, tmp_path)
    assert (done.returncode, done.stdout, done.stderr) == (0, b"", b"")
    assert not (tmp_path / "state").exists()  # the retired hook no longer writes its marker files


@pytest.mark.parametrize("extra", [[], ["--threshold", "0.1"], ["extra", "args"], ["--unknown-flag"]])
def test_legacy_context_hook_ignores_any_extra_arguments(tmp_path: Path, extra: list[str]) -> None:
    done = run(["claude", *extra], over_fifty_percent(tmp_path), tmp_path)
    assert (done.returncode, done.stdout, done.stderr) == (0, b"", b"")
    done = run(extra, "", tmp_path)  # no harness at all
    assert (done.returncode, done.stdout, done.stderr) == (0, b"", b"")


def test_legacy_command_is_hidden_from_help(tmp_path: Path) -> None:
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path), "PYTHONPATH": str(ROOT)}
    done = subprocess.run([sys.executable, "-c", CODE, "handoff", "--help"], env=env, capture_output=True, text=True, timeout=60)
    assert done.returncode == 0 and "context-hook" not in done.stdout


def test_global_off_switch_still_honoured_by_the_controller(tmp_path: Path) -> None:
    # A user who set HERDR_HANDOFF_AUTO=0 to silence the old reminder keeps that switch: it now turns the controller off.
    assert is_enabled("%1", {}, tmp_path)
    assert not is_enabled("%1", {"HERDR_HANDOFF_AUTO": "0"}, tmp_path)
    set_enabled("%1", True, tmp_path)  # a pane switched on does not override the global off switch
    assert not is_enabled("%1", {"HERDR_HANDOFF_AUTO": "0"}, tmp_path)

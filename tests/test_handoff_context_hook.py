"""``herdr-eng handoff context-hook claude``: real subprocess, real transcript files, real state dir."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CODE = "import sys; from herdr_engineering import cli; sys.exit(cli.main(sys.argv[1:]))"


def run_hook(stdin: str, state: Path, **env_extra: str) -> tuple[int, str, str]:
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(state), "PYTHONPATH": str(ROOT),
           "HERDR_ENGINEERING_STATE": str(state / "state"), **env_extra}
    p = subprocess.run([sys.executable, "-c", CODE, "handoff", "context-hook", "claude"], input=stdin,
                       env=env, capture_output=True, text=True, timeout=60)
    return p.returncode, p.stdout, p.stderr


def usage_line(tokens_in: int, cache_read: int = 0, cache_create: int = 0) -> str:
    return json.dumps({"type": "assistant", "message": {"role": "assistant", "usage": {
        "input_tokens": tokens_in, "cache_read_input_tokens": cache_read,
        "cache_creation_input_tokens": cache_create, "output_tokens": 5}}})


def transcript(tmp_path: Path, *lines: str) -> Path:
    path = tmp_path / "t.jsonl"
    path.write_text("\n".join(lines) + "\n")
    return path


def payload(path: Path | str, session: str = "s1") -> str:
    return json.dumps({"session_id": session, "transcript_path": str(path), "hook_event_name": "UserPromptSubmit"})


def test_below_threshold_is_silent(tmp_path):
    t = transcript(tmp_path, usage_line(1000, 50000, 1000))
    assert run_hook(payload(t), tmp_path) == (0, "", "")


def test_at_threshold_injects_additional_context_with_percentage(tmp_path):
    t = transcript(tmp_path, usage_line(10, 90000, 9990))
    rc, out, err = run_hook(payload(t), tmp_path)
    assert rc == 0 and err == ""
    data = json.loads(out)["hookSpecificOutput"]
    assert data["hookEventName"] == "UserPromptSubmit"
    assert "50%" in data["additionalContext"] and "/handoff" in data["additionalContext"]
    assert "subagent" in data["additionalContext"]


def test_second_call_same_session_is_silent_but_other_session_fires(tmp_path):
    t = transcript(tmp_path, usage_line(150000))
    assert run_hook(payload(t, "a"), tmp_path)[1] != ""
    assert run_hook(payload(t, "a"), tmp_path) == (0, "", "")
    assert run_hook(payload(t, "b"), tmp_path)[1] != ""


def test_missing_transcript_and_garbage_stdin_are_silent(tmp_path):
    assert run_hook(payload(tmp_path / "nope.jsonl"), tmp_path) == (0, "", "")
    assert run_hook("not json{", tmp_path) == (0, "", "")
    assert run_hook("", tmp_path) == (0, "", "")


def test_opt_out_env_is_silent(tmp_path):
    t = transcript(tmp_path, usage_line(190000))
    assert run_hook(payload(t), tmp_path, HERDR_HANDOFF_AUTO="0") == (0, "", "")


def test_env_threshold_and_max_override(tmp_path):
    t = transcript(tmp_path, usage_line(30000))
    assert run_hook(payload(t, "x"), tmp_path) == (0, "", "")
    rc, out, _ = run_hook(payload(t, "x"), tmp_path, HERDR_HANDOFF_CONTEXT_THRESHOLD="0.1")
    assert "15%" in json.loads(out)["hookSpecificOutput"]["additionalContext"]
    rc, out, _ = run_hook(payload(t, "y"), tmp_path, HERDR_HANDOFF_CONTEXT_MAX_TOKENS="40000")
    assert "75%" in json.loads(out)["hookSpecificOutput"]["additionalContext"]


def test_uses_last_assistant_message_only(tmp_path):
    big_then_small = transcript(tmp_path, usage_line(190000), json.dumps({"type": "user"}), usage_line(1000))
    assert run_hook(payload(big_then_small), tmp_path) == (0, "", "")
    small_then_big = transcript(tmp_path, usage_line(1000), json.dumps({"type": "user"}), usage_line(190000),
                                json.dumps({"type": "user", "message": {"content": "hi"}}))
    assert run_hook(payload(small_then_big, "z"), tmp_path)[1] != ""


@pytest.mark.parametrize("bad", ["[1,2]", '{"session_id": 5}', '{"transcript_path": "/x"}'])
def test_malformed_payload_shapes_are_silent(tmp_path, bad):
    assert run_hook(bad, tmp_path) == (0, "", "")

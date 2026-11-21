"""Fix round 1, commit 2: tighter headless flags, briefing edge cases, fixture start delay. Real programs only."""
from __future__ import annotations

import json
import os
import pty
import select
import stat
import subprocess
import sys
import time
from pathlib import Path

from herdr_engineering.handoff_adapters.base import Harness
from herdr_engineering.handoff_controller.briefing import (
    _command_output,
    headless_argv,
    write_fallback_briefing,
)
from tests.handoff_controller_herdr import _our_program_pids
from tests.test_handoff_controller_briefing import GH, handoff, make_ref, repo, script_runner  # noqa: F401  (fixtures)

COMPOSER = Path(__file__).parent / "fixtures" / "handoff_controller" / "tui_composer.py"


def _readable(fd: int, timeout_s: float) -> bool:
    """Whether `fd` has input within the timeout. `poll`, not `select`: a loaded test worker holds descriptors past
    1023, which `select` refuses."""
    poller = select.poll()
    poller.register(fd, select.POLLIN)
    return bool(poller.poll(timeout_s * 1000))


def fake_cli(directory: Path, name: str, help_text: str) -> Path:
    """A real executable that prints `help_text` for `--help` and records every argv it is started with."""
    directory.mkdir(parents=True, exist_ok=True)
    seen = directory / f"{name}.argv"
    program = directory / name
    program.write_text(f"#!/bin/sh\necho \"$*\" >> {seen}\n/bin/cat <<'EOF'\n{help_text}\nEOF\n")
    program.chmod(program.stat().st_mode | stat.S_IXUSR)
    return seen


CODEX_HELP = "Usage: codex exec\n      --ignore-user-config\n          Do not load config\n      --ignore-rules\n          Do not load rules\n"
CLAUDE_HELP = "Usage: claude\n  --bare   Minimal mode (OAuth and keychain are never read)\n  --safe-mode   Start with all customizations disabled\n"


def test_codex_gets_the_ignore_flags_when_its_exec_help_lists_them(tmp_path: Path) -> None:
    seen = fake_cli(tmp_path / "bin", "codex", CODEX_HELP)
    argv = headless_argv(Harness.CODEX, env={"PATH": str(tmp_path / "bin")})
    assert argv is not None and argv[:2] == ["codex", "exec"]
    assert "--ignore-user-config" in argv and "--ignore-rules" in argv and argv[-1] == "-"
    assert seen.read_text().split() == ["exec", "--help"]  # nothing but `--help` was run


def test_codex_keeps_the_plain_flags_when_its_help_does_not_list_them(tmp_path: Path) -> None:
    fake_cli(tmp_path / "bin", "codex", "Usage: codex exec\n      --sandbox <MODE>\n")
    assert headless_argv(Harness.CODEX, env={"PATH": str(tmp_path / "bin")}) == [
        "codex", "exec", "--sandbox", "read-only", "--skip-git-repo-check", "--ephemeral", "-"]


def test_claude_uses_safe_mode_but_never_bare_because_bare_drops_the_login(tmp_path: Path) -> None:
    seen = fake_cli(tmp_path / "bin", "claude", CLAUDE_HELP)
    argv = headless_argv(Harness.CLAUDE, env={"PATH": str(tmp_path / "bin")})
    assert argv is not None and "--safe-mode" in argv and "--bare" not in argv
    assert seen.read_text().split() == ["--help"]


def test_claude_without_safe_mode_in_its_help_keeps_the_plain_flags(tmp_path: Path) -> None:
    fake_cli(tmp_path / "bin", "claude", "Usage: claude\n  --print\n")
    argv = headless_argv(Harness.CLAUDE, env={"PATH": str(tmp_path / "bin")})
    assert argv is not None and "--safe-mode" not in argv and "--bare" not in argv


def test_the_tail_keeps_its_first_line_when_it_starts_on_a_line_boundary(handoff, tmp_path: Path, repo: Path) -> None:  # noqa: F811
    prompt_out = tmp_path / "prompt.txt"
    ref = make_ref(tmp_path, repo, transcript="AAAA\nFIRST-LINE-KEPT\nlast\n")
    write_fallback_briefing(ref, repo, tmp_path / "store", script_runner("ok", prompt_out), handoff=handoff, gh=GH,
                            transcript_tail_bytes=len("FIRST-LINE-KEPT\nlast\n"))
    prompt = prompt_out.read_text()
    assert "FIRST-LINE-KEPT" in prompt and "AAAA" not in prompt


def test_the_tail_still_drops_a_line_cut_in_the_middle(handoff, tmp_path: Path, repo: Path) -> None:  # noqa: F811
    prompt_out = tmp_path / "prompt.txt"
    ref = make_ref(tmp_path, repo, transcript="AAAA\nCUT-LINE\nlast\n")
    write_fallback_briefing(ref, repo, tmp_path / "store", script_runner("ok", prompt_out), handoff=handoff, gh=GH,
                            transcript_tail_bytes=len("T-LINE\nlast\n"))
    assert "CUT-LINE" not in prompt_out.read_text() and "last" in prompt_out.read_text()


def test_an_answer_that_arrives_with_the_budget_spent_is_still_used(handoff, tmp_path: Path, repo: Path) -> None:  # noqa: F811
    answer = json.dumps({"goal": "late but good", "remaining": ["x"]})

    def late(harness: Harness, prompt: str, *, cwd: Path, timeout_s: float) -> str | None:
        time.sleep(timeout_s + 0.3)
        return answer

    started = time.monotonic()
    found = write_fallback_briefing(make_ref(tmp_path, repo), repo, tmp_path / "store", late, handoff=handoff, gh=GH,
                                    timeout_s=1.0)
    assert found is not None
    assert time.monotonic() - started < 1.0 + 15.0  # the grace is bounded


def test_the_command_label_comes_from_an_explicit_parameter(tmp_path: Path) -> None:
    failing = [sys.executable, "-c", "import sys; sys.exit(3)"]
    text = _command_output("gh pr list", failing, tmp_path, 10.0, tool="gh")
    assert text == "gh pr list: not available (gh failed, exit 3)"
    other = _command_output("anything", failing, tmp_path, 10.0, tool="git")
    assert other == "anything: not available (git failed, exit 3)"


def test_the_composer_fixture_waits_for_its_start_delay_before_drawing(tmp_path: Path) -> None:
    master, slave = pty.openpty()
    env = {**os.environ, "FAKE_TUI_START_DELAY_S": "1.5"}
    child = subprocess.Popen([sys.executable, str(COMPOSER)], stdin=slave, stdout=slave, env=env, close_fds=True)
    os.close(slave)
    try:
        early = _readable(master, 0.8)
        assert not early, "the fixture drew before its start delay"
        assert _readable(master, 6.0), "the fixture never drew"
        assert "❯" in os.read(master, 8192).decode("utf-8", "replace")
    finally:
        child.kill()
        child.wait()
        os.close(master)


def test_the_composer_fixture_reads_a_start_delay_file_beside_the_program(tmp_path: Path) -> None:
    program = tmp_path / "claude"
    program.write_text(COMPOSER.read_text())
    (tmp_path / "start_delay_s").write_text("1.5")
    master, slave = pty.openpty()
    child = subprocess.Popen([sys.executable, str(program)], stdin=slave, stdout=slave, close_fds=True)
    os.close(slave)
    try:
        assert not _readable(master, 0.8)
        assert _readable(master, 6.0)
    finally:
        child.kill()
        child.wait()
        os.close(master)


def test_the_process_lookup_survives_a_machine_without_pgrep(tmp_path: Path) -> None:
    assert _our_program_pids(tmp_path / "program", env={"PATH": str(tmp_path / "empty")}) == []

"""Briefing wait and the second-model fallback: real git repos, a real herdr-handoff 0.5.0 CLI, real subprocesses.

Every CLI call passes --root under tmp_path; nothing touches the real store or home.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from herdr_engineering.handoff_adapters.base import Harness, SessionRef
from herdr_engineering.handoff_controller.briefing import (
    fallback_supported,
    headless_argv,
    headless_runner,
    run_model,
    wait_for_briefing,
    write_fallback_briefing,
)
from herdr_engineering.handoff_controller.handoff_cli import HandoffCli

FIXTURES = Path(__file__).parent / "fixtures" / "handoff_controller"
WRITER = FIXTURES / "briefing_writer.py"
GH = (sys.executable, str(FIXTURES / "gh_pr_list.py"))
SIBLING = Path.home() / ".local/share/worktrees/herdr-handoff-auto-handoff-ids"
_BOOT = "import sys; sys.path.insert(0, {src!r}); import herdr_handoff_cli as m; sys.exit(m.main())"


def _version_at_least_050(prefix: list[str]) -> bool:
    try:
        done = subprocess.run([*prefix, "--version"], capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    parts = done.stdout.strip().split(".")
    return done.returncode == 0 and len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit() \
        and (int(parts[0]), int(parts[1])) >= (0, 5)


def _cli_prefix() -> tuple[list[str] | None, str]:
    """HERDR_HANDOFF_TEST_ARGV (shlex-split), else the 0.5.0 source checkout, else an installed binary >= 0.5.0."""
    configured = os.environ.get("HERDR_HANDOFF_TEST_ARGV")
    if configured:
        return shlex.split(configured), ""
    if (SIBLING / "src" / "herdr_handoff_cli.py").is_file():
        return [sys.executable, "-c", _BOOT.format(src=str(SIBLING / "src"))], ""
    if _version_at_least_050(["herdr-handoff"]):
        return ["herdr-handoff"], ""
    return None, "needs herdr-handoff >= 0.5.0: set HERDR_HANDOFF_TEST_ARGV or check out its source next to the repo"


@pytest.fixture(scope="module")
def handoff() -> HandoffCli:
    prefix, why = _cli_prefix()
    if prefix is None:
        pytest.skip(why)
    return HandoffCli(prefix)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    work = tmp_path / "work"
    subprocess.run(["git", "init", "-q", "-b", "feat/x", str(work)], check=True)
    for key, value in (("user.name", "T"), ("user.email", "t@example.com"), ("commit.gpgsign", "false")):
        _git(work, "config", key, value)
    (work / "a.txt").write_text("one\n")
    _git(work, "add", "a.txt")
    _git(work, "commit", "-q", "-m", "init the repo")
    (work / "dirty-file.txt").write_text("x\n")  # untracked: shows in `git status --short`
    return work


def make_ref(tmp_path: Path, repo: Path, pane: str = "p-1", transcript: str | None = "TRANSCRIPT-TAIL-MARKER\n") -> SessionRef:
    record = tmp_path / "transcript.jsonl"
    if transcript is not None:
        record.write_text(transcript)
    return SessionRef(Harness.CLAUDE, pane, repo, "sess-123", record)


def create_record(handoff: HandoffCli, root: Path, pane: str, goal: str = "a goal") -> str:
    done = handoff.run(["--root", str(root), "create", "--goal", goal, "--source-pane", pane], timeout_s=60)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)["handoff_id"]


def script_runner(mode: str, prompt_out: Path):
    def runner(harness: Harness, prompt: str, *, cwd: Path, timeout_s: float) -> str | None:
        return run_model([sys.executable, str(WRITER), mode, str(prompt_out)], prompt, cwd=cwd, timeout_s=timeout_s)
    return runner


def now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def earlier() -> str:
    return (datetime.now(UTC) - timedelta(hours=1)).isoformat().replace("+00:00", "Z")


def test_wait_returns_id_when_record_appears(handoff: HandoffCli, tmp_path: Path) -> None:
    root = tmp_path / "store"
    since = earlier()
    holder: list[str] = []
    timer = threading.Timer(0.3, lambda: holder.append(create_record(handoff, root, "p-1")))
    timer.start()
    try:
        found = wait_for_briefing(root, since, "p-1", 30.0, poll_s=0.05)
    finally:
        timer.join()
    assert found == holder[0]


def test_wait_none_on_timeout(tmp_path: Path) -> None:
    assert wait_for_briefing(tmp_path / "store", earlier(), "p-1", 0.2, poll_s=0.05) is None


def test_wait_ignores_record_from_other_pane(handoff: HandoffCli, tmp_path: Path) -> None:
    root = tmp_path / "store"
    create_record(handoff, root, "p-2")
    assert wait_for_briefing(root, earlier(), "p-1", 0.2, poll_s=0.05) is None


def test_wait_ignores_record_older_than_since(handoff: HandoffCli, tmp_path: Path) -> None:
    root = tmp_path / "store"
    old = create_record(handoff, root, "p-1")
    time.sleep(0.05)
    assert wait_for_briefing(root, now(), "p-1", 0.2, poll_s=0.05) is None
    assert wait_for_briefing(root, earlier(), "p-1", 0.2, poll_s=0.05) == old


def test_wait_returns_newest_and_ignores_unparsable_records(handoff: HandoffCli, tmp_path: Path) -> None:
    root = tmp_path / "store"
    create_record(handoff, root, "p-1")
    time.sleep(0.02)
    newest = create_record(handoff, root, "p-1")
    (root / "outbox" / ("handoff_" + "a" * 32 + ".json")).write_text("{not json")
    (root / "outbox" / ("handoff_" + "b" * 32 + ".json")).write_text('["a list"]')
    assert wait_for_briefing(root, earlier(), "p-1", 5.0, poll_s=0.05) == newest


def test_fallback_creates_record_marked_fallback(handoff: HandoffCli, tmp_path: Path, repo: Path) -> None:
    root = tmp_path / "store"
    prompt_out = tmp_path / "prompt.txt"
    ref = make_ref(tmp_path, repo)
    found = write_fallback_briefing(ref, repo, root, script_runner("ok", prompt_out), handoff=handoff, gh=GH)
    assert found is not None
    record = json.loads((root / "outbox" / f"{found}.json").read_text())
    assert record["provenance"]["fallback"] is True
    assert record["source"]["pane_id"] == "p-1"
    assert record["source"]["session_id"] == "sess-123"
    assert record["source"]["harness"] == "claude" and record["target"]["harness"] == "claude"
    assert record["context"]["goal"] == "Ship the handoff controller"
    assert record["context"]["remaining"] == ["write the takeover"]
    assert record["context"]["resume_point"] == "start at briefing.py"
    assert record["links"]["pull_requests"] == ["https://github.com/example/repo/pull/7"]
    assert "fallback" in record["context"]["unverified"][0].lower()
    assert record["workspace"]["worktree"] == str(repo.resolve())
    assert wait_for_briefing(root, earlier(), "p-1", 1.0, poll_s=0.05) == found


def test_fallback_fills_the_goal_only_when_missing_and_reads_fenced_json(
        handoff: HandoffCli, tmp_path: Path, repo: Path) -> None:
    root = tmp_path / "store"
    found = write_fallback_briefing(make_ref(tmp_path, repo), repo, root,
                                    script_runner("fenced", tmp_path / "p.txt"), handoff=handoff, gh=GH)
    assert found is not None
    context = json.loads((root / "outbox" / f"{found}.json").read_text())["context"]
    assert "unknown" in context["goal"] and "fallback writer" in context["goal"]
    assert context["remaining"] == ["just one string"]


def test_fallback_input_includes_git_status_and_pr_list(handoff: HandoffCli, tmp_path: Path, repo: Path) -> None:
    prompt_out = tmp_path / "prompt.txt"
    big = "x" * 5000 + "\nTRANSCRIPT-TAIL-MARKER\n"
    ref = make_ref(tmp_path, repo, transcript=big)
    write_fallback_briefing(ref, repo, tmp_path / "store", script_runner("ok", prompt_out), handoff=handoff, gh=GH,
                            transcript_tail_bytes=100)
    prompt = prompt_out.read_text()
    assert "?? dirty-file.txt" in prompt
    assert "init the repo" in prompt
    assert "feat: the controller" in prompt and "feat/handoff-controller" in prompt
    assert "TRANSCRIPT-TAIL-MARKER" in prompt
    assert "x" * 200 not in prompt  # only the tail went in


def test_fallback_notes_missing_transcript_and_missing_gh_without_failing(
        handoff: HandoffCli, tmp_path: Path, repo: Path) -> None:
    prompt_out = tmp_path / "prompt.txt"
    ref = make_ref(tmp_path, repo, transcript=None)
    found = write_fallback_briefing(ref, repo, tmp_path / "store", script_runner("ok", prompt_out), handoff=handoff,
                                    gh=(str(tmp_path / "no-such-gh"),))
    assert found is not None
    prompt = prompt_out.read_text()
    assert "transcript: not available" in prompt
    assert "gh pr list: not available" in prompt


def test_fallback_unparsable_answer_gives_none_and_no_record(handoff: HandoffCli, tmp_path: Path, repo: Path) -> None:
    root = tmp_path / "store"
    found = write_fallback_briefing(make_ref(tmp_path, repo), repo, root,
                                    script_runner("garbage", tmp_path / "p.txt"), handoff=handoff, gh=GH)
    assert found is None
    assert not list((root / "outbox").glob("handoff_*.json")) if (root / "outbox").exists() else True


def test_fallback_runner_timeout_gives_none(handoff: HandoffCli, tmp_path: Path, repo: Path) -> None:
    root = tmp_path / "store"
    started = time.monotonic()
    found = write_fallback_briefing(make_ref(tmp_path, repo), repo, root, script_runner("sleep", tmp_path / "p.txt"),
                                    handoff=handoff, gh=GH, timeout_s=0.5)
    assert found is None
    assert time.monotonic() - started < 20
    assert not (root / "outbox").exists() or not list((root / "outbox").glob("handoff_*.json"))


def test_headless_argv_per_harness(tmp_path: Path) -> None:
    no_help = {"PATH": str(tmp_path)}  # no harness on PATH: only the base flags
    assert headless_argv(Harness.CLAUDE, no_help) == [
        "claude", "-p", "--output-format", "text", "--no-session-persistence", "--setting-sources", "",
        "--strict-mcp-config", "--disable-slash-commands", "--tools", ""]
    assert headless_argv(Harness.PI, no_help) == [
        "pi", "-p", "--no-session", "--no-tools", "--no-extensions", "--no-mcp", "--no-skills",
        "--no-prompt-templates", "--no-context-files"]
    assert headless_argv(Harness.CODEX, no_help) == [
        "codex", "exec", "--sandbox", "read-only", "--skip-git-repo-check", "--ephemeral", "-"]
    assert fallback_supported(Harness.CODEX)


@pytest.mark.parametrize("harness", [Harness.OPENCODE])
def test_harness_without_a_proven_tool_off_switch_is_unsupported(harness: Harness, tmp_path: Path) -> None:
    assert not fallback_supported(harness) and headless_argv(harness) is None
    assert headless_runner(harness, "p", cwd=tmp_path, timeout_s=5) is None


def test_unsupported_harness_writes_nothing_and_starts_no_process(
        handoff: HandoffCli, tmp_path: Path, repo: Path) -> None:
    prompt_out = tmp_path / "prompt.txt"
    ref = SessionRef(Harness.OPENCODE, "p-1", repo, "s", None)
    assert write_fallback_briefing(ref, repo, tmp_path / "store", script_runner("ok", prompt_out), handoff=handoff,
                                   gh=GH) is None
    assert not prompt_out.exists() and not (tmp_path / "store").exists()


def _bin_with(tmp_path: Path, name: str, body: str) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    script = bin_dir / name
    script.write_text("#!/bin/sh\n" + body + "\n")
    script.chmod(0o755)
    return {"PATH": f"{bin_dir}:/usr/bin:/bin"}


def test_headless_runner_passes_the_prompt_on_stdin(tmp_path: Path) -> None:
    env = _bin_with(tmp_path, "claude", "cat")
    assert headless_runner(Harness.CLAUDE, "the prompt", cwd=tmp_path, timeout_s=10, env=env) == "the prompt"


def test_headless_runner_missing_binary_gives_none(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    assert headless_runner(Harness.CLAUDE, "p", cwd=tmp_path, timeout_s=5, env={"PATH": str(empty)}) is None


def test_headless_runner_timeout_gives_none(tmp_path: Path) -> None:
    env = _bin_with(tmp_path, "pi", "/bin/sleep 30")
    started = time.monotonic()
    assert headless_runner(Harness.PI, "p", cwd=tmp_path, timeout_s=0.5, env=env) is None
    assert time.monotonic() - started < 20


def test_headless_runner_nonzero_exit_gives_none(tmp_path: Path) -> None:
    env = _bin_with(tmp_path, "pi", "echo partial; exit 3")
    assert headless_runner(Harness.PI, "p", cwd=tmp_path, timeout_s=5, env=env) is None


def test_headless_runner_does_not_inherit_the_sessions_identity(tmp_path: Path) -> None:
    env = _bin_with(tmp_path, "claude", "/usr/bin/env")
    env.update({"HERDR_PANE_ID": "p-1", "HERDR_SOCKET_PATH": "s", "HERDR_HANDOFF_STORE": "x",
                "CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli", "CLAUDE_CODE_SESSION_ID": "s",
                "CLAUDE_SESSION_ID": "s", "CODEX_THREAD_ID": "t", "CODEX_SANDBOX": "seatbelt",
                "PI_CODING_AGENT": "true", "OPENCODE": "1", "OPENCODE_PID": "9", "OPENCODE_SESSION_ID": "s",
                "ANTHROPIC_API_KEY": "keep-me", "HOME": "/home/keep"})
    seen = dict(line.split("=", 1) for line in (headless_runner(
        Harness.CLAUDE, "p", cwd=tmp_path, timeout_s=10, env=env) or "").splitlines() if "=" in line)
    assert seen, "the stand-in model call printed nothing"
    stripped = [k for k in seen if k.startswith("HERDR_")] + [k for k in seen if k in (
        "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "CODEX_THREAD_ID",
        "CODEX_SANDBOX", "PI_CODING_AGENT", "OPENCODE", "OPENCODE_PID", "OPENCODE_SESSION_ID")]
    assert stripped == []
    assert seen["ANTHROPIC_API_KEY"] == "keep-me" and seen["HOME"] == "/home/keep"


def test_fallback_model_runs_in_a_fresh_empty_directory_removed_afterwards(
        handoff: HandoffCli, tmp_path: Path, repo: Path) -> None:
    root = tmp_path / "store"
    seen: list[tuple[Path, list[str]]] = []

    def runner(harness: Harness, prompt: str, *, cwd: Path, timeout_s: float) -> str | None:
        seen.append((cwd, [p.name for p in cwd.iterdir()]))
        return None
    assert write_fallback_briefing(make_ref(tmp_path, repo), repo, root, runner, handoff=handoff, gh=GH) is None
    cwd, listing = seen[0]
    assert cwd.parent == root / "fallback-work" and cwd != repo and listing == []
    assert not cwd.exists()


def test_a_raising_runner_gives_none_and_still_removes_the_directory(
        handoff: HandoffCli, tmp_path: Path, repo: Path) -> None:
    root = tmp_path / "store"

    def runner(harness: Harness, prompt: str, *, cwd: Path, timeout_s: float) -> str | None:
        raise RuntimeError("boom")
    assert write_fallback_briefing(make_ref(tmp_path, repo), repo, root, runner, handoff=handoff, gh=GH) is None
    assert list((root / "fallback-work").iterdir()) == []


def test_goal_starting_with_dashes_is_not_read_as_a_flag(handoff: HandoffCli, tmp_path: Path, repo: Path) -> None:
    root = tmp_path / "store"

    def runner(harness: Harness, prompt: str, *, cwd: Path, timeout_s: float) -> str | None:
        return json.dumps({"goal": "--remaining=oops", "resume_point": "-x"})
    found = write_fallback_briefing(make_ref(tmp_path, repo), repo, root, runner, handoff=handoff, gh=GH)
    assert found is not None
    assert json.loads((root / "outbox" / f"{found}.json").read_text())["context"]["goal"] == "--remaining=oops"


def test_timeout_bounds_the_whole_call(handoff: HandoffCli, tmp_path: Path, repo: Path) -> None:
    started = time.monotonic()
    assert write_fallback_briefing(make_ref(tmp_path, repo), repo, tmp_path / "store",
                                   script_runner("sleep", tmp_path / "p.txt"), handoff=handoff, gh=GH,
                                   timeout_s=1.5) is None
    assert time.monotonic() - started < 3.5


def test_transcript_that_is_not_a_regular_file_is_not_read(handoff: HandoffCli, tmp_path: Path, repo: Path) -> None:
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    link = tmp_path / "link.jsonl"
    link.symlink_to(fifo)
    prompt_out = tmp_path / "prompt.txt"
    ref = SessionRef(Harness.CLAUDE, "p-1", repo, "s", link)
    started = time.monotonic()
    assert write_fallback_briefing(ref, repo, tmp_path / "store", script_runner("ok", prompt_out), handoff=handoff,
                                   gh=GH) is not None
    assert time.monotonic() - started < 20
    assert "transcript: not available" in prompt_out.read_text()


def test_git_and_gh_stderr_text_stays_out_of_the_prompt(handoff: HandoffCli, tmp_path: Path) -> None:
    prompt_out = tmp_path / "prompt.txt"
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()
    ref = SessionRef(Harness.CLAUDE, "p-1", not_a_repo, "s", None)
    write_fallback_briefing(ref, not_a_repo, tmp_path / "store", script_runner("ok", prompt_out), handoff=handoff,
                            gh=(sys.executable, str(FIXTURES / "gh_pr_list.py"), "bad"))
    prompt = prompt_out.read_text()
    assert "git status --short: not available (git failed, exit" in prompt
    assert "not a git repository" not in prompt and "unsupported" not in prompt


def test_the_fallback_flag_is_used_when_the_cli_offers_it(handoff: HandoffCli, tmp_path: Path, repo: Path) -> None:
    if "--fallback" not in handoff.run(["create", "--help"], timeout_s=30).stdout:
        pytest.skip("herdr-handoff create has no --fallback yet; the record rewrite is covered by the marked-fallback test")
    root = tmp_path / "store"
    found = write_fallback_briefing(make_ref(tmp_path, repo), repo, root,
                                    script_runner("ok", tmp_path / "p.txt"), handoff=handoff, gh=GH)
    assert found is not None
    assert json.loads((root / "outbox" / f"{found}.json").read_text())["provenance"]["fallback"] is True


def test_naive_since_is_rejected_and_a_non_utc_offset_is_honoured(handoff: HandoffCli, tmp_path: Path) -> None:
    root = tmp_path / "store"
    found = create_record(handoff, root, "p-1")
    naive = (datetime.now(UTC) - timedelta(hours=1)).replace(tzinfo=None).isoformat()
    assert wait_for_briefing(root, naive, "p-1", 0.2, poll_s=0.05) is None  # no zone: ambiguous, never guessed
    plus2 = timezone(timedelta(hours=2))
    assert wait_for_briefing(root, (datetime.now(plus2) - timedelta(hours=1)).isoformat(), "p-1", 0.2,
                             poll_s=0.05) == found
    assert wait_for_briefing(root, (datetime.now(plus2) + timedelta(hours=1)).isoformat(), "p-1", 0.2,
                             poll_s=0.05) is None

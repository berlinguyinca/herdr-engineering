"""Takeover, retries and the compaction fallback: a real isolated Herdr server, a real herdr-handoff 0.5.0 CLI, real git repos.

The composer-shaped fixture program is both the source harness and the child the takeover launches (herdr-handoff
resolves the harness binary with `which`, and the CLI's PATH starts with the fixture's bin directory). herdr-handoff
gets the isolated server's environment and working directory, so no call reaches the user's own Herdr.
"""
from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from herdr_engineering.handoff_adapters import adapter_for
from herdr_engineering.handoff_adapters.base import ExitPlan, Harness, SessionRef, TypedInput
from herdr_engineering.handoff_controller.compaction import (
    CompactionResult,
    compact_with_pointer,
    not_ready_reason,
    pointer_text,
)
from herdr_engineering.handoff_controller.config import ControllerConfig
from herdr_engineering.handoff_controller.events import (
    Colour,
    EventLog,
    HandoffEvent,
    StuckReason,
    source_is_handed_off,
)
from herdr_engineering.handoff_controller.handoff_cli import (
    HandoffCli,
    HandoffCliResult,
    takeover_args,
)
from herdr_engineering.handoff_controller.herdr_cli import HerdrCli
from herdr_engineering.handoff_controller.takeover import (
    Action,
    Outcome,
    RecordView,
    classify,
    next_action,
    next_after_attempts,
    record_view,
    retry_can_help,
    run_takeover,
    stuck_reason,
)
from herdr_engineering.handoff_subagent_registry import record as record_subagent
from tests.handoff_controller_herdr import IsolatedHerdr, isolated_herdr, wait_for
from tests.test_handoff_controller_briefing import _cli_prefix

ADAPTER = adapter_for(Harness.CLAUDE)
herdr_server = pytest.mark.herdr_server
FAST = {"confirm_timeout_s": 15.0, "retry_delay_s": 0.0, "poll_s": 0.1, "compact_ready_timeout_s": 10.0,
        "compact_work_start_s": 5.0, "compact_work_end_s": 15.0}
CHILD_ID = "handoff_" + "a" * 32


# --- decisions and argv, no Herdr needed -----------------------------------------------------------------------------

def test_takeover_args_pass_the_exit_plan_to_herdr_handoff_and_never_type_it() -> None:
    args = takeover_args(Path("/store"), CHILD_ID, parent_pane="w1:p2", worktree=Path("/work"),
                         exit_plan=ExitPlan("/exit", ("Enter",), 15.0), allow_dirty=False, confirm_timeout_s=25.0)
    assert args == ["--root", "/store", "takeover", CHILD_ID, "--parent-pane", "w1:p2", "--direction", "down",
                    "--cwd", "/work", "--source-cwd", "/work", "--confirm-timeout", "25",
                    "--source-exit-text", "/exit", "--source-exit-key", "enter", "--source-exit-timeout", "15"]


def test_takeover_args_allow_dirty_only_when_asked_and_keys_only_plans_have_no_text() -> None:
    plan = ExitPlan(None, ("ctrl+c", "ctrl+c"), 5.0)
    args = takeover_args(Path("/s"), CHILD_ID, parent_pane="p", worktree=Path("/w"), exit_plan=plan,
                         allow_dirty=True, confirm_timeout_s=1.5)
    assert "--allow-dirty-source" in args and "--source-exit-text" not in args
    assert args.count("--source-exit-key") == 2 and args[args.index("--confirm-timeout") + 1] == "1.5"
    clean = takeover_args(Path("/s"), CHILD_ID, parent_pane="p", worktree=Path("/w"), exit_plan=plan,
                          allow_dirty=False, confirm_timeout_s=1.0)
    assert "--allow-dirty-source" not in clean and "--keep-source" not in clean


def test_a_plan_with_nothing_to_send_passes_no_exit_flags() -> None:
    args = takeover_args(Path("/s"), CHILD_ID, parent_pane="p", worktree=Path("/w"),
                         exit_plan=ExitPlan(None, (), 1.0), allow_dirty=False, confirm_timeout_s=1.0)
    assert not [a for a in args if a.startswith("--source-exit")]


def test_a_fresh_record_is_launched_and_a_closed_one_is_done() -> None:
    assert next_action(record_view({"takeover": None})) is Action.LAUNCH
    assert next_action(record_view(None)) is Action.LAUNCH
    closed = record_view({"takeover": {"phase": "source_closed", "child_pane": "w1:p9"}})
    assert next_action(closed) is Action.DONE


@pytest.mark.parametrize("phase", ["launched", "confirmed", "unconfirmed", "source_exit_failed"])
def test_a_recorded_child_is_reused_by_running_takeover_again_never_a_second_launch(phase: str) -> None:
    view = record_view({"takeover": {"phase": phase, "child_pane": "w1:p9"}})
    assert view.child_pane == "w1:p9"
    assert next_action(view) is Action.LAUNCH  # herdr-handoff reuses `child_pane` from the same record


def test_a_successor_without_a_child_is_never_launched_again() -> None:
    assert next_action(record_view({"successor_id": CHILD_ID})) is Action.STUCK
    assert next_action(record_view({"successor_id": CHILD_ID, "takeover": {"phase": "confirmed", "child_pane": "p"}})) \
        is Action.LAUNCH


def test_a_takeover_killed_between_placing_the_pane_and_recording_it_is_not_launched_again() -> None:
    """herdr-handoff 0.5.0 records `launching` before it places the pane and `launched` (with `child_pane`) after: a
    record stuck in `launching` has no child_pane, and a child may or may not exist."""
    assert next_action(record_view({"takeover": {"phase": "launching", "source_pane": "w1:p1"}})) \
        is Action.LAUNCH_UNKNOWN
    assert next_after_attempts(record_view({"takeover": {"phase": "launching"}})) is Action.LAUNCH_UNKNOWN


def test_malformed_records_read_as_absent() -> None:
    assert record_view({"takeover": "x", "target": [], "successor_id": 3}) == RecordView(None, None, None, None)


def test_only_a_confirmed_child_makes_the_handoff_a_fact() -> None:
    def view(phase: str) -> RecordView:
        return record_view({"takeover": {"phase": phase, "child_pane": "w1:p9"}, "target": {"session_id": "s-9"}})

    assert next_after_attempts(view("confirmed")) is Action.SUCCESSOR_LIVE  # e.g. after source_dirty
    assert next_after_attempts(view("source_exit_failed")) is Action.SUCCESSOR_LIVE
    assert next_after_attempts(view("unconfirmed")) is Action.COMPACT  # launch_unconfirmed
    assert next_after_attempts(view("launched")) is Action.COMPACT
    assert next_after_attempts(view("source_closed")) is Action.DONE
    assert next_after_attempts(record_view(None)) is Action.COMPACT  # nothing was ever launched
    assert view("confirmed").child_session_id == "s-9"


def test_source_dirty_is_not_retried_and_every_other_refusal_is() -> None:
    for status in ("launch_unconfirmed", "launch_failed", "source_close_failed", "subagents_running",
                   "source_exit_failed"):
        assert retry_can_help(classify(_result(1, {"status": status})))
    assert not retry_can_help(classify(_result(1, {"status": "source_dirty"})))
    assert retry_can_help(classify(_result(124, "")))


@pytest.mark.parametrize("harness", list(Harness))
def test_every_adapter_types_the_slash_compact_command_and_submits_it(harness: Harness) -> None:
    assert adapter_for(harness).compact_input() == TypedInput("/compact", ("Enter",))


def _event(name: str, reason: str | None = None, colour: Colour | None = None, pane: str = "w1:p1") -> HandoffEvent:
    return HandoffEvent(name, "t", "c", pane, None, Harness.CLAUDE, None, None, None, colour, reason, None)


def test_a_source_with_a_live_successor_is_never_drained_again() -> None:
    live = stuck_reason(StuckReason.SOURCE_NOT_CLOSED_SUCCESSOR_LIVE, "w1:p9", "source_dirty")
    assert source_is_handed_off([_event("handoff.stuck", live, Colour.RED)])
    assert source_is_handed_off([_event("handoff.succeeded", None, Colour.GREEN)])
    assert not source_is_handed_off([])
    assert not source_is_handed_off([_event("handoff.takeover_started")])
    assert not source_is_handed_off([_event("handoff.failed_compacted", "source_dirty", Colour.ORANGE)])
    other = stuck_reason(StuckReason.TAKEOVER_REFUSED_AND_COMPACTION_FAILED, StuckReason.BLOCKED, "source_dirty")
    assert not source_is_handed_off([_event("handoff.stuck", other, Colour.RED)])
    assert not source_is_handed_off([_event("handoff.stuck", StuckReason.LAUNCH_STATE_UNKNOWN.value, Colour.RED)])


def _result(code: int, body: object, err: str = "") -> HandoffCliResult:
    return HandoffCliResult(code, body if isinstance(body, str) else json.dumps(body), err)


def test_classify_reads_the_status_of_the_json_reply() -> None:
    closed = classify(_result(0, {"status": "source_closed", "source_closed": True, "pane_id": "w1:p5"}))
    assert (closed.outcome, closed.child_pane) == (Outcome.CLOSED, "w1:p5")
    dirty = classify(_result(1, {"status": "source_dirty", "reason": "worktree_dirty: x", "pane_id": "w1:p5"}))
    assert dirty.outcome is Outcome.REFUSED and dirty.reason == "source_dirty: worktree_dirty: x"
    assert classify(_result(1, {"status": "subagents_running"})).reason == "subagents_running"


@pytest.mark.parametrize("done", [_result(124, ""), _result(127, "", "no such file"), _result(1, "", "boom\nmore"),
                                  _result(0, "[]"), _result(0, {"no": "status"})])
def test_classify_never_reads_silence_or_noise_as_success(done: HandoffCliResult) -> None:
    assert classify(done).outcome is Outcome.FAILED


def test_exit_status_zero_with_the_source_open_is_not_a_close() -> None:
    assert classify(_result(0, {"status": "confirmed", "source_closed": False})).outcome is Outcome.REFUSED
    assert classify(_result(1, {"status": "source_closed", "source_closed": True})).outcome is Outcome.REFUSED


def test_stuck_reasons_name_what_failed_and_what_came_before() -> None:
    text = stuck_reason(StuckReason.TAKEOVER_REFUSED_AND_COMPACTION_FAILED, StuckReason.WRONG_AGENT, "source_dirty")
    assert text == "takeover_refused_and_compaction_failed: wrong_agent; last takeover result: source_dirty"
    assert stuck_reason(StuckReason.NO_BRIEFING, None, None) == "no_briefing"
    assert {r.value for r in StuckReason} >= {"takeover_refused_and_compaction_failed", "blocked", "wrong_agent",
                                              "git_state_unknown", "on_main_with_changes", "no_briefing"}


def test_the_pointer_text_and_the_reasons_a_wait_can_end_with() -> None:
    assert pointer_text(Path("/s/documents/h.md")) == "Your briefing is /s/documents/h.md; read it after compaction"
    assert not_ready_reason("blocked") is StuckReason.BLOCKED
    assert not_ready_reason("wrong_agent") is StuckReason.WRONG_AGENT
    assert not_ready_reason("timeout") is StuckReason.COMPACTION_NOT_READY
    assert not CompactionResult(False, StuckReason.BLOCKED) and CompactionResult(True)


def test_the_cli_runs_in_the_environment_and_directory_it_was_given(tmp_path: Path) -> None:
    """herdr-handoff passes its environment on to every `herdr` it runs, so this is what keeps it off the user's socket."""
    probe = HandoffCli([sys.executable, "-c", "import os; print(os.environ['HERDR_SOCKET_PATH'], os.getcwd())"],
                       env={"HERDR_SOCKET_PATH": "s.sock"}, cwd=tmp_path)
    done = probe.run([], timeout_s=30)
    assert done.stdout.split() == ["s.sock", str(tmp_path.resolve())], done


# --- the real thing --------------------------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def _server() -> Iterator[IsolatedHerdr]:
    with isolated_herdr("handoff_controller_takeover") as herdr:
        yield herdr


@pytest.fixture
def server(_server: IsolatedHerdr) -> IsolatedHerdr:
    _server.submitted.unlink(missing_ok=True)
    _server.program.with_name("start_delay_s").unlink(missing_ok=True)
    for pane in panes(_server):
        subprocess.run(["herdr", "pane", "close", pane], env=_server.env, cwd=_server.root, capture_output=True,
                       timeout=30, check=False)
    return _server


@pytest.fixture(scope="module")
def prefix() -> list[str]:
    found, why = _cli_prefix()
    if found is None:
        pytest.skip(why)
    return found


@pytest.fixture
def handoff(server: IsolatedHerdr, prefix: list[str]) -> HandoffCli:
    env = dict(server.env)
    env["PATH"] = f"{server.root / 'bin'}:{env.get('PATH', '')}"  # `which claude` is the fixture program
    return HandoffCli(prefix, env=env, cwd=server.root)


def panes(server: IsolatedHerdr) -> list[str]:
    done = subprocess.run(["herdr", "pane", "list"], env=server.env, cwd=server.root, capture_output=True, text=True,
                          timeout=30, check=True)
    return [p["pane_id"] for p in json.loads(done.stdout)["result"]["panes"]]


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)


def make_repo(tmp_path: Path, *, dirty: bool) -> Path:
    """A feature-branch worktree whose only commit is on a real bare remote; `dirty` adds an uncommitted tracked change."""
    bare, work = tmp_path / "remote.git", tmp_path / "work"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    subprocess.run(["git", "init", "-q", "-b", "feat/x", str(work)], check=True)
    for key, value in (("user.name", "T"), ("user.email", "t@example.com"), ("commit.gpgsign", "false")):
        git(work, "config", key, value)
    (work / "a.txt").write_text("one\n")
    git(work, "add", "a.txt")
    git(work, "commit", "-q", "-m", "init")
    git(work, "remote", "add", "origin", str(bare))
    git(work, "push", "-q", "-u", "origin", "feat/x")
    if dirty:
        (work / "a.txt").write_text("two\n")
    return work


def new_record(handoff: HandoffCli, store: Path, pane: str, work: Path) -> str:
    done = handoff.run(["--root", str(store), "create", "--goal", "carry on", "--source-pane", pane,
                        "--source-harness", "claude", "--target-harness", "claude", "--worktree", str(work),
                        "--workspace-cwd", str(work)], timeout_s=60)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)["handoff_id"]


def make_ref(pane: str, work: Path) -> SessionRef:
    return SessionRef(Harness.CLAUDE, pane, work, None, None)


def submitted(server: IsolatedHerdr) -> list[str]:
    return server.submitted.read_text().splitlines() if server.submitted.exists() else []


def start_idle(server: IsolatedHerdr, extra_env: str = "") -> str:
    pane = server.start(extra_env=extra_env)
    assert wait_for(lambda: server.cli.agent_status(pane) == "idle", 15.0), "the source pane never became idle"
    return pane


@herdr_server
def test_success_emits_succeeded_and_closes_source_after_exit(server: IsolatedHerdr, handoff: HandoffCli,
                                                              tmp_path: Path) -> None:
    work, store = make_repo(tmp_path, dirty=False), tmp_path / "store"
    source = start_idle(server)
    handoff_id = new_record(handoff, store, source, work)
    log = EventLog(store)
    done = run_takeover(make_ref(source, work), ADAPTER, handoff_id, ControllerConfig(), log, handoff, server.cli,
                        store_root=store, worktree=work, push_allowed=True, env=server.env, **FAST)
    assert (done.event, done.colour) == ("handoff.succeeded", Colour.GREEN), done
    assert done.handoff_id == handoff_id and done.successor_id is None  # no successor handoff record was made
    assert [e.event for e in log.read()] == ["handoff.takeover_started", "handoff.succeeded"]
    assert log.read()[-1] == done
    assert submitted(server)[0] == "/exit"  # herdr-handoff typed the exit plan into the source; the controller did not
    left = panes(server)
    assert source not in left and len(left) == 1  # the source closed, exactly one successor
    assert done.successor_pane_id == left[0] and done.successor_session_id is None
    record = json.loads((store / "outbox" / f"{handoff_id}.json").read_text())
    assert record["takeover"]["phase"] == "source_closed" and record["takeover"]["child_pane"] == left[0]


@herdr_server
def test_refusal_keeps_source_and_retries_then_compacts(server: IsolatedHerdr, handoff: HandoffCli,
                                                        tmp_path: Path) -> None:
    """The child is launched but never confirmed (herdr-handoff is given a confirm timeout shorter than the child's
    start-up), so nothing proves the handoff happened: the source is compacted, and the event names the child."""
    work, store = make_repo(tmp_path, dirty=False), tmp_path / "store"
    source = start_idle(server, "FAKE_TUI_COMPACT_S=0.8")
    server.start_delay(3.0)  # the child starts slowly: the 0.5 s confirmation cannot see it
    handoff_id = new_record(handoff, store, source, work)
    log = EventLog(store)
    done = run_takeover(make_ref(source, work), ADAPTER, handoff_id, ControllerConfig(max_attempts=2), log, handoff,
                        server.cli, store_root=store, worktree=work, push_allowed=True, env=server.env,
                        **{**FAST, "confirm_timeout_s": 0.5})
    assert (done.event, done.colour) == ("handoff.failed_compacted", Colour.ORANGE), done.reason
    assert done.reason and done.reason.startswith("launch_unconfirmed"), done.reason
    assert [e.event for e in log.read()] == ["handoff.takeover_started", "handoff.failed_compacted"]
    document = handoff.document(store, handoff_id)
    assert document is not None and document.is_file()
    assert submitted(server)[:2] == ["/compact", pointer_text(document)]
    (child,) = [p for p in panes(server) if p != source]  # the source stayed; the retry reused the one child
    assert done.successor_pane_id == child and done.successor_id is None


@herdr_server
def test_a_confirmed_child_with_a_dirty_source_is_red_and_never_compacted(server: IsolatedHerdr, handoff: HandoffCli,
                                                                         tmp_path: Path) -> None:
    work, store = make_repo(tmp_path, dirty=True), tmp_path / "store"
    source = start_idle(server)
    handoff_id = new_record(handoff, store, source, work)
    log = EventLog(store)
    done = run_takeover(make_ref(source, work), ADAPTER, handoff_id, ControllerConfig(max_attempts=3), log, handoff,
                        server.cli, store_root=store, worktree=work, push_allowed=True, env=server.env, **FAST)
    (child,) = [p for p in panes(server) if p != source]
    assert (done.event, done.colour) == ("handoff.stuck", Colour.RED), done
    assert done.reason and done.reason.startswith(f"source_not_closed_successor_live: {child}"), done.reason
    assert "last takeover result: source_dirty" in done.reason
    assert done.successor_pane_id == child
    assert submitted(server) == []  # no /compact, no pointer: the successor is already working
    assert source in panes(server)  # the controller never closes the source
    assert source_is_handed_off(log.read())


@herdr_server
def test_a_source_that_ignores_exit_is_retried_on_the_same_child_then_red(server: IsolatedHerdr, handoff: HandoffCli,
                                                                        tmp_path: Path) -> None:
    work, store = make_repo(tmp_path, dirty=False), tmp_path / "store"
    source = start_idle(server, "FAKE_TUI_IGNORE_EXIT=1")
    handoff_id = new_record(handoff, store, source, work)
    done = run_takeover(make_ref(source, work), ShortExit(1.0), handoff_id, ControllerConfig(max_attempts=2),  # type: ignore[arg-type]
                        EventLog(store), handoff, server.cli, store_root=store, worktree=work, push_allowed=True,
                        env=server.env, **FAST)
    (child,) = [p for p in panes(server) if p != source]  # one child, reused by the retry
    assert (done.event, done.colour) == ("handoff.stuck", Colour.RED), done
    assert done.reason and done.reason.startswith("source_not_closed_successor_live"), done.reason
    assert "source_exit_failed" in done.reason and done.successor_pane_id == child
    assert [t for t in submitted(server) if t != "/exit"] == []


@herdr_server
def test_retry_after_crash_does_not_launch_second_child(server: IsolatedHerdr, handoff: HandoffCli,
                                                        tmp_path: Path) -> None:
    work, store = make_repo(tmp_path, dirty=False), tmp_path / "store"
    source = start_idle(server)
    handoff_id = new_record(handoff, store, source, work)
    crashed = handoff.takeover(store, handoff_id, parent_pane=source, worktree=work, exit_plan=ADAPTER.exit_plan(),
                               allow_dirty=False, confirm_timeout_s=15.0, keep_source=True, timeout_s=90.0)
    assert json.loads(crashed.stdout)["status"] == "confirmed", crashed  # the child is confirmed; the source is open
    (child,) = [p for p in panes(server) if p != source]
    log = EventLog(store)
    done = run_takeover(make_ref(source, work), ADAPTER, handoff_id, ControllerConfig(), log, handoff, server.cli,
                        store_root=store, worktree=work, push_allowed=True, env=server.env, **FAST)
    assert done.event == "handoff.succeeded", done
    assert panes(server) == [child]  # the source closed and no second child was launched
    assert done.successor_pane_id == child


@herdr_server
def test_a_closed_record_is_finished_without_calling_takeover(server: IsolatedHerdr, handoff: HandoffCli,
                                                              tmp_path: Path) -> None:
    work, store = make_repo(tmp_path, dirty=False), tmp_path / "store"
    source = start_idle(server)
    handoff_id = new_record(handoff, store, source, work)
    first = run_takeover(make_ref(source, work), ADAPTER, handoff_id, ControllerConfig(), EventLog(store), handoff,
                         server.cli, store_root=store, worktree=work, push_allowed=True, env=server.env, **FAST)
    assert first.event == "handoff.succeeded"
    again = run_takeover(make_ref(source, work), ADAPTER, handoff_id, ControllerConfig(), EventLog(store), handoff,
                         server.cli, store_root=store, worktree=work, push_allowed=True, env=server.env, **FAST)
    assert (again.event, again.successor_pane_id) == ("handoff.succeeded", first.successor_pane_id)
    assert len(panes(server)) == 1


@herdr_server
def test_push_not_allowed_passes_allow_dirty_and_the_dirty_source_is_closed(server: IsolatedHerdr, handoff: HandoffCli,
                                                                           tmp_path: Path) -> None:
    work, store = make_repo(tmp_path, dirty=True), tmp_path / "store"
    source = start_idle(server)
    handoff_id = new_record(handoff, store, source, work)
    done = run_takeover(make_ref(source, work), ADAPTER, handoff_id, ControllerConfig(), EventLog(store), handoff,
                        server.cli, store_root=store, worktree=work, push_allowed=False, env=server.env, **FAST)
    assert done.event == "handoff.succeeded", done
    assert source not in panes(server)


@herdr_server
def test_compaction_pointer_typed_after_compact(server: IsolatedHerdr, tmp_path: Path) -> None:
    source = start_idle(server, "FAKE_TUI_COMPACT_S=0.8")
    pointer = tmp_path / "documents" / "h.md"
    done = compact_with_pointer(make_ref(source, tmp_path), ADAPTER, pointer, server.cli, poll_s=0.1,
                                ready_timeout_s=10.0, work_start_timeout_s=5.0, work_end_timeout_s=15.0)
    assert done and done.reason is None
    assert submitted(server) == ["/compact", f"Your briefing is {pointer}; read it after compaction"]
    assert "Compacted" in server.cli.read_visible(source)  # the pointer came after the compaction finished


@herdr_server
def test_compaction_is_not_typed_over_a_draft(server: IsolatedHerdr, tmp_path: Path) -> None:
    source = start_idle(server, "FAKE_TUI_PREFILL=half-written")
    done = compact_with_pointer(make_ref(source, tmp_path), ADAPTER, tmp_path / "h.md", server.cli, poll_s=0.1,
                                ready_timeout_s=0.6)
    assert not done and done.reason is StuckReason.COMPACTION_NOT_READY
    assert submitted(server) == [] and "half-written" in server.cli.read_visible(source)


@herdr_server
def test_compaction_is_not_typed_into_a_blocked_agent_or_another_harness(server: IsolatedHerdr, tmp_path: Path) -> None:
    blocked = server.start(extra_env="FAKE_TUI_BLOCKED=1")
    assert wait_for(lambda: server.cli.agent_status(blocked) == "blocked", 15.0)
    done = compact_with_pointer(make_ref(blocked, tmp_path), ADAPTER, tmp_path / "h.md", server.cli, poll_s=0.1,
                                ready_timeout_s=0.5)
    assert done.reason is StuckReason.BLOCKED
    idle = start_idle(server)
    codex = SessionRef(Harness.CODEX, idle, tmp_path, None, None)
    other = compact_with_pointer(codex, adapter_for(Harness.CODEX), tmp_path / "h.md", server.cli, poll_s=0.1,
                                 ready_timeout_s=0.5)
    assert other.reason is StuckReason.WRONG_AGENT
    assert submitted(server) == []


@herdr_server
def test_no_way_forward_is_red_with_reason(server: IsolatedHerdr, handoff: HandoffCli, tmp_path: Path) -> None:
    work, store = make_repo(tmp_path, dirty=False), tmp_path / "store"
    source = start_idle(server, "FAKE_TUI_PREFILL=half-written")
    server.start_delay(3.0)  # the child starts slowly: the 0.5 s confirmation cannot see it
    handoff_id = new_record(handoff, store, source, work)
    log = EventLog(store)
    done = run_takeover(make_ref(source, work), ADAPTER, handoff_id, ControllerConfig(max_attempts=2), log, handoff,
                        server.cli, store_root=store, worktree=work, push_allowed=True, env=server.env,
                        **{**FAST, "confirm_timeout_s": 0.5, "compact_ready_timeout_s": 0.6})
    assert (done.event, done.colour) == ("handoff.stuck", Colour.RED), done
    assert done.reason and done.reason.startswith("takeover_refused_and_compaction_failed: compaction_not_ready")
    assert "last takeover result: launch_unconfirmed" in done.reason
    assert [e.event for e in log.read()] == ["handoff.takeover_started", "handoff.stuck"]
    assert source in panes(server) and submitted(server) == []  # nothing typed, nothing closed
    assert done.successor_pane_id in panes(server) and not source_is_handed_off(log.read())


@herdr_server
def test_finish_runs_for_every_terminal_event_and_the_gate_registry_is_purged(
        server: IsolatedHerdr, handoff: HandoffCli, tmp_path: Path) -> None:
    work, store = make_repo(tmp_path, dirty=True), tmp_path / "store"
    source = start_idle(server)
    handoff_id = new_record(handoff, store, source, work)
    state = {**server.env, "HERDR_ENGINEERING_STATE": str(tmp_path / "state"), "HERDR_PANE_ID": source}
    record_subagent("start", json.dumps({"agent_id": "a1", "agent_type": "x"}), state)
    registry = tmp_path / "state" / "subagents"
    assert any(registry.rglob("a1"))
    done = run_takeover(make_ref(source, work), ADAPTER, handoff_id, ControllerConfig(max_attempts=1), EventLog(store),
                        handoff, server.cli, store_root=store, worktree=work, push_allowed=True, env=state, **FAST)
    assert done.event == "handoff.stuck"  # a terminal event that is not a success still ends the drain
    assert not any(registry.rglob("a1"))


# --- no herdr server: herdr-handoff that fails or hangs ---------------------------------------------------------------

class ShortExit:
    """The claude adapter with a short wait for the source process to end (and so a short bound on one takeover call)."""

    harness = Harness.CLAUDE

    def __init__(self, process_end_s: float) -> None:
        self._end = process_end_s

    def __getattr__(self, name: str) -> object:
        return getattr(ADAPTER, name)

    def exit_plan(self) -> ExitPlan:
        return ExitPlan("/exit", ("Enter",), self._end)


@pytest.mark.parametrize("script, expected", [
    ("import sys,time\nif 'inspect' in sys.argv: print('{}')\nif 'takeover' in sys.argv: time.sleep(30)", "exit 124"),
    ("import sys\nif 'inspect' in sys.argv: print('{}')\nif 'takeover' in sys.argv: print('boom', file=sys.stderr); sys.exit(2)", "exit 2: boom"),
], ids=["timed_out", "failed"])
def test_attempts_exhausted_by_failures_end_red_without_compacting(script: str, expected: str, tmp_path: Path) -> None:
    broken = HandoffCli([sys.executable, "-c", script], env={"PATH": "/usr/bin:/bin"}, cwd=tmp_path)
    ref = make_ref("w1:p1", tmp_path)
    log = EventLog(tmp_path / "store")
    done = run_takeover(ref, ShortExit(0.2), CHILD_ID, ControllerConfig(max_attempts=2), log, broken, HerdrCli(),  # type: ignore[arg-type]
                        store_root=tmp_path / "store", worktree=tmp_path, push_allowed=True, env={}, confirm_timeout_s=0.1,
                        call_margin_s=0.5, retry_delay_s=0.0, poll_s=0.1)
    assert (done.event, done.colour) == ("handoff.stuck", Colour.RED), done
    assert done.reason and done.reason.startswith("no_briefing") and expected in done.reason, done.reason
    assert [e.event for e in log.read()] == ["handoff.takeover_started", "handoff.stuck"]

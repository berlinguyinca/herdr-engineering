"""Final fix wave, commit 2: controller failure modes. New names are reached through their modules so that each test
fails on its own while the feature is missing."""
# ruff: noqa: F401, F811
from __future__ import annotations

import importlib
import json
import os
import stat
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

import herdr_engineering
from herdr_engineering.handoff_adapters.base import Harness, SessionRef
from herdr_engineering.handoff_adapters.claude import ClaudeAdapter
from herdr_engineering.handoff_controller import briefing as briefingmod
from herdr_engineering.handoff_controller import credit as creditmod
from herdr_engineering.handoff_controller import events as eventsmod
from herdr_engineering.handoff_controller import proc as procmod
from herdr_engineering.handoff_controller import run as ctlrun
from herdr_engineering.handoff_controller import switch as switchmod
from herdr_engineering.handoff_controller import takeover as takeovermod
from herdr_engineering.handoff_controller import wip as wipmod
from herdr_engineering.handoff_controller.config import ControllerConfig
from herdr_engineering.handoff_controller.events import Colour, EventLog, HandoffEvent
from herdr_engineering.handoff_controller.handoff_cli import HandoffCli
from herdr_engineering.handoff_controller.herdr_cli import AgentPane, HerdrCli
from herdr_engineering.handoff_controller.switch import set_enabled
from tests.handoff_controller_herdr import isolated_herdr
from tests.test_handoff_controller_run import (
    CFG,
    ROOT,
    World,
    _briefing_record,
    _event,
    _git,
    _repo_on,
    _typed_chain,
    herdr_server,
)
from tests.test_handoff_controller_wave1 import Wave, _fake_handoff

B = CFG.briefing_timeout_s
D = CFG.drain_timeout_s


def _fake(tmp_path: Path, *, inspect_ok: bool = True, takeover_ok: bool = True) -> tuple[HandoffCli, Path]:
    """A real program standing in for herdr-handoff: logs its subcommands; inspect and takeover can be made to fail."""
    calls = tmp_path / "calls.txt"
    script = tmp_path / "fake-handoff2.py"
    script.write_text(
        "import json, sys\n"
        f"open({str(calls)!r}, 'a').write(' '.join(a for a in sys.argv[1:] if not a.startswith('/')) + '\\n')\n"
        "args = sys.argv[1:]\n"
        "sub = args[2] if len(args) > 2 and args[0] == '--root' else ''\n"
        "if sub == 'takeover':\n"
        f"    if {takeover_ok!r}:\n"
        "        print(json.dumps({'status': 'source_closed', 'source_closed': True, 'pane_id': 'child'}))\n"
        "    else:\n"
        "        print(json.dumps({'status': 'refused', 'reason': 'no'})); sys.exit(1)\n"
        "elif sub == 'inspect':\n"
        f"    if {inspect_ok!r}:\n"
        "        print(json.dumps({'handoff_id': args[3]}))\n"
        "    else:\n"
        "        sys.exit(1)\n"
        "elif sub == 'resume':\n"
        "    print(json.dumps({'document': '/x/briefing.md'}))\n"
        "else:\n"
        "    sys.exit(1)\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return HandoffCli((sys.executable, str(script))), calls


def _run_takeover(tmp_path: Path, handoff: HandoffCli) -> HandoffEvent:
    ref = SessionRef(Harness.CLAUDE, "p1", tmp_path, "s1", None)
    return takeovermod.run_takeover(
        ref, ClaudeAdapter(home=tmp_path / "home"), "handoff_" + "a" * 32, CFG, EventLog(tmp_path / "store"), handoff,
        HerdrCli("/nonexistent/herdr"), store_root=tmp_path / "store", worktree=tmp_path, push_allowed=False, env={},
        retry_delay_s=0.0, compact_ready_timeout_s=0.1, sleep=lambda _s: None, ready=lambda: True)


# --- I1 -------------------------------------------------------------------------------------------------------------

def test_an_unreadable_record_ends_stuck_and_is_never_compacted(tmp_path: Path) -> None:
    fake, calls = _fake(tmp_path, inspect_ok=False, takeover_ok=False)
    done = _run_takeover(tmp_path, fake)
    assert done.event == "handoff.stuck" and (done.reason or "").startswith("record_unreadable")
    assert "takeover" not in calls.read_text().split()  # nothing is launched blind either


# --- I2, I3 ---------------------------------------------------------------------------------------------------------

def test_wip_commit_outside_a_git_repository_has_nothing_to_commit(tmp_path: Path) -> None:
    result = wipmod.wip_commit(tmp_path, push_allowed=False)
    assert (result.committed, result.reason, result.error, result.unpushed) == (False, None, None, ())


def test_a_worktree_that_is_not_a_repository_goes_on_to_the_takeover(tmp_path: Path) -> None:
    w = Wave(tmp_path)  # World.repo is a plain directory
    fake, calls = _fake_handoff(tmp_path)
    w.ctl = w.controller()
    w.ctl.handoff = fake
    w.session("p1", 85.0)
    _typed_chain(w, started=w.clock[0] - 10)
    _briefing_record(w.store, "p1", "2999-01-01T00:00:00Z")
    w.tick(w.pane("p1", "idle"))
    assert w.names()[-2:] == ["handoff.takeover_started", "handoff.succeeded"]


def _conflict(repo: Path) -> None:
    _repo_on("feat/x", repo, dirty=False)
    _git(repo, "checkout", "-q", "-b", "other")
    (repo / "a.txt").write_text("other\n")
    _git(repo, "commit", "-q", "-am", "other")
    _git(repo, "checkout", "-q", "feat/x")
    (repo / "a.txt").write_text("mine\n")
    _git(repo, "commit", "-q", "-am", "mine")
    subprocess.run(["git", "-C", str(repo), "merge", "other"], capture_output=True, text=True, check=False)
    assert (repo / ".git" / "MERGE_HEAD").exists()


def test_wip_commit_refuses_during_a_merge_and_stages_nothing(tmp_path: Path) -> None:
    repo = tmp_path / "r"
    repo.mkdir()
    _conflict(repo)
    head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout
    result = wipmod.wip_commit(repo, push_allowed=False)
    assert result.reason is wipmod.WipReason.MERGE_IN_PROGRESS and not result.committed
    assert subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout == head
    staged = subprocess.run(["git", "-C", str(repo), "diff", "--cached", "--name-only", "--diff-filter=AMD"],
                            capture_output=True, text=True).stdout
    assert staged == ""  # the conflicted file was not staged by `git add -u`
    (repo / ".git" / "MERGE_HEAD").unlink()  # unmerged entries alone are enough
    assert wipmod.wip_commit(repo, push_allowed=False).reason is wipmod.WipReason.MERGE_IN_PROGRESS


@pytest.mark.parametrize("push", [False, True])
def test_a_merge_in_progress_goes_on_only_where_push_is_not_allowed(tmp_path: Path, push: bool) -> None:
    w = Wave(tmp_path, cfg=replace(CFG, push_default=push))
    _conflict(w.repo)
    fake, calls = _fake_handoff(tmp_path)
    w.ctl = w.controller()
    w.ctl.handoff = fake
    w.session("p1", 85.0)
    _typed_chain(w, started=w.clock[0] - 10)
    _briefing_record(w.store, "p1", "2999-01-01T00:00:00Z")
    w.tick(w.pane("p1", "idle"))
    if push:
        assert w.names()[-1] == "handoff.stuck" and (w.events()[-1].reason or "").startswith("merge_in_progress")
        assert not calls.exists()
    else:
        assert w.names()[-2:] == ["handoff.takeover_started", "handoff.succeeded"]
        assert "--allow-dirty-source" in calls.read_text().split()


# --- I4 -------------------------------------------------------------------------------------------------------------

def test_an_open_chain_whose_pane_is_gone_ends_stuck_pane_gone_once(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.session("p1", 85.0)
    _typed_chain(w, started=w.clock[0] - 10)
    w.tick()  # no panes at all
    w.clock[0] += 30
    w.tick()
    assert not [e for e in w.events() if e.event == "handoff.stuck"]  # not yet a minute
    w.clock[0] += 31
    w.tick()
    w.tick()
    stuck = [e for e in w.events() if e.event == "handoff.stuck"]
    assert [(e.reason, e.colour, e.chain_id) for e in stuck] == [("pane_gone", Colour.RED, "c1")]


def test_an_open_chain_of_another_session_ends_stuck_pane_gone(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.session("p1", 10.0)
    w.ctl.log.append(_event("handoff.triggered", w.clock[0] - 50, chain="old", session="s-before"))
    w.tick(w.pane("p1", "idle"))  # the pane now holds another session
    stuck = [e for e in w.events() if e.event == "handoff.stuck"]
    assert [(e.reason, e.session_id) for e in stuck] == [("pane_gone", "s-before")]


# --- I5 -------------------------------------------------------------------------------------------------------------

def test_backoff_doubles_from_five_to_sixty_seconds() -> None:
    assert [ctlrun.backoff_s(n) for n in range(1, 8)] == [5.0, 10.0, 20.0, 40.0, 60.0, 60.0, 60.0]


def test_a_state_dir_that_cannot_be_made_is_exit_one_with_one_line(tmp_path: Path,
                                                                  capsys: pytest.CaptureFixture[str]) -> None:
    w = World(tmp_path)
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    w.ctl.state_dir = blocker / "state"
    assert ctlrun.serve(controller=w.ctl, once=True) == 1
    err = capsys.readouterr().err
    assert err.count("\n") == 1 and "Error" in err and str(tmp_path) not in err  # the type only


def _stub_herdr(tmp_path: Path, cwd: Path) -> HerdrCli:
    """A real program answering `agent list` with one idle claude agent."""
    stub = tmp_path / "herdr-stub"
    row = {"pane_id": "p1", "tab_id": "t1", "agent": "claude", "agent_status": "idle", "cwd": str(cwd),
           "agent_session": {"value": "s1"}}
    stub.write_text(f"#!{sys.executable}\nimport json\nprint(json.dumps({{'result': {{'agents': [{row!r}]}}}}))\n")
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    return HerdrCli(str(stub))


def test_a_tick_that_fails_on_the_store_is_exit_one_once_and_retried_with_backoff_in_the_loop(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    w = World(tmp_path)
    w.ctl.herdr = _stub_herdr(tmp_path, w.repo)
    w.ctl.locate = lambda adapter, pane: None
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    w.ctl.log = EventLog(blocker / "store")  # every read of the log raises NotADirectoryError
    assert ctlrun.serve(controller=w.ctl, once=True) == 1
    assert capsys.readouterr().err.count("\n") == 1
    stop = threading.Event()
    result: list[int] = []
    thread = threading.Thread(target=lambda: result.append(
        ctlrun.serve(controller=w.ctl, interval_s=0.01, retry_s=0.01, stop=stop)))
    thread.start()
    time.sleep(0.3)
    assert thread.is_alive()  # it did not die of the failure
    blocker.unlink()  # the store comes back
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not (blocker / "store" / "events.jsonl").exists():
        time.sleep(0.05)
    stop.set()
    thread.join(10)
    assert result == [0] and (blocker / "store" / "events.jsonl").exists()


# --- I6 -------------------------------------------------------------------------------------------------------------

def test_credited_time_follows_the_monotonic_clock_and_ignores_a_sleep() -> None:
    clock = creditmod.TickClock(interval_s=5.0)
    clock.tick(1000.0, 50.0)
    clock.tick(1005.0, 55.0)
    assert clock.now(1005.0) == pytest.approx(1005.0)
    clock.tick(1005.0 + 10_800.0, 60.0)  # three hours of sleep: the monotonic clock stood still
    assert clock.now(1005.0 + 10_800.0) == pytest.approx(1010.0)
    assert clock.credited(1005.0) == pytest.approx(1005.0)  # earlier times keep their place
    assert clock.credited(1005.0 + 10_800.0) == pytest.approx(1010.0)


def test_a_tick_gap_longer_than_three_intervals_is_credited_one_interval() -> None:
    clock = creditmod.TickClock(interval_s=5.0)
    clock.tick(1000.0, 50.0)
    clock.tick(1100.0, 150.0)  # the process was stopped for 100 s
    assert clock.now(1100.0) == pytest.approx(1005.0)
    clock.tick(1112.0, 162.0)  # 12 s: under three intervals, credited in full
    assert clock.now(1112.0) == pytest.approx(1017.0)


def test_after_a_restart_an_open_chain_is_as_old_as_the_wall_clock_says() -> None:
    clock = creditmod.TickClock(interval_s=5.0)
    clock.tick(50_000.0, 1.0, oldest_open=1_000.0)  # the chain's first event is 49 000 s old on the wall clock
    assert clock.now(50_000.0) - clock.credited(1_000.0) == pytest.approx(49_000.0)


def test_a_sleeping_machine_does_not_run_out_the_briefing_timeout(tmp_path: Path) -> None:
    w = World(tmp_path)
    mono = [100.0]
    w.ctl.mono = lambda: mono[0]
    w.ctl.ticks = creditmod.TickClock(interval_s=5.0)
    w.session("p1", 85.0)
    _typed_chain(w, started=w.clock[0] - 10)
    w.tick(w.pane("p1", "idle"))
    w.clock[0] += 3 * 3600  # the lid was closed: wall time passed, the monotonic clock did not
    mono[0] += 5
    w.tick(w.pane("p1", "idle"))
    assert w.names() == ["handoff.triggered", "handoff.drain_started"]  # still waiting for the briefing
    for _ in range(int(B / 5) + 1):  # real waiting, tick by tick, runs the timeout out as before
        w.clock[0] += 5
        mono[0] += 5
        w.tick(w.pane("p1", "idle"))
    assert w.names()[2:] == ["handoff.drained", "handoff.stuck"]


def test_a_restarted_controller_counts_an_old_chain_by_the_wall_clock(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.ctl.ticks = creditmod.TickClock(interval_s=5.0)
    w.session("p1", 85.0)
    _typed_chain(w, started=w.clock[0] - 3 * 3600)  # the controller was down for three hours: the bound has passed
    w.tick(w.pane("p1", "idle"))
    assert w.names()[:3] == ["handoff.triggered", "handoff.drain_started", "handoff.drained"]


# --- I7 -------------------------------------------------------------------------------------------------------------

def test_an_uncertain_order_is_retyped_at_most_once_per_chain() -> None:
    rt = ctlrun.PaneRuntime("s1")
    ctlrun.note_uncertain(rt, 1000.0, CFG)
    assert rt.cooldown_until == 1000.0 + B and rt.retypes == 1
    ctlrun.note_uncertain(rt, 2000.0, CFG)  # the retype was uncertain too: no third order
    assert rt.cooldown_until == float("inf") and rt.retypes == 2
    chain = ctlrun.Chain("c", 1000.0, "t", None, False, 1000.0)
    assert ctlrun.next_step(chain, rt, ctlrun.Decision.DRAIN, 2000.0 + D, CFG, True) is ctlrun.LoopStep.DRAIN_STUCK
    rt.typed = False
    assert ctlrun.next_step(chain, rt, ctlrun.Decision.DRAIN, 1000.0 + D - 1, CFG, True) is ctlrun.LoopStep.CHECK_BRIEFING


# --- I9 -------------------------------------------------------------------------------------------------------------

def test_the_chain_view_lives_in_events_and_says_in_which_phase_the_chain_is() -> None:
    phases = eventsmod.Phase
    assert [p.value for p in phases] == ["draining", "waiting", "briefing", "takeover", "done_green", "done_orange",
                                         "done_red"]
    start = _event("handoff.triggered", 100.0)
    assert eventsmod.last_chain([start]).phase is phases.DRAINING  # type: ignore[union-attr]
    waiting = replace(_event("handoff.drain_started", 101.0), event="handoff.waiting", reason="working")
    assert eventsmod.last_chain([start, waiting]).phase is phases.WAITING  # type: ignore[union-attr]
    got = _event("handoff.briefing_written", 102.0, handoff_id="handoff_a")
    assert eventsmod.last_chain([start, got]).phase is phases.BRIEFING  # type: ignore[union-attr]
    took = _event("handoff.takeover_started", 103.0)
    assert eventsmod.last_chain([start, got, took]).phase is phases.TAKEOVER  # type: ignore[union-attr]
    for name, colour, phase in (("handoff.succeeded", Colour.GREEN, phases.DONE_GREEN),
                                ("handoff.failed_compacted", Colour.ORANGE, phases.DONE_ORANGE),
                                ("handoff.stuck", Colour.RED, phases.DONE_RED)):
        end = _event(name, 104.0, colour=colour, reason="x" if name == "handoff.stuck" else None)
        assert eventsmod.last_chain([start, got, took, end]).phase is phase  # type: ignore[union-attr]
        assert eventsmod.open_chain([start, got, took, end]) is None
    assert eventsmod.pane_events([start], "p1", "s1") == [start] and eventsmod.pane_events([start], "p2", "s1") == []


def test_an_event_line_with_a_key_this_version_does_not_know_still_reads() -> None:
    line = json.loads(_event("handoff.triggered", 100.0).to_json())
    line["added_by_a_newer_version"] = {"x": 1}
    assert HandoffEvent.from_json(json.dumps(line)).event == "handoff.triggered"


def test_the_side_pane_callables_are_injected_and_a_missing_module_is_the_only_silent_case(tmp_path: Path) -> None:
    asked: list[str] = []

    def ask(tab_id: str) -> None:
        asked.append(tab_id)

    w = World(tmp_path)
    w.ctl.ensure_side_pane = ask
    w.ctl.is_side_pane = lambda pane: pane.pane_id == "side"
    w.session("p1", 10.0)
    w.tick(w.pane("p1", tab="t7"), w.pane("side", tab="t7"), w.pane("p2", tab="t7"))
    assert asked == ["t7"] and "side" not in w.ctl.runtime
    built = ctlrun.build_controller({"HOME": str(tmp_path), "HERDR_HANDOFF_STORE": str(tmp_path / "s")},
                                    require_binaries=False, ensure_side_pane=ask,
                                    is_side_pane=lambda pane: True)
    assert built.ensure_side_pane is ask and built.is_side_pane(w.pane("x"))
    # the default is silent only for `herdr_engineering.side_pane` itself missing, never for a broken import inside it
    assert ctlrun.default_ensure_side_pane.__name__ == "default_ensure_side_pane"


def test_the_default_side_pane_hook_reports_a_module_that_fails_to_import(tmp_path: Path,
                                                                       monkeypatch: pytest.MonkeyPatch,
                                                                       capsys: pytest.CaptureFixture[str]) -> None:
    ctlrun.default_ensure_side_pane("t1")  # herdr_engineering.side_pane does not exist (yet): nothing to say
    assert capsys.readouterr().err == ""
    # The broken module lives outside the checkout and is found through this process's package path only: a file written
    # into the source tree would be imported by every parallel test worker for as long as it existed.
    (tmp_path / "side_pane.py").write_text("import a_module_that_is_not_installed\n")
    monkeypatch.setattr(herdr_engineering, "__path__", [str(tmp_path), *herdr_engineering.__path__])
    importlib.invalidate_caches()
    try:
        ctlrun.default_ensure_side_pane("t1")
    finally:
        sys.modules.pop("herdr_engineering.side_pane", None)
    assert "side pane" in capsys.readouterr().err


# --- I10 ------------------------------------------------------------------------------------------------------------

def _red(at: float, chain: str) -> HandoffEvent:
    return _event("handoff.stuck", at, chain=chain, session="s-p1", reason="x", colour=Colour.RED)


def test_gave_up_is_said_only_when_a_drain_would_have_started_and_once_across_restarts(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.session("p1", 40.0)
    base = w.clock[0]
    for i, at in enumerate((base - 90_000, base - 60_000, base - 30_000)):
        w.ctl.log.append(_red(at, f"c{i}"))
    w.tick(w.pane("p1", "idle"))
    assert [e.reason for e in w.events() if e.event == "handoff.skipped"] == []  # nothing to give up on below the limit
    w.session("p1", 85.0)
    w.tick(w.pane("p1", "idle"))
    again = w.controller()  # a restart
    again.locate = w.ctl.locate
    ctlrun.run_once(again, [w.pane("p1", "idle")])
    assert [e.reason for e in w.events() if e.event == "handoff.skipped"] == ["gave_up"]


def test_a_switch_toggled_off_and_on_between_two_ticks_starts_over(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.session("p1", 85.0)
    base = w.clock[0]
    for i, at in enumerate((base - 90_000, base - 60_000, base - 30_000)):
        w.ctl.log.append(_red(at, f"c{i}"))
    w.tick(w.pane("p1", "idle"))
    assert w.names()[-1] == "handoff.skipped"
    set_enabled("p1", False, w.state)  # off and on again, and the controller never saw it off
    set_enabled("p1", True, w.state)
    marker = switchmod.toggled_at("p1", w.state)
    assert marker is not None
    w.clock[0] = marker + 1
    w.tick(w.pane("p1", "idle"))
    assert w.names()[-2:] == ["handoff.triggered", "handoff.drain_started"]


def test_a_failed_help_probe_is_not_remembered_and_that_call_is_unsupported(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    env = {"PATH": str(bin_dir)}
    assert briefingmod.headless_argv(Harness.CLAUDE, env) is None  # no `claude` yet: unsupported for this call
    claude = bin_dir / "claude"
    claude.write_text("#!/bin/sh\necho '--safe-mode  run safely'\n")
    claude.chmod(0o755)
    argv = briefingmod.headless_argv(Harness.CLAUDE, env)
    assert argv is not None and "--safe-mode" in argv


def test_the_harness_programs_are_found_in_the_extra_bin_dirs(tmp_path: Path) -> None:
    extra = tmp_path / "extra"
    extra.mkdir()
    for name in ("herdr", "herdr-handoff", "git", "claude", "codex"):
        program = extra / name
        program.write_text("#!/bin/sh\necho found-in-extra\n")
        program.chmod(0o755)
    env = {"PATH": "/nonexistent", "HOME": str(tmp_path), "HERDR_HANDOFF_STORE": str(tmp_path / "s")}
    found = ctlrun.resolve_binaries(env, [extra])
    assert dict(found.harness_bins) == {"claude": extra / "claude", "codex": extra / "codex"}
    try:
        ctlrun.build_controller(env, bin_dirs=[extra])
        assert procmod.run_group(["claude", "--version"], 10.0).stdout.strip() == "found-in-extra"
    finally:
        for name in ("git", "claude", "codex", "pi"):
            procmod.use_tool(name, None)


def test_a_replaced_event_log_is_detected_by_its_inode_even_when_it_grew(tmp_path: Path) -> None:
    log = EventLog(tmp_path)
    for i in range(2):
        log.append(_event("handoff.triggered", 100.0 + i, chain=f"old{i}"))
    _, offset = log.read_since(0)
    before = log.generation
    other = EventLog(tmp_path / "other")
    for i in range(4):
        other.append(_event("handoff.triggered", 200.0 + i, chain=f"new{i}"))
    os.replace(tmp_path / "other" / "events.jsonl", tmp_path / "events.jsonl")
    new, new_offset = log.read_since(offset)
    assert [e.chain_id for e in new] == ["new0", "new1", "new2", "new3"] and log.generation == before + 1
    assert log.read_since(new_offset) == ([], new_offset) and log.generation == before + 1


def test_a_log_that_was_cut_shorter_counts_as_replaced_too(tmp_path: Path) -> None:
    log = EventLog(tmp_path)
    for i in range(3):
        log.append(_event("handoff.triggered", 100.0 + i, chain=f"c{i}"))
    _, offset = log.read_since(0)
    (tmp_path / "events.jsonl").write_text("")
    before = log.generation
    log.append(_event("handoff.triggered", 300.0, chain="fresh"))
    new, _ = log.read_since(offset)
    assert [e.chain_id for e in new] == ["fresh"] and log.generation == before + 1


@herdr_server
def test_the_cli_runs_one_real_tick_against_an_isolated_server(tmp_path: Path) -> None:
    with isolated_herdr("cli-once") as h:
        store = tmp_path / "store"
        extra = tmp_path / "extra"
        extra.mkdir()
        stub = extra / "herdr-handoff"
        stub.write_text("#!/bin/sh\nexit 1\n")
        stub.chmod(0o755)
        h.start()
        env = {**h.env, "HERDR_HANDOFF_STORE": str(store), "HERDR_HANDOFF_BIN_DIRS": str(extra),
               "HERDR_ENGINEERING_STATE": str(h.root / "state"), "PYTHONPATH": str(ROOT)}
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not (h.cli.agents() or []):
            time.sleep(0.2)
        run = subprocess.run([sys.executable, "-m", "herdr_engineering.cli", "handoff", "controller", "run", "--once"],
                             env=env, cwd=h.root, capture_output=True, text=True, timeout=120, check=False)
        assert run.returncode == 0, run.stderr
        lines = [json.loads(line) for line in (store / "events.jsonl").read_text().splitlines()]
        assert [(e["event"], e["reason"]) for e in lines] == [("handoff.skipped", "no_session")]

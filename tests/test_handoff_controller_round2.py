"""Fix round 2: the controller re-checks the session before every takeover and waits out a busy one. New names are
reached through their modules so that each test fails on its own while the feature is missing."""
# ruff: noqa: F401, F811
from __future__ import annotations

import json
import subprocess
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

from herdr_engineering.handoff_adapters.base import Harness, SessionRef
from herdr_engineering.handoff_adapters.claude import ClaudeAdapter
from herdr_engineering.handoff_controller import credit as creditmod
from herdr_engineering.handoff_controller import drain as drainmod
from herdr_engineering.handoff_controller import events as eventsmod
from herdr_engineering.handoff_controller import run as ctlrun
from herdr_engineering.handoff_controller import takeover as takeovermod
from herdr_engineering.handoff_controller import wip as wipmod
from herdr_engineering.handoff_controller.events import Colour, EventLog, HandoffEvent
from herdr_engineering.handoff_controller.herdr_cli import AgentPane, HerdrCli
from tests.test_handoff_controller_run import (
    CFG,
    World,
    _briefing_record,
    _event,
    _git,
    _repo_on,
    _typed_chain,
)
from tests.test_handoff_controller_wave1 import Wave, _fake_handoff, _typed
from tests.test_handoff_controller_wave2 import _fake, _stub_herdr

B = CFG.briefing_timeout_s
D = CFG.drain_timeout_s


def _briefed(w: Wave, tmp_path: Path, *, branch: str = "feat/x") -> Path:
    """A Wave world with a clean repo, a fake herdr-handoff, a typed chain and a briefing the session wrote."""
    _repo_on(branch, w.repo, dirty=False)
    fake, calls = _fake_handoff(tmp_path)
    w.ctl.handoff = fake
    _typed(w)
    _briefing_record(w.store, "p1", "2999-01-01T00:00:00Z")
    return calls


# --- 1: the readiness check before every takeover call ---------------------------------------------------------------

def test_a_user_typing_between_the_checkpoint_and_the_takeover_means_herdr_handoff_is_never_called(tmp_path: Path) -> None:
    w = Wave(tmp_path)
    calls = _briefed(w, tmp_path)
    looks: list[int] = []

    def look(ref: object, adapter: object, herdr: object) -> drainmod.Look:
        looks.append(1)
        return drainmod.Look("idle", len(looks) == 1, False)  # ready at the first look, a composer with text at the second

    w.ctl.look = look
    w.tick(w.pane("p1", "idle"))
    assert len(looks) >= 2 and "takeover" not in (calls.read_text().split() if calls.exists() else [])
    assert w.names()[-1] == "handoff.waiting" and w.waiting()[-1].reason == "working"
    w.ctl.look = lambda ref, adapter, herdr: drainmod.Look("idle", True, False)  # the user stopped
    w.tick(w.pane("p1", "idle"))
    assert w.names()[-1] == "handoff.succeeded" and calls.read_text().split().count("takeover") == 1
    assert w.names().count("handoff.waiting") == 1


def test_a_retry_after_a_refusal_looks_at_the_session_again(tmp_path: Path) -> None:
    fake, calls = _fake(tmp_path, takeover_ok=False)
    looks: list[int] = []

    def ready() -> bool:
        looks.append(1)
        return len(looks) == 1  # ready for the first attempt, not for the retry

    ref = SessionRef(Harness.CLAUDE, "p1", tmp_path, "s1", None)
    done = takeovermod.run_takeover(
        ref, ClaudeAdapter(home=tmp_path / "home"), "handoff_" + "a" * 32, CFG, EventLog(tmp_path / "store"), fake,
        HerdrCli("/nonexistent/herdr"), store_root=tmp_path / "store", worktree=tmp_path, push_allowed=False, env={},
        retry_delay_s=0.0, compact_ready_timeout_s=0.1, sleep=lambda _s: None, ready=ready)
    assert done is None and calls.read_text().split().count("takeover") == 1 and len(looks) == 2
    names = [e.event for e in EventLog(tmp_path / "store").read()]
    assert names == ["handoff.takeover_started"] * 2  # the chain's and the one attempt; no terminal event: the chain goes on


def test_a_takeover_resumed_after_a_restart_is_gated_too(tmp_path: Path) -> None:
    w = Wave(tmp_path)
    calls = _briefed(w, tmp_path)
    w.ctl.log.append(_event("handoff.briefing_written", w.clock[0] - 5, chain="c1", session="s-p1",
                            handoff_id="handoff_" + "c" * 32))
    w.ctl.log.append(_event("handoff.takeover_started", w.clock[0] - 4, chain="c1", session="s-p1"))
    w.tick(w.pane("p1", "working"))  # the restart finds a takeover under way and a session that works again
    assert not calls.exists() and w.names()[-1] == "handoff.waiting"


# --- 2: a busy session over the ceiling is waited for ------------------------------------------------------------------

def test_a_busy_session_over_the_ceiling_is_waited_for_until_the_drain_timeout(tmp_path: Path) -> None:
    w = Wave(tmp_path)
    asked: list[str] = []
    w.ctl = w.controller(runner=lambda harness, prompt, *, cwd, timeout_s: asked.append(prompt))
    w.session("p1", 95.0)
    w.tick(w.pane("p1", "working"))
    assert asked == [] and "handoff.stuck" not in w.names()  # no headless call on the first tick
    for _ in range(int(D / 60) - 1):
        w.clock[0] += 60
        w.tick(w.pane("p1", "working"))
    assert asked == [] and "handoff.stuck" not in w.names()  # still waiting just before the bound
    w.clock[0] += 60
    w.tick(w.pane("p1", "working"))  # the bound: the last resort
    assert len(asked) == 1 and w.names()[-1] == "handoff.stuck"


def test_the_busy_drain_outcomes_wait_and_the_others_do_not() -> None:
    assert [ctlrun.drain_is_busy(o) for o in ("blocked", "timeout", "not_ready")] == [True, True, True]
    assert [ctlrun.drain_is_busy(o) for o in ("prompt_uncertain", "too_long", "wrong_agent")] == [False] * 3


# --- 3: nothing stageable ------------------------------------------------------------------------------------------------

def _dirty_submodule(repo: Path, sub: Path) -> None:
    _repo_on("main", sub, dirty=False)
    _git(repo, "-c", "protocol.file.allow=always", "submodule", "add", str(sub), "vendor")
    _git(repo, "commit", "-q", "-m", "add the submodule")
    (repo / "vendor" / "a.txt").write_text("changed inside the submodule\n")  # `git add -u` stages nothing for it


def test_a_dirty_submodule_is_nothing_stageable_and_no_commit_is_tried(tmp_path: Path) -> None:
    repo = tmp_path / "work"
    repo.mkdir()
    _repo_on("feature", repo, dirty=False)
    _dirty_submodule(repo, tmp_path / "sub")
    head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout
    result = wipmod.wip_commit(repo, push_allowed=False)
    assert result.reason is wipmod.WipReason.NOTHING_STAGEABLE and not result.committed and result.error is None
    assert subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True,
                          text=True).stdout == head


def test_the_loop_goes_on_to_the_takeover_with_the_dirty_source_allowed_when_nothing_is_stageable(tmp_path: Path) -> None:
    w = Wave(tmp_path, cfg=replace(CFG, push_default=True))
    calls = _briefed(w, tmp_path)
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    _git(w.repo, "remote", "add", "origin", str(remote))
    _git(w.repo, "push", "-q", "-u", "origin", "feat/x")
    _dirty_submodule(w.repo, tmp_path / "sub")
    _git(w.repo, "push", "-q", "origin", "feat/x")
    (w.repo / "vendor" / "a.txt").write_text("changed again\n")
    w.tick(w.pane("p1", "idle"))
    assert w.names()[-1] == "handoff.succeeded"
    assert [e.reason for e in w.events() if e.event == "handoff.wip_committed"] == ["nothing_stageable"]
    assert "--allow-dirty-source" in calls.read_text()  # even though pushing is allowed here


# --- 4: pane_gone needs a minute of absence and is not a failure of the session --------------------------------------------

def test_a_chain_ends_pane_gone_only_after_a_minute_of_credited_absence(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.session("p1", 85.0)
    _typed_chain(w, started=w.clock[0] - 10)
    w.tick()
    w.clock[0] += 59
    w.tick()
    assert "handoff.stuck" not in w.names()
    w.clock[0] += 1
    w.tick()
    assert [(e.reason, e.colour) for e in w.events() if e.event == "handoff.stuck"] == [("pane_gone", Colour.RED)]


def test_a_pane_that_comes_back_starts_the_absence_over(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.session("p1", 85.0)
    _typed_chain(w, started=w.clock[0] - 10)
    w.tick()
    w.clock[0] += 40
    w.tick(w.pane("p1", "working"))
    w.clock[0] += 10
    w.tick()
    w.clock[0] += 50
    w.tick()
    assert "handoff.stuck" not in w.names()


def test_pane_gone_does_not_count_towards_giving_up() -> None:
    events: list[HandoffEvent] = []
    for n in range(3):
        chain = f"c{n}"
        events += [_event("handoff.triggered", 1000.0 + n * 10, chain=chain),
                   _event("handoff.stuck", 1005.0 + n * 10, chain=chain, reason="pane_gone", colour=Colour.RED)]
    assert not ctlrun.gave_up(events, CFG)
    events.append(_event("handoff.stuck", 1100.0, chain="c0b", reason="drain_timeout", colour=Colour.RED))
    assert not ctlrun.gave_up(events, CFG)  # one real failure is still only one


# --- 5: a compaction that may be under way is never typed again -------------------------------------------------------------

def test_a_restart_in_the_middle_of_a_compaction_ends_stuck_and_types_nothing(tmp_path: Path) -> None:
    w = Wave(tmp_path)
    calls = _briefed(w, tmp_path)
    w.ctl.log.append(_event("handoff.briefing_written", w.clock[0] - 5, chain="c1", session="s-p1",
                            handoff_id="handoff_" + "c" * 32))
    w.ctl.log.append(_event("handoff.takeover_started", w.clock[0] - 4, chain="c1", session="s-p1"))
    w.ctl.log.append(_event("handoff.takeover_started", w.clock[0] - 3, chain="c1", session="s-p1",
                            reason=eventsmod.COMPACTING))
    w.tick(w.pane("p1", "idle"))
    last = w.events()[-1]
    assert (last.event, last.reason, last.colour) == ("handoff.stuck", "compaction_state_unknown", Colour.RED)
    assert not calls.exists()


# --- 6: handoff.waiting is said when the state changes ----------------------------------------------------------------------

def test_a_quiet_working_flicker_with_the_same_reason_says_nothing_new(tmp_path: Path) -> None:
    w = Wave(tmp_path)
    _typed(w)
    w.tick(w.pane("p1", "working"))
    w.tick(w.pane("p1", "idle"))  # quiet for one tick
    w.tick(w.pane("p1", "working"))
    assert [(e.reason, e.subagents_live) for e in w.waiting()] == [("working", 0)]


class _FlakyLog(EventLog):
    """A log whose disk fails once on the `handoff.stuck` append."""

    failed = False

    def append(self, event: HandoffEvent) -> None:
        if event.event == "handoff.stuck" and not self.failed:
            self.failed = True
            raise OSError("disk")
        super().append(event)


def test_the_fallback_does_not_say_drained_again_when_it_failed_midway(tmp_path: Path) -> None:
    w = Wave(tmp_path)
    w.ctl.log = _FlakyLog(w.store)
    _typed(w)
    w.tick(w.pane("p1", "idle"))  # quiet from here
    w.clock[0] += B
    w.tick(w.pane("p1", "idle"))  # the fallback writer answers nothing; the stuck event cannot be written
    w.clock[0] += 5
    w.tick(w.pane("p1", "idle"))
    assert w.names().count("handoff.drained") == 1 and w.names()[-1] == "handoff.stuck"


# --- 7: serve --------------------------------------------------------------------------------------------------------------

class _RaisingLog(EventLog):
    def __init__(self, store: Path, error: BaseException) -> None:
        super().__init__(store)
        self.error = error

    def read_since(self, offset: int) -> tuple[list[HandoffEvent], int]:
        raise self.error


def test_any_exception_escaping_a_tick_is_logged_by_type_and_backed_off(tmp_path: Path,
                                                                        capsys: pytest.CaptureFixture[str]) -> None:
    w = World(tmp_path)
    w.ctl.herdr = _stub_herdr(tmp_path, w.repo)
    w.ctl.log = _RaisingLog(w.store, ValueError("secret detail"))
    assert ctlrun.serve(controller=w.ctl, once=True) == 1
    err = capsys.readouterr().err
    assert "ValueError" in err and "secret detail" not in err and err.count("\n") == 1


@pytest.mark.parametrize("error", [KeyboardInterrupt(), SystemExit(3)])
def test_an_interrupt_or_an_exit_still_ends_the_loop(tmp_path: Path, error: BaseException) -> None:
    w = World(tmp_path)
    w.ctl.herdr = _stub_herdr(tmp_path, w.repo)
    w.ctl.log = _RaisingLog(w.store, error)
    with pytest.raises(type(error)):
        ctlrun.serve(controller=w.ctl, once=True)
    assert not ctlrun.lock_held(w.ctl.state_dir)  # the lock is released on the way out


def test_the_lock_is_retried_before_another_controller_is_assumed(tmp_path: Path) -> None:
    holder = ctlrun.ControllerLock(tmp_path)
    assert holder.acquire()
    threading.Timer(0.4, holder.release).start()
    other = ctlrun.ControllerLock(tmp_path)
    assert other.acquire()  # the first controller was just leaving
    other.release()
    stays = ctlrun.ControllerLock(tmp_path)
    assert stays.acquire()
    started = time.monotonic()
    third = ctlrun.ControllerLock(tmp_path)
    assert not third.acquire() and 0.8 <= time.monotonic() - started < 3.0
    stays.release()


def test_after_a_restart_an_events_age_is_its_wall_clock_age() -> None:
    clock = creditmod.TickClock(interval_s=5.0)
    clock.tick(50_000.0, 1.0)
    assert clock.now(50_000.0) - clock.credited(1_000.0) == pytest.approx(49_000.0)
    clock.tick(50_100.0, 101.0)  # a gap between the ticks of a running process is still one interval
    clock.tick(50_300.0, 301.0)
    assert clock.now(50_300.0) == pytest.approx(50_005.0 + 5.0)


def test_the_credit_breaks_stay_bounded_and_recent_times_keep_their_credit() -> None:
    clock = creditmod.TickClock(interval_s=5.0)
    wall, mono = 1_000_000.0, 0.0
    clock.tick(wall, mono)
    for _ in range(2000):  # a lid closed every few minutes for days
        wall, mono = wall + 3600.0, mono + 5.0
        clock.tick(wall, mono)
    assert len(clock._breaks) < 20
    assert clock.credited(wall) == pytest.approx(clock.now(wall))


# --- 8: events, protocols, the checkpoint ---------------------------------------------------------------------------------------

def test_a_line_with_only_the_required_keys_still_reads() -> None:
    line = '{"event":"handoff.triggered","at":"2026-11-18T00:00:00Z","chain_id":"c","pane_id":"p1","harness":"claude"}'
    event = HandoffEvent.from_json(line)
    assert (event.session_id, event.reason, event.colour, event.context_pct, event.commit) == (None,) * 5


def test_the_side_pane_protocols_live_in_events_and_run_re_exports_them() -> None:
    assert eventsmod.IsSidePane is ctlrun.IsSidePane and eventsmod.EnsureSidePane is ctlrun.EnsureSidePane


def test_commits_that_were_already_unpushed_are_recorded_when_pushing_is_not_allowed(tmp_path: Path) -> None:
    w = World(tmp_path)
    _repo_on("feature", w.repo, dirty=False)  # a clean tree whose only commit is not on any remote
    _git(w.repo, "remote", "add", "origin", str(w.repo))  # a repository with no remote has nothing to push
    ref = SessionRef(Harness.CLAUDE, "p1", w.repo, "s-p1", None)
    assert ctlrun._checkpoint(w.ctl, ref, "c1", w.repo, False, 85.0)
    done = [e for e in w.events() if e.event == "handoff.wip_committed"]
    assert [(e.reason, e.commit) for e in done] == [("push_not_allowed", None)]


# --- side pane hooks ------------------------------------------------------------------------------------------------

def test_a_pane_the_hook_flags_is_never_managed_even_over_the_limit(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.session("side", 95.0)
    w.ctl.is_side_pane = lambda pane: pane.pane_id == "side"
    w.tick(w.pane("side", "idle"))
    assert w.events() == [] and "side" not in w.ctl.runtime


def test_a_hook_that_raises_skips_the_pane_for_that_tick_and_logs_one_line(tmp_path: Path,
                                                                         capsys: pytest.CaptureFixture[str]) -> None:
    w = World(tmp_path)
    w.session("p1", 95.0)

    def broken(pane: object) -> bool:
        raise RuntimeError("secret")

    w.ctl.is_side_pane = broken
    w.tick(w.pane("p1", "idle"))
    w.tick(w.pane("p1", "idle"))
    assert w.events() == []
    err = capsys.readouterr().err
    assert err.count("side pane check failed") == 1 and "RuntimeError" in err and "secret" not in err
    w.ctl.is_side_pane = lambda pane: False  # the hook works again: the pane is managed
    w.tick(w.pane("p1", "idle"))
    assert w.names()[0] == "handoff.triggered"


def test_the_side_pane_is_ensured_for_each_new_session_in_a_tab_and_not_on_every_tick(tmp_path: Path) -> None:
    w = World(tmp_path)
    asked: list[str] = []
    w.ctl.ensure_side_pane = asked.append
    w.session("p1", 10.0)
    w.session("p2", 10.0)
    w.session("p3", 10.0)
    w.tick(w.pane("p1", tab="t1"))
    w.tick(w.pane("p1", tab="t1"))
    assert asked == ["t1"]
    w.tick(w.pane("p1", tab="t1"), w.pane("p2", tab="t1"))  # a second session in the tab
    w.tick(w.pane("p1", tab="t1"), w.pane("p2", tab="t1"))
    assert asked == ["t1", "t1"]
    w.tick(w.pane("p2", tab="t1"), w.pane("p3", tab="t1"))  # p1 was replaced by a successor
    assert asked == ["t1", "t1", "t1"]
    w.tick(w.pane("p1", tab="t1"), w.pane("p2", tab="t1"), w.pane("p3", tab="t2"))  # one call per tab per tick
    assert asked.count("t2") == 1


def test_the_default_side_pane_check_is_false_for_a_pane_without_a_marker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERDR_ENGINEERING_STATE", str(tmp_path / "state"))  # the check reads markers only, never Herdr
    w_pane = AgentPane("p1", "t1", "claude", "idle", None, None)
    assert ctlrun.default_is_side_pane(w_pane) is False
    assert ctlrun.Controller.__dataclass_fields__["is_side_pane"].default is ctlrun.default_is_side_pane


def test_a_refused_checkpoint_push_takes_over_with_the_dirty_source_allowed_and_keeps_the_error(tmp_path: Path) -> None:
    w = Wave(tmp_path, cfg=replace(CFG, push_default=True))
    calls = _briefed(w, tmp_path)
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    _git(w.repo, "remote", "add", "origin", str(remote))
    _git(w.repo, "push", "-q", "-u", "origin", "feat/x")
    hook = remote / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\necho refused-by-hook >&2\nexit 1\n")
    hook.chmod(0o755)
    (w.repo / "a.txt").write_text("changed after the last push\n")
    _git(w.repo, "add", "-u")  # a tracked change for the checkpoint to commit
    w.tick(w.pane("p1", "idle"))
    assert w.names()[-1] == "handoff.succeeded"
    (done,) = [e for e in w.events() if e.event == "handoff.wip_committed"]
    assert done.reason == "push_failed" and done.error and "refused-by-hook" in done.error
    assert "--allow-dirty-source" in calls.read_text()  # the unpushed commit must not make the close refuse

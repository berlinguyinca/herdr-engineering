"""Final fix wave, commit 3: controller cleanups. New names are reached through their modules so that each test fails
on its own while the feature is missing."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from herdr_engineering import handoff_subagent_registry as registry
from herdr_engineering.handoff_adapters.base import Harness, SessionRef
from herdr_engineering.handoff_adapters.claude import ClaudeAdapter
from herdr_engineering.handoff_controller import briefing as briefingmod
from herdr_engineering.handoff_controller import events as eventsmod
from herdr_engineering.handoff_controller import run as ctlrun
from herdr_engineering.handoff_controller import takeover as takeovermod
from herdr_engineering.handoff_controller.events import EventLog, HandoffEvent
from herdr_engineering.handoff_controller.herdr_cli import HerdrCli
from tests.test_handoff_controller_run import CFG, ROOT, World, _git, _repo_on
from tests.test_handoff_controller_wave2 import _fake, _run_takeover


def _checkpoint_events(tmp_path: Path, *, allowed: bool) -> tuple[list[HandoffEvent], str]:
    w = World(tmp_path)
    _repo_on("feature", w.repo, dirty=True)
    ref = SessionRef(Harness.CLAUDE, "p1", w.repo, "s-p1", None)
    assert ctlrun._checkpoint(w.ctl, ref, "c1", w.repo, allowed, 85.0)
    return [e for e in w.events() if e.event == "handoff.wip_committed"], subprocess.run(["git", "-C", str(w.repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()


def test_the_wip_event_carries_the_commit_and_why_it_was_not_pushed(tmp_path: Path) -> None:
    (done,), head = _checkpoint_events(tmp_path, allowed=False)
    assert done.commit and head.startswith(done.commit) and done.reason == "push_not_allowed"


def test_the_wip_event_says_committed_when_nothing_was_left_to_push(tmp_path: Path) -> None:
    w = World(tmp_path)
    _repo_on("feature", w.repo, dirty=True)
    _git(w.repo, "remote", "add", "origin", str(w.repo))  # no upstream, no push: the reason is the checkpoint's own
    ref = SessionRef(Harness.CLAUDE, "p1", w.repo, "s-p1", None)
    assert ctlrun._checkpoint(w.ctl, ref, "c1", w.repo, False, 85.0)
    done = next(e for e in w.events() if e.event == "handoff.wip_committed")
    assert done.reason in ("committed", "push_not_allowed") and done.commit


def test_the_state_root_is_one_public_helper_that_reads_the_given_home(tmp_path: Path) -> None:
    env = {"HOME": str(tmp_path)}
    assert registry.state_root(env) == tmp_path / ".local/share/herdr-engineering/state"
    assert registry.state_root({**env, "HERDR_ENGINEERING_STATE": str(tmp_path / "s")}) == tmp_path / "s"
    assert ctlrun.state_dir_for(env) == registry.state_root(env)
    assert not hasattr(registry, "_state_dir") or registry._state_dir(env) == registry.state_root(env) / "subagents"


def test_the_cli_and_the_controller_use_no_private_state_dir_import() -> None:
    for name in ("herdr_engineering/cli.py", "herdr_engineering/handoff_controller/run.py"):
        assert "import _state_dir" not in (ROOT / name).read_text(), name


def test_the_status_probe_neither_creates_nor_holds_the_lock(tmp_path: Path) -> None:
    assert ctlrun.lock_held(tmp_path) is False
    assert not ctlrun.lock_path(tmp_path).exists()  # a probe of a host that never ran a controller leaves nothing behind
    lock = ctlrun.ControllerLock(tmp_path)
    assert lock.acquire() and ctlrun.lock_held(tmp_path) is True
    lock.release()
    other = ctlrun.ControllerLock(tmp_path)
    assert ctlrun.lock_held(tmp_path) is False and other.acquire()
    other.release()


def test_the_two_step_enums_have_distinct_names() -> None:
    from herdr_engineering.handoff_controller import drain
    assert hasattr(ctlrun, "LoopStep") and not hasattr(ctlrun, "Step")
    assert hasattr(drain, "Step") and ctlrun.LoopStep is not drain.Step


def test_there_is_one_timestamp_helper() -> None:
    assert eventsmod.iso(0.0) == "1970-01-01T00:00:00Z"
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?Z", eventsmod.now_iso())
    assert not hasattr(ctlrun, "_iso") and not hasattr(takeovermod, "_now")


def test_there_is_one_set_of_terminal_events_and_no_provenance_patcher() -> None:
    for name in ("run", "takeover", "compaction"):
        assert "_TERMINAL" not in (ROOT / f"herdr_engineering/handoff_controller/{name}.py").read_text()
    assert not hasattr(briefingmod, "_mark_fallback")


def test_a_restart_in_the_compaction_fallback_does_not_replay_the_takeover_attempts(tmp_path: Path) -> None:
    fake, calls = _fake(tmp_path, takeover_ok=False)
    log = EventLog(tmp_path / "store")
    first = _run_takeover(tmp_path, fake)  # attempts fail, then compaction (no herdr here) fails
    assert first.event == "handoff.stuck"
    attempts = calls.read_text().split().count("takeover")
    assert attempts >= 1
    marks = [e for e in log.read() if e.event == "handoff.takeover_started" and e.reason == eventsmod.COMPACTING]
    assert len(marks) == 1  # the log says that the fallback began
    again = takeovermod.run_takeover(
        SessionRef(Harness.CLAUDE, "p1", tmp_path, "s1", None), ClaudeAdapter(home=tmp_path / "home"),
        "handoff_" + "a" * 32, CFG, log, fake, HerdrCli("/nonexistent/herdr"), store_root=tmp_path / "store",
        worktree=tmp_path, push_allowed=False, env={}, resumed=True, retry_delay_s=0.0, compact_ready_timeout_s=0.1,
        sleep=lambda _s: None)
    assert again.event == "handoff.stuck"
    assert calls.read_text().split().count("takeover") == attempts  # not one more launch
    assert sum(1 for e in log.read() if e.event == "handoff.takeover_started" and e.reason is None) == 1

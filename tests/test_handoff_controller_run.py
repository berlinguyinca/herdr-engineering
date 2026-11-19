"""The controller loop. Decisions are pure functions tested here without Herdr; the loop is driven with real files, real
git repos and a real `herdr agent list` row type; the `herdr_server` tests drive a real isolated Herdr server."""
from __future__ import annotations

import json
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from herdr_engineering.handoff_adapters.base import Harness, SessionRef
from herdr_engineering.handoff_adapters.claude import ClaudeAdapter
from herdr_engineering.handoff_controller.config import ControllerConfig, config_from_env
from herdr_engineering.handoff_controller.drain import agent_matches
from herdr_engineering.handoff_controller.events import Colour, EventLog, HandoffEvent
from herdr_engineering.handoff_controller.handoff_cli import HandoffCli
from herdr_engineering.handoff_controller.herdr_cli import (
    AgentPane,
    HerdrCli,
    harness_for_agent_name,
)
from herdr_engineering.handoff_controller.run import (
    Chain,
    Controller,
    ControllerLock,
    PaneRuntime,
    Step,
    build_controller,
    drain_not_typed_reason,
    lock_held,
    may_start,
    next_step,
    open_chain,
    pane_events,
    push_allowed,
    run_once,
    serve,
    status_lines,
)
from herdr_engineering.handoff_controller.switch import set_enabled
from herdr_engineering.handoff_controller.trigger import Decision
from tests.handoff_controller_herdr import isolated_herdr, wait_for
from tests.test_handoff_controller_briefing import GH

ROOT = Path(__file__).resolve().parents[1]
herdr_server = pytest.mark.herdr_server
CFG = ControllerConfig()
WINDOW = 200_000  # claude-opus-4-8 in the model table; the adapter below has no model catalogue


def _event(name: str, at: float = 1000.0, *, chain: str = "c1", pane: str = "p1", session: str | None = "s1",
           reason: str | None = None, colour: Colour | None = None, handoff_id: str | None = None) -> HandoffEvent:
    from herdr_engineering.handoff_controller.run import _iso
    return HandoffEvent(name, _iso(at), chain, pane, session, Harness.CLAUDE, handoff_id, None, None, colour, reason, None)


def _usage_line(used: int) -> str:
    return json.dumps({"type": "assistant", "isSidechain": False, "message": {
        "model": "claude-opus-4-8", "usage": {"input_tokens": used, "cache_read_input_tokens": 0,
                                              "cache_creation_input_tokens": 0}}}) + "\n"


class World:
    """A controller over tmp dirs, whose panes are given by the test and whose Claude sessions are fixture transcripts."""

    def __init__(self, tmp_path: Path, *, now: float = 5000.0, cfg: ControllerConfig = CFG) -> None:
        self.tmp = tmp_path
        self.store = tmp_path / "store"
        self.state = tmp_path / "state"
        self.clock = [now]
        self.cfg = cfg
        self.located: list[str] = []
        self.transcripts: dict[str, Path] = {}
        self.repo = tmp_path / "repo"
        self.repo.mkdir()
        self.env = {"HERDR_ENGINEERING_STATE": str(self.state / "engineering")}
        self.ctl = self.controller()

    def controller(self, *, runner: object = None, **kwargs: object) -> Controller:
        def locate(adapter: object, pane: AgentPane) -> SessionRef | None:
            self.located.append(pane.pane_id)
            path = self.transcripts.get(pane.pane_id)
            return SessionRef(Harness.CLAUDE, pane.pane_id, self.repo, f"s-{pane.pane_id}", path) if path else None

        def no_answer(harness: Harness, prompt: str, *, cwd: Path, timeout_s: float) -> str | None:
            return None

        ctl = Controller(self.cfg, self.store, self.state, self.env, HerdrCli("/nonexistent/herdr"), HandoffCli(),
                         EventLog(self.store), locate=locate, clock=lambda: self.clock[0], sleep=lambda _s: None,
                         harness_runner=runner or no_answer, gh=GH,
                         drain_attempt_s=0.2, drain_poll_s=0.05, fallback_timeout_s=30.0,
                         takeover_kwargs=kwargs or {})  # type: ignore[arg-type]
        ctl.adapters[Harness.CLAUDE] = ClaudeAdapter(home=self.tmp / "home")
        return ctl

    def session(self, pane: str, pct: float) -> None:
        path = self.transcripts.setdefault(pane, self.tmp / f"{pane}.jsonl")
        path.write_text(_usage_line(int(WINDOW * pct / 100)))

    def pane(self, pane: str = "p1", status: str = "idle", name: str | None = "claude", tab: str | None = "t1") -> AgentPane:
        return AgentPane(pane, tab, name, status, self.repo, f"s-{pane}")

    def tick(self, *panes: AgentPane) -> None:
        run_once(self.ctl, list(panes))

    def events(self, pane: str | None = None) -> list[HandoffEvent]:
        return [e for e in self.ctl.log.read() if pane is None or e.pane_id == pane]

    def names(self, pane: str = "p1") -> list[str]:
        return [e.event for e in self.events(pane)]


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)


def _repo_on(branch: str, path: Path, *, dirty: bool) -> None:
    subprocess.run(["git", "init", "-q", "-b", branch, str(path)], check=True)
    for key, value in (("user.name", "T"), ("user.email", "t@example.com"), ("commit.gpgsign", "false")):
        _git(path, "config", key, value)
    (path / "a.txt").write_text("one\n")
    _git(path, "add", "a.txt")
    _git(path, "commit", "-q", "-m", "init")
    if dirty:
        (path / "a.txt").write_text("two\n")


def _briefing_record(store: Path, pane: str, created_at: str, hex_id: str = "a" * 32) -> str:
    handoff_id = f"handoff_{hex_id}"
    (store / "outbox").mkdir(parents=True, exist_ok=True)
    (store / "outbox" / f"{handoff_id}.json").write_text(json.dumps(
        {"handoff_id": handoff_id, "source": {"pane_id": pane}, "created_at": created_at}))
    return handoff_id


# --- pure decisions -------------------------------------------------------------------------------------------------

def test_the_agent_names_the_loop_and_the_drain_guard_share() -> None:
    assert [harness_for_agent_name(n) for n in ("claude", "codex", "pi", "opencode", "droid", None)] == [
        Harness.CLAUDE, Harness.CODEX, Harness.PI, Harness.OPENCODE, None, None]
    assert agent_matches("pi", Harness.PI) and not agent_matches("pi", Harness.CLAUDE) and not agent_matches(None, Harness.PI)


def test_next_step_starts_a_chain_only_at_the_limit_and_only_when_allowed() -> None:
    rt = PaneRuntime("s1")
    assert next_step(None, rt, Decision.NONE, 0, CFG, True) is Step.NONE
    assert next_step(None, rt, Decision.DRAIN, 0, CFG, True) is Step.DRAIN
    assert next_step(None, rt, Decision.OVER_CEILING, 0, CFG, True) is Step.DRAIN
    assert next_step(None, rt, Decision.DRAIN, 0, CFG, False) is Step.NONE


def test_next_step_waits_for_the_briefing_then_falls_back_once_the_timeout_passes() -> None:
    chain = Chain("c1", 100.0, "t", None, False)
    rt = PaneRuntime("s1", typed=True, wait_since=100.0)
    assert next_step(chain, rt, Decision.DRAIN, 100.0 + CFG.briefing_timeout_s - 1, CFG, True) is Step.CHECK_BRIEFING
    assert next_step(chain, rt, Decision.NONE, 100.0 + CFG.briefing_timeout_s, CFG, True) is Step.FALLBACK


def test_next_step_retries_the_drain_until_the_drain_timeout_and_never_types_during_a_cooldown() -> None:
    chain = Chain("c1", 100.0, None, None, False)
    rt = PaneRuntime("s1")
    assert next_step(chain, rt, Decision.DRAIN, 150.0, CFG, True) is Step.DRAIN
    cooling = PaneRuntime("s1", cooldown_until=500.0)
    assert next_step(chain, cooling, Decision.DRAIN, 150.0, CFG, True) is Step.CHECK_BRIEFING
    assert next_step(chain, cooling, Decision.DRAIN, 500.0, CFG, True) is Step.DRAIN
    assert next_step(chain, rt, Decision.DRAIN, 100.0 + CFG.drain_timeout_s, CFG, True) is Step.DRAIN_STUCK


def test_next_step_finishes_a_chain_that_has_a_briefing_or_a_started_takeover() -> None:
    rt = PaneRuntime("s1")
    assert next_step(Chain("c", 1.0, "t", "handoff_x", False), rt, Decision.NONE, 2.0, CFG, True) is Step.FINISH
    assert next_step(Chain("c", 1.0, "t", None, True), rt, Decision.NONE, 2.0, CFG, True) is Step.FINISH


def test_open_chain_is_the_last_triggered_chain_without_a_terminal_event() -> None:
    assert open_chain([]) is None
    done = [_event("handoff.triggered", chain="a"), _event("handoff.succeeded", chain="a", colour=Colour.GREEN)]
    assert open_chain(done) is None
    log = [*done, _event("handoff.triggered", 2000.0, chain="b"), _event("handoff.drain_started", 2001.0, chain="b"),
           _event("handoff.briefing_fallback", 2002.0, chain="b", handoff_id="handoff_x"),
           _event("handoff.takeover_started", chain="b"), _event("handoff.skipped", chain="skipped:p1", reason="no_record")]
    chain = open_chain(log)
    assert chain is not None and chain.chain_id == "b" and chain.handoff_id == "handoff_x" and chain.takeover_started
    assert chain.triggered_at == pytest.approx(2000.0) and chain.drain_started_at is not None


def test_a_reused_pane_id_with_another_session_sees_no_old_events() -> None:
    old = [_event("handoff.triggered", session="old"), _event("handoff.succeeded", session="old", colour=Colour.GREEN)]
    assert pane_events(old, "p1", "new") == [] and pane_events(old, "p2", "old") == []
    assert len(pane_events(old, "p1", "old")) == 2 and len(pane_events(old, "p1", None)) == 2


def test_may_start_never_after_a_handoff_and_not_again_at_once_after_a_failure() -> None:
    assert may_start([], 0.0, CFG)
    won = [_event("handoff.succeeded", 100.0, colour=Colour.GREEN)]
    assert not may_start(won, 10 ** 9, CFG)
    red = [_event("handoff.stuck", 100.0, reason="blocked", colour=Colour.RED)]
    assert not may_start(red, 100.0 + CFG.drain_timeout_s - 1, CFG)
    assert may_start(red, 100.0 + CFG.drain_timeout_s, CFG)
    orange = [_event("handoff.failed_compacted", 100.0, colour=Colour.ORANGE)]
    assert not may_start(orange, 101.0, CFG)


def test_push_is_allowed_by_repo_or_by_the_default(tmp_path: Path) -> None:
    repo, worktree = tmp_path / "repo", tmp_path / "wt" / "feature"
    worktree.mkdir(parents=True)
    repo.mkdir()
    assert not push_allowed(CFG, worktree, repo / ".git")
    allowing = ControllerConfig(push_allowed_repos=(str(repo),))
    assert push_allowed(allowing, worktree, repo / ".git")  # the worktree belongs to the allowed repository
    assert not push_allowed(allowing, worktree, None)
    assert push_allowed(ControllerConfig(push_default=True), worktree, None)
    assert push_allowed(ControllerConfig(push_allowed_repos=(str(tmp_path / "wt"),)), worktree, None)


def test_config_from_env_reads_the_push_policy() -> None:
    assert config_from_env({}) == ControllerConfig()
    cfg = config_from_env({"HERDR_HANDOFF_PUSH_REPOS": "/a:/b", "HERDR_HANDOFF_PUSH_DEFAULT": "1"})
    assert cfg.push_allowed_repos == ("/a", "/b") and cfg.push_default


def test_every_drain_outcome_has_a_stuck_reason() -> None:
    reasons = {o: drain_not_typed_reason(o).value for o in ("blocked", "timeout", "wrong_agent", "too_long", "prompt_uncertain")}
    assert reasons["blocked"] == "blocked" and reasons["timeout"] == "drain_timeout" and reasons["wrong_agent"] == "wrong_agent"
    assert drain_not_typed_reason("something new").value == "drain_timeout"


# --- the loop, without Herdr ----------------------------------------------------------------------------------------

def test_a_session_below_the_limit_produces_no_event(tmp_path: Path) -> None:
    world = World(tmp_path)
    world.session("p1", 40.0)
    world.tick(world.pane())
    assert world.events() == []


def test_skipped_event_once_per_reason(tmp_path: Path) -> None:
    world = World(tmp_path)
    world.session("p1", 90.0)
    set_enabled("p1", False, world.state)
    for _ in range(3):
        world.tick(world.pane())
    world.ctl = world.controller()  # a restart: the log is the memory
    world.tick(world.pane())
    skipped = world.events()
    assert [(e.event, e.reason) for e in skipped] == [("handoff.skipped", "switch_off")]
    set_enabled("p1", True, world.state)
    world.tick(world.pane("p1", "working"))
    assert world.names()[-1] == "handoff.triggered"  # switched back on: the session at 90% is handled


def test_a_session_without_usage_is_skipped_once_as_no_record_and_a_session_without_record_as_no_session(tmp_path: Path) -> None:
    world = World(tmp_path)
    world.transcripts["p1"] = tmp_path / "empty.jsonl"
    world.transcripts["p1"].write_text("")
    for _ in range(2):
        world.tick(world.pane("p1"), world.pane("p2"))
    assert [(e.pane_id, e.reason) for e in world.events()] == [("p1", "no_record"), ("p2", "no_session")]


def test_a_pane_that_runs_no_known_agent_is_left_alone(tmp_path: Path) -> None:
    world = World(tmp_path)
    world.tick(world.pane("p1", name=None), world.pane("p2", name="droid"))
    assert world.events() == [] and world.located == []


def test_the_session_record_is_located_once_and_again_when_the_agent_changes_or_the_pane_goes(tmp_path: Path) -> None:
    world = World(tmp_path)
    world.session("p1", 10.0)
    world.tick(world.pane())
    world.tick(world.pane())
    assert world.located == ["p1"]
    world.tick(AgentPane("p1", "t1", "claude", "idle", world.repo, "other-session"))
    assert world.located == ["p1", "p1"]
    world.tick()  # the pane went away
    world.tick(world.pane())
    assert world.located == ["p1", "p1", "p1"]


def test_a_failed_lookup_is_retried_after_a_while_not_every_tick(tmp_path: Path) -> None:
    world = World(tmp_path)
    world.tick(world.pane())
    world.tick(world.pane())
    assert world.located == ["p1"]
    world.clock[0] += 31.0
    world.session("p1", 10.0)
    world.tick(world.pane())
    assert world.located == ["p1", "p1"]


def test_at_the_limit_a_working_session_is_triggered_once_and_nothing_is_typed(tmp_path: Path) -> None:
    world = World(tmp_path)
    world.session("p1", 81.0)
    for _ in range(3):
        world.tick(world.pane("p1", "working"))
    triggered = world.events()
    assert [e.event for e in triggered] == ["handoff.triggered"]
    assert triggered[0].context_pct == pytest.approx(81.0, abs=0.1)


@pytest.mark.parametrize(("status", "reason"), [("working", "drain_timeout"), ("blocked", "blocked")])
def test_a_drain_that_never_gets_a_turn_ends_stuck_after_the_drain_timeout(tmp_path: Path, status: str, reason: str) -> None:
    world = World(tmp_path)
    world.session("p1", 85.0)
    world.tick(world.pane("p1", status))
    world.clock[0] += CFG.drain_timeout_s
    world.tick(world.pane("p1", status))
    last = world.events()[-1]
    assert (last.event, last.reason, last.colour) == ("handoff.stuck", reason, Colour.RED)
    world.tick(world.pane("p1", status))  # no new chain right away
    assert len(world.events()) == 2


def test_over_the_ceiling_with_an_order_that_cannot_be_typed_goes_straight_to_the_last_resort(tmp_path: Path) -> None:
    world = World(tmp_path)  # the fallback writer answers nothing, so the last resort has no briefing and the chain is stuck
    world.session("p1", 95.0)
    world.tick(world.pane("p1", "working"))
    assert world.names() == ["handoff.triggered", "handoff.stuck"]
    assert world.events()[-1].reason == "no_briefing"
    world.tick(world.pane("p1", "working"))
    assert len(world.events()) == 2  # never a loop


def test_a_session_with_a_live_successor_is_not_looked_at(tmp_path: Path) -> None:
    world = World(tmp_path)
    world.session("p1", 99.0)
    world.ctl.log.append(_event("handoff.succeeded", 1.0, session="s-p1", colour=Colour.GREEN))
    world.tick(world.pane("p1", "working"))
    assert world.names() == ["handoff.succeeded"]


def _typed_chain(world: World, *, started: float) -> None:
    """The log of a chain whose order was typed `started` seconds-clock ago; a restart rebuilds the rest from it."""
    world.ctl.log.append(_event("handoff.triggered", started, chain="c1", session="s-p1"))
    world.ctl.log.append(_event("handoff.drain_started", started + 1, chain="c1", session="s-p1"))


def test_a_restart_waits_for_the_briefing_without_typing_again_and_falls_back_at_the_deadline(tmp_path: Path) -> None:
    world = World(tmp_path)
    world.session("p1", 85.0)
    _typed_chain(world, started=world.clock[0] - 10)
    world.tick(world.pane("p1", "idle"))
    assert world.names() == ["handoff.triggered", "handoff.drain_started"]  # waiting; nothing new, nothing typed
    world.clock[0] += CFG.briefing_timeout_s
    world.tick(world.pane("p1", "idle"))
    assert world.names()[2:] == ["handoff.drained", "handoff.stuck"]  # the fallback writer answered nothing
    assert world.events()[-1].reason == "no_briefing"


def test_a_briefing_the_session_wrote_is_picked_up_and_a_dirty_main_stops_the_chain(tmp_path: Path) -> None:
    world = World(tmp_path)
    _repo_on("main", world.repo, dirty=True)
    world.session("p1", 85.0)
    _typed_chain(world, started=world.clock[0] - 10)
    written = _briefing_record(world.store, "p1", "2999-01-01T00:00:00Z")
    world.tick(world.pane("p1", "idle"))
    names = world.names()
    assert names[2:] == ["handoff.drained", "handoff.briefing_written", "handoff.stuck"]
    assert world.events()[3].handoff_id == written
    assert world.events()[-1].reason.startswith("on_main_with_changes")  # type: ignore[union-attr]
    assert (world.repo / "a.txt").read_text() == "two\n"  # nothing was committed on main


def test_a_worktree_whose_git_state_is_unknown_stops_the_chain(tmp_path: Path) -> None:
    world = World(tmp_path)  # world.repo is not a git repository
    world.session("p1", 85.0)
    _typed_chain(world, started=world.clock[0] - 10)
    _briefing_record(world.store, "p1", "2999-01-01T00:00:00Z")
    world.tick(world.pane("p1", "idle"))
    assert world.events()[-1].event == "handoff.stuck" and world.events()[-1].reason.startswith("git_state_unknown")  # type: ignore[union-attr]


def test_a_briefing_found_during_an_uncertain_prompts_cooldown_ends_the_wait(tmp_path: Path) -> None:
    world = World(tmp_path)
    world.session("p1", 85.0)
    _typed_chain(world, started=world.clock[0] - 10)
    world.tick(world.pane("p1", "idle"))  # builds the runtime (typed, since a restart cannot tell)
    world.ctl.runtime["p1"].typed = False
    world.ctl.runtime["p1"].cooldown_until = world.clock[0] + 100
    _briefing_record(world.store, "p1", "2999-01-01T00:00:00Z")
    world.tick(world.pane("p1", "idle"))
    assert "handoff.briefing_written" in world.names()


def test_one_panes_failure_does_not_stop_the_others(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    world = World(tmp_path)
    world.session("p2", 85.0)
    world.transcripts["p1"] = tmp_path / "unreadable"
    world.transcripts["p1"].mkdir()  # reading a directory as a transcript is an OSError the adapter reports as no record
    broken = world.ctl.locate

    def locate(adapter: object, pane: AgentPane) -> SessionRef | None:
        if pane.pane_id == "p1":
            raise RuntimeError("boom")
        return broken(adapter, pane)  # type: ignore[arg-type]

    world.ctl.locate = locate
    world.tick(world.pane("p1"), world.pane("p2", "working"))
    assert world.names("p2") == ["handoff.triggered"]
    assert "p1: RuntimeError: boom" in capsys.readouterr().err


def test_the_side_pane_hook_is_skipped_when_the_module_does_not_exist_and_asked_once_per_tab(tmp_path: Path) -> None:
    world = World(tmp_path)
    world.session("p1", 10.0)
    world.tick(world.pane("p1", tab="t7"), world.pane("p2", tab="t7"))
    assert world.ctl._tabs == {"t7"}


def test_a_tick_with_no_herdr_does_nothing(tmp_path: Path) -> None:
    world = World(tmp_path)
    run_once(world.ctl)  # the herdr binary is /nonexistent: no listing, no events, no error
    assert world.events() == []


# --- host: lock, loop, status, CLI, manifest ------------------------------------------------------------------------

def test_a_second_controller_exits_zero_with_a_message(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    world = World(tmp_path)
    first = ControllerLock(world.state)
    assert first.acquire() and lock_held(world.state)
    assert serve(controller=world.ctl, once=True) == 0
    assert "another handoff controller is running" in capsys.readouterr().out
    first.release()
    assert not lock_held(world.state)
    assert serve(controller=world.ctl, once=True) == 0 and not lock_held(world.state)


def test_status_lists_the_lock_and_the_last_event_of_each_chain_in_progress(tmp_path: Path) -> None:
    world = World(tmp_path)
    world.ctl.log.append(_event("handoff.triggered", 1.0, chain="open", pane="p9"))
    world.ctl.log.append(_event("handoff.succeeded", 2.0, chain="done", colour=Colour.GREEN))
    world.ctl.log.append(_event("handoff.skipped", 3.0, chain="skipped:p1", reason="switch_off"))
    lines = status_lines(world.state, world.ctl.log)
    assert lines[0] == "controller: not running" and len(lines) == 2
    assert lines[1].startswith("open pane p9 claude: handoff.triggered at ")


def test_the_cli_refuses_to_start_without_its_programs_and_prints_status(tmp_path: Path) -> None:
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "HERDR_ENGINEERING_STATE": str(tmp_path / "state" / "engineering"),
           "HERDR_HANDOFF_STORE": str(tmp_path / "store"), "HERDR_HANDOFF_BIN_DIRS": "", "PYTHONPATH": str(ROOT)}
    # no `herdr` on this PATH and no extra dirs: one line on stderr, exit 2, and never a tick against a real Herdr
    run = subprocess.run([sys.executable, "-m", "herdr_engineering.cli", "handoff", "controller", "run", "--once"],
                         env=env, cwd=ROOT, capture_output=True, text=True, timeout=60, check=False)
    assert run.returncode == 2 and run.stderr.count("\n") == 1 and "herdr" in run.stderr, run.stderr
    status = subprocess.run([sys.executable, "-m", "herdr_engineering.cli", "handoff", "controller", "status"],
                            env=env, cwd=ROOT, capture_output=True, text=True, timeout=60, check=False)
    assert status.returncode == 0 and status.stdout.strip() == "controller: not running"
    assert build_controller(env, require_binaries=False).store_root == tmp_path / "store"


def test_plugin_manifest_valid() -> None:
    manifest = tomllib.loads((ROOT / "deploy" / "herdr-handoff-controller-plugin" / "herdr-plugin.toml").read_text())
    assert manifest["id"] == "herdr.handoff-controller" and manifest["min_herdr_version"] == "0.9.1"
    assert manifest["startup"] == [{"command": ["herdr-eng", "handoff", "controller", "run"]}]
    assert [(a["id"], a["command"]) for a in manifest["actions"]] == [
        ("handoff-controller-status", ["herdr-eng", "handoff", "controller", "status"])]
    assert set(manifest["platforms"]) == {"linux", "macos"}
    for key in ("name", "version", "description"):
        assert manifest[key]


# --- the loop against a real isolated Herdr server -----------------------------------------------------------------

def _server_world(h: object, tmp_path: Path, pct: float) -> tuple[World, str]:
    """A controller whose Herdr is the isolated server, with its one pane running the fixture program as `claude`."""
    from tests.handoff_controller_herdr import IsolatedHerdr
    assert isinstance(h, IsolatedHerdr)
    world = World(tmp_path)
    world.ctl = world.controller()
    world.ctl.herdr = h.cli
    world.env = {**h.env, "HERDR_ENGINEERING_STATE": str(h.root / "state")}
    world.ctl.env = world.env
    pane = h.start()
    assert wait_for(lambda: any(p.pane_id == pane and p.name == "claude" and p.status == "idle" for p in h.cli.agents() or []), 20.0)
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(_usage_line(int(WINDOW * pct / 100)))
    world.ctl.locate = lambda adapter, p: SessionRef(Harness.CLAUDE, p.pane_id, h.root, "sess-1", transcript)
    world.ctl.drain_attempt_s = 10.0
    return world, pane


@herdr_server
def test_run_once_drains_session_at_limit(tmp_path: Path) -> None:
    with isolated_herdr("run-drain") as h:
        world, pane = _server_world(h, tmp_path, 81.0)
        run_once(world.ctl)
        assert [e.event for e in world.events(pane)] == ["handoff.triggered", "handoff.drain_started"]
        assert wait_for(lambda: h.submitted.exists() and "Context is nearly full" in h.submitted.read_text(), 10.0)
        assert world.ctl.runtime[pane].typed
        run_once(world.ctl)  # waiting for the briefing: nothing more is typed
        assert h.submitted.read_text().count("Context is nearly full") == 1


@herdr_server
def test_run_once_ignores_session_below_limit(tmp_path: Path) -> None:
    with isolated_herdr("run-below") as h:
        world, pane = _server_world(h, tmp_path, 40.0)
        run_once(world.ctl)
        assert world.events() == [] and not (h.submitted.exists() and h.submitted.read_text().strip())


@herdr_server
def test_restart_resumes_from_event_log(tmp_path: Path) -> None:
    with isolated_herdr("run-restart") as h:
        world, pane = _server_world(h, tmp_path, 85.0)
        run_once(world.ctl)
        assert wait_for(lambda: h.submitted.exists() and "Context is nearly full" in h.submitted.read_text(), 10.0)
        before = world.events(pane)
        restarted = world.controller()  # a new process: no memory, same store and event log
        restarted.herdr, restarted.env, restarted.locate = world.ctl.herdr, world.ctl.env, world.ctl.locate
        run_once(restarted)
        assert world.events(pane) == before  # no new chain, no second drain_started
        assert h.submitted.read_text().count("Context is nearly full") == 1
        assert restarted.runtime[pane].typed

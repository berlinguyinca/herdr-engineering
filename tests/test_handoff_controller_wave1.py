"""Final fix wave, commit 1: the controller never acts on a session that is still working. New names are reached through
their modules so that each test fails on its own while the feature is missing."""
# ruff: noqa: F401, F811
from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

import pytest

from herdr_engineering.handoff_adapters.base import Harness
from herdr_engineering.handoff_controller import drain as drainmod
from herdr_engineering.handoff_controller import run as ctlrun
from herdr_engineering.handoff_controller.briefing import write_fallback_briefing
from herdr_engineering.handoff_controller.events import Colour, HandoffEvent
from herdr_engineering.handoff_controller.handoff_cli import HandoffCli
from herdr_engineering.handoff_controller.herdr_cli import AgentPane
from herdr_engineering.handoff_subagent_registry import record
from tests.test_handoff_controller_briefing import (
    handoff,
    make_ref,
    repo,
    script_runner,
)
from tests.test_handoff_controller_run import (
    CFG,
    World,
    _briefing_record,
    _event,
    _repo_on,
    _typed_chain,
)

B = CFG.briefing_timeout_s
D = CFG.drain_timeout_s


class Wave(World):
    """A world whose panes' readiness is what the test says: `status` per pane, the composer always empty."""

    def __init__(self, tmp_path: Path, **kwargs: object) -> None:
        self.status: dict[str, str] = {}
        super().__init__(tmp_path, **kwargs)  # type: ignore[arg-type]

    def controller(self, *, runner: object = None, **kwargs: object) -> ctlrun.Controller:
        ctl = super().controller(runner=runner, **kwargs)
        ctl.look = lambda ref, adapter, herdr: drainmod.Look(
            self.status.get(ref.pane_id, "idle"), self.status.get(ref.pane_id, "idle") in ("idle", "done"), False)
        return ctl

    def pane(self, pane: str = "p1", status: str = "idle", name: str | None = "claude", tab: str | None = "t1") -> AgentPane:
        self.status[pane] = status
        return super().pane(pane, status, name, tab)

    def subagent(self, agent_id: str, pane: str = "p1") -> None:
        record("start", json.dumps({"agent_id": agent_id, "agent_type": "Explore"}), {**self.env, "HERDR_PANE_ID": pane})

    def subagent_stops(self, agent_id: str, pane: str = "p1") -> None:
        record("stop", json.dumps({"agent_id": agent_id}), {**self.env, "HERDR_PANE_ID": pane})

    def waiting(self) -> list[HandoffEvent]:
        return [e for e in self.events() if e.event == "handoff.waiting"]


def _typed(w: Wave) -> None:
    w.session("p1", 85.0)
    _typed_chain(w, started=w.clock[0] - 10)


def _fake_handoff(tmp_path: Path) -> tuple[HandoffCli, Path]:
    """A real program that stands in for herdr-handoff: logs each subcommand, answers takeover with a closed source."""
    calls = tmp_path / "calls.txt"
    script = tmp_path / "fake-handoff.py"
    script.write_text(
        "import json, sys\n"
        f"open({str(calls)!r}, 'a').write(' '.join(a for a in sys.argv[1:] if not a.startswith('/')) + '\\n')\n"
        "args = sys.argv[1:]\n"
        "sub = args[2] if len(args) > 2 and args[0] == '--root' else ''\n"
        "if sub == 'takeover':\n"
        "    print(json.dumps({'status': 'source_closed', 'source_closed': True, 'pane_id': 'child'}))\n"
        "elif sub == 'inspect':\n"
        "    print(json.dumps({'handoff_id': args[3]}))\n"
        "elif sub == 'resume':\n"
        "    print(json.dumps({'document': '/x/briefing.md'}))\n"
        "else:\n"
        "    sys.exit(1)\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return HandoffCli((sys.executable, str(script))), calls


def test_waiting_is_an_event_with_a_reason_and_old_lines_still_read() -> None:
    event = HandoffEvent("handoff.waiting", "2026-11-18T00:00:00Z", "c", "p1", "s", Harness.CLAUDE, None, None, None,
                         None, "subagents", 80.0, subagents_live=2)
    assert HandoffEvent.from_json(event.to_json()).subagents_live == 2
    old = '{"event":"handoff.triggered","at":"2026-11-18T00:00:00Z","chain_id":"c","pane_id":"p1","session_id":"s",' \
          '"harness":"claude","handoff_id":null,"predecessor_id":null,"successor_id":null,"colour":null,"reason":null,' \
          '"context_pct":1.0}'
    assert HandoffEvent.from_json(old).subagents_live is None
    with pytest.raises(ValueError):
        HandoffEvent("handoff.waiting", "t", "c", "p1", "s", Harness.CLAUDE, None, None, None, None, None, None)


def test_a_working_session_is_never_given_a_fallback_when_the_briefing_timeout_passes(tmp_path: Path) -> None:
    w = Wave(tmp_path)
    _typed(w)
    w.tick(w.pane("p1", "working"))
    w.clock[0] += B + 60
    w.tick(w.pane("p1", "working"))
    w.tick(w.pane("p1", "working"))
    assert w.names() == ["handoff.triggered", "handoff.drain_started", "handoff.waiting"]
    waiting = w.waiting()
    assert [(e.reason, e.subagents_live) for e in waiting] == [("working", 0)]  # said once, not at every tick


def test_the_briefing_timeout_counts_only_the_time_the_session_is_idle(tmp_path: Path) -> None:
    w = Wave(tmp_path)
    _typed(w)
    for _ in range(4):  # a long working spell
        w.tick(w.pane("p1", "working"))
        w.clock[0] += 100
    w.tick(w.pane("p1", "idle"))  # the quiet time starts here
    w.clock[0] += B - 1
    w.tick(w.pane("p1", "idle"))
    assert "handoff.drained" not in w.names()
    w.tick(w.pane("p1", "working"))  # it worked again: the quiet time starts over
    w.clock[0] += 100
    w.tick(w.pane("p1", "idle"))
    w.clock[0] += B - 1
    w.tick(w.pane("p1", "idle"))
    assert "handoff.drained" not in w.names()
    w.clock[0] += 1
    w.tick(w.pane("p1", "idle"))
    assert w.names()[-2:] == ["handoff.drained", "handoff.stuck"]  # the writer answered nothing


def test_live_subagents_hold_the_chain_and_each_change_is_said_once(tmp_path: Path) -> None:
    w = Wave(tmp_path)
    _typed(w)
    w.subagent("a1")
    w.subagent("a2")
    for _ in range(3):
        w.clock[0] += B
        w.tick(w.pane("p1", "idle"))
    assert [(e.reason, e.subagents_live) for e in w.waiting()] == [("subagents", 2)]
    assert "handoff.drained" not in w.names()
    w.subagent_stops("a1")
    w.tick(w.pane("p1", "idle"))
    assert [(e.reason, e.subagents_live) for e in w.waiting()] == [("subagents", 2), ("subagents", 1)]
    restarted = w.controller()  # a new process: the log is the memory, so the same state is not said again
    ctlrun.run_once(restarted, [w.pane("p1", "idle")])
    assert len(w.waiting()) == 2
    w.subagent_stops("a2")
    w.tick(w.pane("p1", "idle"))
    w.clock[0] += B
    w.tick(w.pane("p1", "idle"))
    assert w.names()[-2:] == ["handoff.drained", "handoff.stuck"]


def test_when_the_wait_bound_is_hit_the_fallback_briefing_lists_the_running_subagents(
        tmp_path: Path, handoff: HandoffCli) -> None:
    w = Wave(tmp_path)
    _repo_on("feat/x", w.repo, dirty=False)
    prompt = tmp_path / "prompt.txt"
    w.ctl = w.controller(runner=script_runner("ok", prompt), compact_ready_timeout_s=0.1)
    w.ctl.handoff = handoff
    w.ctl.look = lambda ref, adapter, herdr: drainmod.Look("working", False, False)
    _typed(w)
    w.subagent("agent-xyz")
    w.tick(w.pane("p1", "working"))
    w.clock[0] += D + 1  # the bound: the trigger plus the drain timeout
    w.tick(w.pane("p1", "working"))
    fallback = [e for e in w.events() if e.event == "handoff.briefing_fallback"]
    assert len(fallback) == 1 and fallback[0].reason is None
    stored = json.loads((w.store / "outbox" / f"{fallback[0].handoff_id}.json").read_text())
    assert any("agent-xyz" in line for line in stored["context"]["unverified"])
    assert "agent-xyz" in prompt.read_text()
    assert "handoff.takeover_started" not in w.names()  # written, but the session is still working: no takeover yet


def test_the_fallback_briefing_writer_takes_the_subagents_into_the_unverified_field(
        tmp_path: Path, handoff: HandoffCli, repo: Path) -> None:
    ref = make_ref(tmp_path, repo)
    root = tmp_path / "store"
    found = write_fallback_briefing(ref, repo, root, script_runner("ok", tmp_path / "p.txt"), handoff=handoff,
                                    subagents=[{"agent_id": "agent-7", "agent_type": "Explore", "started": 1}])
    assert found is not None
    unverified = json.loads((root / "outbox" / f"{found}.json").read_text())["context"]["unverified"]
    assert any("agent-7" in line and "Explore" in line for line in unverified)


def test_takeover_waits_for_a_ready_pane_and_the_last_resort_follows_the_deadline(tmp_path: Path) -> None:
    w = Wave(tmp_path)
    _repo_on("feat/x", w.repo, dirty=False)
    fake, calls = _fake_handoff(tmp_path)
    w.ctl = w.controller(compact_ready_timeout_s=0.1)
    w.ctl.handoff = fake
    w.ctl.look = lambda ref, adapter, herdr: drainmod.Look("working", False, False)
    _typed(w)
    _briefing_record(w.store, "p1", "2999-01-01T00:00:00Z")
    w.tick(w.pane("p1", "working"))
    assert w.names()[-2:] == ["handoff.briefing_written", "handoff.waiting"]
    assert "handoff.takeover_started" not in w.names() and not calls.exists()
    w.clock[0] += B - 1
    w.tick(w.pane("p1", "working"))
    assert "handoff.takeover_started" not in w.names()
    w.clock[0] += 1  # the deadline: no takeover attempts, and the compaction is typed only into a ready pane
    w.tick(w.pane("p1", "working"))
    assert w.names()[-2:] == ["handoff.takeover_started", "handoff.stuck"]
    assert "takeover" not in calls.read_text().split() and "compaction_not_ready" in (w.events()[-1].reason or "")


def test_takeover_is_entered_once_the_pane_is_ready_with_no_subagents(tmp_path: Path) -> None:
    w = Wave(tmp_path)
    _repo_on("feat/x", w.repo, dirty=False)
    fake, calls = _fake_handoff(tmp_path)
    w.ctl = w.controller()
    w.ctl.handoff = fake
    w.ctl.look = lambda ref, adapter, herdr: drainmod.Look(w.status["p1"], w.status["p1"] == "idle", False)
    _typed(w)
    _briefing_record(w.store, "p1", "2999-01-01T00:00:00Z")
    w.subagent("a1")
    w.tick(w.pane("p1", "idle"))
    assert w.waiting()[-1].reason == "subagents" and not calls.exists()
    w.subagent_stops("a1")
    w.tick(w.pane("p1", "idle"))
    assert w.names()[-2:] == ["handoff.takeover_started", "handoff.succeeded"]
    assert "takeover" in calls.read_text().split()


def test_only_a_fallback_written_over_the_ceiling_makes_a_last_resort_chain() -> None:
    triggered = _event("handoff.triggered", 100.0, reason="over_ceiling")
    typed = _event("handoff.drain_started", 101.0)
    written = _event("handoff.briefing_written", 102.0, handoff_id="handoff_a")
    chain = ctlrun.open_chain([triggered, typed, written])
    assert chain is not None and not chain.last_resort  # the order went out and the session wrote its own briefing
    over = _event("handoff.briefing_fallback", 102.0, handoff_id="handoff_a", reason="over_ceiling")
    chain = ctlrun.open_chain([triggered, over])
    assert chain is not None and chain.last_resort
    plain = _event("handoff.briefing_fallback", 102.0, handoff_id="handoff_a")
    chain = ctlrun.open_chain([triggered, typed, plain])
    assert chain is not None and not chain.last_resort


@pytest.mark.parametrize("last_resort", [False, True])
def test_a_chain_with_a_briefing_gets_the_normal_attempts_unless_it_is_the_last_resort(
        tmp_path: Path, last_resort: bool) -> None:
    w = Wave(tmp_path)
    _repo_on("feat/x", w.repo, dirty=False)
    fake, calls = _fake_handoff(tmp_path)
    w.ctl = w.controller(compact_ready_timeout_s=0.1)
    w.ctl.handoff = fake
    w.session("p1", 95.0)
    start = w.clock[0] - 10
    w.ctl.log.append(_event("handoff.triggered", start, chain="c1", session="s-p1", reason="over_ceiling"))
    w.ctl.log.append(_event("handoff.drain_started", start + 1, chain="c1", session="s-p1"))
    if last_resort:
        w.ctl.log.append(_event("handoff.briefing_fallback", start + 2, chain="c1", session="s-p1",
                                handoff_id="handoff_" + "b" * 32, reason="over_ceiling"))
    else:
        w.ctl.log.append(_event("handoff.briefing_written", start + 2, chain="c1", session="s-p1",
                                handoff_id="handoff_" + "b" * 32))
    w.tick(w.pane("p1", "idle"))
    assert ("takeover" in calls.read_text().split()) is (not last_resort)


def test_the_over_the_ceiling_last_resort_still_needs_no_ready_pane_to_write_the_briefing(tmp_path: Path) -> None:
    w = Wave(tmp_path)
    w.session("p1", 95.0)
    w.tick(w.pane("p1", "working"))  # the order cannot be typed over the ceiling: the last resort, as before
    assert w.names() == ["handoff.triggered", "handoff.stuck"] and w.events()[-1].colour is Colour.RED

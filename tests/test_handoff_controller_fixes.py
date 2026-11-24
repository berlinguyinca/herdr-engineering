"""Fix round 1 of the controller loop: state per chain, retry cap, host checks. New names are reached through the module
so that each test fails on its own while the feature is missing."""
from __future__ import annotations

import os
import stat
import subprocess
import time
from pathlib import Path

import pytest

from herdr_engineering.handoff_controller import proc
from herdr_engineering.handoff_controller import run as ctlrun
from herdr_engineering.handoff_controller.events import Colour, EventLog
from herdr_engineering.handoff_controller.run import PaneRuntime, may_start, open_chain, pane_events
from herdr_engineering.handoff_controller.switch import set_enabled
from tests.handoff_controller_herdr import isolated_herdr
from tests.test_handoff_controller_run import (
    CFG,
    ROOT,
    World,
    _briefing_record,
    _event,
    _repo_on,
    _typed_chain,
)

herdr_server = pytest.mark.herdr_server
D = CFG.drain_timeout_s


def _red(at: float, chain: str, colour: Colour = Colour.RED, session: str = "s-p1"):
    name = "handoff.stuck" if colour is Colour.RED else "handoff.failed_compacted"
    return _event(name, at, chain=chain, session=session, reason="blocked" if colour is Colour.RED else None, colour=colour)


# 1 --------------------------------------------------------------------------------------------------------------------
def test_a_new_chain_does_not_inherit_the_old_chains_runtime(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.session("p1", 85.0)
    t0 = w.clock[0]
    w.ctl.log.append(_event("handoff.triggered", t0 - 5000, chain="A", session="s-p1"))
    w.ctl.log.append(_event("handoff.drain_started", t0 - 4990, chain="A", session="s-p1"))
    w.ctl.log.append(_event("handoff.failed_compacted", t0 - 4000, chain="A", session="s-p1", colour=Colour.ORANGE))
    w.ctl.runtime["p1"] = PaneRuntime("s-p1", typed=True, wait_since=t0 - 4990)  # what chain A left behind
    w.tick(w.pane("p1", "working"))  # chain B starts; the pane is busy, so nothing can be typed
    after_first = w.names()
    assert after_first[-1] == "handoff.triggered"
    w.clock[0] += 1
    w.tick(w.pane("p1", "working"))
    assert w.names() == after_first  # B has typed nothing, so it must not wait for a briefing or fall back


# 4 --------------------------------------------------------------------------------------------------------------------
def test_retry_backoff_doubles_then_the_session_is_given_up_on() -> None:
    one = [_red(100.0, "a")]
    two = [*one, _red(10_000.0, "b", Colour.ORANGE)]
    three = [*two, _red(30_000.0, "c")]
    assert not may_start(one, 100.0 + D - 1, CFG) and may_start(one, 100.0 + D, CFG)
    assert not may_start(two, 10_000.0 + 2 * D - 1, CFG) and may_start(two, 10_000.0 + 2 * D, CFG)
    assert not ctlrun.gave_up(two, CFG) and ctlrun.gave_up(three, CFG)
    assert not may_start(three, 10 ** 9, CFG)
    off = _event("handoff.skipped", 31_000.0, chain="skipped:p1", session=None, reason="switch_off")
    assert not ctlrun.gave_up([*three, off], CFG) and may_start([*three, off], 31_000.0, CFG)
    assert not ctlrun.gave_up(pane_events(three, "p1", "another-session"), CFG)


def test_gave_up_is_said_once_and_a_switch_toggle_starts_over(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.session("p1", 85.0)
    base = w.clock[0]
    for i, at in enumerate((base - 90_000, base - 60_000, base - 30_000)):
        w.ctl.log.append(_red(at, f"c{i}"))
    for _ in range(3):
        w.tick(w.pane("p1", "working"))
    assert [(e.event, e.reason) for e in w.events()][3:] == [("handoff.skipped", "gave_up")]
    set_enabled("p1", False, w.state)
    w.tick(w.pane("p1", "working"))
    set_enabled("p1", True, w.state)
    w.tick(w.pane("p1", "working"))
    assert [e.reason for e in w.events() if e.event == "handoff.skipped"] == ["gave_up", "switch_off"]
    assert w.names()[-1] == "handoff.triggered"


# 5 --------------------------------------------------------------------------------------------------------------------
def test_blocked_is_read_at_the_moment_the_drain_times_out_not_remembered(tmp_path: Path) -> None:
    w = World(tmp_path)  # a "restarted" controller: no memory of any earlier drain outcome
    w.session("p1", 85.0)
    w.ctl.log.append(_event("handoff.triggered", w.clock[0] - D, chain="A", session="s-p1"))
    w.tick(w.pane("p1", "blocked"))
    assert w.events()[-1].event == "handoff.stuck" and w.events()[-1].reason == "blocked"


def test_a_chain_triggered_over_the_ceiling_says_so_in_the_log(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.session("p1", 95.0)
    w.tick(w.pane("p1", "working"))
    first = w.events()[0]
    assert first.event == "handoff.triggered" and first.reason == "over_ceiling"
    chain = open_chain([_event("handoff.triggered", 1.0, chain="x", reason="over_ceiling")])
    assert chain is not None and not chain.last_resort  # only the fallback written over the ceiling makes it the last resort
    chain = open_chain([_event("handoff.triggered", 1.0, chain="x"),
                        _event("handoff.briefing_fallback", 2.0, chain="x", handoff_id="h", reason="over_ceiling")])
    assert chain is not None and chain.last_resort
    chain = open_chain([_event("handoff.triggered", 1.0, chain="x")])
    assert chain is not None and not chain.last_resort


# 6 --------------------------------------------------------------------------------------------------------------------
def test_a_finish_that_keeps_raising_ends_the_chain_stuck_after_three_tries(tmp_path: Path) -> None:
    w = World(tmp_path)
    _repo_on("feat", w.repo, dirty=False)
    w.ctl = w.controller(bogus=1.0)  # run_takeover gets an argument it does not know: a TypeError every time
    w.session("p1", 85.0)
    _typed_chain(w, started=w.clock[0] - 10)
    _briefing_record(w.store, "p1", "2999-01-01T00:00:00Z")
    for _ in range(3):
        w.tick(w.pane("p1", "idle"))
    last = w.events()[-1]
    assert last.event == "handoff.stuck" and last.reason == "internal_error:TypeError"


# 7 --------------------------------------------------------------------------------------------------------------------
def test_a_record_that_goes_missing_mid_chain_does_not_stall_the_chain(tmp_path: Path) -> None:
    w = World(tmp_path)  # world.repo is no git repo: finishing ends stuck git_state_unknown, which is fine here
    w.session("p1", 85.0)
    _typed_chain(w, started=w.clock[0] - 10)
    w.transcripts["p1"].write_text("")  # rotated away: no usage reading any more
    _briefing_record(w.store, "p1", "2999-01-01T00:00:00Z")
    w.tick(w.pane("p1", "idle"))
    assert "handoff.briefing_written" in w.names() and "handoff.skipped" not in w.names()


def test_a_record_that_stays_missing_ends_the_chain_stuck_no_record(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.session("p1", 85.0)
    _typed_chain(w, started=w.clock[0] - 10)
    w.transcripts["p1"].write_text("")
    w.tick(w.pane("p1", "idle"))
    assert w.names() == ["handoff.triggered", "handoff.drain_started"]
    w.clock[0] += D
    w.tick(w.pane("p1", "idle"))
    assert w.events()[-1].event == "handoff.stuck" and w.events()[-1].reason == "no_record"


# 8 --------------------------------------------------------------------------------------------------------------------
def test_the_controllers_own_pane_and_side_panes_are_never_managed(tmp_path: Path) -> None:
    w = World(tmp_path)
    for p in ("p1", "p2", "p3"):
        w.session(p, 90.0)
    w.ctl.env = {**w.env, "HERDR_PANE_ID": "p1"}
    w.ctl.is_side_pane = lambda pane: pane.pane_id == "p2"
    w.tick(w.pane("p1", "working"), w.pane("p2", "working"), w.pane("p3", "working"))
    assert w.names("p1") == [] and w.names("p2") == [] and w.names("p3") == ["handoff.triggered"]
    assert "p1" not in w.located and "p2" not in w.located


# 9 --------------------------------------------------------------------------------------------------------------------
def test_read_since_returns_only_new_complete_lines(tmp_path: Path) -> None:
    log = EventLog(tmp_path)
    a, b = _event("handoff.triggered", 1.0, chain="a"), _event("handoff.triggered", 2.0, chain="b")
    log.append(a)
    events, offset = log.read_since(0)
    assert events == [a]
    assert log.read_since(offset) == ([], offset)
    log.append(b)
    with (tmp_path / "events.jsonl").open("ab") as f:
        f.write(b'{"event":"handoff.trig')  # an append in progress
    events, again = log.read_since(offset)
    assert events == [b] and again < (tmp_path / "events.jsonl").stat().st_size
    assert log.read() == [a, b]


def test_the_loop_reads_events_appended_between_ticks_without_rereading(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.session("p1", 85.0)
    w.tick(w.pane("p1", "working"))
    w.ctl.log.append(_event("handoff.succeeded", w.clock[0], chain="x", session="s-p1", colour=Colour.GREEN))
    before = len(w.events())
    w.tick(w.pane("p1", "working"))
    assert len(w.events()) == before  # the success written by someone else was seen: nothing new is started
    assert w.ctl.log_offset == (w.store / "events.jsonl").stat().st_size


def test_a_tab_without_managed_panes_is_forgotten(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.session("p1", 10.0)
    w.tick(w.pane("p1", tab="t7"))
    assert set(w.ctl._tabs) == {"t7"}
    w.tick()
    assert set(w.ctl._tabs) == set()


# 10 -------------------------------------------------------------------------------------------------------------------
def test_old_fallback_work_directories_are_swept_at_start(tmp_path: Path) -> None:
    w = World(tmp_path)
    work = w.store / "fallback-work"
    old, new = work / "old", work / "new"
    old.mkdir(parents=True)
    new.mkdir()
    (old / "f").write_text("x")
    stale = time.time() - 2 * 3600
    os.utime(old, (stale, stale))
    assert ctlrun.sweep_fallback_work(w.store, time.time()) == 1
    assert not old.exists() and new.exists()
    os.utime(new, (stale, stale))
    assert ctlrun.serve(controller=w.ctl, once=True) == 0 and not new.exists()


# 3 --------------------------------------------------------------------------------------------------------------------
def _fake_bin(directory: Path, name: str, output: str = "") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(f"#!/bin/sh\necho {output or name}\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def test_binaries_resolve_to_absolute_paths_in_the_usual_user_dirs(tmp_path: Path) -> None:
    bindir = tmp_path / "home" / ".local" / "bin"
    for name in ("herdr", "herdr-handoff", "git"):
        _fake_bin(bindir, name)
    env = {"PATH": "/nonexistent", "HOME": str(tmp_path / "home")}
    assert ctlrun.default_bin_dirs(env) == [bindir, Path("/opt/homebrew/bin"), Path("/usr/local/bin")]
    found = ctlrun.resolve_binaries({**env, "HERDR_HANDOFF_BIN_DIRS": str(bindir)})
    assert found.herdr == bindir / "herdr" and found.handoff == bindir / "herdr-handoff" and found.git == bindir / "git"
    assert found.gh is None  # optional


def test_a_missing_required_binary_is_a_one_line_error_and_exit_code_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    env = {"PATH": "/nonexistent", "HOME": str(tmp_path)}
    assert ctlrun.serve(env=env, once=True, bin_dirs=()) == 2
    err = capsys.readouterr().err
    assert err.count("\n") == 1 and "herdr" in err


def test_the_resolved_paths_reach_the_clis_and_git_calls(tmp_path: Path) -> None:
    bindir = tmp_path / "bin"
    for name in ("herdr", "herdr-handoff"):
        _fake_bin(bindir, name)
    _fake_bin(bindir, "git", "the-resolved-git")
    env = {"PATH": "/nonexistent", "HOME": str(tmp_path), "HERDR_HANDOFF_STORE": str(tmp_path / "store")}
    try:
        ctl = ctlrun.build_controller(env, bin_dirs=(bindir,))
        assert ctl.herdr._binary == str(bindir / "herdr") and ctl.handoff._prefix == (str(bindir / "herdr-handoff"),)
        assert proc.run_group(["git", "status"], 10.0).stdout.strip() == "the-resolved-git"
    finally:
        proc.use_tool("git", None)


# 2 --------------------------------------------------------------------------------------------------------------------
@herdr_server
def test_herdr_accepts_the_plugin_manifest(tmp_path: Path) -> None:
    with isolated_herdr("run-plugin") as h:
        plugin = ROOT / "deploy" / "herdr-handoff-controller-plugin"
        linked = subprocess.run(["herdr", "plugin", "link", str(plugin), "--disabled"], env=h.env, cwd=h.root,
                                capture_output=True, text=True, timeout=30, check=False)  # disabled: nothing is started
        assert linked.returncode == 0, linked.stdout + linked.stderr
        listed = subprocess.run(["herdr", "plugin", "list", "--json"], env=h.env, cwd=h.root, capture_output=True,
                                text=True, timeout=30, check=False)
        assert "herdr.handoff-controller" in listed.stdout and "handoff-controller-status" in listed.stdout, listed.stdout
        broken = tmp_path / "broken"
        broken.mkdir()
        (broken / "herdr-plugin.toml").write_text('id = "x.broken"\n[[startup]]\nnot_a_command = 1\n')
        refused = subprocess.run(["herdr", "plugin", "link", str(broken), "--disabled"], env=h.env, cwd=h.root,
                                 capture_output=True, text=True, timeout=30, check=False)
        assert refused.returncode != 0  # the check above is a real validation: Herdr does reject a bad manifest
        subprocess.run(["herdr", "plugin", "unlink", "herdr.handoff-controller"], env=h.env, cwd=h.root,
                       capture_output=True, text=True, timeout=30, check=False)

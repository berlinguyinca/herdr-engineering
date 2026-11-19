"""``herdr-eng handoff subagent-hook`` / ``subagents``: real CLI subprocess, tmp HOME and state."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from herdr_engineering import handoff_subagent_registry as reg

REPO = str(Path(__file__).resolve().parents[1])


def cli(tmp: Path, *argv: str, stdin: str = "", pane: str | None = "%1") -> subprocess.CompletedProcess[str]:
    code = "import sys; from herdr_engineering import cli; sys.exit(cli.main(sys.argv[1:]))"
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp), "PYTHONPATH": REPO, "HERDR_ENGINEERING_STATE": str(tmp / "state")}
    if pane is not None:
        env["HERDR_PANE_ID"] = pane
    return subprocess.run([sys.executable, "-c", code, "handoff", *argv], cwd=REPO, env=env, input=stdin,
                          capture_output=True, text=True, timeout=60)


def hook(tmp: Path, phase: str, agent_id: str, **kw) -> subprocess.CompletedProcess[str]:
    payload = {"session_id": "s", "hook_event_name": "x", "agent_id": agent_id, "agent_type": "Explore"}
    return cli(tmp, "subagent-hook", phase, stdin=json.dumps(payload), **kw)


def pane_dir(tmp: Path, pane: str = "%1") -> Path:
    return tmp / "state/subagents" / pane.replace("%", "_")


def test_start_creates_file_and_stop_removes_it(tmp_path: Path) -> None:
    p = hook(tmp_path, "start", "a1")
    assert (p.returncode, p.stdout, p.stderr) == (0, "", "")
    entry = json.loads((pane_dir(tmp_path) / "a1").read_text())
    assert entry["agent_id"] == "a1" and entry["agent_type"] == "Explore"
    assert abs(entry["started"] - time.time()) < 60 and isinstance(entry["started"], int)
    assert list(pane_dir(tmp_path).iterdir()) == [pane_dir(tmp_path) / "a1"]
    p = hook(tmp_path, "stop", "a1")
    assert (p.returncode, p.stdout) == (0, "")
    assert not (pane_dir(tmp_path) / "a1").exists()


def test_two_agents_and_listing(tmp_path: Path) -> None:
    hook(tmp_path, "start", "a1")
    hook(tmp_path, "start", "a2")
    out = json.loads(cli(tmp_path, "subagents", "--json").stdout)
    assert sorted(e["agent_id"] for e in out) == ["a1", "a2"]
    hook(tmp_path, "stop", "a1")
    out = json.loads(cli(tmp_path, "subagents", "--json").stdout)
    assert [e["agent_id"] for e in out] == ["a2"]
    assert "a2" in cli(tmp_path, "subagents").stdout
    assert json.loads(cli(tmp_path, "subagents", "--pane", "%9", "--json").stdout) == []


def test_stop_for_unknown_id_is_ok(tmp_path: Path) -> None:
    p = hook(tmp_path, "stop", "nope")
    assert (p.returncode, p.stdout, p.stderr) == (0, "", "")


def test_no_pane_id_is_noop(tmp_path: Path) -> None:
    p = hook(tmp_path, "start", "a1", pane=None)
    assert (p.returncode, p.stdout, p.stderr) == (0, "", "")
    assert not (tmp_path / "state").exists()


def test_missing_agent_id_is_noop(tmp_path: Path) -> None:
    p = cli(tmp_path, "subagent-hook", "start", stdin=json.dumps({"session_id": "s"}))
    assert p.returncode == 0 and p.stdout == "" and not (tmp_path / "state").exists()


def test_garbage_stdin_exits_zero_silently(tmp_path: Path) -> None:
    for raw in ("", "not json", "[1,2]", '{"agent_id": 5}'):
        for phase in ("start", "stop"):
            p = cli(tmp_path, "subagent-hook", phase, stdin=raw)
            assert (p.returncode, p.stdout, p.stderr) == (0, "", "")


def test_unwritable_state_exits_zero_silently(tmp_path: Path) -> None:
    (tmp_path / "state").write_text("a file, not a directory")
    p = hook(tmp_path, "start", "a1")
    assert (p.returncode, p.stdout, p.stderr) == (0, "", "")


def test_path_traversal_agent_and_pane_stay_inside(tmp_path: Path) -> None:
    hook(tmp_path, "start", "../x")
    hook(tmp_path, "start", "..")
    root = tmp_path / "state/subagents"
    assert not (tmp_path / "state/x").exists() and not (root / "x").exists()
    files = [p for p in pane_dir(tmp_path).iterdir()]
    assert len(files) == 2 and all(p.resolve().is_relative_to(root.resolve()) for p in files)
    hook(tmp_path, "start", "a", pane="../../evil")
    assert not (tmp_path / "evil").exists()
    assert [p.name for p in root.iterdir() if "evil" in p.name] == [".._.._evil"]


def test_stale_entries_pruned_by_subagents(tmp_path: Path) -> None:
    hook(tmp_path, "start", "fresh")
    d = pane_dir(tmp_path)
    old = {"agent_id": "old", "agent_type": "x", "started": int(time.time()) - 7 * 3600}
    (d / "old").write_text(json.dumps(old))
    (d / "junk").write_text("garbage")
    p = cli(tmp_path, "subagents", "--json")
    assert p.returncode == 0 and [e["agent_id"] for e in json.loads(p.stdout)] == ["fresh"]
    assert sorted(x.name for x in d.iterdir()) == ["fresh"]


def test_subagents_without_pane_exits_zero(tmp_path: Path) -> None:
    p = cli(tmp_path, "subagents", "--json", pane=None)
    assert p.returncode == 0 and json.loads(p.stdout) == []


# --- typed status / wait / purge (in-process, real files under .herdr-test/) ---------------------------------------

TEST_ROOT = Path(REPO) / ".herdr-test" / "subagents"


@pytest.fixture
def state_env() -> object:
    shutil.rmtree(TEST_ROOT, ignore_errors=True)
    TEST_ROOT.mkdir(parents=True)
    yield {"HERDR_ENGINEERING_STATE": str(TEST_ROOT), "HERDR_PANE_ID": "%7"}
    shutil.rmtree(TEST_ROOT, ignore_errors=True)


def _payload(agent_id: str) -> str:
    return json.dumps({"agent_id": agent_id, "agent_type": "Explore"})


def test_status_unknown_for_unseen_pane(state_env) -> None:
    assert reg.status("%7", state_env) == reg.SubagentStatus(known=False, live=0)


def test_status_known_zero_after_stop(state_env) -> None:
    reg.record("start", _payload("a1"), state_env)
    reg.record("stop", _payload("a1"), state_env)
    assert reg.status("%7", state_env) == reg.SubagentStatus(known=True, live=0)


def test_status_counts_live_agents(state_env) -> None:
    reg.record("start", _payload("a1"), state_env)
    reg.record("start", _payload("a2"), state_env)
    assert reg.status("%7", state_env) == reg.SubagentStatus(known=True, live=2)


def test_stale_entry_not_counted(state_env) -> None:
    reg.record("start", _payload("a1"), state_env)
    path = reg._pane_dir(state_env, "%7") / "a1"
    entry = json.loads(path.read_text())
    entry["started"] = int(time.time()) - 7 * 3600
    path.write_text(json.dumps(entry))
    assert reg.status("%7", state_env) == reg.SubagentStatus(known=True, live=0)


def test_wait_until_idle_true_when_last_agent_stops(state_env) -> None:
    reg.record("start", _payload("a1"), state_env)
    timer = threading.Timer(0.3, reg.record, args=("stop", _payload("a1"), state_env))
    timer.start()
    try:
        assert reg.wait_until_idle("%7", state_env, timeout_s=5, poll_s=0.05) is True
    finally:
        timer.join()


def test_wait_until_idle_false_on_timeout(state_env) -> None:
    reg.record("start", _payload("a1"), state_env)
    started = time.monotonic()
    assert reg.wait_until_idle("%7", state_env, timeout_s=0.2, poll_s=0.05) is False
    assert time.monotonic() - started >= 0.2


def test_wait_until_idle_true_immediately_for_unknown_pane(state_env) -> None:
    started = time.monotonic()
    assert reg.wait_until_idle("%7", state_env, timeout_s=30, poll_s=5) is True
    assert time.monotonic() - started < 1


def test_purge_pane_removes_entries_and_returns_count(state_env) -> None:
    reg.record("start", _payload("a1"), state_env)
    reg.record("start", _payload("a2"), state_env)
    assert reg.purge_pane("%7", state_env) == 2
    assert reg.status("%7", state_env) == reg.SubagentStatus(known=False, live=0)
    assert reg.purge_pane("%7", state_env) == 0


def test_reused_pane_id_does_not_see_closed_panes_leftovers(state_env) -> None:
    reg.record("start", _payload("old"), state_env)
    reg.purge_pane("%7", state_env)
    reg.record("start", _payload("new"), state_env)
    assert [e["agent_id"] for e in reg.live_entries(state_env, "%7")] == ["new"]


def test_purge_leaves_other_panes_alone(state_env) -> None:
    reg.record("start", _payload("a1"), state_env)
    reg.record("start", _payload("b1"), {**state_env, "HERDR_PANE_ID": "%8"})
    reg.purge_pane("%7", state_env)
    assert reg.status("%8", state_env) == reg.SubagentStatus(known=True, live=1)


def test_concurrent_hooks_on_one_agent_id_leave_one_file_and_no_exception(state_env) -> None:
    errors: list[BaseException] = []
    barrier = threading.Barrier(8)

    def worker() -> None:
        try:
            barrier.wait()
            for _ in range(50):
                reg.record("start", _payload("same"), state_env)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert [p.name for p in reg._pane_dir(state_env, "%7").iterdir()] == ["same"]


def test_without_pane_id_record_is_a_noop_and_status_unknown(state_env) -> None:
    env = {"HERDR_ENGINEERING_STATE": state_env["HERDR_ENGINEERING_STATE"]}
    reg.record("start", _payload("a1"), env)
    assert list(TEST_ROOT.iterdir()) == []
    assert reg.status("%7", env) == reg.SubagentStatus(known=False, live=0)
    assert reg.status("", env) == reg.SubagentStatus(known=False, live=0)

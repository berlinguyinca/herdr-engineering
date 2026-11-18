"""``herdr-eng handoff subagent-hook`` / ``subagents``: real CLI subprocess, tmp HOME and state."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

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

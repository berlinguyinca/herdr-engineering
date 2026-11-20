"""``herdr-eng handoff subagents --status/--wait/--purge``: real CLI subprocesses, state under ``.herdr-test/``."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO = str(Path(__file__).resolve().parents[1])
STATE = Path(REPO) / ".herdr-test" / "subagents-cli"


@pytest.fixture(autouse=True)
def state_dir() -> Iterator[Path]:
    shutil.rmtree(STATE, ignore_errors=True)
    STATE.mkdir(parents=True)
    yield STATE
    shutil.rmtree(STATE, ignore_errors=True)


def cli(*argv: str, stdin: str = "", pane: str | None = "%1") -> subprocess.CompletedProcess[str]:
    code = "import sys; from herdr_engineering import cli; sys.exit(cli.main(sys.argv[1:]))"
    env = {"PATH": "/usr/bin:/bin", "HOME": str(STATE), "PYTHONPATH": REPO, "HERDR_ENGINEERING_STATE": str(STATE)}
    if pane is not None:
        env["HERDR_PANE_ID"] = pane
    return subprocess.run([sys.executable, "-c", code, "handoff", *argv], cwd=REPO, env=env, input=stdin,
                          capture_output=True, text=True, timeout=60)


def hook(phase: str, agent_id: str, pane: str = "%1") -> None:
    payload = {"session_id": "s", "hook_event_name": "x", "agent_id": agent_id, "agent_type": "Explore"}
    assert cli("subagent-hook", phase, stdin=json.dumps(payload), pane=pane).returncode == 0


def test_cli_status_json_and_wait_exit_codes() -> None:
    assert json.loads(cli("subagents", "--pane", "%1", "--status", "--json").stdout) == {"known": False, "live": 0}
    hook("start", "a1")
    assert json.loads(cli("subagents", "--pane", "%1", "--status", "--json").stdout) == {"known": True, "live": 1}
    timed_out = cli("subagents", "--pane", "%1", "--wait", "0.2", "--json")
    assert timed_out.returncode == 3
    assert json.loads(timed_out.stdout) == {"known": True, "live": 1}
    hook("stop", "a1")
    idle = cli("subagents", "--pane", "%1", "--wait", "2", "--json")
    assert idle.returncode == 0
    assert json.loads(idle.stdout) == {"known": True, "live": 0}


def test_pane_falls_back_to_the_environment() -> None:
    hook("start", "a1", pane="%4")
    out = cli("subagents", "--status", "--json", pane="%4")
    assert json.loads(out.stdout) == {"known": True, "live": 1}


def test_missing_pane_is_an_argparse_error() -> None:
    p = cli("subagents", "--status", pane=None)
    assert p.returncode == 2 and "--pane" in p.stderr


def test_modes_are_mutually_exclusive() -> None:
    for flags in (("--status", "--purge"), ("--status", "--wait", "1"), ("--wait", "1", "--purge")):
        p = cli("subagents", "--pane", "%1", *flags)
        assert p.returncode == 2 and "not allowed with" in p.stderr


def test_purge_removes_entries_and_reports_count() -> None:
    hook("start", "a1")
    hook("start", "a2")
    p = cli("subagents", "--pane", "%1", "--purge", "--json")
    assert (p.returncode, json.loads(p.stdout)) == (0, {"purged": 2})
    assert json.loads(cli("subagents", "--pane", "%1", "--status", "--json").stdout) == {"known": False, "live": 0}
    assert cli("subagents", "--pane", "%1", "--purge").stdout.strip() == "0"


def test_wait_on_unknown_pane_is_idle() -> None:
    assert cli("subagents", "--pane", "%9", "--wait", "5").returncode == 0


def test_plain_listing_is_unchanged() -> None:
    hook("start", "a1")
    assert json.loads(cli("subagents", "--json").stdout)[0]["agent_id"] == "a1"
    assert cli("subagents").stdout.startswith("a1\tExplore\t")

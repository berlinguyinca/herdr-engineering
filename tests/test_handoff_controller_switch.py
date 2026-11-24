"""Per-session auto-handoff switch: real files and a real CLI subprocess."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from herdr_engineering.handoff_controller.switch import is_enabled, set_enabled

REPO = str(Path(__file__).resolve().parents[1])


def test_default_on(tmp_path: Path) -> None:
    assert is_enabled("%1", {}, tmp_path)


def test_env_zero_turns_off(tmp_path: Path) -> None:
    assert not is_enabled("%1", {"HERDR_HANDOFF_AUTO": "0"}, tmp_path)
    assert is_enabled("%1", {"HERDR_HANDOFF_AUTO": "1"}, tmp_path)


def test_pane_marker_turns_off_only_that_pane(tmp_path: Path) -> None:
    set_enabled("%1", False, tmp_path)
    assert not is_enabled("%1", {}, tmp_path)
    assert is_enabled("%2", {}, tmp_path)
    assert (tmp_path / "handoff-controller" / "off" / "_1").exists()


def test_on_removes_marker(tmp_path: Path) -> None:
    set_enabled("%1", False, tmp_path)
    set_enabled("%1", True, tmp_path)
    set_enabled("%1", True, tmp_path)  # idempotent
    assert is_enabled("%1", {}, tmp_path)


def cli(tmp: Path, *argv: str, pane: str | None = None) -> subprocess.CompletedProcess[str]:
    code = "import sys; from herdr_engineering import cli; sys.exit(cli.main(sys.argv[1:]))"
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp), "PYTHONPATH": REPO, "HERDR_ENGINEERING_STATE": str(tmp / "state")}
    if pane is not None:
        env["HERDR_PANE_ID"] = pane
    return subprocess.run([sys.executable, "-c", code, "handoff", "auto", *argv], cwd=REPO, env=env,
                          capture_output=True, text=True, timeout=60)


def test_cli_off_on_roundtrip(tmp_path: Path) -> None:
    state = tmp_path / "state"
    assert cli(tmp_path, "off", "--pane", "%7").returncode == 0
    assert not is_enabled("%7", {}, state)
    assert cli(tmp_path, "on", pane="%7").returncode == 0  # falls back to HERDR_PANE_ID
    assert is_enabled("%7", {}, state)


def test_cli_without_pane_is_an_error(tmp_path: Path) -> None:
    result = cli(tmp_path, "off")
    assert result.returncode == 2
    assert "--pane" in result.stderr


def test_cli_subagents_only_still_rejected_for_off(tmp_path: Path) -> None:
    result = cli(tmp_path, "off", "--pane", "%1", "--subagents-only")
    assert result.returncode == 2

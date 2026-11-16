"""Real child-process acceptance for non-reaping exit observation."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from herdr_engineering import build_metrics


@pytest.mark.parametrize("already_exited", [False, True])
def test_exit_observation_leaves_child_status_and_resources_for_wait4(already_exited: bool) -> None:
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(.05); raise SystemExit(7)"]
    )
    try:
        if already_exited:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                state = subprocess.check_output(
                    ["ps", "-o", "stat=", "-p", str(child.pid)], text=True
                )
                if state.strip().startswith("Z"):
                    break
                time.sleep(0.01)
            else:
                pytest.fail("owned child did not become a zombie")
        build_metrics._wait_without_reaping(child.pid)
        pid, status, usage = os.wait4(child.pid, 0)
        child.returncode = os.waitstatus_to_exitcode(status)
        assert pid == child.pid
        assert child.returncode == 7
        assert usage.ru_maxrss > 0
    finally:
        if child.returncode is None:
            child.kill()
            child.wait(timeout=5)


def test_many_short_measured_children_complete_with_their_real_exit_codes(tmp_path: Path) -> None:
    for index in range(12):
        result = build_metrics.run_measured(["sh", "-c", f"exit {index % 3}"], state_dir=tmp_path)
        assert result.exit_code == index % 3
    events = [json.loads(line) for line in (tmp_path / "builds.jsonl").read_text().splitlines()]
    assert len([event for event in events if event["event"] == "build_finished"]) == 12


@pytest.mark.e2e
def test_recorded_build_cli_reports_the_real_failure_and_releases_slot(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("fabric_build_cache:\n  enabled: false\n")
    environment = dict(
        os.environ,
        HERDR_ENGINEERING_STATE=str(tmp_path),
        HERDR_ENGINEERING_CONFIG=str(config),
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "herdr_engineering.cli",
            "build-coordinate",
            "--record",
            "--slots",
            "1",
            "--max-load",
            "0",
            "--",
            "sh",
            "-c",
            "exit 7",
        ],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 7, result.stderr
    events = [
        json.loads(line)
        for line in (tmp_path / "build-gates/builds.jsonl").read_text().splitlines()
    ]
    finished = [event for event in events if event["event"] == "build_finished"]
    assert len(finished) == 1 and finished[0]["exit_code"] == 7
    from herdr_engineering.build_coord import BuildGate

    assert BuildGate(state_dir=tmp_path / "build-gates", slots=1).status()[0]["held"] is False

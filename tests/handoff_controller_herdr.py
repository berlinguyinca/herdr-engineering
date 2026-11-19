"""A real, isolated, headless Herdr server for the controller tests, with real executables named after the harnesses.

No mocks: the server is the installed `herdr` binary on a private socket and its own HOME; panes run
tests/fixtures/handoff_controller/tui_composer.py installed as `claude`, so Herdr's own agent detection reports it.
The socket path is relative to the working directory because unix socket paths are length-limited (the checkout is deep).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest

from herdr_engineering.handoff_controller.herdr_cli import HerdrCli

SCRIPT = Path(__file__).parent / "fixtures" / "handoff_controller" / "tui_composer.py"
_SOCKET_LIMIT = 100  # sun_path is 104 bytes on macOS, 108 on Linux; the server adds a `-client.sock` sibling


@dataclass(frozen=True)
class IsolatedHerdr:
    cli: HerdrCli
    env: Mapping[str, str]
    root: Path
    submitted: Path  # one line per line the fixture program submitted

    def start(self, *, extra_env: str = "") -> str:
        """Open a workspace running the fixture program as `claude`; the pane id. `extra_env` is `NAME=value ...` for the program."""
        made = subprocess.run(["herdr", "workspace", "create", "--cwd", str(self.root), "--no-focus"], env=self.env,
                              capture_output=True, text=True, timeout=30, check=True)
        pane = json.loads(made.stdout)["result"]["root_pane"]["pane_id"]
        command = f"{extra_env} {self.root / 'bin' / 'claude'}".strip()  # absolute: the user's shell profile may reorder PATH
        subprocess.run(["herdr", "pane", "run", pane, command], env=self.env, capture_output=True, text=True,
                       timeout=30, check=True)
        return pane


def wait_for(until: Callable[[], bool], timeout_s: float, step_s: float = 0.1) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if until():
            return True
        time.sleep(step_s)
    return until()


@contextmanager
def isolated_herdr(topic: str) -> Iterator[IsolatedHerdr]:
    """Start the server; skips when `herdr` is not installed or cannot start. Everything is removed afterwards."""
    herdr = shutil.which("herdr")
    if not herdr:
        pytest.skip("herdr binary not installed; these tests drive a real isolated Herdr server")
    absolute = (Path(".herdr-test") / topic).resolve()  # one directory per test file: CI runs files in parallel
    socket = os.path.relpath(absolute / "t.sock")
    if len(socket) > _SOCKET_LIMIT:
        pytest.skip(f"socket path {socket} is too long for a unix socket; run pytest from the repository root")
    shutil.rmtree(absolute, ignore_errors=True)
    (absolute / "bin").mkdir(parents=True)
    (absolute / "home").mkdir()
    program = absolute / "bin" / "claude"
    shutil.copy(SCRIPT, program)
    program.chmod(0o755)
    env = {k: v for k, v in os.environ.items() if not k.startswith("HERDR_")}
    env.update(HOME=str(absolute / "home"), XDG_CONFIG_HOME=str(absolute / "home" / "cfg"), HERDR_SOCKET_PATH=socket,
               FAKE_TUI_SUBMITTED=str(absolute / "submitted.txt"))
    log = (absolute / "server.log").open("w")
    server = subprocess.Popen([herdr, "server"], env=env, stdout=log, stderr=log, cwd=str(Path.cwd()))
    try:
        if not wait_for(lambda: subprocess.run([herdr, "workspace", "list"], env=env, capture_output=True,
                                               timeout=10).returncode == 0, 15.0, 0.2):
            pytest.skip("could not start an isolated herdr server in this environment")
        yield IsolatedHerdr(HerdrCli(herdr, env), env, absolute, absolute / "submitted.txt")
    finally:
        subprocess.run([herdr, "server", "stop"], env=env, capture_output=True, timeout=10, check=False)
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
        log.close()
        shutil.rmtree(absolute, ignore_errors=True)

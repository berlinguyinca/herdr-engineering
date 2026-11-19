"""A real, isolated, headless Herdr server for the controller tests, with real executables named after the harnesses.

No mocks: the server is the installed `herdr` binary on a private socket and its own HOME; panes run
tests/fixtures/handoff_controller/tui_composer.py installed as `claude`, so Herdr's own agent detection reports it.
Unix socket paths are length-limited and the checkout path is deep, so the server runs with its own directory as working
directory and a relative socket name; `IsolatedHerdr.cli` carries that directory as its `cwd`.
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import signal
import subprocess
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest

from herdr_engineering.handoff_controller.herdr_cli import HerdrCli

SCRIPT = Path(__file__).parent / "fixtures" / "handoff_controller" / "tui_composer.py"
REPO_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class IsolatedHerdr:
    cli: HerdrCli
    env: Mapping[str, str]
    root: Path
    submitted: Path  # one line per line the fixture program submitted
    program: Path

    def _herdr(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["herdr", *args], env=self.env, cwd=self.root, capture_output=True, text=True,
                              timeout=30, check=True)

    def start_delay(self, seconds: float) -> None:
        """Programs started from now on wait `seconds` before drawing (the fixture reads `start_delay_s` beside itself)."""
        self.program.with_name("start_delay_s").write_text(str(seconds))

    def start(self, *, extra_env: str = "") -> str:
        """Open a workspace running the fixture program as `claude`; the pane id. `extra_env` is `NAME=value ...` for the program."""
        made = self._herdr("workspace", "create", "--cwd", str(self.root), "--no-focus")
        pane = json.loads(made.stdout)["result"]["root_pane"]["pane_id"]
        # absolute path: the user's shell profile may reorder PATH
        self._herdr("pane", "run", pane, f"{extra_env} {shlex.quote(str(self.program))}".strip())
        return pane


def wait_for(until: Callable[[], bool], timeout_s: float, step_s: float = 0.1) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if until():
            return True
        time.sleep(step_s)
    return until()


def _our_program_pids(program: Path, env: Mapping[str, str] | None = None) -> list[int]:
    """Processes whose command line runs our own copy of the fixture program (its path is unique to this server);
    none when `pgrep` is not installed."""
    try:
        found = subprocess.run(["pgrep", "-f", str(program)], capture_output=True, text=True, check=False, env=env)
    except FileNotFoundError:
        return []
    return [int(pid) for pid in found.stdout.split() if int(pid) != os.getpid()]


def _teardown(herdr: str, env: Mapping[str, str], root: Path, server: subprocess.Popen[bytes], program: Path) -> None:
    subprocess.run([herdr, "server", "stop"], env=env, cwd=root, capture_output=True, timeout=10, check=False)
    try:
        server.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass
    if server.poll() is None:  # a dead server's group id may already belong to someone else
        try:
            os.killpg(server.pid, signal.SIGKILL)  # the group this fixture started, nothing else
        except (ProcessLookupError, PermissionError):
            pass
    for pid in _our_program_pids(program):  # panes run in sessions of their own
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    wait_for(lambda: not _our_program_pids(program), 5.0)
    shutil.rmtree(root, ignore_errors=True)


@contextmanager
def isolated_herdr(topic: str) -> Iterator[IsolatedHerdr]:
    """Start the server; skips when `herdr` is not installed or cannot start. Everything is removed afterwards."""
    herdr = shutil.which("herdr")
    if not herdr:
        pytest.skip("herdr binary not installed; these tests drive a real isolated Herdr server")
    worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
    root = REPO_ROOT / ".herdr-test" / f"{topic}-{worker}-{os.getpid()}"
    try:
        (root / "bin").mkdir(parents=True)
        (root / "home").mkdir()
    except OSError as error:
        pytest.skip(f"cannot create {root}: {error}")
    program = root / "bin" / "claude"
    shutil.copy(SCRIPT, program)
    program.chmod(0o755)
    env = {k: v for k, v in os.environ.items() if not k.startswith("HERDR_")}
    env.update(HOME=str(root / "home"), XDG_CONFIG_HOME=str(root / "home" / "cfg"), HERDR_SOCKET_PATH="s.sock",
               FAKE_TUI_SUBMITTED=str(root / "submitted.txt"))
    log = (root / "server.log").open("w")
    server = subprocess.Popen([herdr, "server"], env=env, stdout=log, stderr=log, cwd=root, start_new_session=True)
    try:
        if not wait_for(lambda: subprocess.run([herdr, "workspace", "list"], env=env, cwd=root, capture_output=True,
                                               timeout=10).returncode == 0, 15.0, 0.2):
            pytest.skip("could not start an isolated herdr server in this environment")
        yield IsolatedHerdr(HerdrCli(herdr, env, root), env, root, root / "submitted.txt", program)
    finally:
        _teardown(herdr, env, root, server, program)
        log.close()

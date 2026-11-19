"""The controller's view of `herdr-handoff`: one subprocess per call, never raises."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .proc import run_group


@dataclass(frozen=True)
class HandoffCliResult:
    returncode: int  # 124 timed out, 127 could not be started
    stdout: str
    stderr: str


class HandoffCli:
    """`argv_prefix` is the command that starts the CLI (default the installed `herdr-handoff`); `env` replaces
    the environment (the HERDR_HANDOFF_STORE of another store, a moved HOME)."""

    def __init__(self, argv_prefix: Sequence[str] = ("herdr-handoff",), env: Mapping[str, str] | None = None) -> None:
        self._prefix = tuple(argv_prefix)
        self._env = dict(env) if env is not None else None

    def run(self, args: Sequence[str], *, timeout_s: float, input_text: str | None = None) -> HandoffCliResult:
        done = run_group([*self._prefix, *args], timeout_s, env=self._env, input_text=input_text)
        return HandoffCliResult(done.returncode, done.stdout, done.stderr)

"""Compatibility gate for the standalone herdr-handoff package and skills."""
from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

HARNESS_DIRS = ("codex", "claude", "pi", "opencode")

# The flows `herdr-eng` drives through the CLI.  They are checked by name rather than inferred from the
# binary existing: takeover and the successor's confirm arrived in herdr-handoff v0.3.0, and a host pinned
# to v0.2.0 has a CLI that satisfies an existence check and cannot honour a takeover close.
CLI_COMMANDS = ("handoff", "receive", "takeover", "confirm")


@dataclass(frozen=True)
class HandoffDependencyResult:
    status: str
    detail: str
    missing_harnesses: list[str] = field(default_factory=list)
    executable: str | None = None
    version: str = ""
    capabilities: dict[str, bool] = field(default_factory=dict)


def _advertises(help_text: str, command: str) -> bool:
    """Whether `--help` really lists the command, not merely mentions the word somewhere."""
    return re.search(rf"(?:^|\s){re.escape(command)}(?:\s|$)", help_text, re.MULTILINE) is not None


def _probe_cli(path: str) -> tuple[str, dict[str, bool]]:
    """Ask the installed CLI what version it is and which commands it offers.

    A probe that cannot run reports nothing (`({},)`) rather than assuming a capability; the caller turns
    that into a gap.  Neither `--version` nor `--help` changes state.
    """
    try:
        shown = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=15)
        help_out = subprocess.run([path, "--help"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return "", {}
    lines = shown.stdout.strip().splitlines()
    return (lines[0] if lines else ""), {
        command: _advertises(help_out.stdout + help_out.stderr, command) for command in CLI_COMMANDS}


def check_handoff_dependency(*, skill_root: Path | None = None,
                             executable: str | None = "auto",
                             required_commands: Sequence[str] = CLI_COMMANDS
                             ) -> HandoffDependencyResult:
    root = Path(skill_root or Path.home() / ".codex" / "skills" / ".." / "..").expanduser()
    if skill_root is None:
        paths = {
            "codex": Path.home() / ".codex" / "skills" / "herdr-handoff",
            "claude": Path.home() / ".claude" / "skills" / "herdr-handoff",
            "pi": Path.home() / ".pi" / "agent" / "skills" / "herdr-handoff",
            "opencode": Path.home() / ".config" / "opencode" / "skills" / "herdr-handoff",
        }
    else:
        paths = {name: root / name / "herdr-handoff" for name in HARNESS_DIRS}
    missing = [f"{name}:{skill}" for name, path in paths.items()
               for skill in ("herdr", "herdr-handoff", "herdr-artifact")
               if not (path.parent / skill / "SKILL.md").is_file()]
    resolved = shutil.which("herdr-handoff") if executable == "auto" else executable
    if missing or not resolved:
        missing_text = f"missing skills: {', '.join(missing)}" if missing else "all Herdr skills present"
        cli_text = "herdr-handoff CLI missing" if not resolved else f"CLI: {resolved}"
        return HandoffDependencyResult("fail", f"{missing_text}; {cli_text}", missing, resolved)

    version, capabilities = _probe_cli(resolved)
    problems: list[str] = []
    if not capabilities:
        problems.append(f"CLI at {resolved} could not be run to check its commands")
    else:
        absent = [command for command in required_commands if not capabilities.get(command)]
        if absent:
            problems.append(f"CLI: {resolved} ({version or 'version unknown'}) does not provide "
                            + ", ".join(absent)
                            + " - refresh the pinned dependency with scripts/install-handoff.sh")
    if problems:
        return HandoffDependencyResult("fail", "; ".join(problems), [], resolved, version, capabilities)
    return HandoffDependencyResult(
        "ok", f"all Herdr skills present; CLI: {resolved}" + (f" ({version})" if version else ""),
        [], resolved, version, capabilities)

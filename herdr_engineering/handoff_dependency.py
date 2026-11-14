"""Compatibility gate for the standalone herdr-handoff package and skills."""
from __future__ import annotations

import re
import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

HARNESS_DIRS = ("codex", "claude", "pi", "opencode")

# The flows `herdr-eng` drives through the CLI, taken from what it actually invokes
# (`herdr-engineering/skill_data/handoff/SKILL.md`: create, then launch; plus takeover and the
# successor's confirm).  They are checked by name rather than inferred from the binary existing:
# takeover and confirm arrived in herdr-handoff v0.3.0, and a host pinned to v0.2.0 has a CLI that
# satisfies an existence check and cannot honour a takeover close.
CLI_COMMANDS = ("create", "launch", "takeover", "confirm")


@dataclass(frozen=True)
class HandoffDependencyResult:
    status: str
    detail: str
    missing_harnesses: list[str] = field(default_factory=list)
    executable: str | None = None
    version: str = ""
    capabilities: dict[str, bool] = field(default_factory=dict)


def _advertises(help_text: str, command: str) -> bool:
    """Whether the CLI's own help really offers this command.

    argparse lists subcommands as one brace group on the usage line
    (`{create,inspect,...,takeover,confirm}`), so matching "word at start of line" reads the newest CLI
    as missing the commands it just gained.  A brace group elsewhere - an option rendered
    `--mode {fast,slow}` - is not a list of subcommands, and reading every group as one made a CLI that
    spells its commands out in prose look like it lacked them.
    """
    groups = re.findall(r"\{([A-Za-z0-9][A-Za-z0-9,_\- ]*)\}", help_text)
    if any(command in [choice.strip() for choice in group.split(",")] for group in groups):
        return True
    # A *closed* subcommand set is rendered as a group followed by `...`.  When the CLI declares one and
    # the command is not in it, nothing else counts: `confirm` appearing in the description of another
    # command is exactly the false confidence this check exists to remove.  Without such a set, the
    # word-boundary search (which ignores `herdr-handoff` and `--flags`) reads the rest of the help.
    if re.search(r"\{[A-Za-z0-9][A-Za-z0-9,_\- ]*\}\s*\.\.\.\s", help_text):
        return False
    return re.search(rf"(?<![\w-]){re.escape(command)}(?![\w-])", help_text) is not None


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
    version, capabilities = _probe_cli(resolved) if resolved else ("", {})
    if missing or not resolved:
        # Report the version even here: an operator fixing missing skills needs to know which CLI the
        # skills are supposed to match.
        missing_text = f"missing skills: {', '.join(missing)}" if missing else "all Herdr skills present"
        cli_text = ("herdr-handoff CLI missing" if not resolved
                    else f"CLI: {resolved}" + (f" ({version})" if version else ""))
        return HandoffDependencyResult("fail", f"{missing_text}; {cli_text}", missing, resolved,
                                       version, capabilities)
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

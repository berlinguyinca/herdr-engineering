"""Compatibility gate for the standalone herdr-handoff package and skills."""
from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

HARNESS_DIRS = ("codex", "claude", "pi", "opencode")


@dataclass(frozen=True)
class HandoffDependencyResult:
    status: str
    detail: str
    missing_harnesses: list[str] = field(default_factory=list)
    executable: str | None = None


def check_handoff_dependency(*, skill_root: Path | None = None,
                             executable: str | None = "auto") -> HandoffDependencyResult:
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
    return HandoffDependencyResult("ok", f"all Herdr skills present; CLI: {resolved}", [], resolved)

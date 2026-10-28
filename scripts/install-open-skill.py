#!/usr/bin/env python3
"""Link the canonical HerdR viewing skill into supported harness skill directories."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

TARGETS = {
    "claude": ".claude/skills/herdr-open",
    "codex": ".codex/skills/herdr-open",
    "pi": ".pi/agent/skills/herdr-open",
    "opencode": ".config/opencode/skills/herdr-open",
}
INSTRUCTIONS = {
    "claude": ".claude/CLAUDE.md",
    "codex": ".codex/AGENTS.md",
    "pi": ".pi/agent/AGENTS.md",
    "opencode": ".config/opencode/AGENTS.md",
}
START = "<!-- HERDR:OPEN:START -->"
END = "<!-- HERDR:OPEN:END -->"


def activation(home: Path, harness: str) -> tuple[Path, str, str]:
    """Prepare one managed instruction block, retaining surrounding user text."""
    path = home / INSTRUCTIONS[harness]
    if harness in {"pi", "codex"}:
        candidates = ["AGENTS.override.md", "AGENTS.md"]
        if harness == "pi":
            candidates += ["AGENTS.MD", "CLAUDE.md", "CLAUDE.MD"]
        path = next((path.parent / name for name in candidates
                     if (path.parent / name).is_file()), path)
    original = path.read_text(encoding="utf-8") if path.exists() else ""
    skill = home / TARGETS[harness] / "SKILL.md"
    launcher = skill.parent / "scripts/open-client.py"
    block = f"""{START}
## HerdR generated-file viewing

At the start of each task that may create or update pictures or HTML, and when
explicitly asked to open a file or website, read {json.dumps(str(skill))} and follow it automatically throughout that task.
Use the bundled launcher {json.dumps(str(launcher))} with python3; it runs the
maintained HerdR implementation even when the global herdr-eng command is older.
Before generating output, take the task's baseline with `begin`. At task completion,
run `discover` and offer the relevant generated files: open all, select files, or
skip. Wait for the person's choice before transfer or viewer launch. Retain the
task state through continuations and avoid duplicate offers. Resolve the person's
actual device automatically with the launcher; never ask which machine, SSH
account, operating system, or viewer they use. Never assume the execution host
is the viewing device. If discovery fails, report it briefly without a setup
interview. Never automatically offer Markdown files: open Markdown only when
the person explicitly asks to open that file, using the launcher's `file` command
without a second permission question. Run `dismiss` when they skip.
{END}"""
    if START in original or END in original:
        if original.count(START) != 1 or original.count(END) != 1:
            raise ValueError(f"Malformed HerdR activation block in {path}")
        start, end = original.index(START), original.index(END)
        if end < start:
            raise ValueError(f"Malformed HerdR activation block in {path}")
        updated = original[:start] + block + original[end + len(END):]
    else:
        prefix = ""
        # OpenCode otherwise inherits Claude's global rules. Creating a new
        # AGENTS.md must retain that context instead of silently shadowing it.
        fallback = home / INSTRUCTIONS["claude"]
        if harness == "opencode" and not path.exists() and fallback.is_file():
            prefix = f"Read {json.dumps(str(fallback))} for the existing global instructions.\n\n"
        updated = original + ("\n\n" if original else "") + prefix + block + "\n"
    return path, original, updated


def install(home: Path, source: Path, harnesses: list[str], *, dry_run: bool = False,
            activate: bool = False) -> dict:
    """Preflight every requested path, preserving existing skills on any conflict."""
    source = source.expanduser().resolve()
    home = home.expanduser().absolute()
    result = {"ok": False, "source": str(source), "harnesses": {}}
    if not (source / "SKILL.md").is_file():
        result["error"] = "Source must contain SKILL.md"
        return result

    for harness in dict.fromkeys(harnesses):
        target = home / TARGETS[harness]
        if target.is_symlink() and target.resolve() == source:
            status = "unchanged"
        elif target.exists() or target.is_symlink():
            status = "conflict"
        else:
            status = "would-install"
        result["harnesses"][harness] = {"path": str(target), "status": status}
    if any(row["status"] == "conflict" for row in result["harnesses"].values()):
        result["error"] = "Existing skill paths were preserved; no links were installed"
        return result

    updates = []
    if activate:
        result["activation"] = {}
        try:
            for harness in result["harnesses"]:
                path, original, updated = activation(home, harness)
                updates.append((harness, path, original, updated))
                result["activation"][harness] = {
                    "path": str(path), "mode": "instructions",
                    "status": "unchanged" if original == updated else "would-update",
                }
        except (OSError, UnicodeError, ValueError) as exc:
            result["error"] = str(exc)
            return result

    if not dry_run:
        for row in result["harnesses"].values():
            if row["status"] == "unchanged":
                continue
            target = Path(row["path"])
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                # symlink_to fails if a competing installer creates this path;
                # it never removes or replaces a user-owned entry.
                target.symlink_to(source, target_is_directory=True)
            except OSError as exc:
                row["status"] = "error"
                result["error"] = f"Could not install {target}: {exc}"
                return result
            row["status"] = "installed"
        for harness, path, original, updated in updates:
            if original == updated:
                continue
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                # Writing through existing symlinks keeps shared instructions
                # shared; replacing the path would break that user's setup.
                path.write_text(updated, encoding="utf-8")
            except OSError as exc:
                result["activation"][harness]["status"] = "error"
                result["error"] = f"Could not activate {path}: {exc}"
                return result
            result["activation"][harness]["status"] = "updated"
    result["ok"] = True
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, default=Path.home(), help="Harness user's home")
    parser.add_argument(
        "--source", type=Path,
        default=Path(__file__).resolve().parents[1] / "skills" / "herdr-open",
        help="Canonical skill directory (keep this checkout available)",
    )
    parser.add_argument("--harness", action="append", choices=tuple(TARGETS))
    parser.add_argument("--dry-run", action="store_true", help="Inspect without writing files")
    parser.add_argument("--activate", action="store_true",
                        help="Enable automatic task lifecycle through managed global instructions")
    args = parser.parse_args(argv)
    result = install(args.home, args.source, args.harness or list(TARGETS),
                     dry_run=args.dry_run, activate=args.activate)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

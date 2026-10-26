"""Harness-neutral discovery of local Agent Skills manifests."""
from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path

SKILL_ID = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")


def _has_description(text: str) -> bool:
    if not text.startswith("---"):
        return False
    end = re.search(r"(?m)^---\s*$", text[3:])
    return bool(end and re.search(r"(?m)^description:\s*\S", text[3:3 + end.start()]))


def discover_skills(roots: Iterable[str | Path], *, max_skills: int = 100) -> list[str]:
    """Return stable ``skill:<id>`` names for valid directory-based SKILL.md manifests."""
    if not isinstance(max_skills, int) or isinstance(max_skills, bool) or max_skills < 0:
        raise ValueError("max_skills must be a non-negative integer")
    found: set[str] = set()
    for raw_root in roots:
        root = Path(raw_root).expanduser()
        try:
            manifests = sorted(
                [p for p in root.rglob("SKILL.md") if len(p.relative_to(root).parts) <= 6]
                + list(root.glob("*.md")),
                key=lambda p: str(p),
            )
        except OSError:
            continue
        for manifest in manifests:
            rel = manifest.relative_to(root)
            if any(part.startswith(".") or part == "node_modules" for part in rel.parts):
                continue
            skill_id = manifest.parent.name if manifest.name == "SKILL.md" else manifest.stem
            if not SKILL_ID.fullmatch(skill_id):
                continue
            try:
                text = manifest.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                continue
            if not _has_description(text):
                continue
            found.add("skill:" + skill_id)
            if len(found) >= max_skills:
                return sorted(found)[:max_skills]
    return sorted(found)[:max_skills]

"""Harness-neutral discovery of local Agent Skills manifests."""
from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path

SKILL_ID = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")


def discover_skills(roots: Iterable[str | Path], *, max_skills: int = 100) -> list[str]:
    """Return stable ``skill:<id>`` names for valid directory-based SKILL.md manifests."""
    if not isinstance(max_skills, int) or isinstance(max_skills, bool) or max_skills < 0:
        raise ValueError("max_skills must be a non-negative integer")
    found: set[str] = set()
    for raw_root in roots:
        root = Path(raw_root).expanduser()
        try:
            entries = sorted(root.iterdir(), key=lambda p: p.name)
        except OSError:
            continue
        for entry in entries:
            if entry.name.startswith(".") or not entry.is_dir() or not SKILL_ID.fullmatch(entry.name):
                continue
            manifest = entry / "SKILL.md"
            try:
                text = manifest.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                continue
            if not re.search(r"(?m)^---\s*$", text) or not re.search(r"(?m)^description:\s*\S", text):
                continue
            found.add("skill:" + entry.name)
            if len(found) >= max_skills:
                return sorted(found)[:max_skills]
    return sorted(found)[:max_skills]

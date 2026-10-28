"""Install, upgrade, verify and remove the ``summary`` skill (``/summary``).

One canonical skill (``skills/summary``) is installed once per scope and
linked into each agent's skill directory; Pi and OpenCode additionally get a
tiny command template so ``/summary`` is a real slash command. Codex is
reached through ``$summary`` (or ``/skills``); no Codex command file is
written because a bare custom ``/summary`` is not a documented Codex feature.

Ownership follows the herdr-handoff precedent: never overwrite what we did not
write. Every file we create is recorded in a manifest with its sha256, so an
upgrade rewrites a file only while it still matches what we last wrote, and
uninstall removes only unmodified files and links that point at our copy.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

VERSION = 1
SOURCE = Path(__file__).resolve().parents[1] / "skills" / "summary"
MANIFEST_NAME = ".summary-manifest.json"


@dataclass
class Result:
    target: str
    action: str  # installed | upgraded | unchanged | preserved | removed | missing | stale | ok
    detail: str = ""

    def to_dict(self) -> dict:
        return {"target": self.target, "action": self.action, "detail": self.detail}


@dataclass
class Layout:
    scope: str
    canonical: Path
    manifest: Path
    skill_links: dict[str, Path]  # agent -> link path
    pi_prompt: Path
    opencode_command: Path
    results: list[Result] = field(default_factory=list)


def layout(*, scope: str = "global", home: Path | None = None, project: Path | None = None) -> Layout:
    if scope == "global":
        h = Path(home or Path.home())
        canonical = h / ".local/share/herdr-engineering/skills/summary"
        oc_dir = "command" if (h / ".config/opencode/command").is_dir() else "commands"
        return Layout(
            scope, canonical, canonical.parent / MANIFEST_NAME,
            {"claude": h / ".claude/skills/summary", "codex": h / ".codex/skills/summary",
             "pi": h / ".pi/agent/skills/summary", "opencode": h / ".config/opencode/skills/summary"},
            h / ".pi/agent/prompts/summary.md", h / ".config/opencode" / oc_dir / "summary.md")
    if scope == "project":
        p = Path(project or Path.cwd())
        canonical = p / ".agents/skills/summary"
        oc_dir = "command" if (p / ".opencode/command").is_dir() else "commands"
        return Layout(
            scope, canonical, canonical.parent / MANIFEST_NAME,
            {"claude": p / ".claude/skills/summary"},  # Codex, Pi, OpenCode read .agents/skills directly
            p / ".pi/prompts/summary.md", p / ".opencode" / oc_dir / "summary.md")
    raise ValueError(f"unknown scope: {scope}")


def _sha(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _load(lay: Layout) -> dict:
    try:
        data = json.loads(lay.manifest.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _sources() -> dict[str, bytes]:
    return {"skill": (SOURCE / "SKILL.md").read_bytes(),
            "pi": (SOURCE / "adapters/pi-summary.md").read_bytes(),
            "opencode": (SOURCE / "adapters/opencode-summary.md").read_bytes()}


def _place(lay: Layout, files: dict, path: Path, data: bytes, dry: bool) -> Result:
    """Write ``data`` at ``path`` only if absent or still exactly what we last wrote."""
    key = str(path)
    new = _sha_bytes(data)
    current = _sha(path)
    if current is None and not path.is_symlink():
        if not dry:
            _atomic_write(path, data)
            files[key] = new
        return Result(key, "installed")
    if current == new:
        if not dry:
            files[key] = new
        return Result(key, "unchanged")
    if files.get(key) == current:
        if not dry:
            _atomic_write(path, data)
            files[key] = new
        return Result(key, "upgraded")
    return Result(key, "preserved", "exists and was not written by this installer (customized or foreign); left untouched")


def _link(lay: Layout, links: dict, link: Path, dry: bool) -> Result:
    key = str(link)
    if link.is_symlink():
        if os.path.realpath(link) == os.path.realpath(lay.canonical):
            links[key] = str(lay.canonical)
            return Result(key, "unchanged")
        return Result(key, "preserved", "a different link exists here; left untouched")
    if link.exists():
        return Result(key, "preserved", "a user skill named summary exists here; left untouched")
    if not dry:
        link.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(os.path.relpath(lay.canonical, link.parent) if lay.scope == "project" else lay.canonical, link)
        links[key] = str(lay.canonical)
    return Result(key, "installed")


def install(*, scope: str = "global", home: Path | None = None, project: Path | None = None,
            dry_run: bool = False) -> list[Result]:
    lay = layout(scope=scope, home=home, project=project)
    src = _sources()
    manifest = _load(lay)
    files: dict[str, str] = dict(manifest.get("files", {}))
    links: dict[str, str] = dict(manifest.get("links", {}))
    out = [_place(lay, files, lay.canonical / "SKILL.md", src["skill"], dry_run)]
    out += [_link(lay, links, p, dry_run) for p in lay.skill_links.values()]
    out.append(_place(lay, files, lay.pi_prompt, src["pi"], dry_run))
    out.append(_place(lay, files, lay.opencode_command, src["opencode"], dry_run))
    if not dry_run:
        _atomic_write(lay.manifest, json.dumps(
            {"name": "summary", "version": VERSION, "skill_sha256": _sha_bytes(src["skill"]),
             "files": files, "links": links}, indent=2, sort_keys=True).encode())
    return out


def uninstall(*, scope: str = "global", home: Path | None = None, project: Path | None = None,
              dry_run: bool = False) -> list[Result]:
    lay = layout(scope=scope, home=home, project=project)
    manifest = _load(lay)
    files: dict[str, str] = manifest.get("files", {})
    out: list[Result] = []
    for key, link in ((str(p), p) for p in lay.skill_links.values()):
        if link.is_symlink() and os.path.realpath(link) == os.path.realpath(lay.canonical):
            if not dry_run:
                link.unlink()
            out.append(Result(key, "removed"))
        elif link.is_symlink() or link.exists():
            out.append(Result(key, "preserved", "not our link; left untouched"))
    for key, recorded in files.items():
        path = Path(key)
        if _sha(path) is None:
            out.append(Result(key, "missing"))
        elif _sha(path) == recorded:
            if not dry_run:
                path.unlink()
            out.append(Result(key, "removed"))
        else:
            out.append(Result(key, "preserved", "modified since install; left untouched"))
    if not dry_run:
        left = lay.canonical.exists() and any(lay.canonical.iterdir())
        if lay.canonical.exists() and not left:
            lay.canonical.rmdir()
        if not (lay.canonical.exists()):
            lay.manifest.unlink(missing_ok=True)
    return out


def verify(*, scope: str = "global", home: Path | None = None, project: Path | None = None) -> tuple[bool, list[Result]]:
    """Read-only check. ``ok`` is False when the skill is missing or invalid; stale/customized files are reported."""
    lay = layout(scope=scope, home=home, project=project)
    src = _sources()
    manifest = _load(lay)
    out: list[Result] = []
    skill = lay.canonical / "SKILL.md"
    body = skill.read_text(errors="replace") if skill.is_file() else ""
    valid = body.startswith("---") and "\nname: summary\n" in body.split("\n---", 1)[0] + "\n"
    ok = skill.is_file() and valid
    if not skill.is_file():
        out.append(Result(str(skill), "missing", "run: herdr-eng summary install"))
    elif not valid:
        out.append(Result(str(skill), "stale", "SKILL.md frontmatter does not declare name: summary"))
    elif _sha(skill) == _sha_bytes(src["skill"]):
        out.append(Result(str(skill), "ok", f"matches canonical source (version {manifest.get('version', '?')})"))
    elif manifest.get("files", {}).get(str(skill)) == _sha(skill):
        out.append(Result(str(skill), "stale", "older managed copy; run: herdr-eng summary install"))
    else:
        out.append(Result(str(skill), "preserved", "customized copy; not managed"))
    for agent, link in lay.skill_links.items():
        good = link.is_symlink() and os.path.realpath(link) == os.path.realpath(lay.canonical)
        if good:
            out.append(Result(str(link), "ok", agent))
        elif link.exists():
            out.append(Result(str(link), "preserved", f"{agent}: user-owned summary skill"))
        else:
            out.append(Result(str(link), "missing", agent))
            ok = False
    for path, want in ((lay.pi_prompt, src["pi"]), (lay.opencode_command, src["opencode"])):
        cur = _sha(path)
        if cur is None:
            out.append(Result(str(path), "missing"))
            ok = False
        elif cur == _sha_bytes(want):
            out.append(Result(str(path), "ok"))
        elif manifest.get("files", {}).get(str(path)) == cur:
            out.append(Result(str(path), "stale", "older managed copy; run: herdr-eng summary install"))
        else:
            out.append(Result(str(path), "preserved", "customized command; not managed"))
    return ok, out

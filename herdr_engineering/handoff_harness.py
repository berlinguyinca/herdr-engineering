"""Which agent harness is running this command? Used by the ``/handoff`` skill to pick a default target.

The answer must be reliable, so it is never guessed from "the newest session in this directory".
Evidence, strongest first:

1. an explicit ``--harness`` value,
2. the nearest ancestor process that is one of the four harnesses (real process table),
3. the Herdr pane's reported agent (``herdr pane get $HERDR_PANE_ID``),
4. harness marker variables in the environment (exactly one harness may match).

Anything else raises :class:`Undetectable`; the skill then asks the user to name the target.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

HARNESSES = ("claude", "pi", "codex", "opencode")

# Marker variables each harness exports into the tools it runs. Presence-only checks.
# UNVERIFIED across versions: claude (CLAUDECODE) and opencode (OPENCODE) are widely observed;
# pi and codex markers are best-effort, which is why ancestry and the pane agent come first.
ENV_MARKERS: dict[str, tuple[str, ...]] = {
    "claude": ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"),
    "pi": ("PI_CODING_AGENT",),
    "codex": ("CODEX_THREAD_ID", "CODEX_SANDBOX", "CODEX_MANAGED_BY_NPM"),
    "opencode": ("OPENCODE", "OPENCODE_PID"),
}


class Undetectable(Exception):
    """The current harness could not be established from reliable evidence."""


@dataclass(frozen=True)
class Detection:
    harness: str
    method: str  # explicit | process-ancestry | herdr-pane | environment
    evidence: str

    def to_dict(self) -> dict:
        return {"ok": True, "harness": self.harness, "method": self.method, "evidence": self.evidence}


def _stat_fields(proc: Path, pid: int) -> tuple[str, int] | None:
    try:
        text = (proc / str(pid) / "stat").read_text()
        comm = text[text.index("(") + 1:text.rindex(")")]
        return comm, int(text[text.rindex(")") + 1:].split()[1])
    except (OSError, ValueError, IndexError):
        return None


def _names(proc: Path, pid: int, comm: str) -> list[str]:
    """comm plus the basenames of the first two argv words (``node /x/bin/pi`` runs as node)."""
    names = [comm]
    try:
        words = (proc / str(pid) / "cmdline").read_bytes().split(b"\0")[:2]
        names += [os.path.basename(w.decode(errors="replace")) for w in words if w]
    except OSError:
        pass
    return names


def _from_ancestry(proc: Path, start: int) -> Detection | None:
    pid, hops = start, 0
    while pid > 1 and hops < 64:
        fields = _stat_fields(proc, pid)
        if fields is None:
            return None
        comm, ppid = fields
        for name in _names(proc, pid, comm):
            for harness in HARNESSES:
                if name == harness or name.startswith(harness + "-") and name[len(harness) + 1:].replace(".", "").isdigit():
                    return Detection(harness, "process-ancestry", f"ancestor process {name} (pid {pid})")
        pid, hops = ppid, hops + 1
    return None


def _from_pane(env: dict[str, str], herdr_bin: str | None) -> Detection | None:
    pane, exe = env.get("HERDR_PANE_ID"), herdr_bin or shutil.which("herdr")
    if not pane or not exe:
        return None
    try:
        out = subprocess.run([exe, "pane", "get", pane], capture_output=True, text=True, timeout=10, env=env)
        agent = json.loads(out.stdout)["result"]["pane"].get("agent")
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError, AttributeError):
        return None
    return Detection(agent, "herdr-pane", f"herdr reports agent {agent} for pane {pane}") if agent in HARNESSES else None


def _from_env(env: dict[str, str]) -> Detection | None:
    hits = {h: [v for v in names if env.get(v)] for h, names in ENV_MARKERS.items()}
    hits = {h: v for h, v in hits.items() if v}
    if len(hits) > 1:
        raise Undetectable("environment markers disagree (" + ", ".join(f"{h}: {','.join(v)}" for h, v in hits.items())
                           + "); name the target explicitly")
    if hits:
        harness, names = next(iter(hits.items()))
        return Detection(harness, "environment", f"environment variable {names[0]} is set")
    return None


def detect(*, explicit: str | None = None, env: dict[str, str] | None = None, proc: Path = Path("/proc"),
           pid: int | None = None, herdr_bin: str | None = None) -> Detection:
    if explicit is not None:
        if explicit not in HARNESSES:
            raise Undetectable(f"unknown harness {explicit!r}; use one of {', '.join(HARNESSES)}")
        return Detection(explicit, "explicit", "named by the caller")
    env = dict(os.environ if env is None else env)
    for found in (lambda: _from_ancestry(proc, pid or os.getpid()), lambda: _from_pane(env, herdr_bin),
                  lambda: _from_env(env)):
        result = found()
        if result:
            return result
    raise Undetectable("could not tell which harness is running this session (no harness ancestor process, "
                       "Herdr pane agent or marker variable); ask the user to name one of: " + ", ".join(HARNESSES))

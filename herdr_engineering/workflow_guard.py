"""Pure decision functions for the managed ``PreToolUse`` guard hook.

``decide(payload)`` returns ``None`` (no opinion) or ``(decision, reason)`` where decision is
``"deny"`` or ``"ask"``.  It never touches the network and fails open: any unexpected input or
internal error yields ``None``.  The only side effect is a short-timeout local ``git`` call to
read the current branch / ``origin`` URL.
"""
from __future__ import annotations

import os
import re
import shlex
import subprocess

MATCHER = "Bash|Write|Edit|MultiEdit|NotebookEdit"
_WRITE_TOOLS = {"Write": "file_path", "Edit": "file_path", "MultiEdit": "file_path",
                "NotebookEdit": "notebook_path"}
_PROTECTED = {"main", "master"}
_SEPARATORS = {"&&", "||", ";", "|", "&", "\n"}
_SCRATCHPAD = re.compile(r"^/(?:private/)?tmp/claude-\d+(?:/|$)")
_GIT_TIMEOUT_S = 1.0

Decision = tuple[str, str]

TMP_REASON = ("Writing scratch files under /tmp is not allowed. Use a gitignored project directory "
              "such as .<project>/<topic>/ instead (the Claude Code harness scratchpad under "
              "/tmp/claude-<id>/ is the only exception).")
MAIN_REASON = "Pushing to main/master is blocked. Push a feature branch and open a PR instead."
HERDR_REASON = "pushing herdr-engineering needs explicit user approval"


def _norm(path: str, cwd: str) -> str:
    p = path if os.path.isabs(path) else os.path.join(cwd or os.getcwd(), path)
    return os.path.normpath(p)


def check_write_path(path: str, cwd: str) -> Decision | None:
    full = _norm(path, cwd)
    if full == "/private/tmp" or full.startswith("/private/tmp/"):
        full = full[len("/private"):]
    if full != "/tmp" and not full.startswith("/tmp/"):
        return None
    if _SCRATCHPAD.match(full):
        return None
    return "deny", TMP_REASON


def _split_commands(command: str) -> list[list[str]]:
    lex = shlex.shlex(command, posix=True, punctuation_chars=";&|")
    lex.whitespace_split = True
    lex.commenters = ""
    cmds: list[list[str]] = [[]]
    for tok in lex:
        if tok in _SEPARATORS or (tok and set(tok) <= set(";&|")):
            cmds.append([])
        else:
            cmds[-1].append(tok)
    return [c for c in cmds if c]


def _parse_push(argv: list[str]) -> tuple[str | None, list[str]] | None:
    """For a ``git ... push ...`` argv return (-C dir, positional args after push) else None."""
    if not argv or os.path.basename(argv[0]) != "git":
        return None
    i, workdir = 1, None
    while i < len(argv):
        a = argv[i]
        if a == "-C" and i + 1 < len(argv):
            workdir = argv[i + 1] if workdir is None or os.path.isabs(argv[i + 1]) \
                else os.path.join(workdir, argv[i + 1])
            i += 2
        elif a in ("-c", "--git-dir", "--work-tree", "--namespace") and i + 1 < len(argv):
            i += 2
        elif a.startswith("-"):
            i += 1
        else:
            break
    if i >= len(argv) or argv[i] != "push":
        return None
    opts_with_value = {"--repo", "--receive-pack", "--exec", "-o", "--push-option", "--signed"}
    pos: list[str] = []
    j = i + 1
    while j < len(argv):
        a = argv[j]
        if a == "--":
            pos += argv[j + 1:]
            break
        if a in opts_with_value:
            j += 2
            continue
        if not a.startswith("-"):
            pos.append(a)
        j += 1
    return workdir, pos


def _git(repo: str, *args: str) -> str | None:
    try:
        done = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True,
                              timeout=_GIT_TIMEOUT_S, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def _targets_protected(refspec: str) -> bool:
    dst = refspec.lstrip("+").rsplit(":", 1)[-1]
    dst = dst.removeprefix("refs/heads/")
    return dst in _PROTECTED


def _check_push(workdir: str | None, pos: list[str], cwd: str) -> Decision | None:
    repo = _norm(workdir, cwd) if workdir else cwd
    refspecs = pos[1:]  # pos[0] is the remote
    if refspecs:
        if any(_targets_protected(r) for r in refspecs):
            return "deny", MAIN_REASON
    else:
        branch = _git(repo, "symbolic-ref", "--short", "-q", "HEAD")
        if branch in _PROTECTED:
            return "deny", MAIN_REASON
    url = _git(repo, "remote", "get-url", "origin")
    if url and "herdr-engineering" in url:
        return "ask", HERDR_REASON
    return None


def check_bash(command: str, cwd: str) -> Decision | None:
    result: Decision | None = None
    for argv in _split_commands(command):
        parsed = _parse_push(argv)
        if parsed is None:
            continue
        d = _check_push(parsed[0], parsed[1], cwd)
        if d and d[0] == "deny":
            return d
        result = result or d
    return result


def decide(payload: object) -> Decision | None:
    try:
        if not isinstance(payload, dict):
            return None
        tool, inp = payload.get("tool_name"), payload.get("tool_input")
        cwd = payload.get("cwd")
        cwd = cwd if isinstance(cwd, str) and cwd else os.getcwd()
        if not isinstance(tool, str) or not isinstance(inp, dict):
            return None
        if tool in _WRITE_TOOLS:
            path = inp.get(_WRITE_TOOLS[tool])
            return check_write_path(path, cwd) if isinstance(path, str) and path else None
        if tool == "Bash":
            command = inp.get("command")
            return check_bash(command, cwd) if isinstance(command, str) else None
    except Exception:  # a guard must fail open
        return None
    return None

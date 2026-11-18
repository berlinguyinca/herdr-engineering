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
_SEPARATORS = {"&&", "||", ";", "|", "&"}
_SCRATCHPAD = re.compile(r"^/(?:private/)?tmp/claude-\d+(?:/|$)")
_GIT_TIMEOUT_S = 1.0

Decision = tuple[str, str]

TMP_REASON = ("Writing scratch files under /tmp is not allowed. Use a gitignored project directory "
              "such as .<project>/<topic>/ instead (the Claude Code harness scratchpad under "
              "/tmp/claude-<id>/ is the only exception).")
MAIN_REASON = "Pushing to main/master is blocked. Push a feature branch and open a PR instead."
HERDR_REASON = "pushing herdr-engineering needs explicit user approval"
UNKNOWN_REASON = ("Cannot tell which repository or branch this push targets (unresolved cd). "
                  "Confirm it does not push to main/master.")


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


_REDIR = re.compile(r"^\d*(?:>>?|<<<?|<)")
_REDIR_BARE = re.compile(r"^\d*(?:>>?|<<<?|<)$")


def _tokenize(line: str) -> list[list[str]]:
    lex = shlex.shlex(line, posix=True, punctuation_chars=";&|\n(){}")
    lex.whitespace_split = True
    lex.whitespace = " \t\r"  # an unquoted newline is a command separator
    lex.commenters = ""
    toks = list(lex)
    cmds: list[list[str]] = [[]]
    i = 0
    while i < len(toks):
        tok = toks[i]
        nxt = toks[i + 1] if i + 1 < len(toks) else ""
        i += 1
        if tok == "&" and _REDIR.match(nxt):
            continue  # the "&" of "&>file"; the redirect itself is dropped next
        if tok and set(tok) <= set(";&|\n(){}"):
            for ch in tok:  # runs like ");" merge into one token; "()" are subshell scope markers
                cmds += [[ch], []] if ch in "()" else [[]]
        elif _REDIR_BARE.match(tok):
            if i < len(toks) and toks[i] == "&":  # "2>&1": shlex splits it into "2>", "&", "1"
                i += 1
            i += 1  # drop the redirect target
        elif _REDIR.match(tok):
            continue  # attached target (">/dev/null", "2>log")
        else:
            cmds[-1].append(tok)
    return [c for c in cmds if c]


_HEREDOC = re.compile(r"<<-?\s*(['\"]?)(\w+)\1")


def _strip_heredocs(command: str) -> str:
    """Drop heredoc bodies and their terminator lines so body text is not parsed as commands.

    Limit: the scan is textual, so a quoted ``<<WORD`` that is not a heredoc swallows the lines up
    to a later ``WORD`` line, which could hide a push placed there.
    """
    out: list[str] = []
    terminators: list[str] = []
    for line in command.split("\n"):
        if terminators:
            if line.strip() == terminators[0]:
                terminators.pop(0)
            continue
        out.append(line)
        terminators = [m.group(2) for m in _HEREDOC.finditer(line) if m.group(0) != "<<<"]
    if terminators:  # unterminated heredoc: keep everything rather than hide a command
        return command
    return "\n".join(out)


def _split_commands(command: str) -> list[list[str]]:
    """Tokenize the whole command; unquoted newlines separate commands like ``;``.

    Backslash-newline joins lines and heredoc bodies are skipped.  Raises ValueError when the
    command cannot be tokenized.
    """
    return _tokenize(_strip_heredocs(command).replace("\\\n", ""))


_GIT_PUSH = re.compile(r"\bgit\b[^;&|\n]*?\bpush\b([^;&|\n]*)")


def _fallback(command: str, cwd: str) -> Decision | None:
    """Conservative scan for a command shlex rejected: fail closed for obvious pushes to main.

    Trade-off: this works on the raw text, so a push-looking string inside an untokenizable
    quoted message can be denied even though it is only text.
    """
    result: Decision | None = None
    for m in _GIT_PUSH.finditer(command):
        words = [w.strip("'\"`") for w in m.group(1).split()]
        if any(_targets_protected(w, None) for w in words if w and not w.startswith("-")):
            return "deny", MAIN_REASON
        url = _git(cwd, "remote", "get-url", "origin")
        if url and "herdr-engineering" in url:
            result = ("ask", HERDR_REASON)
    return result


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
    opts_with_value = {"--repo", "--receive-pack", "--exec", "-o", "--push-option"}
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


def _targets_protected(refspec: str, current: str | None) -> bool:
    spec = refspec.lstrip("+")
    if ":" in spec:
        dst = spec.rsplit(":", 1)[-1]
    elif spec in ("HEAD", "@"):
        dst = current or ""  # HEAD/@ without a destination pushes to the current branch
    else:
        dst = spec
    return dst.removeprefix("refs/heads/") in _PROTECTED


def _check_push(workdir: str | None, pos: list[str], cwd: str | None) -> Decision | None:
    if workdir:
        repo = _norm(workdir, cwd) if cwd or os.path.isabs(workdir) else None
    else:
        repo = cwd
    refspecs = pos[1:]  # pos[0] is the remote
    branch = _git(repo, "symbolic-ref", "--short", "-q", "HEAD") if repo else None
    if any(_targets_protected(r, branch) for r in refspecs) or (not refspecs and branch in _PROTECTED):
        return "deny", MAIN_REASON
    if repo is None:
        if not refspecs or any(r.lstrip("+") in ("HEAD", "@") for r in refspecs):
            return "ask", UNKNOWN_REASON
        return None
    url = _git(repo, "remote", "get-url", "origin")
    if url and "herdr-engineering" in url:
        return "ask", HERDR_REASON
    return None


_KEYWORDS = {"if", "then", "else", "elif", "do", "while", "until", "!", "time", "command", "exec",
             "nohup", "sudo", "env"}
_ASSIGN = re.compile(r"^[A-Za-z_]\w*=")
_SHELLS = {"bash", "sh", "zsh"}
_MAX_DEPTH = 3


def _strip_prefix(argv: list[str]) -> list[str]:
    """Drop leading keywords, ``VAR=val`` assignments and ``env``/``sudo``-style wrappers."""
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in _KEYWORDS:
            i += 1
            if a == "sudo" and i < len(argv) and argv[i] in ("-u", "-g"):
                i += 2
            while a in ("env", "sudo", "nohup") and i < len(argv) and argv[i].startswith("-"):
                i += 1
        elif _ASSIGN.match(a):
            i += 1
        else:
            break
    return argv[i:]


def _cd(argv: list[str], cwd: str | None) -> str | None:
    args = [a for a in argv[1:] if not a.startswith("-") or a == "-"]
    target = args[0] if args else "~"
    if target == "-":
        return None
    path = os.path.expanduser(target)
    if not os.path.isabs(path):
        if cwd is None:
            return None
        path = os.path.join(cwd, path)
    path = os.path.normpath(path)
    return path if os.path.isdir(path) else None


def _guard_argvs(argvs: list[list[str]], cwd: str | None, depth: int) -> Decision | None:
    result: Decision | None = None
    stack: list[str | None] = []
    for raw in argvs:
        if raw == ["("]:
            stack.append(cwd)
            continue
        if raw == [")"]:
            cwd = stack.pop() if stack else cwd
            continue
        argv = _strip_prefix(raw)
        if not argv:
            continue
        name = os.path.basename(argv[0])
        d: Decision | None = None
        if name == "cd":
            cwd = _cd(argv, cwd)
        elif name in _SHELLS and "-c" in argv[:-1] and depth < _MAX_DEPTH:
            d = check_bash(argv[argv.index("-c") + 1], cwd, depth + 1)
        else:
            parsed = _parse_push(argv)
            if parsed is not None:
                d = _check_push(parsed[0], parsed[1], cwd)
        if d and d[0] == "deny":
            return d
        result = result or d
    return result


def check_bash(command: str, cwd: str | None, depth: int = 0) -> Decision | None:
    try:
        argvs = _split_commands(command)
    except ValueError:
        return _fallback(command, cwd or os.getcwd())
    return _guard_argvs(argvs, cwd, depth)


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

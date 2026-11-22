"""Pure decision functions for the managed ``PreToolUse`` guard hook.

``decide(payload)`` returns ``None`` (no opinion) or ``(decision, reason)`` where decision is
``"deny"`` or ``"ask"`` (``"ask"`` is internal: ``decide`` turns it into a deny unless the one-shot
override token exists, because ``ask`` is not prompted in Claude Code auto permission mode).  It never touches the network and fails open: any unexpected input or
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
ALLOW_PUSH_ENV = "HERDR_WORKFLOW_ALLOW_PUSH"
""" Env var overriding the one-shot push-approval token path (tests, nonstandard state dirs). """

Decision = tuple[str, str]

TMP_REASON = ("Writing scratch files under /tmp is not allowed. Use a gitignored project directory "
              "such as .<project>/<topic>/ instead (the Claude Code harness scratchpad under "
              "/tmp/claude-<id>/ is the only exception).")
TOKEN_REASON = ("The push-approval token is for the human only: ask the user to run it with the `!` "
                "prefix. Creating or editing it yourself defeats the guard.")
MAIN_REASON = "Pushing to main/master is blocked. Push a feature branch and open a PR instead."
HERDR_REASON = "force or destructive push to herdr-engineering needs explicit user approval"
UNKNOWN_REASON = ("Cannot tell which repository or branch this push targets (unresolved cd). "
                  "Confirm it does not push to main/master.")


def allow_push_path() -> str:
    """Token file the human creates (``! touch <path>``) to approve exactly one guarded push."""
    return os.environ.get(ALLOW_PUSH_ENV) or os.path.join(
        os.path.expanduser("~"), ".local", "state", "herdr-engineering", "allow-push")


def _push_denial(reason: str) -> str:
    return (f"{reason}. Ask the user to run `! touch {allow_push_path()}` (the `!` prefix runs it in "
            "this session), then retry the push once. The approval is one-shot. Do not create that "
            "file yourself.")


def _consume_token() -> bool:
    try:
        os.unlink(allow_push_path())
    except OSError:
        return False
    return True


def _mentions_token(command: str) -> bool:
    path = allow_push_path()
    home = os.path.expanduser("~")
    forms = {path, "herdr-engineering/allow-push"}
    if path.startswith(home + os.sep):
        rel = path[len(home):]
        forms |= {"~" + rel, "$HOME" + rel, "${HOME}" + rel}
    return any(f in command for f in forms)


def _norm(path: str, cwd: str) -> str:
    p = path if os.path.isabs(path) else os.path.join(cwd or os.getcwd(), path)
    return os.path.normpath(p)


def check_write_path(path: str, cwd: str) -> Decision | None:
    full = _norm(path, cwd)
    if full == _norm(allow_push_path(), cwd):
        return "deny", TOKEN_REASON
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


# --all/--branches push every local branch, so a stray local main rides along unseen.
_DESTRUCTIVE_LONG = ("--force", "--force-with-lease", "--force-if-includes", "--mirror", "--delete",
                     "--prune", "--all", "--branches")
_SHORT_DESTRUCTIVE = frozenset("fd")


def _destructive_flag(arg: str) -> bool:
    """True for a push option that forces, deletes or prunes (git accepts unique long prefixes)."""
    if arg.startswith("--"):
        name = arg.split("=", 1)[0]
        return len(name) >= 3 and any(long.startswith(name) for long in _DESTRUCTIVE_LONG)
    return arg.startswith("-") and len(arg) > 1 and bool(_SHORT_DESTRUCTIVE & set(arg[1:]))


def _destructive_refspec(refspec: str) -> bool:
    """``+src:dst`` forces; ``:dst`` (empty source) deletes the remote ref; a glob (``*``) can
    match main without naming it."""
    return refspec.startswith(("+", ":")) or "*" in refspec


def _is_destructive(args: list[str]) -> bool:
    return any(_destructive_flag(a) if a.startswith("-") else _destructive_refspec(a) for a in args)


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
        if not _is_destructive([w for w in words if w]):
            continue  # a plain non-main push to herdr-engineering is allowed
        url = _git(cwd, "remote", "get-url", "origin")
        if url and "herdr-engineering" in url:
            result = ("ask", HERDR_REASON)
    return result


def _parse_push(argv: list[str]) -> tuple[str | None, list[str], bool] | None:
    """For a ``git ... push ...`` argv return (-C dir, positional args after push, destructive).

    ``destructive`` is true for any force/delete/mirror/prune option, a ``+`` refspec or an
    empty-source (``:branch``) refspec.
    """
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
    destructive = False
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
        elif _destructive_flag(a):
            destructive = True
        j += 1
    destructive = destructive or any(_destructive_refspec(r) for r in pos[1:])
    return workdir, pos, destructive


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


def _check_push(workdir: str | None, pos: list[str], cwd: str | None,
                destructive: bool = False) -> Decision | None:
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
    if destructive and url and "herdr-engineering" in url:
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
                d = _check_push(parsed[0], parsed[1], cwd, parsed[2])
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
            if not isinstance(command, str):
                return None
            if _mentions_token(command):
                return "deny", TOKEN_REASON
            result = check_bash(command, cwd)
            if result and result[0] == "ask":
                return None if _consume_token() else ("deny", _push_denial(result[1]))
            return result
    except Exception:  # a guard must fail open
        return None
    return None

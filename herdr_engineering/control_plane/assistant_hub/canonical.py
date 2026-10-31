"""Canonicalizer: raw worker action -> ``ActionKey`` per simple command (security boundary).

The shell parser is a blacklist, so this module is an ALLOWLIST: only heads it explicitly knows get a known verb.
Every other head (interpreters, ``xargs``, ``find -exec``, ``git -c``, shells without ``-c``, ``ssh`` with an
interactive session ...) becomes ``Verb.UNKNOWN`` with ``owner_decides=True``. ``Derived.details`` carries the argv,
redirects, cwd and cluster context of every element so ``vetoes`` can inspect them independently of the verb.

``RawAction.command`` is always a complete command line (``git push -f``, never just ``push -f``). Paths are
normalized with ``os.path.normpath``; symlinks are not followed, so any ``hint`` fact used is listed in
``worker_attested_facts`` and a disagreement between hint and derivation makes the owner decide.
"""

from __future__ import annotations

import os
import shlex
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace

from .shellparse import XARGS_MARKER, SimpleCommand, Unparseable, parse
from .verbs import STRICTER, VERB_RISK, ActionKey, FlagClass, Verb

HINT_KEYS = frozenset({"cwd", "repo", "recipient", "host"})
SHELL_TOOLS = frozenset({"shell", "bash", "sh", "zsh", "git", "gh", "ssh", "cluster", "exec", "command",
                         "terminal", "slurm"})
_MAX_REMOTE_DEPTH = 3
_STD_DIRS = ("/usr/bin/", "/bin/", "/usr/local/bin/", "/usr/sbin/", "/sbin/")


@dataclass(frozen=True)
class RawAction:
    tool: str
    command: str | None = None
    operation: str | None = None
    recipients: tuple[str, ...] = ()
    repo: str | None = None
    environment: str | None = None
    hint: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Element:
    """Facts about one simple command that the vetoes need (argv is stripped of the xargs marker)."""

    key: ActionKey
    argv: tuple[str, ...]
    redirects: tuple[str, ...]
    env: tuple[tuple[str, str], ...]
    on_cluster: bool
    cwd: str | None
    owner_decides: bool
    dynamic: bool
    xargs: bool


@dataclass(frozen=True)
class Derived:
    key: ActionKey | None
    elements: tuple[ActionKey, ...]
    owner_decides: bool
    reason: str
    worker_attested_facts: tuple[str, ...]
    details: tuple[Element, ...] = ()
    raw_text: str = ""
    hint_host: str = ""
    unparsed: bool = False
    hint_cwd: str = ""


@dataclass(frozen=True)
class _Cls:
    verb: Verb
    flag: FlagClass = FlagClass.NONE
    scope: str = ""
    target: str = ""
    owner: bool = False
    reason: str = ""


def mk_key(tool: str, verb: Verb, scope: str = "", target: str = "", flag: FlagClass = FlagClass.NONE) -> ActionKey:
    return ActionKey(tool, verb, scope, target, flag, VERB_RISK[verb])


# ---------------------------------------------------------------- shared helpers (also used by vetoes)

def std_name(head: str) -> str:
    """Program name; only bare names and standard install dirs count (``./ls`` stays ``./ls``)."""
    if "/" not in head:
        return head
    directory, _, base = head.rpartition("/")
    return base if directory + "/" in _STD_DIRS else head


def resolve_path(token: str, cwd: str | None) -> str | None:
    """Normalize ``token`` against ``cwd``; None when the location cannot be known (``~``, relative without cwd)."""
    token = token.strip()
    if not token or token.startswith("~") or "\x00" in token:
        return None
    if not token.startswith("/"):
        if cwd is None:
            return None
        token = cwd + "/" + token
    norm = os.path.normpath(token)
    return "/" + norm.lstrip("/")


def split_git(argv: tuple[str, ...] | list[str]) -> tuple[str, list[str], bool] | None:
    """For a ``git`` argv return (subcommand, args after it, saw -c/unknown global option); None when not git."""
    if not argv or std_name(argv[0]) != "git":
        return None
    i, odd, n = 1, False, len(argv)
    while i < n:
        a = argv[i]
        if a == "-C" and i + 1 < n:
            i += 2
        elif a in ("--no-pager", "--no-optional-locks", "--no-replace-objects", "--literal-pathspecs"):
            i += 1
        elif a.startswith("-"):
            odd = True
            i += 2 if a in ("-c", "--git-dir", "--work-tree", "--namespace", "--exec-path") else 1
        else:
            return a, list(argv[i + 1:]), odd
    return "", [], odd


@dataclass(frozen=True)
class PushFacts:
    force: bool
    delete: bool
    unsafe: bool  # --receive-pack / --exec style options that run programs
    remote: str


_PUSH_FORCE_LONG = ("--force", "--force-with-lease", "--force-if-includes", "--mirror")


def git_push_facts(argv: tuple[str, ...] | list[str]) -> PushFacts | None:
    parts = split_git(argv)
    if parts is None or parts[0] != "push":
        return None
    force = delete = unsafe = False
    positional: list[str] = []
    for a in parts[1]:
        if a == "--":
            continue
        if a.startswith("--"):
            name = a.split("=", 1)[0]
            if name in _PUSH_FORCE_LONG or name.startswith("--force"):
                force = True
            elif name == "--delete":
                delete = True
            elif name in ("--receive-pack", "--exec"):
                unsafe = True
        elif a.startswith("-") and len(a) > 1:
            force = force or "f" in a[1:]
            delete = delete or "d" in a[1:]
        else:
            positional.append(a)
    for ref in positional[1:]:
        if ref.startswith("+"):
            force = True
        elif ref.startswith(":"):
            force = delete = True
    return PushFacts(force, delete, unsafe, positional[0] if positional else "")


def repo_from_url(text: str) -> str:
    """``org/name`` from a remote URL (``git@host:org/name.git``, ``https://host/org/name``) or ``org/name``."""
    text = text.strip().removesuffix(".git")
    if text.startswith((".", "/", "~")):
        return ""
    if "://" in text:
        parts = text.split("://", 1)[1].split("/")[1:]
    elif "@" in text and ":" in text:
        parts = text.split(":", 1)[1].split("/")
    else:
        parts = text.split("/")
    parts = [p for p in parts if p]
    return "/".join(parts[-2:]) if len(parts) >= 2 else ""


def expand_braces(token: str, limit: int = 64) -> list[str]:
    """Expand ``{a,b}`` lists (bounded); unexpandable text is left as is."""
    out = [token]
    for _ in range(8):
        nxt: list[str] = []
        changed = False
        for t in out:
            opened = t.find("{")
            if opened == -1:
                nxt.append(t)
                continue
            depth, parts, start, end = 0, [], opened + 1, -1
            for i in range(opened, len(t)):
                c = t[i]
                if c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        end = i
                        break
                elif c == "," and depth == 1:
                    parts.append(t[start:i])
                    start = i + 1
            if end == -1 or not parts:
                nxt.append(t)
                continue
            parts.append(t[start:end])
            changed = True
            nxt.extend(t[:opened] + p + t[end + 1:] for p in parts)
        out = nxt[:limit]
        if not changed:
            break
    return out[:limit]


SCAN_HEADS = frozenset({
    "git", "gh", "curl", "wget", "http", "https", "rm", "rmdir", "mv", "cp", "truncate", "dd", "shred", "unlink",
    "find", "rsync", "tee", "sed", "apt", "apt-get", "aptitude", "dnf", "yum", "zypper", "conda", "mamba",
    "micromamba", "cargo", "npm", "crontab", "systemctl", "apptainer", "singularity", "make", "gem", "go", "spack",
    "poetry", "uv", "pipx", "yarn", "pnpm", "brew", "nix-env", "python", "python3", "cat", "head", "tail", "less",
    "more", "grep", "rg", "base64", "xxd", "od", "strings", "echo", "printenv", "env", "ssh", "scp", "sftp",
    "install", "chmod", "chown", "ln", "touch", "mkdir", "tar", "unzip", "zip", "nc",
})
_SCAN_SEPARATORS = ";&|\n()`"


def scan_elements(text: str, on_cluster: bool, cwd: str | None, only: frozenset[str] | None = None) -> list[Element]:
    """Best-effort, over-approximate inspection records from raw text (quotes and wrappers ignored)."""
    cleaned = text.replace("\\", "").replace("'", "").replace('"', "")
    for sep in _SCAN_SEPARATORS:
        cleaned = cleaned.replace(sep, "\n")
    out: list[Element] = []
    for line in cleaned.split("\n"):
        words = line.split()
        redirects: list[str] = []
        for j, tok in enumerate(words):
            if tok[:1] in "<>":
                target = tok.lstrip("<>&|")
                if not target and j + 1 < len(words):
                    target = words[j + 1]
                if target and target != "/dev/null":
                    redirects.append(target)
        for i, tok in enumerate(words):
            name = std_name(tok)
            argv: list[str] | None = None
            if (name in SCAN_HEADS or name.startswith("pip")) and (only is None or name in only):
                argv = words[i:]
            elif tok == "push" and only is None:
                argv = ["git", *words[i:]]
            if argv:
                key = mk_key("shell", Verb.UNKNOWN, "", " ".join(argv))
                out.append(Element(key, tuple(argv), tuple(redirects), (), on_cluster, cwd, True, False, False))
    return out


# ---------------------------------------------------------------- command families

_RO_SIMPLE = frozenset({
    "ls", "cat", "head", "tail", "grep", "wc", "stat", "diff", "pwd", "echo", "true", "false", "id", "uname", "df",
    "du", "which", "file", "cut", "tr", "basename", "dirname", "realpath", "readlink", "cd",
})
_FS_MUTATING = frozenset({"mv", "cp", "chmod", "chown", "truncate", "dd", "shred", "mkdir", "touch", "ln", "install",
                          "tee", "chgrp"})
_CLUSTER_RO = frozenset({"squeue", "sacct", "sinfo", "sprio", "sstat", "sshare", "sdiag"})
_SRUN_SHORT_ARG = frozenset("nNcptAwxJoeDqGCdmLMrBiT")
_SRUN_SHORT_FLAG = frozenset("vQuksOHlXZE")
_SRUN_LONG_BOOL = frozenset({"pty", "exclusive", "overcommit", "quiet", "verbose", "unbuffered", "label",
                             "no-kill", "oversubscribe", "contiguous", "requeue", "wait-all-nodes"})
_GH_RO = {("pr", "view"), ("pr", "list"), ("pr", "diff"), ("pr", "status"), ("pr", "checks"), ("issue", "view"),
          ("issue", "list"), ("issue", "status"), ("repo", "view"), ("run", "list"), ("run", "view")}


def _unknown(reason: str, target: str = "") -> _Cls:
    return _Cls(Verb.UNKNOWN, target=target, owner=True, reason=reason)


def _ro_generic(name: str, args: list[str]) -> _Cls:
    flags = [a for a in args if a.startswith("-")]
    pos = [a for a in args if not a.startswith("-")]
    if name == "date" and (any(f in ("-s", "--set") or f.startswith("--set=") or (f[1:2] != "-" and "s" in f)
                              for f in flags) or any(not p.startswith("+") for p in pos)):
        return _unknown("date may set the clock")
    if name == "hostname" and pos:
        return _unknown("hostname may set the name")
    if name == "uniq" and len(pos) > 1:
        return _Cls(Verb.SHELL_MUTATING, reason="uniq with an output file")
    if name == "sort":
        if any(f == "--output" or f.startswith("--output=") or (f[1:2] != "-" and "o" in f) for f in flags):
            return _Cls(Verb.SHELL_MUTATING, reason="sort -o writes a file")
        if any(f.startswith("--compress-program") for f in flags):
            return _unknown("sort runs a compress program")
    if name == "file" and any(f in ("--magic-file", "--compile") or (f[1:2] != "-" and ("C" in f or "m" in f))
                              for f in flags):
        return _unknown("file with a magic database option")
    if name == "rg" and any(f.startswith(("--pre", "--hostname-bin")) for f in flags):
        return _unknown("rg runs a preprocessor")
    return _Cls(Verb.SHELL_READ_ONLY)


def _find(args: list[str]) -> _Cls:
    if any(a in ("-exec", "-execdir", "-ok", "-okdir") for a in args):
        return _unknown("find runs commands")
    if "-delete" in args:
        return _Cls(Verb.FS_DELETE, FlagClass.RECURSIVE)
    if any(a.startswith(("-fprint", "-fls")) for a in args):
        return _Cls(Verb.SHELL_MUTATING, reason="find writes a file")
    return _Cls(Verb.SHELL_READ_ONLY)


_GIT_RO = frozenset({"status", "log", "diff", "show", "rev-parse", "ls-files", "blame", "describe"})
_GIT_MUT = frozenset({"add", "commit", "checkout", "switch", "restore", "merge", "rebase", "pull", "fetch", "clone",
                      "stash", "tag", "cherry-pick", "revert", "mv", "rm", "init", "worktree", "clean", "reset"})
_BRANCH_RO_FLAGS = frozenset({"-a", "-r", "-v", "-vv", "--list", "-l", "--show-current", "--all", "--remotes",
                              "--verbose"})


def _git(argv: list[str], raw_repo: str | None) -> _Cls:
    parts = split_git(argv)
    assert parts is not None
    sub, args, odd = parts
    if odd:
        return _unknown("git global option may run programs")
    if sub == "push":
        facts = git_push_facts(argv)
        assert facts is not None
        scope = repo_from_url(facts.remote) if "/" in facts.remote else ""
        if facts.unsafe:
            return _unknown("git push runs a program", scope)
        verb = Verb.GIT_FORCE_PUSH if facts.force or facts.delete else Verb.GIT_PUSH
        return _Cls(verb, FlagClass.FORCE if verb is Verb.GIT_FORCE_PUSH else FlagClass.NONE, scope,
                    " ".join(a for a in args if not a.startswith("-")))
    if sub == "reset":
        hard = "--hard" in args
        return _Cls(Verb.SHELL_MUTATING, FlagClass.HARD if hard else FlagClass.NONE)
    if sub == "clean":
        return _Cls(Verb.SHELL_MUTATING, FlagClass.HARD)
    if sub in _GIT_RO:
        if sub == "diff" and "--no-index" in args:
            return _unknown("git diff --no-index reads arbitrary files")
        if any(a == "--output" or a.startswith(("--output=", "--ext-diff", "--textconv")) for a in args):
            return _Cls(Verb.SHELL_MUTATING, reason="git writes or runs a helper")
        return _Cls(Verb.SHELL_READ_ONLY)
    if sub == "remote":
        if not args or args in (["-v"], ["--verbose"]):
            return _Cls(Verb.SHELL_READ_ONLY)
        return _Cls(Verb.SHELL_MUTATING)
    if sub == "branch":
        if all(a in _BRANCH_RO_FLAGS for a in args):
            return _Cls(Verb.SHELL_READ_ONLY)
        return _Cls(Verb.SHELL_MUTATING)
    if sub in _GIT_MUT:
        return _Cls(Verb.SHELL_MUTATING)
    return _unknown(f"git {sub or '?'} is not in the allowlist")


def _gh(args: list[str]) -> _Cls:
    repo = ""
    rest: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-R", "--repo") and i + 1 < len(args):
            repo = args[i + 1]
            i += 2
            continue
        if a.startswith("--repo="):
            repo = a.split("=", 1)[1]
        else:
            rest.append(a)
        i += 1
    if not rest:
        return _unknown("gh without subcommand")
    if rest[0] == "api":
        return _gh_api(rest[1:], repo)
    pair = tuple(rest[:2])
    pos = [a for a in rest[2:] if not a.startswith("-")]
    target = pos[0] if pos else ""
    if pair == ("pr", "comment"):
        return _Cls(Verb.PR_COMMENT, scope=repo, target=target)
    if pair == ("pr", "merge"):
        return _Cls(Verb.PR_MERGE, scope=repo, target=target)
    if pair in _GH_RO:
        return _Cls(Verb.SHELL_READ_ONLY, scope=repo, target=target)
    return _unknown(f"gh {' '.join(pair)} is not in the allowlist", target)


def api_method(args: list[str]) -> str:
    """Explicit HTTP method of a ``gh api``/``curl`` style argv ('' when none), upper-cased."""
    for i, a in enumerate(args):
        low = a.lower()
        if a in ("-X", "--method", "--request") and i + 1 < len(args):
            return args[i + 1].upper()
        if low.startswith(("--method=", "--request=")):
            return a.split("=", 1)[1].upper()
        if a.startswith("-X") and len(a) > 2:
            return a[2:].upper()
    return ""


def _gh_api(args: list[str], repo: str) -> _Cls:
    method = api_method(args)
    body = any(a in ("-f", "-F", "--field", "--raw-field", "--input") or a.startswith(("--field=", "--raw-field=",
                                                                                         "--input="))
               for a in args)
    pos = [a for a in args if not a.startswith("-")]
    graphql = bool(pos) and pos[-1] == "graphql"
    if method in ("", "GET") and not body and not graphql:
        return _Cls(Verb.SHELL_READ_ONLY, scope=repo)
    return _Cls(Verb.SHELL_MUTATING, scope=repo, reason="gh api with a non-GET request")


def _fs_cls(name: str, args: list[str]) -> _Cls:
    short = "".join(a[1:] for a in args if a.startswith("-") and not a.startswith("--"))
    recursive = "r" in short or "R" in short or any(a in ("--recursive",) for a in args)
    flag = FlagClass.HARD if "--no-preserve-root" in args else FlagClass.RECURSIVE if recursive else FlagClass.NONE
    if name in ("rm", "rmdir", "unlink"):
        return _Cls(Verb.FS_DELETE, flag)
    if name == "rsync":
        deleting = any(a.startswith(("--delete", "--del", "--remove-source-files")) for a in args)
        return _Cls(Verb.FS_DELETE if deleting else Verb.SHELL_MUTATING, FlagClass.RECURSIVE if deleting else flag)
    if name in ("chmod", "chown", "chgrp"):
        return _Cls(Verb.SHELL_MUTATING, flag if recursive else FlagClass.NONE)
    return _Cls(Verb.SHELL_MUTATING)


def _cluster_simple(name: str, args: list[str]) -> _Cls:
    pos = [a for a in args if not a.startswith("-")]
    if name in _CLUSTER_RO:
        return _Cls(Verb.CLUSTER_READ_ONLY)
    if name == "scontrol":
        return _Cls(Verb.CLUSTER_READ_ONLY if pos[:1] and pos[0] in ("show", "ping") else Verb.CLUSTER_MUTATING)
    if name == "sacctmgr":
        return _Cls(Verb.CLUSTER_READ_ONLY if pos[:1] and pos[0] in ("show", "list") else Verb.CLUSTER_MUTATING)
    return _Cls(Verb.CLUSTER_MUTATING)  # scancel, salloc, sbatch handled elsewhere


def classify_head(argv: list[str], raw_repo: str | None = None) -> _Cls:
    """Classify one argv (xargs marker already removed) by its head; allowlist only."""
    head = std_name(argv[0])
    args = argv[1:]
    if head == "git":
        return _git(argv, raw_repo)
    if head == "gh":
        return _gh(args)
    if head in _RO_SIMPLE or head in ("sort", "uniq", "date", "hostname", "rg"):
        return _ro_generic(head, args)
    if head == "find":
        return _find(args)
    if head in ("rm", "rmdir", "unlink", "rsync", "chmod", "chown", "chgrp") or head in _FS_MUTATING:
        return _fs_cls(head, args)
    if head in _CLUSTER_RO or head in ("scontrol", "sacctmgr", "scancel", "salloc"):
        return _cluster_simple(head, args)
    return _unknown(f"{head} is not in the allowlist")


# ---------------------------------------------------------------- walking parsed commands

@dataclass
class _State:
    raw: RawAction
    out: list[Element] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    redirected_git: bool = False  # cd/pushd or git -C/--git-dir/--work-tree seen: hint repo is not trustworthy


def _emit(st: _State, cmd: SimpleCommand, argv: list[str], cls: _Cls, on_cluster: bool, cwd: str | None,
          scope_default: str, xargs: bool) -> None:
    verb, flag, owner = cls.verb, cls.flag, cls.owner
    real_redirects = tuple(r for r in cmd.redirects if r != "/dev/null")
    sudo = any(k == "__sudo__" for k, _ in cmd.env)
    if verb in (Verb.SHELL_READ_ONLY, Verb.CLUSTER_READ_ONLY) and (real_redirects or sudo):
        verb = Verb.CLUSTER_MUTATING if verb is Verb.CLUSTER_READ_ONLY else Verb.SHELL_MUTATING
    if on_cluster:
        if verb is Verb.SHELL_READ_ONLY:
            verb = Verb.CLUSTER_READ_ONLY
        elif verb not in (Verb.CLUSTER_READ_ONLY, Verb.CLUSTER_MUTATING):
            verb = Verb.CLUSTER_MUTATING
    dynamic = cmd.dynamic
    if dynamic or xargs:
        owner = True
    reason = cls.reason or ("dynamic words" if dynamic else "xargs" if xargs else "")
    scope = cls.scope or scope_default
    head = std_name(argv[0])
    family = ("cluster" if verb in (Verb.CLUSTER_READ_ONLY, Verb.CLUSTER_MUTATING) else
              "git" if head == "git" else "gh" if head == "gh" else "shell")
    target = cls.target or " ".join(argv)
    key = mk_key(family, verb, scope, target, flag)
    st.out.append(Element(key, tuple(argv), real_redirects, cmd.env, on_cluster, cwd,
                          owner or verb is Verb.UNKNOWN, dynamic, xargs))
    if reason:
        st.reasons.append(reason)
    if head == "git" and any(a in ("-C", "--git-dir", "--work-tree") or a.startswith(("--git-dir=", "--work-tree="))
                             for a in argv[1:]):
        st.redirected_git = True


def _walk(st: _State, cmds: list[SimpleCommand], on_cluster: bool, cwd: str | None, depth: int,
          scope_default: str) -> None:
    stack: list[str | None] = []
    for cmd in cmds:
        argv = [a for a in cmd.argv if a != XARGS_MARKER]
        xargs = len(argv) != len(cmd.argv)
        if not argv:
            continue
        head = std_name(argv[0])
        if head in ("cd", "pushd", "popd"):
            args = [a for a in argv[1:] if not a.startswith("-")]
            _emit(st, cmd, argv, _Cls(Verb.SHELL_READ_ONLY), on_cluster, cwd, scope_default, xargs)
            st.redirected_git = True
            if head == "popd":
                cwd = stack.pop() if stack else None
            else:
                if head == "pushd":
                    stack.append(cwd)
                cwd = resolve_path(args[0] if args else "~", cwd)
            continue
        if head in ("ssh", "srun", "sbatch"):
            _remote(st, cmd, argv, head, xargs, on_cluster, cwd, depth, scope_default)
            continue
        cls = classify_head(argv, st.raw.repo)
        _emit(st, cmd, argv, cls, on_cluster, cwd, scope_default, xargs)


def _remote_cmds(st: _State, text: str, cmd: SimpleCommand, cwd: str | None, depth: int, scope: str,
                 outer: list[str]) -> None:
    def fallback(reason: str) -> None:
        _emit(st, cmd, outer, _unknown(reason), True, cwd, scope, False)
        st.out.extend(scan_elements(text, True, cwd))

    if depth >= _MAX_REMOTE_DEPTH:
        fallback("remote nesting too deep")
        return
    try:
        inner = parse(text)
    except Unparseable as exc:
        fallback(f"remote command unparseable: {exc}")
        return
    if not inner:
        fallback("remote command parsed to nothing")
        return
    _walk(st, inner, True, cwd, depth + 1, scope)


def _remote(st: _State, cmd: SimpleCommand, argv: list[str], head: str, xargs: bool, on_cluster: bool,
            cwd: str | None, depth: int, scope_default: str) -> None:
    if head == "ssh":
        _ssh(st, cmd, argv, xargs, on_cluster, cwd, depth)
    elif head == "srun":
        _srun(st, cmd, argv, xargs, cwd, depth, scope_default)
    else:
        _sbatch(st, cmd, argv, xargs, cwd, depth, scope_default)


_SSH_FLAGS = frozenset("qTnt46xvC")
_SSH_ARG_ALLOWED = frozenset("ipl")
_SSH_OPT_ALLOWED = frozenset({"batchmode", "connecttimeout", "stricthostkeychecking", "proxyjump", "port", "user",
                              "identityfile", "serveraliveinterval", "serveralivecountmax", "connectionattempts",
                              "userknownhostsfile", "loglevel", "identitiesonly"})


def _ssh_option_ok(letter: str, value: str) -> bool:
    if letter == "J":
        return True  # the fry veto inspects the value
    if letter == "o":
        return value.split("=", 1)[0].split(" ", 1)[0].strip().lower() in _SSH_OPT_ALLOWED
    return letter in _SSH_ARG_ALLOWED


def _ssh(st: _State, cmd: SimpleCommand, argv: list[str], xargs: bool, on_cluster: bool, cwd: str | None,
         depth: int) -> None:
    i, n, bad = 1, len(argv), False
    while i < n:
        a = argv[i]
        if a == "--":
            i += 1
            break
        if not a.startswith("-") or a == "-":
            break
        letter = a[1:2]
        if letter in "iplJo":
            val = a[2:] if len(a) > 2 else (argv[i + 1] if i + 1 < n else "")
            i += 1 if len(a) > 2 else 2
            bad = bad or not _ssh_option_ok(letter, val)
        else:
            bad = bad or not all(c in _SSH_FLAGS for c in a[1:])
            i += 1
    if i >= n:
        _emit(st, cmd, argv, _unknown("ssh without a destination"), True, cwd, "", xargs)
        return
    host = argv[i].rsplit("@", 1)[-1].lower()
    remote = argv[i + 1:]
    if bad:
        _emit(st, cmd, argv, _unknown("ssh option is not in the allowlist"), True, cwd, host, xargs)
    elif not remote:
        _emit(st, cmd, argv, _unknown("interactive ssh session"), True, cwd, host, xargs)
    else:
        _emit(st, cmd, argv, _Cls(Verb.CLUSTER_READ_ONLY), True, cwd, host, xargs)
    if remote:
        _remote_cmds(st, " ".join(remote), cmd, None, depth, host, argv)


def _srun_command(argv: list[str]) -> tuple[list[str] | None, str, str | None]:
    """Return (command argv or None when ambiguous, reason, --chdir/-D value)."""
    i, n, chdir = 1, len(argv), None
    while i < n:
        a = argv[i]
        if a == "--":
            return argv[i + 1:], "", chdir
        if a.startswith("--"):
            if a.startswith("--chdir="):
                chdir = a.split("=", 1)[1]
                i += 1
            elif a == "--chdir" and i + 1 < n:
                chdir = argv[i + 1]
                i += 2
            elif "=" in a or a[2:] in _SRUN_LONG_BOOL:
                i += 1
            else:
                return None, f"srun option {a} has an ambiguous value", chdir
            continue
        if a.startswith("-") and len(a) > 1:
            if a[1] in _SRUN_SHORT_ARG:
                value = a[2:] if len(a) > 2 else (argv[i + 1] if i + 1 < n else "")
                if a[1] == "D":
                    chdir = value
                i += 1 if len(a) > 2 else 2
            elif all(c in _SRUN_SHORT_FLAG for c in a[1:]):
                i += 1
            else:
                return None, f"srun option {a} is not understood", chdir
            continue
        return argv[i:], "", chdir
    return [], "", chdir


def _srun(st: _State, cmd: SimpleCommand, argv: list[str], xargs: bool, cwd: str | None, depth: int,
          scope: str) -> None:
    inner, why, chdir = _srun_command(argv)
    inner_cwd = resolve_path(chdir, cwd) if chdir is not None else cwd
    if inner is None:
        _emit(st, cmd, argv, _unknown(why), True, cwd, scope, xargs)
        st.out.extend(scan_elements(" ".join(argv), True, inner_cwd))
        return
    if not inner:
        _emit(st, cmd, argv, _unknown("srun without a command"), True, cwd, scope, xargs)
        return
    _emit(st, cmd, argv, _Cls(Verb.CLUSTER_READ_ONLY), True, cwd, scope, xargs)
    _remote_cmds(st, shlex.join(inner), cmd, inner_cwd, depth, scope, argv)


def _sbatch(st: _State, cmd: SimpleCommand, argv: list[str], xargs: bool, cwd: str | None, depth: int,
            scope: str) -> None:
    wrap: str | None = None
    chdir: str | None = None
    for i, a in enumerate(argv):
        nxt = argv[i + 1] if i + 1 < len(argv) else None
        if a.startswith("--wrap="):
            wrap = a.split("=", 1)[1]
        elif a == "--wrap" and nxt is not None:
            wrap = nxt
        elif a.startswith("--chdir="):
            chdir = a.split("=", 1)[1]
        elif a == "--chdir" or a == "-D":
            chdir = nxt
        elif a.startswith("-D") and len(a) > 2 and not a.startswith("--"):
            chdir = a[2:]
    inner_cwd = resolve_path(chdir, cwd) if chdir is not None else cwd
    if wrap is None:
        _emit(st, cmd, argv, _Cls(Verb.CLUSTER_MUTATING, owner=True, reason="sbatch script is not inspected"),
              True, cwd, scope, xargs)
        return
    _emit(st, cmd, argv, _Cls(Verb.CLUSTER_MUTATING), True, cwd, scope, xargs)
    _remote_cmds(st, wrap, cmd, inner_cwd, depth, scope, argv)


# ---------------------------------------------------------------- structured tools

def _hint(raw: RawAction) -> dict[str, str]:
    return {k: v.strip() for k, v in raw.hint.items() if k in HINT_KEYS and isinstance(v, str) and v.strip()}


def _unknown_derived(raw: RawAction, reason: str, facts: list[str], details: tuple[Element, ...] = (),
                     hint_host: str = "") -> Derived:
    key = mk_key(raw.tool.strip().lower() or "unknown", Verb.UNKNOWN, raw.repo or "", raw.command or "")
    return Derived(key, (), True, reason, tuple(facts), details, raw.command or "", hint_host)


def split_recipients(values: Iterable[str]) -> tuple[set[str], bool]:
    """Split on , ; and whitespace, drop display names; returns (addresses, saw_invalid_entry)."""
    out: set[str] = set()
    invalid = False
    for value in values:
        text = value
        while "<" in text and ">" in text.split("<", 1)[1]:
            before, rest = text.split("<", 1)
            inner, after = rest.split(">", 1)
            text = f"{before.rsplit(' ', 1)[0] if ' ' in before.strip() else ''} {inner} {after}"
        for part in text.replace(",", " ").replace(";", " ").split():
            addr = part.strip("<>\"'").lower()
            if "@" not in addr or addr.startswith("@") or addr.endswith("@"):
                invalid = True
            elif addr:
                out.add(addr)
    return out, invalid


def _email(raw: RawAction, hint: dict[str, str], facts: list[str]) -> Derived:
    op = (raw.operation or "").strip().lower()
    recipients, invalid = split_recipients(raw.recipients)
    owner, notes = invalid, ["invalid recipient"] if invalid else []
    if "recipient" in hint:
        attested, bad = split_recipients([hint["recipient"]])
        facts.append(f"recipient={hint['recipient'].lower()}")
        if bad or (recipients and not attested <= recipients):
            owner = True
            notes.append("hint recipient disagrees with the request")
        recipients |= attested
    if op not in ("draft", "send"):
        return _unknown_derived(raw, f"email operation {op!r} is not draft or send", facts)
    scope = ",".join(sorted(recipients))
    if op == "send" and not recipients:
        owner = True
        notes.append("send without recipients")
    flag = FlagClass.MULTIPLE if len(recipients) > 1 else FlagClass.NONE
    key = mk_key("email", Verb.EMAIL_SEND if op == "send" else Verb.EMAIL_DRAFT, scope, "", flag)
    return Derived(key, (key,), owner, "; ".join(notes), tuple(facts))


def _deploy(raw: RawAction, hint: dict[str, str], facts: list[str]) -> Derived:
    env = (raw.environment or "").strip().lower()
    repo = raw.repo or ""
    if "repo" in hint:
        facts.append(f"repo={hint['repo']}")
        if repo and repo.lower() != hint["repo"].lower():
            facts.append("disagreement: hint repo differs from request repo")
            verb = {"canary": Verb.DEPLOY_CANARY, "prod": Verb.DEPLOY_PROD,
                    "production": Verb.DEPLOY_PROD}.get(env, Verb.UNKNOWN)
            key = mk_key("deploy", verb, repo)
            return Derived(key, (key,), True, "hint repo disagrees with the request", tuple(facts))
        repo = repo or hint["repo"]
    verb = {"canary": Verb.DEPLOY_CANARY, "prod": Verb.DEPLOY_PROD, "production": Verb.DEPLOY_PROD}.get(env)
    if verb is None:
        key = mk_key("deploy", Verb.UNKNOWN, repo, env)
        return Derived(key, (key,), True, f"unknown deploy environment {env!r}", tuple(facts))
    key = mk_key("deploy", verb, repo)
    return Derived(key, (key,), False, "", tuple(facts))


def _bare_env_records(text: str, cwd: str | None) -> list[Element]:
    return scan_elements(text, False, cwd, frozenset({"env", "printenv"}))


def _shell(raw: RawAction, hint: dict[str, str], facts: list[str]) -> Derived:
    text = raw.command
    host = hint.get("host", "")
    if host:
        facts.append(f"host={host}")
    hint_cwd = hint.get("cwd", "")
    cwd = None
    if hint_cwd:
        facts.append(f"cwd={hint_cwd}")
        cwd = resolve_path(hint_cwd, None)
    if text is None:
        return _unknown_derived(raw, "no command", facts, hint_host=host)
    cluster_ctx = any(w in host.lower() for w in ("cluster", "hive", "farm", "franklin", "node"))
    try:
        cmds = parse(text)
    except Unparseable as exc:
        recs = tuple(scan_elements(text, cluster_ctx or _mentions_cluster(text), cwd))
        d = _unknown_derived(raw, f"unparseable: {exc}", facts, recs, host)
        return replace(d, unparsed=True, hint_cwd=hint_cwd)
    if not cmds:
        recs = tuple(scan_elements(text, cluster_ctx or _mentions_cluster(text), cwd))
        d = _unknown_derived(raw, "no executable command found", facts, recs, host)
        return replace(d, unparsed=True, hint_cwd=hint_cwd)
    st = _State(raw)
    _walk(st, cmds, False, cwd, 0, raw.repo or "")
    notes: list[str] = []
    use_hint_repo = "repo" in hint
    if use_hint_repo:
        facts.append(f"repo={hint['repo']}")
        if st.redirected_git:
            use_hint_repo = False
            notes.append("hint repo not trusted after cd or git -C/--git-dir/--work-tree")
        elif not raw.repo:
            st = _State(raw)
            _walk(st, cmds, False, cwd, 0, hint["repo"])
    keys = tuple(e.key for e in st.out)
    best = keys[0]
    for k in keys[1:]:
        best = STRICTER(best, k)
    owner = any(e.owner_decides for e in st.out) or bool(notes)
    reasons = [*dict.fromkeys(st.reasons), *notes]
    if hint_cwd and cwd is None:
        owner = True
        reasons.append("hint cwd is not an absolute path")
    repos = {e.key.resource_scope.casefold() for e in st.out if e.key.tool in ("git", "gh") and e.key.resource_scope}
    attested = ([hint["repo"]] if use_hint_repo else []) + ([raw.repo] if raw.repo else [])
    repos |= {a.casefold() for a in attested}
    if len(repos) > 1:
        owner = True
        reasons.append("repository scopes disagree")
        facts.append("disagreement: repository scopes differ between hint, request and command")
    details = (*st.out, *_bare_env_records(text, cwd))
    return Derived(best, keys, owner, "; ".join(reasons), tuple(facts), details, text, host, False, hint_cwd)


def _mentions_cluster(text: str) -> bool:
    words = text.lower().replace("'", " ").replace('"', " ").replace(";", " ").split()
    return any(w in ("ssh", "srun", "sbatch") for w in words)


_TOOLS: dict[str, Callable[[RawAction, dict[str, str], list[str]], Derived]] = {
    "email": _email, "deploy": _deploy,
}


def derive(raw: RawAction) -> Derived:
    tool = raw.tool.strip().lower()
    hint = _hint(raw)
    facts: list[str] = []
    if tool in _TOOLS:
        return _TOOLS[tool](raw, hint, facts)
    if tool in SHELL_TOOLS:
        return _shell(raw, hint, facts)
    return _unknown_derived(raw, f"unknown tool {tool!r}", facts)

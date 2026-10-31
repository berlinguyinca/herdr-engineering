"""Conservative shell command parser for assistant hub action canonicalization.

``parse`` flattens a shell command line into the simple commands it would run: it splits on ``&&``, ``||``, ``;``,
``&``, pipes and newlines, flattens ``( )`` and ``{ ; }`` groups, extracts ``$(...)`` and backtick substitutions and
``sh -c`` strings (recursively, to depth 4), and strips well-known wrappers (``env``, ``sudo``, ``timeout``...).

The parser feeds an allowlist, so a construct it misreads is a bypass. Anything it cannot classify with certainty
(unbalanced quoting, heredocs, process substitution, arithmetic, ANSI-C quoting, ``eval``/``source``, shell keywords,
variable command names, unknown wrapper options, oversized or NUL input) raises ``Unparseable`` and the caller must
fall back to the owner. Nothing here executes or evaluates the input.

``parse`` may return an empty list (``x=1``, ``()``, ``bash -c ''``): callers must treat an empty result as
"owner decides", never as "nothing to veto". Commands carry ``dynamic=True`` when a word's value is only known at
run time (``$VAR``, globs, brace expansion, ``~``); callers must not auto-approve those.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

MAX_LENGTH = 8192
MAX_DEPTH = 4
SUDO_MARKER = ("__sudo__", "1")
XARGS_MARKER = "__xargs__"

_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\+?=")
_NAMED_FD = re.compile(r"^\{[A-Za-z_][A-Za-z0-9_]*\}$")
_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh"})
_STD_DIRS = ("/usr/bin/", "/bin/", "/usr/local/bin/", "/usr/sbin/", "/sbin/")
_FORBIDDEN = frozenset({
    "eval", "source", ".", "trap", "[[", "[", "test", "let", "declare", "typeset", "local", "printf", "read",
    "unset", "set", "shopt", "alias", "export", "readonly", "mapfile", "readarray", "getopts", "hash", "enable",
    "fc", "history", "bind", "compgen", "complete", "compopt", "caller",
})
_FORBIDDEN_ENV = frozenset({
    "PS4", "BASH_ENV", "ENV", "LD_PRELOAD", "LD_LIBRARY_PATH", "PATH", "PROMPT_COMMAND", "IFS", "SHELLOPTS",
    "BASHOPTS", "LD_AUDIT", "LD_DEBUG_OUTPUT", "NODE_OPTIONS", "NODE_PATH", "PYTHONSTARTUP", "PYTHONPATH",
    "PYTHONHOME", "PERL5OPT", "PERL5LIB", "RUBYOPT", "RUBYLIB", "PAGER", "EDITOR", "VISUAL", "SSH_ASKPASS",
})
_FORBIDDEN_ENV_PREFIXES = ("GIT_", "BASH_FUNC_")
_ARRAY_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\[.*\]\+?=")
_BRACE_EXPANSION = re.compile(r"\{[^{}]*(?:,|\.\.)[^{}]*\}")
_DYNAMIC_DOLLAR = frozenset("{@*?#$!-_")
_RESERVED = frozenset({
    "if", "then", "elif", "else", "fi", "while", "until", "do", "done", "for", "select", "case", "esac",
    "function", "coproc", "in", "!",
})
_SHELL_LONG_FLAGS = frozenset({"login", "norc", "noprofile", "posix", "restricted", "noediting", "verbose"})
_MAX_WRAPPERS = 16


class Unparseable(Exception):
    """The command contains a construct the parser cannot classify safely."""


@dataclass(frozen=True)
class SimpleCommand:
    argv: tuple[str, ...]
    env: tuple[tuple[str, str], ...]
    wrapped: bool
    redirects: tuple[str, ...] = ()
    dynamic: bool = False


@dataclass(frozen=True)
class _Spec:
    flags: str = ""
    argflags: str = ""
    optattached: str = ""
    long_noarg: frozenset[str] = frozenset()
    long_arg: frozenset[str] = frozenset()
    long_opt: frozenset[str] = frozenset()
    numeric: bool = False
    bare_dash: bool = False


def _fs(*names: str) -> frozenset[str]:
    return frozenset(names)


_SPECS: dict[str, _Spec] = {
    "env": _Spec(flags="i0v", argflags="uCa", long_noarg=_fs("ignore-environment", "null", "debug"),
                 long_arg=_fs("unset", "chdir", "argv0"), bare_dash=True),
    "command": _Spec(flags="p"),
    "exec": _Spec(flags="cl", argflags="a"),
    "builtin": _Spec(),
    "time": _Spec(flags="pv"),
    "nohup": _Spec(),
    "nice": _Spec(argflags="n", long_arg=_fs("adjustment"), numeric=True),
    "ionice": _Spec(flags="t", argflags="cn", long_noarg=_fs("ignore"), long_arg=_fs("class", "classdata")),
    "stdbuf": _Spec(argflags="ioe", long_arg=_fs("input", "output", "error")),
    "timeout": _Spec(flags="v", argflags="sk", long_noarg=_fs("preserve-status", "foreground", "verbose"),
                     long_arg=_fs("signal", "kill-after")),
    "sudo": _Spec(flags="AbEHnPS", argflags="ugCDprtTUR",
                  long_noarg=_fs("non-interactive", "set-home", "background", "stdin", "askpass",
                                 "preserve-env-all"),
                  long_arg=_fs("user", "group", "close-from", "chdir", "prompt", "role", "type",
                               "command-timeout", "other-user", "chroot"),
                  long_opt=_fs("preserve-env")),
    "xargs": _Spec(flags="0rtpxo", argflags="nLIPdsEa", optattached="lie",
                   long_noarg=_fs("null", "no-run-if-empty", "verbose", "interactive", "exit", "open-tty"),
                   long_arg=_fs("max-args", "max-lines", "max-procs", "delimiter", "max-chars", "arg-file",
                                "process-slot-var"),
                   long_opt=_fs("replace", "eof")),
}


def parse(command: str) -> list[SimpleCommand]:
    """Return every simple command in ``command`` or raise ``Unparseable``."""
    if not isinstance(command, str):
        raise Unparseable("command is not text")
    if len(command.encode("utf-8", errors="surrogatepass")) > MAX_LENGTH:
        raise Unparseable("command too long")
    if not command.strip():
        raise Unparseable("empty command")
    try:
        return _parse(command, 0, (), False)
    except RecursionError as exc:
        raise Unparseable("nesting too deep") from exc


def _parse(text: str, depth: int, inherited: tuple[tuple[str, str], ...], wrapped: bool) -> list[SimpleCommand]:
    if depth > MAX_DEPTH:
        raise Unparseable("nesting too deep")
    for ch in text:
        if ch not in "\t\n" and ord(ch) < 32:
            raise Unparseable("control character in command")
    segments = _Scanner(text, depth, inherited).run()
    out: list[SimpleCommand] = []
    for segment in segments:
        out.extend(_resolve(segment, depth, inherited, wrapped))
    return out


class _Text(str):
    """A word's text plus facts the resolver needs: assignable (name part unquoted), danger (literal ``$(``)."""

    assignable: bool
    danger: bool
    dynamic: bool

    def __new__(cls, text: str, assignable: bool, danger: bool, dynamic: bool) -> _Text:
        obj = super().__new__(cls, text)
        obj.assignable = assignable
        obj.danger = danger
        obj.dynamic = dynamic
        return obj


def _is_assignment(word: str) -> bool:
    if not getattr(word, "assignable", True):
        return False
    if _ARRAY_ASSIGN.match(word):
        raise Unparseable("array element assignment")
    match = _ASSIGN.match(word)
    if match and match.group(0).endswith("+="):
        raise Unparseable("append assignment")
    return bool(match)


def _is_env_word(word: str) -> bool:
    """``env`` and ``sudo`` treat any ``NAME=value`` argument (even quoted, even odd names) as an assignment."""
    if _is_assignment(word):
        return True
    return "=" in word and not word.startswith(("=", "-"))


@dataclass
class _Segment:
    words: list[_Text] = field(default_factory=list)
    dynamic: bool = False
    redirects: list[str] = field(default_factory=list)
    subs: list[SimpleCommand] = field(default_factory=list)


class _Scanner:
    """Single left-to-right pass producing command segments of words, redirect targets and substitutions."""

    def __init__(self, text: str, depth: int, inherited: tuple[tuple[str, str], ...]) -> None:
        self.s = text
        self.depth = depth
        self.inherited = inherited
        self.segments: list[_Segment] = []
        self.cur = _Segment()
        self.buf: list[str] = []
        self.in_word = False
        self.quoted = False
        self.lit: list[str] = []
        self.uq: list[str] = []
        self.dyn = False
        self.eq_seen = False
        self.pre_eq_quoted = False
        self.pending: str | None = None
        self.parens = 0
        self.braces = 0

    def run(self) -> list[_Segment]:
        s, n, i = self.s, len(self.s), 0
        while i < n:
            c = s[i]
            if c in " \t":
                self._flush()
                i += 1
            elif c == "\n":
                self._end()
                i += 1
            elif c == "\\":
                i = self._backslash(i)
            elif c == "'":
                i = self._squote(i)
            elif c == '"':
                i = self._dquote(i)
            elif c == "`":
                i = self._backtick(i)
            elif c == "$":
                i = self._dollar(i, in_dq=False)
            elif c == "#" and not self.in_word:
                nl = s.find("\n", i)
                i = n if nl == -1 else nl
            elif c in "<>":
                i = self._redirect(i)
            elif c == "&":
                i = self._ampersand(i)
            elif c == "|":
                self._end()
                i += 2 if s[i + 1:i + 2] in ("|", "&") else 1
            elif c == ";":
                if s[i + 1:i + 2] in (";", "&"):
                    raise Unparseable("case terminator")
                self._end()
                i += 1
            elif c == "(":
                i = self._open_paren(i)
            elif c == ")":
                self._end()
                if self.parens == 0:
                    raise Unparseable("unbalanced parenthesis")
                self.parens -= 1
                i += 1
            else:
                self._plain(c)
                i += 1
        self._end()
        if self.parens or self.braces:
            raise Unparseable("unbalanced group")
        return self.segments

    def _add(self, text: str, quoted: bool = False, literal: bool = True) -> None:
        self.buf.append(text)
        self.lit.append(text if literal else "\x01")
        self.in_word = True
        if quoted:
            self.quoted = True
            if not self.eq_seen:
                self.pre_eq_quoted = True

    def _plain(self, c: str) -> None:
        if not self.in_word and c == "~":
            self.dyn = True
        if c == "=":
            self.eq_seen = True
        self.uq.append(c)
        self._add(c)

    def _reset_word(self) -> None:
        self.buf = []
        self.in_word = False
        self.quoted = False
        self.lit = []
        self.uq = []
        self.dyn = False
        self.eq_seen = False
        self.pre_eq_quoted = False

    def _flush(self) -> None:
        if not self.in_word:
            return
        text, quoted = "".join(self.buf), self.quoted
        literal = "".join(self.lit)
        danger = "$(" in literal or "`" in literal
        uq = "".join(self.uq)
        dynamic = self.dyn or any(ch in uq for ch in "*?[") or bool(_BRACE_EXPANSION.search(uq))
        if _ASSIGN.match(uq):
            value = uq.partition("=")[2]
            dynamic = dynamic or value.startswith("~") or ":~" in value
        if dynamic:
            self.cur.dynamic = True
        assignable = not self.pre_eq_quoted
        self._reset_word()
        if self.pending is not None:
            if danger:
                raise Unparseable("literal command substitution text in redirect target")
            kind, self.pending = self.pending, None
            if kind == "dup" and (text.isdigit() or text == "-"):
                return
            self.cur.redirects.append(text)
            return
        if not self.cur.words and not quoted:
            if text == "{":
                self.braces += 1
                return
            if text == "}":
                if self.braces == 0:
                    raise Unparseable("unbalanced brace")
                self.braces -= 1
                return
        self.cur.words.append(_Text(text, assignable, danger, dynamic))

    def _end(self) -> None:
        self._flush()
        if self.pending is not None:
            raise Unparseable("redirection without target")
        if self.cur.words or self.cur.redirects or self.cur.subs:
            self.segments.append(self.cur)
        self.cur = _Segment()

    def _backslash(self, i: int) -> int:
        nxt = self.s[i + 1:i + 2]
        if not nxt:
            raise Unparseable("trailing backslash")
        if nxt != "\n":
            self._add(nxt, quoted=True)
        return i + 2

    def _squote(self, i: int) -> int:
        j = self.s.find("'", i + 1)
        if j == -1:
            raise Unparseable("unbalanced single quote")
        self._add(self.s[i + 1:j], quoted=True)
        return j + 1

    def _dquote(self, i: int) -> int:
        s, n = self.s, len(self.s)
        self._add("", quoted=True)
        i += 1
        while i < n:
            c = s[i]
            if c == '"':
                return i + 1
            if c == "\\":
                nxt = s[i + 1:i + 2]
                if not nxt:
                    break
                if nxt in '$`"\\':
                    self._add(nxt, quoted=True)
                    i += 2
                elif nxt == "\n":
                    i += 2
                else:
                    self._add("\\")
                    i += 1
            elif c == "$":
                i = self._dollar(i, in_dq=True)
            elif c == "`":
                i = self._backtick(i)
            else:
                self._add(c)
                i += 1
        raise Unparseable("unbalanced double quote")

    def _dollar(self, i: int, in_dq: bool) -> int:
        s = self.s
        nxt = s[i + 1:i + 2]
        if nxt == "(":
            if s[i + 2:i + 3] == "(":
                raise Unparseable("arithmetic expansion")
            j = _find_close_paren(s, i + 2, self.depth + 1)
            self._substitute(s[i + 2:j])
            self._add(s[i:j + 1], literal=False)
            self.dyn = True
            return j + 1
        if nxt == "{":
            j = s.find("}", i + 2)
            if j == -1:
                raise Unparseable("unbalanced parameter expansion")
            if any(ch in s[i + 2:j] for ch in "()`$\"'\\{@[!"):
                raise Unparseable("complex parameter expansion")
            self._add(s[i:j + 1], literal=False)
            self.dyn = True
            return j + 1
        if nxt == "[":
            raise Unparseable("legacy arithmetic expansion")
        if nxt in ("'", '"') and not in_dq:
            raise Unparseable("ANSI-C or locale quoting")
        self._add("$")
        if nxt.isalnum() or nxt in _DYNAMIC_DOLLAR:
            self.dyn = True
        return i + 1

    def _backtick(self, i: int) -> int:
        s, n = self.s, len(self.s)
        j, inner = i + 1, []
        while j < n and s[j] != "`":
            if s[j] == "\\":
                nxt = s[j + 1:j + 2]
                if not nxt:
                    break
                inner.append(nxt if nxt in "`\\$" else "\\" + nxt)
                j += 2
            else:
                inner.append(s[j])
                j += 1
        if j >= n:
            raise Unparseable("unbalanced backtick")
        self._substitute("".join(inner))
        self._add(s[i:j + 1], literal=False)
        self.dyn = True
        return j + 1

    def _substitute(self, inner: str) -> None:
        self.cur.subs.extend(_parse(inner, self.depth + 1, self.inherited, True))

    def _redirect(self, i: int) -> int:
        s = self.s
        c = s[i]
        if s.startswith("<<", i):
            raise Unparseable("heredoc or here-string")
        if self.in_word and not self.quoted and ("".join(self.buf).isdigit() or _NAMED_FD.match("".join(self.buf))):
            self._reset_word()
        else:
            self._flush()
        j = i + 1
        if c == ">" and s[j:j + 1] in (">", "|", "&"):
            j += 1
        elif c == "<" and s[j:j + 1] in ("&", ">"):
            j += 1
        if s[j:j + 1] == "(":
            raise Unparseable("process substitution")
        self._begin_redirect(s[j - 1] == "&")
        return j

    def _begin_redirect(self, dup: bool) -> None:
        if self.pending is not None:
            raise Unparseable("redirection without target")
        self.pending = "dup" if dup else "file"

    def _ampersand(self, i: int) -> int:
        s = self.s
        nxt = s[i + 1:i + 2]
        self._flush()
        if nxt == "&":
            self._end()
            return i + 2
        if nxt == ">":
            j = i + 2
            if s[j:j + 1] == ">":
                j += 1
            if s[j:j + 1] == "(":
                raise Unparseable("process substitution")
            self._begin_redirect(False)
            return j
        self._end()
        return i + 1

    def _open_paren(self, i: int) -> int:
        self._flush()
        if self.cur.words or self.cur.redirects or self.pending is not None:
            raise Unparseable("parenthesis after a command word")
        if self.s[i + 1:i + 2] == "(":
            raise Unparseable("arithmetic command")
        self.parens += 1
        return i + 1


def _find_close_paren(s: str, start: int, level: int) -> int:
    if level > MAX_DEPTH:
        raise Unparseable("nesting too deep")
    depth, i, n = 1, start, len(s)
    while i < n:
        c = s[i]
        if c == "\\":
            i += 2
        elif c == "'":
            j = s.find("'", i + 1)
            if j == -1:
                raise Unparseable("unbalanced single quote")
            i = j + 1
        elif c == '"':
            i = _skip_dquote(s, i, level)
        elif c == "`":
            i = _skip_backtick(s, i)
        elif c == "(":
            depth += 1
            i += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i
            i += 1
        else:
            i += 1
    raise Unparseable("unbalanced command substitution")


def _skip_dquote(s: str, i: int, level: int) -> int:
    n = len(s)
    i += 1
    while i < n:
        c = s[i]
        if c == "\\":
            i += 2
        elif c == '"':
            return i + 1
        elif c == "$" and s[i + 1:i + 2] == "(":
            i = _find_close_paren(s, i + 2, level + 1) + 1
        elif c == "`":
            i = _skip_backtick(s, i)
        else:
            i += 1
    raise Unparseable("unbalanced double quote")


def _skip_backtick(s: str, i: int) -> int:
    n = len(s)
    j = i + 1
    while j < n:
        if s[j] == "\\":
            j += 2
        elif s[j] == "`":
            return j + 1
        else:
            j += 1
    raise Unparseable("unbalanced backtick")


def _skip_options(args: list[str], spec: _Spec) -> int:
    """Return the index of the first non-option argument; unknown options raise."""
    i, n = 0, len(args)
    while i < n:
        a = args[i]
        if a == "--":
            return i + 1
        if a == "-" and spec.bare_dash:
            i += 1
        elif a.startswith("--"):
            name, eq, _ = a[2:].partition("=")
            if name in spec.long_noarg and not eq:
                i += 1
            elif name in spec.long_arg:
                i += 1 if eq else 2
            elif name in spec.long_opt:
                i += 1
            else:
                raise Unparseable(f"unknown option {a}")
        elif a.startswith("-") and len(a) > 1:
            cluster = a[1:]
            if spec.numeric and cluster.isdigit():
                i += 1
                continue
            i += 1 + _cluster_args(cluster, spec, a)
        else:
            return i
        if i > n:
            raise Unparseable("option is missing its argument")
    return i


def _cluster_args(cluster: str, spec: _Spec, original: str) -> int:
    """Validate a short-option cluster; return 1 if it consumes the next word as its argument."""
    for j, ch in enumerate(cluster):
        if ch in spec.flags:
            continue
        if ch in spec.optattached:
            return 0
        if ch in spec.argflags:
            return 0 if j + 1 < len(cluster) else 1
        raise Unparseable(f"unknown option {original}")
    return 0


def _std_name(head: str) -> str | None:
    """Program name for wrapper/shell detection; only bare names or standard install dirs count."""
    if "/" not in head:
        return head
    directory, _, base = head.rpartition("/")
    return base if directory + "/" in _STD_DIRS else None


def _shell_c_string(args: list[str]) -> str | None:
    """Return the ``-c`` string of a shell invocation, or None when it is not a ``-c`` invocation."""
    i, n, seen_c = 0, len(args), False
    while i < n:
        a = args[i]
        if a == "--":
            i += 1
            break
        if a.startswith("--"):
            if a[2:] not in _SHELL_LONG_FLAGS:
                raise Unparseable(f"unknown shell option {a}")
            i += 1
        elif a[:1] in ("-", "+") and len(a) > 1:
            cluster = a[1:]
            if not cluster.isalpha():
                raise Unparseable(f"unknown shell option {a}")
            if any(ch in "oO" for ch in cluster[:-1]):
                raise Unparseable(f"ambiguous shell option {a}")
            if a[0] == "-" and "c" in cluster:
                seen_c = True
            i += 2 if cluster[-1] in "oO" else 1
        else:
            break
    if not seen_c:
        return None
    if i >= n:
        raise Unparseable("shell -c without a command string")
    return args[i]


def _null_command(
    seg: _Segment, env: tuple[tuple[str, str], ...], wrapped: bool
) -> list[SimpleCommand]:
    if not seg.redirects:
        return []
    return [SimpleCommand((":",), env, wrapped, tuple(seg.redirects), seg.dynamic)]


def _add_env(env: list[tuple[str, str]], word: str) -> None:
    key, _, value = word.partition("=")
    if key.rstrip("+") in _FORBIDDEN_ENV or key.startswith(_FORBIDDEN_ENV_PREFIXES):
        raise Unparseable(f"assignment to {key} is not allowed")
    env.append((key, value))


def _resolve(
    segment: _Segment, depth: int, inherited: tuple[tuple[str, str], ...], wrapped: bool
) -> list[SimpleCommand]:
    words = list(segment.words)
    env = list(inherited)
    k = 0
    while k < len(words) and _is_assignment(words[k]):
        _add_env(env, words[k])
        k += 1
    main = _unwrap(words[k:], env, segment, depth, wrapped)
    return main + segment.subs


def _check_head(head: str) -> None:
    if getattr(head, "dynamic", False):
        raise Unparseable("command name is dynamic")
    if _ARRAY_ASSIGN.match(head):
        raise Unparseable("array element assignment")
    if head in _FORBIDDEN or head in _RESERVED:
        raise Unparseable(f"unsupported construct {head!r}")


def _unwrap(
    argv: list[str], env: list[tuple[str, str]], seg: _Segment, depth: int, wrapped: bool
) -> list[SimpleCommand]:
    xargs = sudo = False
    for _ in range(_MAX_WRAPPERS):
        if not argv and xargs:
            argv = ["echo"]
        if not argv:
            _reject_danger(seg, None)
            return _null_command(seg, tuple(env), wrapped)
        head = argv[0]
        _check_head(head)
        name = _std_name(head)
        if name not in _SPECS:
            break
        rest = argv[1:]
        if name == "command" and rest and rest[0].startswith("-") and set(rest[0][1:]) & set("vV"):
            break
        idx = _skip_options(rest, _SPECS[name])
        if name in ("env", "sudo"):
            while idx < len(rest) and _is_env_word(rest[idx]):
                _add_env(env, rest[idx])
                idx += 1
        if name == "timeout":
            if idx >= len(rest):
                raise Unparseable("timeout without duration")
            idx += 1
        if name == "sudo" and not sudo:
            sudo = True
            env.append(SUDO_MARKER)
        if name == "xargs":
            xargs = True
        argv = rest[idx:]
    else:
        raise Unparseable("too many wrappers")
    return _finish(argv, env, seg, depth, wrapped, xargs)


def _reject_danger(seg: _Segment, allowed: str | None) -> None:
    """Literal ``$(``/backtick text can be evaluated later (``${x@P}``, ``declare``...), except in a ``-c`` string."""
    for word in seg.words:
        if word.danger and word is not allowed:
            raise Unparseable("literal command substitution text")


def _finish(
    argv: list[str], env: list[tuple[str, str]], seg: _Segment, depth: int, wrapped: bool, xargs: bool
) -> list[SimpleCommand]:
    head = argv[0]
    if not head or "$" in head or "`" in head:
        raise Unparseable("command name is not a literal")
    if _std_name(head) in _SHELLS:
        string = _shell_c_string(argv[1:])
        if string is not None:
            _reject_danger(seg, string)
            inner = _parse(string, depth + 1, tuple(env), True)
            return inner + _null_command(seg, tuple(env), wrapped)
    _reject_danger(seg, None)
    if xargs:
        argv = [*argv, XARGS_MARKER]
    return [SimpleCommand(tuple(argv), tuple(env), wrapped, tuple(seg.redirects), seg.dynamic)]

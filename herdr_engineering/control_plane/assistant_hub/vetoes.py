"""Fixed hard-deny vetoes over every element of a derived action (security boundary).

Vetoes inspect argv, redirects, cwd and cluster context of EVERY element in ``Derived.details`` (not just the
stricter key) and never depend on the verb the canonicalizer assigned. ``extra`` rules can only add vetoes: the
fixed rules run first and their result is returned unchanged, whatever codes the extra rules use.

Codes: force_push, protection_disable, quobyte_delete, fry_route, cluster_install, secret_print.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from fnmatch import fnmatchcase

from .canonical import Derived, Element, api_method, git_push_facts, resolve_path, std_name
from .verbs import Verb

FRY = "fry.fiehnlab.ucdavis.edu"
QUOBYTE = "/quobyte"
CLUSTER_WORDS = ("cluster", "hive", "farm", "franklin", "node")


@dataclass(frozen=True)
class Veto:
    code: str
    reason: str


@dataclass(frozen=True)
class VetoRule:
    code: str
    reason: str
    verb: Verb | None = None
    argv_contains: str | None = None


# ---------------------------------------------------------------- 1. force push / protection

def _force_push(el: Element) -> Veto | None:
    facts = git_push_facts(el.argv)
    if facts is not None and (facts.force or facts.delete):
        return Veto("force_push", "force push, mirror push or remote ref deletion is never allowed")
    return None


def _protection(el: Element) -> Veto | None:
    head, args = std_name(el.argv[0]), list(el.argv[1:])
    if head == "gh" and args[:1] == ["api"] or head in ("curl", "wget", "http", "https"):
        if api_method(args) not in ("DELETE", "PUT", "PATCH"):
            return None
        for tok in args:
            path = "/" + tok.split("?", 1)[0].lower().strip("/")
            if fnmatchcase(path, "*/branches/*/protection*") or fnmatchcase(path, "*/rulesets*"):
                return Veto("protection_disable", "changing branch protection or rulesets is never allowed")
    return None


# ---------------------------------------------------------------- 2. quobyte

_DEL_HEADS = frozenset({"rm", "rmdir", "mv", "truncate", "shred", "unlink", "tee", "chmod", "chown", "chgrp", "touch",
                        "mkdir", "ln"})
_DEST_HEADS = frozenset({"cp", "install", "rsync", "scp"})
_RSYNC_DELETE = ("--delete", "--del", "--remove-source-files")


def _under_quobyte(path: str | None) -> bool:
    return path is not None and (path == QUOBYTE or path.startswith(QUOBYTE + "/"))


def _path_tokens(el: Element) -> list[str]:
    """Resolved path-like values of argv (including ``of=...`` / ``--opt=...`` values) that could be known."""
    out: list[str] = []
    for tok in el.argv[1:]:
        cands = [] if tok.startswith("-") and "=" not in tok else [tok]
        if "=" in tok:
            cands.append(tok.split("=", 1)[1])
        for cand in cands:
            resolved = resolve_path(cand, el.cwd)
            if resolved is None and el.cwd is None:
                resolved = _dotdot_into_quobyte(cand)
            if resolved is not None:
                out.append(resolved)
    return out


def _dotdot_into_quobyte(token: str) -> str | None:
    """Unknown cwd: ``../quobyte/x`` may be anywhere, but could be /quobyte; treat it as /quobyte (fail closed)."""
    if token.startswith(("/", "~")):
        return None
    norm = os.path.normpath(token)
    parts = norm.split("/")
    while parts and parts[0] == "..":
        parts.pop(0)
    if len(parts) < len(norm.split("/")) and parts[:1] == ["quobyte"]:
        return "/" + "/".join(parts)
    return None


def _touches_quobyte(el: Element) -> bool:
    paths = [*_path_tokens(el), *(resolve_path(r, el.cwd) or "" for r in el.redirects)]
    return any(_under_quobyte(p) for p in paths)


def _quobyte(el: Element, anywhere: bool) -> Veto | None:
    head, args = std_name(el.argv[0]), list(el.argv[1:])
    veto = Veto("quobyte_delete", "deleting or modifying anything under /quobyte is never allowed")
    if any(_under_quobyte(resolve_path(r, el.cwd)) for r in el.redirects):
        return veto
    paths = _path_tokens(el)
    hit = any(_under_quobyte(p) for p in paths)
    positional = [a for a in args if not a.startswith("-")]
    if head in _DEL_HEADS:
        if hit or ((el.xargs or el.dynamic) and anywhere and head not in ("mkdir", "touch", "tee")):
            return veto
    elif head == "dd":
        if any(_under_quobyte(resolve_path(a[3:], el.cwd)) for a in args if a.startswith("of=")):
            return veto
    elif head in _DEST_HEADS:
        deleting = head == "rsync" and any(a.startswith(_RSYNC_DELETE) for a in args)
        if deleting and hit:
            return veto
        if positional and _under_quobyte(resolve_path(positional[-1], el.cwd)):
            return veto
        if (el.xargs or el.dynamic) and anywhere and deleting:
            return veto
    elif head == "find":
        runs = any(a in ("-exec", "-execdir", "-ok", "-okdir") for a in args)
        exec_destructive = runs and any(std_name(a) in _DEL_HEADS | _DEST_HEADS for a in args)
        if hit and ("-delete" in args or exec_destructive):
            return veto
    elif head == "sed":
        in_place = any(a == "--in-place" or a.startswith("--in-place=")
                       or (a.startswith("-") and not a.startswith("--") and "i" in a) for a in args)
        if in_place and hit:
            return veto
    return None


# ---------------------------------------------------------------- 3. fry

def _fry_in(texts: Iterable[str]) -> bool:
    return any(FRY in t.lower() for t in texts)


# ---------------------------------------------------------------- 4. cluster installs

_APT_SUBS = frozenset({"install", "upgrade", "remove", "purge", "update", "dist-upgrade", "full-upgrade",
                       "groupinstall", "localinstall", "autoremove", "reinstall"})
_CONDA_SUBS = frozenset({"install", "create", "remove", "update", "upgrade"})
_SYSTEMCTL_SUBS = frozenset({"enable", "start", "restart", "edit", "stop", "disable", "mask", "reload",
                             "daemon-reload", "reenable", "link", "set-default"})
_NPM_SUBS = frozenset({"install", "i", "add"})


def _is_installer(argv: tuple[str, ...]) -> bool:
    head, args = std_name(argv[0]), list(argv[1:])
    pos = [a for a in args if not a.startswith("-")]
    sub = pos[0] if pos else ""
    if head.startswith("python") and "-m" in args:
        idx = args.index("-m")
        return args[idx + 1:idx + 2] in (["pip"], ["pip3"]) and "install" in args[idx + 2:]
    if head.startswith("pip") or head == "pipx":
        return sub == "install"
    if head == "uv":
        return sub in ("pip", "tool", "add", "sync") and any(a in ("install", "add", "sync") for a in pos)
    if head in ("apt", "apt-get", "aptitude", "dnf", "yum", "zypper", "pacman", "brew"):
        return sub in _APT_SUBS or head in ("pacman", "brew") and bool(sub)
    if head in ("conda", "mamba", "micromamba"):
        return sub in _CONDA_SUBS
    if head == "cargo":
        return sub == "install"
    if head == "npm":
        glob = any(a in ("-g", "--global") or a == "--location=global" for a in args)
        return sub in _NPM_SUBS and glob
    if head == "crontab":
        return args != ["-l"]
    if head == "systemctl":
        return sub in _SYSTEMCTL_SUBS
    if head in ("apptainer", "singularity"):
        return sub == "build"
    if head == "make":
        return "install" in pos
    return head == "gem" and sub == "install"


def _cluster_install(el: Element, hint_host: str) -> Veto | None:
    on_cluster = el.on_cluster or any(w in hint_host.lower() for w in CLUSTER_WORDS)
    if on_cluster and _is_installer(el.argv):
        return Veto("cluster_install", "installing software or editing services on a cluster is never allowed")
    return None


# ---------------------------------------------------------------- 5. secrets

_SECRET_HEADS = frozenset({"cat", "echo", "printenv", "env", "head", "tail", "less", "more", "grep", "rg", "base64",
                           "xxd", "od", "strings"})
_SECRET_BASENAMES = ("*key*", "*token*", "*secret*", "*.pem", "id_rsa*", "id_ed25519*", ".env*", "credentials*",
                     ".netrc")
_SECRET_VARS = ("*key*", "*token*", "*secret*", "*password*", "*passwd*", "*credential*")
_SECRET_DIRS = ("/.aws/", "/.ssh/")
_PATTERN_HEADS = frozenset({"grep", "rg"})


def _secret_path(token: str) -> bool:
    low = token.strip().lower().rstrip("/") if token.strip() not in ("/",) else ""
    if not low:
        return False
    base = low.rsplit("/", 1)[-1]
    if any(fnmatchcase(base, pat) for pat in _SECRET_BASENAMES):
        return True
    wrapped = "/" + low.lstrip("/") + "/"
    return any(d in wrapped for d in _SECRET_DIRS)


def _var_names(token: str) -> list[str]:
    names: list[str] = []
    i = 0
    while (i := token.find("$", i)) != -1:
        j = i + 1
        if token[j:j + 1] == "{":
            end = token.find("}", j)
            raw = token[j + 1:end if end != -1 else len(token)]
            j = (end if end != -1 else len(token)) + 1
        else:
            k = j
            while k < len(token) and (token[k].isalnum() or token[k] == "_"):
                k += 1
            raw, j = token[j:k], k
        name = raw.lstrip("!#").split(":", 1)[0].split("-", 1)[0].split("=", 1)[0]
        if name:
            names.append(name)
        i = max(j, i + 1)
    return names


def _secret_var(name: str) -> bool:
    return any(fnmatchcase(name.lower(), pat) for pat in _SECRET_VARS)


def _secret(el: Element) -> Veto | None:
    head, args = std_name(el.argv[0]), list(el.argv[1:])
    if head not in _SECRET_HEADS:
        return None
    veto = Veto("secret_print", "printing secrets, keys or tokens is never allowed")
    if head in ("echo", "printenv", "env"):
        for tok in args:
            if any(_secret_var(n) for n in _var_names(tok)):
                return veto
        if head == "printenv" and any(_secret_var(a) for a in args if not a.startswith("-")):
            return veto
    flags_with_pattern = any(a in ("-e", "-f", "--regexp", "--file") or a.startswith(("--regexp=", "--file="))
                             for a in args)
    positional = [a for a in args if not a.startswith("-")]
    if head in _PATTERN_HEADS and not flags_with_pattern and positional:
        positional = positional[1:]
    if head == "echo" or head == "printenv":
        return None
    candidates = [*positional, *el.redirects]
    for tok in args:
        if tok.startswith("-") and "=" in tok:
            candidates.append(tok.split("=", 1)[1])
    if any(_secret_path(t) for t in candidates):
        return veto
    return None


# ---------------------------------------------------------------- driver

def _fixed(derived: Derived) -> Veto | None:
    if _fry_in([derived.raw_text, derived.hint_host]):
        return Veto("fry_route", "routing through the retired Fry host is never allowed")
    anywhere = any(_touches_quobyte(e) for e in derived.details)
    for el in derived.details:
        if _fry_in([*el.argv, *el.redirects, *(v for _, v in el.env)]):
            return Veto("fry_route", "routing through the retired Fry host is never allowed")
        for found in (_force_push(el), _protection(el), _quobyte(el, anywhere),
                      _cluster_install(el, derived.hint_host), _secret(el)):
            if found is not None:
                return found
    return None


def _extra(derived: Derived, rules: Sequence[VetoRule]) -> Veto | None:
    for rule in rules:
        if rule.verb is None and rule.argv_contains is None:
            return Veto(rule.code, rule.reason)
        needle = (rule.argv_contains or "").lower()
        for el in derived.details:
            verb_ok = rule.verb is None or el.key.verb is rule.verb
            argv_ok = rule.argv_contains is None or needle in " ".join(el.argv).lower()
            if verb_ok and argv_ok:
                return Veto(rule.code, rule.reason)
        if rule.verb is None and needle and needle in derived.raw_text.lower():
            return Veto(rule.code, rule.reason)
    return None


def check_vetoes(derived: Derived, extra: Sequence[VetoRule] = ()) -> Veto | None:
    """Return the first fixed veto, else the first matching extra rule, else None. Extras only ever add."""
    return _fixed(derived) or _extra(derived, extra)


FIXED_VETOES: tuple[str, ...] = ("force_push", "protection_disable", "quobyte_delete", "fry_route",
                                 "cluster_install", "secret_print")

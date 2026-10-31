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

from .canonical import (
    Derived,
    Element,
    api_method,
    classify_head,
    expand_braces,
    git_push_facts,
    resolve_path,
    split_git,
    std_name,
)
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

_PROTECTION_MUTATIONS = ("deletebranchprotectionrule", "updatebranchprotectionrule", "createbranchprotectionrule",
                         "deleteruleset", "updateruleset", "createruleset")
_BODY_FLAGS = ("-f", "-F", "--field", "--raw-field", "--input", "-d", "--data", "--data-raw", "--data-binary",
               "--form", "-T", "--upload-file", "--json")


def _force_push(el: Element) -> Veto | None:
    facts = git_push_facts(el.argv)
    if facts is not None and (facts.force or facts.delete):
        return Veto("force_push", "force push, mirror push or remote ref deletion is never allowed")
    return None


def _protection(el: Element) -> Veto | None:
    head, args = std_name(el.argv[0]), list(el.argv[1:])
    if not (head == "gh" and args[:1] == ["api"] or head in ("curl", "wget", "http", "https")):
        return None
    veto = Veto("protection_disable", "changing branch protection or rulesets is never allowed")
    if any(m in " ".join(args).lower() for m in _PROTECTION_MUTATIONS):
        return veto
    method = api_method(args)
    body = any(a in _BODY_FLAGS or a.startswith(("--field=", "--raw-field=", "--input=", "--data=", "--form="))
               for a in args)
    if method in ("GET", "HEAD") or (not method and not body):
        return None
    for tok in args:
        path = "/" + tok.split("?", 1)[0].lower().strip("/")
        if fnmatchcase(path, "*/branches/*/protection*") or fnmatchcase(path, "*/rulesets*"):
            return veto
    return None


# ---------------------------------------------------------------- 2/3. quobyte

_COPY_HEADS = frozenset({"cp", "install", "scp", "rsync", "ln"})
_WRITE_HEADS = frozenset({"cp", "install", "scp", "rsync", "ln", "curl", "wget", "unzip", "zip", "touch", "mkdir",
                          "chmod", "chown", "chgrp", "tee", "dd", "mv", "rm", "rmdir", "truncate", "shred", "unlink",
                          "patch"})
_ROOT_HEADS = frozenset({"mv", "chmod", "chown", "chgrp", "truncate", "shred", "unlink", "rm", "rmdir"})
_RSYNC_DELETE = ("--delete", "--del", "--remove-source-files")
_GLOB = "*?["


def _under_quobyte(path: str | None) -> bool:
    return path is not None and (path == QUOBYTE or path.startswith(QUOBYTE + "/"))


def _tar_writes(args: list[str]) -> bool:
    letters = "".join(a[1:] for a in args if a.startswith("-") and not a.startswith("--"))
    if args and not args[0].startswith("-"):
        letters += args[0]
    return any(c in letters for c in "xru") or any(a in ("--extract", "--delete", "--append", "--update")
                                                   for a in args)


def _in_place(args: list[str]) -> bool:
    return any(a == "--in-place" or a.startswith("--in-place=")
               or (a.startswith("-") and not a.startswith("--") and "i" in a) for a in args)


def _mutating(el: Element) -> bool:
    head, args = std_name(el.argv[0]), list(el.argv[1:])
    if head == "tar":
        return _tar_writes(args)
    if head == "sed":
        return _in_place(args)
    if head == "find":
        return any(a in ("-delete", "-exec", "-execdir", "-ok", "-okdir") or a.startswith(("-fprint", "-fls"))
                   for a in args)
    if head in _WRITE_HEADS:
        return True
    return classify_head(list(el.argv)).verb in (Verb.SHELL_MUTATING, Verb.FS_DELETE)


def _candidates(tok: str) -> list[str]:
    """Path-like values inside a token: the token, ``opt=value``, attached ``-tVALUE`` and ``host:path``."""
    out = [] if tok.startswith("-") and "=" not in tok and not (len(tok) > 2 and tok[2:3] == "/") else [tok]
    if "=" in tok:
        out.append(tok.split("=", 1)[1])
    if tok.startswith("-") and not tok.startswith("--") and len(tok) > 2:
        out.append(tok[2:])
    if ":" in tok and not tok.startswith(("/", "-")):
        out.append(tok.split(":", 1)[1])
    return out


def _token_hits(tok: str, cwd: str | None) -> tuple[bool, bool]:
    """(could be under /quobyte, resolves to the filesystem root) over brace/glob/variable forms of ``tok``."""
    quobyte = root = False
    for cand in _candidates(tok):
        for exp in expand_braces(cand):
            static = exp.split("$", 1)[0] if "$" in exp else exp
            if "$" in exp and static.startswith(QUOBYTE):
                quobyte = True
            resolved = resolve_path(exp, cwd)
            if resolved is None and cwd is None:
                resolved = _dotdot_into_quobyte(exp)
            if _under_quobyte(resolved):
                quobyte = True
            if resolved == "/":
                root = True
            if any(c in exp for c in _GLOB):
                full = exp if exp.startswith("/") else (f"{cwd}/{exp}" if cwd else "")
                comps = [c for c in full.split("/") if c]
                if comps and fnmatchcase("quobyte", comps[0]):
                    quobyte = True
    return quobyte, root


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


def _dest_tokens(args: list[str]) -> list[str]:
    positional = [a for a in args if not a.startswith("-")]
    out = positional[-1:]
    for i, a in enumerate(args):
        if a in ("-t", "--target-directory") and i + 1 < len(args):
            out.append(args[i + 1])
        elif a.startswith("--target-directory="):
            out.append(a.split("=", 1)[1])
        elif a.startswith("-t") and len(a) > 2 and not a.startswith("--"):
            out.append(a[2:])
    return out


def _touches_quobyte(el: Element) -> bool:
    hits = [_token_hits(t, el.cwd)[0] for t in el.argv[1:]] + [_token_hits(r, el.cwd)[0] for r in el.redirects]
    return any(hits)


def _quobyte(el: Element, anywhere: bool) -> Veto | None:
    head, args = std_name(el.argv[0]), list(el.argv[1:])
    veto = Veto("quobyte_delete", "deleting or modifying anything under /quobyte is never allowed")
    if any(_token_hits(r, el.cwd)[0] for r in el.redirects):
        return veto
    if not _mutating(el):
        return None
    deleting = head == "rsync" and any(a.startswith(_RSYNC_DELETE) for a in args)
    if head == "dd":
        values = [a[3:] for a in args if a.startswith("of=")]
        return veto if any(_token_hits(v, el.cwd)[0] for v in values) else None
    if head in _COPY_HEADS and not deleting:
        return veto if any(_token_hits(t, el.cwd)[0] for t in _dest_tokens(args)) else None
    if _under_quobyte(el.cwd) or any(_token_hits(a, el.cwd)[0] for a in args):
        return veto
    if (el.xargs or el.dynamic) and anywhere:
        return veto
    root = classify_head(list(el.argv)).verb is Verb.FS_DELETE or head in _ROOT_HEADS
    if root and any(_token_hits(a, el.cwd)[1] for a in args if not a.startswith("-")):
        return veto
    return None


_TEXT_DESTRUCTIVE = frozenset({"rm", "rmdir", "mv", "truncate", "dd", "shred", "unlink", "find", "rsync", "cp", "tee",
                               "install", "ln", "touch", "mkdir", "chmod", "chown", "tar", "unzip", "curl", "wget",
                               "scp", "sed"})


def _words(text: str) -> list[str]:
    return "".join(c if c.isalnum() or c in "_-" else " " for c in text.lower()).split()


def _text_scan(derived: Derived) -> Veto | None:
    """Over-approximate rules for text the parser could not handle (documented over-veto)."""
    text = derived.raw_text.lower()
    words = _words(text)
    if "/quobyte" in text and (">" in text or _TEXT_DESTRUCTIVE & set(words)):
        return Veto("quobyte_delete", "unparseable command mentions /quobyte with a modifying word")
    if ("protection" in words or "rulesets" in words or "ruleset" in words) and any(
            w in ("delete", "put", "patch", "xdelete", "xput", "xpatch") for w in words):
        return Veto("protection_disable", "unparseable command mentions protection or rulesets with a write method")
    return None


# ---------------------------------------------------------------- 3. fry

_HOST_HEADS = frozenset({"ssh", "scp", "rsync", "sftp"})


def _fry_in(texts: Iterable[str]) -> bool:
    return any(FRY in t.lower() for t in texts)


def _fry_bare(el: Element) -> bool:
    if std_name(el.argv[0]) not in _HOST_HEADS:
        return False
    for tok in el.argv[1:]:
        flat = "".join(" " if c in "@:,= /" else c for c in tok.lower())
        if "fry" in flat.split():
            return True
    return False


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
        return sub in ("add", "sync") or (sub == "pip" and "install" in pos) or (sub == "tool" and "install" in pos)
    if head == "poetry":
        return sub in ("add", "install")
    if head in ("apt", "apt-get", "aptitude", "dnf", "yum", "zypper"):
        return sub in _APT_SUBS
    if head in ("pacman", "brew"):
        return bool(sub)
    if head in ("conda", "mamba", "micromamba"):
        return sub in _CONDA_SUBS
    if head in ("cargo", "go", "spack"):
        return sub == "install"
    if head in ("npm", "pnpm"):
        glob = any(a in ("-g", "--global") or a == "--location=global" for a in args)
        return sub in _NPM_SUBS and glob
    if head == "yarn":
        return sub == "global"
    if head == "nix-env":
        return any(a in ("-i", "--install", "-iA") for a in args)
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
                           "xxd", "od", "strings", "cp", "mv", "scp", "rsync", "ln", "tar", "zip", "curl", "nc",
                           "wget", "sftp", "sed", "awk"})
_SECRET_BASENAMES = ("*key*", "*token*", "*secret*", "*.pem", "id_rsa*", "id_ed25519*", ".env*", "credentials*",
                     ".netrc", ".git-credentials", ".pgpass", ".npmrc", ".pypirc")
_SAMPLE_SECRET_NAMES = (".env", ".env.local", "id_rsa", "id_rsa.pub", "id_ed25519", "credentials",
                        "credentials.json", ".netrc", "server.pem", "secret", "token", "key", ".git-credentials",
                        ".pgpass", ".npmrc", ".pypirc", "environ")
_SECRET_VARS = ("*key*", "*token*", "*secret*", "*password*", "*passwd*", "*credential*")
_SECRET_DIRS = ("/.aws/", "/.ssh/", "/.docker/", "/.kube/", "/.gnupg/", "/.config/gh/")
_SECRET_DIR_NAMES = (".aws", ".ssh", ".docker", ".kube", ".gnupg")
_PATTERN_HEADS = frozenset({"grep", "rg", "sed", "awk"})
_GIT_FILE_SUBS = frozenset({"diff", "show", "blame", "log", "grep", "cat-file"})


def _pure_wildcard(comp: str) -> bool:
    return bool(comp) and all(c in "*?[]" for c in comp)


def _secret_one(tok: str) -> bool:
    low = tok.strip().lower().lstrip("@")
    if not low or low == "/":
        return False
    comps = [c for c in low.split("/") if c]
    base = comps[-1] if comps else ""
    wrapped = "/" + "/".join(comps) + "/"
    if any(d in wrapped for d in _SECRET_DIRS):
        return True
    if base == "environ" and "/proc/" in wrapped:
        return True
    if any(fnmatchcase(base, pat) for pat in _SECRET_BASENAMES):
        return True
    for comp in comps[:-1]:
        if any(c in comp for c in _GLOB) and not _pure_wildcard(comp) and any(
                fnmatchcase(name, comp) for name in _SECRET_DIR_NAMES):
            return True
    return any(c in base for c in _GLOB) and not _pure_wildcard(base) and any(
        fnmatchcase(name, base) for name in _SAMPLE_SECRET_NAMES)


def _secret_path(token: str) -> bool:
    return any(_secret_one(exp) for exp in expand_braces(token))


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


def _file_candidates(args: list[str]) -> list[str]:
    out: list[str] = []
    for tok in args:
        if tok.startswith("-") and "=" not in tok:
            continue
        out.append(tok)
        if "=" in tok:
            out.append(tok.split("=", 1)[1])
        if ":" in tok and not tok.startswith("/"):
            out.append(tok.rsplit(":", 1)[1])
    return out


def _secret(el: Element) -> Veto | None:
    head, args = std_name(el.argv[0]), list(el.argv[1:])
    veto = Veto("secret_print", "printing secrets, keys or tokens is never allowed")
    if head == "gh" and args[:2] == ["auth", "token"]:
        return veto
    if head == "gh" and args[:2] == ["auth", "status"] and any(a in ("-t", "--show-token") for a in args):
        return veto
    if head in ("env", "printenv"):
        names = [a for a in args if not a.startswith("-") and "=" not in a]
        if not names and head == "printenv" or not names and head == "env":
            return veto
    if head == "git":
        parts = split_git(el.argv)
        if parts is not None and parts[0] in _GIT_FILE_SUBS:
            if any(_secret_path(c) for c in _file_candidates(parts[1])):
                return veto
        return None
    if head not in _SECRET_HEADS:
        return None
    if head in ("echo", "printenv", "env"):
        for tok in args:
            if any(_secret_var(n) for n in _var_names(tok)):
                return veto
        if head == "printenv" and any(_secret_var(a) for a in args if not a.startswith("-")):
            return veto
        return None
    flags_with_pattern = any(a in ("-e", "-f", "--regexp", "--file") or a.startswith(("--regexp=", "--file="))
                             for a in args)
    candidates = _file_candidates(args)
    if head in _PATTERN_HEADS and not flags_with_pattern and candidates:
        candidates = candidates[1:]
    if any(_secret_path(t) for t in [*candidates, *el.redirects]):
        return veto
    return None


# ---------------------------------------------------------------- driver

def _fixed(derived: Derived) -> Veto | None:
    fry = Veto("fry_route", "routing through the retired Fry host is never allowed")
    if _fry_in([derived.raw_text, derived.hint_host]) or derived.hint_host.strip().lower() == "fry":
        return fry
    if derived.unparsed:
        found = _text_scan(derived)
        if found is not None:
            return found
    anywhere = any(_touches_quobyte(e) for e in derived.details)
    for el in derived.details:
        if _fry_in([*el.argv, *el.redirects, *(v for _, v in el.env)]) or _fry_bare(el):
            return fry
        for found in (_force_push(el), _protection(el), _quobyte(el, anywhere),
                      _cluster_install(el, derived.hint_host), _secret(el)):
            if found is not None:
                return found
    unknown_cwd = bool(derived.hint_cwd) and resolve_path(derived.hint_cwd, None) is None
    if unknown_cwd and "quobyte" in derived.hint_cwd.lower() and any(_mutating(e) for e in derived.details):
        return Veto("quobyte_delete", "the attested working directory mentions /quobyte and the command modifies")
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

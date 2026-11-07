"""Deploy matching: the hub, never the worker, decides which project and scope a command belongs to.

A command maps to a configured deploy scope only when it is ONE plain command whose argv is exactly
``[<command>, "--ref", <40 lowercase hex chars>]``, run for the configured repo.  Everything else (another flag order,
``--ref=``, a short sha, a wrapper, a chain, a redirect, a different path, a repo the config does not name) is not
matched and stays an ordinary unknown action that the owner decides.  The match is textual and strict on purpose: the
accepted characters contain no shell syntax, so the text can only mean this argv.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .canonical import RawAction, argv_digest, mk_key
from .shellparse import Unparseable, parse
from .verbs import ActionKey, Verb

DEPLOY_TOOLS = frozenset({"shell", "bash", "sh", "zsh", "exec", "command", "terminal"})
_SHA = re.compile(r"[0-9a-f]{40}")
_PLAIN = re.compile(r"[A-Za-z0-9_./+-]+ --ref [0-9a-f]{40}")      # nothing a shell would treat specially, single spaces


@dataclass(frozen=True)
class DeployMatch:
    project: str
    scope: str
    repo: str
    sha: str
    argv_digest: str


def request_key(match: DeployMatch) -> ActionKey:
    """The key of one deploy request: its target digests the argv, so it binds exactly one commit."""
    return mk_key("deploy", Verb.DEPLOY_PROD, match.repo, match.argv_digest)


def scope_target(project: str, scope: str) -> str:
    return f"scope:{project}/{scope}"


def scope_key(match: DeployMatch) -> ActionKey:
    """The key a standing grant is stored under: the project and scope, no commit.  Its target is never empty, so it
    can never equal the key of a ``deploy`` tool request (which has an empty target)."""
    return mk_key("deploy", Verb.DEPLOY_PROD, match.repo, scope_target(match.project, match.scope))


def _plain_command(text: Any) -> list[str] | None:
    """The argv when ``text`` is exactly ``<word> --ref <sha>`` in the plain alphabet, and the real parser agrees."""
    if not isinstance(text, str) or not _PLAIN.fullmatch(text):
        return None
    argv = text.split(" ")
    try:
        cmds = parse(text)
    except Unparseable:
        return None
    if len(cmds) != 1:
        return None
    cmd = cmds[0]
    if list(cmd.argv) != argv or cmd.env or cmd.redirects or cmd.wrapped or cmd.dynamic:
        return None
    return argv


def _scopes(deploy_cfg: Any) -> list[tuple[str, str, str, str]]:
    """(project, scope, repo, command) for every well-formed configured scope; anything malformed is skipped."""
    out: list[tuple[str, str, str, str]] = []
    projects = deploy_cfg.get("projects") if isinstance(deploy_cfg, dict) else None
    if not isinstance(projects, dict):
        return out
    for pid, proj in projects.items():
        scopes = proj.get("scopes") if isinstance(proj, dict) else None
        repo = proj.get("repo") if isinstance(proj, dict) else None
        if not isinstance(pid, str) or not isinstance(repo, str) or not isinstance(scopes, dict):
            continue
        for sid, scope in scopes.items():
            command = scope.get("command") if isinstance(scope, dict) else None
            if isinstance(sid, str) and isinstance(command, str):
                out.append((pid, sid, repo, command))
    return out


def match_deploy(raw: RawAction, deploy_cfg: Any) -> DeployMatch | None:
    """The configured project and scope this request is a deploy of, or None (an ordinary unknown action)."""
    if raw.tool.strip().lower() not in DEPLOY_TOOLS or not raw.repo or not isinstance(raw.repo, str):
        return None
    argv = _plain_command(raw.command)
    if argv is None or not _SHA.fullmatch(argv[2]):
        return None
    hint = {k: v.strip() for k, v in raw.hint.items() if isinstance(v, str) and v.strip()}
    if hint.get("host"):
        return None                                       # it would run somewhere the hub knows nothing about
    for pid, sid, repo, command in _scopes(deploy_cfg):
        if raw.repo.casefold() != repo.casefold() or argv[0] != command:
            continue
        if "repo" in hint and hint["repo"].casefold() != repo.casefold():
            return None                                   # a hint can only make the match stricter
        return DeployMatch(pid, sid, repo, argv[2], argv_digest(argv))
    return None


def scope_mode(deploy_cfg: Any, project: str, scope: str, repo: str) -> str:
    """The CURRENT yaml mode of a configured scope ("ask" or "auto"), or "" when the scope (or that repo) is gone.
    ``auto`` is a switch the owner can flip back at any time: only the exact string "auto" lets a standing grant act."""
    for pid, sid, rrepo, _command in _scopes(deploy_cfg):
        if (pid, sid) == (project, scope) and rrepo.casefold() == repo.casefold():
            mode = deploy_cfg["projects"][pid]["scopes"][sid].get("mode")
            return mode if isinstance(mode, str) else ""
    return ""


def scope_config(deploy_cfg: Any, match: DeployMatch) -> dict[str, Any]:
    """The CURRENT config dict of the matched scope ({} when it is gone): what the hub verifies is the live config's
    ``require`` and ``health_url``, never anything the request carried."""
    for pid, sid, rrepo, _command in _scopes(deploy_cfg):
        if (pid, sid) == (match.project, match.scope) and rrepo.casefold() == match.repo.casefold():
            scope = deploy_cfg["projects"][pid]["scopes"][sid]
            return scope if isinstance(scope, dict) else {}
    return {}

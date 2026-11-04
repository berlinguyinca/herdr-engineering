"""``control_plane.ai_sync`` (central fleet YAML): the sync repo a host joins with ``ai-sync fabric join``.

Served read-only by ``GET /api/v1/ai-sync/config``. Never carries credentials: a URL with userinfo
(``https://user:pw@...``), an unknown key or a non-boolean switch is refused, so a secret pasted here
can neither be stored nor echoed.
"""
from __future__ import annotations

import re
from typing import Any

ENV_ALLOWLIST = ("claude", "codex", "opencode", "antigravity", "pi")
_KEYS = {"repo_url", "branch", "envs", "merge_enabled"}
_HTTPS = re.compile(r"^https://[A-Za-z0-9][A-Za-z0-9.-]*(:\d{1,5})?/[A-Za-z0-9._~/%+-]+$")
_SSH_URL = re.compile(r"^ssh://(?:[A-Za-z0-9._-]+@)?[A-Za-z0-9][A-Za-z0-9.-]*(:\d{1,5})?/[A-Za-z0-9._~/%+-]+$")
_SCP = re.compile(r"^[A-Za-z0-9._-]+@[A-Za-z0-9][A-Za-z0-9.-]*:[A-Za-z0-9._~/+-]+$")
_BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")


def parse_ai_sync(section: Any) -> dict:
    """Validate the section and return the served shape; ValueError names the field, never its value."""
    if not isinstance(section, dict):
        raise ValueError("ai_sync must be a mapping")
    if set(section) - _KEYS:
        raise ValueError("ai_sync: unknown key (allowed: repo_url, branch, envs, merge_enabled)")
    url = section.get("repo_url")
    if not isinstance(url, str) or len(url) > 300 or not (
            _HTTPS.fullmatch(url) or _SSH_URL.fullmatch(url) or _SCP.fullmatch(url)):
        raise ValueError("ai_sync.repo_url must be an https:// or ssh git URL without credentials, whitespace or leading '-'")
    branch = section.get("branch", "main")
    if (not isinstance(branch, str) or not _BRANCH.fullmatch(branch) or ".." in branch or "//" in branch
            or branch.endswith(("/", ".", ".lock")) or "/." in branch or "@{" in branch):
        raise ValueError("ai_sync.branch is not a safe git ref name")
    envs = section.get("envs", ["claude"])
    if (not isinstance(envs, list) or not envs or len(set(envs)) != len(envs)
            or not all(isinstance(e, str) and e in ENV_ALLOWLIST for e in envs)):
        raise ValueError(f"ai_sync.envs must be a non-empty unique list from {', '.join(ENV_ALLOWLIST)}")
    merge = section.get("merge_enabled", False)
    if not isinstance(merge, bool):
        raise ValueError("ai_sync.merge_enabled must be a boolean")
    return {"repo_url": url, "branch": branch, "envs": list(envs), "merge_enabled": merge}

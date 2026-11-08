"""``control_plane.agent_rules`` (central fleet YAML): the shared rules repo every host installs, pinned to a ref.

Served read-only by ``GET /api/v1/agent-rules/config`` and passed to the fleet playbooks as extra vars. Never carries
credentials: a URL with userinfo, an unknown key or an unsafe ref is refused, so a secret pasted here can neither be
stored nor echoed. ``ref`` is mandatory: an unpinned rules repo would make every host's agent rules float.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .ai_sync_config import _BRANCH, _HTTPS, _SCP, _SSH_URL

_KEYS = {"repo_url", "ref"}


def parse_agent_rules(section: Any) -> dict:
    """Validate the section and return the served shape; ValueError names the field, never its value."""
    if not isinstance(section, dict):
        raise ValueError("agent_rules must be a mapping")
    if set(section) - _KEYS:
        raise ValueError("agent_rules: unknown key (allowed: repo_url, ref)")
    url = section.get("repo_url")
    if not isinstance(url, str) or len(url) > 300 or not (
            _HTTPS.fullmatch(url) or _SSH_URL.fullmatch(url) or _SCP.fullmatch(url)):
        raise ValueError("agent_rules.repo_url must be an https:// or ssh git URL without credentials, whitespace or leading '-'")
    ref = section.get("ref")
    if (not isinstance(ref, str) or not _BRANCH.fullmatch(ref) or ".." in ref or "//" in ref
            or ref.endswith(("/", ".", ".lock")) or "/." in ref or "@{" in ref):
        raise ValueError("agent_rules.ref is required and must be a safe tag, branch or commit name")
    return {"repo_url": url, "ref": ref}


def agent_rules_vars(fleet_config: Path) -> dict[str, str]:
    """Ansible extra vars for the configured section; {} when the file or section is absent or unusable."""
    from ..fleet_config import FleetConfigError, FleetConfigStore
    try:
        raw = FleetConfigStore(Path(fleet_config).expanduser()).current().section("control_plane").get("agent_rules")
        if raw is None:
            return {}
        cfg = parse_agent_rules(raw)
    except (FleetConfigError, ValueError, OSError):
        return {}
    return {"agent_rules_repo_url": cfg["repo_url"], "agent_rules_ref": cfg["ref"]}

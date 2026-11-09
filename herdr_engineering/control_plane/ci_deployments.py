"""``control_plane.ci_deployments``: components a CI pipeline reports, not a host updater.

An operator allowlist ``{component: host id}``. A pipeline (for example Woodpecker, as the fleet
host ``woodpecker-ci``) POSTs deployment events for these components with that host's token. They
are accepted and shown, and never planned, gated, permitted, escalated or forced: only a loaded
definition (InferWeave ``deploy/pipelines``) makes a component part of the coordinator.
"""
from __future__ import annotations

import re

KEY = "control_plane.ci_deployments"
MAX_CI_COMPONENTS = 32
#: Same component shape as ``deployments_monitor._COMPONENT``.
_COMPONENT = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
_HOST = re.compile(r"\S{1,253}")
#: The first https URL in an event's evidence: the run page a pipeline reports.
_URL = re.compile(r"https://[^\s<>\"'`]{1,512}")


def _entry_error(component: object, host: object) -> str | None:
    if not isinstance(component, str) or not _COMPONENT.fullmatch(component):
        return f"{KEY}: component {str(component)[:64]!r} must match [a-z0-9][a-z0-9_-]{{0,63}}"
    if not isinstance(host, str) or not _HOST.fullmatch(host):
        return f"{KEY}: host id for {component!r} must be a non-empty string without spaces"
    return None


def parse_ci_deployments(value: object) -> tuple[dict[str, str], list[str]]:
    """``(mapping, errors)``. Any error refuses the whole map (an empty mapping): a half-read
    allowlist is not one the operator wrote. ``None`` (not configured) is an empty map."""
    if value is None:
        return {}, []
    if not isinstance(value, dict):
        return {}, [f"{KEY} must be a mapping of component to host id"]
    if len(value) > MAX_CI_COMPONENTS:
        return {}, [f"{KEY} allows at most {MAX_CI_COMPONENTS} components"]
    errors = [e for k, v in value.items() if (e := _entry_error(k, v))]
    if errors:
        return {}, errors[:5]
    return dict(value), []


def evidence_url(evidence: object) -> str | None:
    """The first https URL in ``evidence`` (already redacted text), or None."""
    match = _URL.search(evidence) if isinstance(evidence, str) else None
    return match.group(0) if match else None

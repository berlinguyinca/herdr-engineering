"""The optional services the dashboard Settings page lists, as data.

Single source of truth for ``herdr-eng settings``; a test parses ``packages/service-catalog.js`` and asserts the
capability -> config key pairs match, so the two cannot drift. Only key NAMES live here, never configuration values.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Service:
    capability: str
    label: str
    config_key: str  # dotted path in the central fleet-config.yaml; "" when always on
    always_on: bool = False
    needs_values: bool = False  # the key takes more than a boolean flag: set it by hand


def _s(capability: str, label: str, key: str, needs_values: bool = False) -> Service:
    return Service(capability, label, key, needs_values=needs_values)


def _on(capability: str, label: str) -> Service:
    return Service(capability, label, "", always_on=True)


SERVICES: tuple[Service, ...] = (
    _on("collab_v1", "Collaboration rooms"),
    _on("ci_v1", "CI monitor"),
    _on("github_v1", "GitHub"),
    _on("mission_activity_v1", "Mission activity"),
    _on("plannotator_reviews_v1", "Plan reviews"),
    _s("ci_actions_v1", "CI Retry and Cancel", "control_plane.ci_actions", True),
    _s("deployments_v1", "Deployments page", "control_plane.deployment_definitions", True),
    _s("deployment_actions_v1", "Deployment actions", "control_plane.deployment_actions", True),
    _s("review_gate_v1", "Review gate", "control_plane.review_gate.enabled"),
    _s("share_v1", "Artifact sharing", "control_plane.share.enabled"),
    _s("members_v1", "Member artifacts", "control_plane.collab.members.enabled"),
    _s("collab_artifacts_v1", "Artifact collaborators", "control_plane.collab_artifacts.enabled"),
    _s("artifact_delete_v1", "Artifact delete", "control_plane.artifact_delete.owner_key_digests", True),
    _s("assistant_v1", "Personal assistant", "control_plane.personal_assistant.enabled"),
    _s("assistant_team_briefs", "Assistant team briefs", "control_plane.personal_assistant.team_briefs.enabled"),
    _s("assistant_external_actions", "Assistant external actions",
       "control_plane.personal_assistant.external_actions.enabled"),
    _s("unified_workspace_v1", "Teams and team map", "control_plane.unified_workspace.enabled"),
    _s("project_intelligence_v1", "Project intelligence", "control_plane.project_intelligence.enabled"),
    _s("memory_v1", "Memory", "control_plane.memory.enabled"),
    _s("architecture_v1", "Architecture", "control_plane.architecture.enabled"),
    _s("architecture_explain_v1", "Architecture explain", "control_plane.architecture.explain", True),
    _s("skill_registry_v1", "Skill registry", "control_plane.skill_registry", True),
    _s("build_cache_v1", "Build cache", "control_plane.build_cache.enabled"),
)

BY_CAPABILITY: dict[str, Service] = {s.capability: s for s in SERVICES}


def safe_services() -> tuple[Service, ...]:
    """Services `enable --all-safe` may switch on: a plain boolean flag, not always on."""
    return tuple(s for s in SERVICES if not s.always_on and not s.needs_values)

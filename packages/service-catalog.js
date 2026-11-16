// Every optional service the dashboard knows about: the capability /api/v1/health?probe=1 advertises while it works, a label,
// and the YAML key (central fleet config, never an environment variable) that turns it on. Settings lists these; a service
// that is off has no link anywhere else. Only key NAMES live here, never configuration values.
export const SERVICES = [
  { capability: 'collab_v1', group: 'Collaboration', label: 'Collaboration rooms', description: 'Rooms, messages and handoffs between agents and people.', alwaysOn: true, configKey: '' },
  { capability: 'ci_v1', group: 'Delivery', label: 'CI monitor', description: 'Pipeline status from the CI provider.', alwaysOn: true, configKey: '' },
  { capability: 'github_v1', group: 'Delivery', label: 'GitHub', description: 'Issues, boards and pull request trails.', alwaysOn: true, configKey: '' },
  { capability: 'mission_activity_v1', group: 'Monitor', label: 'Mission activity', description: 'The activity feed for missions.', alwaysOn: true, configKey: '' },
  { capability: 'plannotator_reviews_v1', group: 'Delivery', label: 'Plan reviews', description: 'Plan review annotations.', alwaysOn: true, configKey: '' },
  { capability: 'ci_actions_v1', group: 'Delivery', label: 'CI Retry and Cancel', description: 'Operators can retry or cancel pipelines from the dashboard.', configKey: 'control_plane.ci_actions' },
  { capability: 'deployments_v1', group: 'Delivery', label: 'Deployments page', description: 'Staged rollouts, read-only.', configKey: 'control_plane.deployment_definitions' },
  { capability: 'deployment_actions_v1', group: 'Delivery', label: 'Deployment actions', description: 'Operator actions on staged rollouts.', configKey: 'control_plane.deployment_actions' },
  { capability: 'review_gate_v1', group: 'Delivery', label: 'Review gate', description: 'The pull request review gate and its queue.', configKey: 'control_plane.review_gate.enabled' },
  { capability: 'share_v1', group: 'Artifacts', label: 'Artifact sharing', description: 'Share links for artifacts.', configKey: 'control_plane.share.enabled' },
  { capability: 'members_v1', group: 'Artifacts', label: 'Member artifacts', description: 'Operator controls for member-owned artifacts.', configKey: 'control_plane.collab.members.enabled' },
  { capability: 'collab_artifacts_v1', group: 'Artifacts', label: 'Artifact collaborators', description: 'Collaborator grants, activity and diffs on artifacts.', configKey: 'control_plane.collab_artifacts.enabled' },
  { capability: 'artifact_delete_v1', group: 'Artifacts', label: 'Artifact delete', description: 'Delete, restore and purge artifacts.', configKey: 'control_plane.artifact_delete.owner_key_digests' },
  { capability: 'assistant_v1', group: 'Agents', label: 'Personal assistant', description: 'Assistants, grants and proposals.', configKey: 'control_plane.personal_assistant.enabled' },
  { capability: 'assistant_team_briefs', group: 'Agents', label: 'Assistant team briefs', description: 'Briefs shared with a team.', configKey: 'control_plane.personal_assistant.team_briefs.enabled' },
  { capability: 'assistant_external_actions', group: 'Agents', label: 'Assistant external actions', description: 'Actions the assistant files outside the fabric.', configKey: 'control_plane.personal_assistant.external_actions.enabled' },
  { capability: 'unified_workspace_v1', group: 'Agents', label: 'Teams and team map', description: 'The unified workspace and its work graph.', configKey: 'control_plane.unified_workspace.enabled' },
  { capability: 'project_intelligence_v1', group: 'Code', label: 'Project intelligence', description: 'Repository and pull request intelligence.', configKey: 'control_plane.project_intelligence.enabled' },
  { capability: 'memory_v1', group: 'Code', label: 'Memory', description: 'Shared agent memory.', configKey: 'control_plane.memory.enabled' },
  { capability: 'architecture_v1', group: 'Code', label: 'Architecture', description: 'The architecture map.', configKey: 'control_plane.architecture.enabled' },
  { capability: 'architecture_explain_v1', group: 'Code', label: 'Architecture explain', description: 'Model-written explanations of the architecture map.', configKey: 'control_plane.architecture.explain' },
  { capability: 'skill_registry_v1', group: 'Code', label: 'Skill registry', description: 'Published agent skills.', configKey: 'control_plane.skill_registry' },
  { capability: 'build_cache_v1', group: 'Fabric', label: 'Build cache', description: 'Build cache statistics.', configKey: 'control_plane.build_cache.enabled' },
];

const advertised = (capabilities) => new Set(Array.isArray(capabilities) ? capabilities : []);

export function describeServices(capabilities) {
  const on = advertised(capabilities);
  return SERVICES.map((service) => {
    const enabled = on.has(service.capability);
    return { ...service, alwaysOn: Boolean(service.alwaysOn), enabled, howToEnable: enabled || service.alwaysOn ? '' : service.configKey };
  });
}

// WC-006 rollout gate. The control plane advertises `work_center_v1` in /api/v1/health `capabilities` only when
// `control_plane.work_center.enabled: true` (YAML, default off). Every Work Command Center surface (the Overview "This week"
// card, the session Work Context header, the GitHub board "Open in Herdr" links) is gated by this one predicate.
export const WORK_CENTER_CAPABILITY = 'work_center_v1';
export const workCenterEnabled = (capabilities) => Array.isArray(capabilities) && capabilities.includes(WORK_CENTER_CAPABILITY);

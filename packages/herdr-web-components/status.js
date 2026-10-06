// herdr-web-components — status metadata (Spec §9-10).
//
// Pure, DOM-free module so it is unit-testable with `node --test`. Maps the
// canonical mission stages and generic statuses onto a semantic color token
// AND a human label. The label/icon is the primary signal; color is always
// secondary (never color-alone — a11y, Spec §88).

// Generic lifecycle statuses -> semantic token + label.
export const STATUS_META = Object.freeze({
  healthy: { token: '--status-ok', label: 'Healthy' },
  complete: { token: '--status-ok', label: 'Complete' },
  active: { token: '--status-active', label: 'Active' },
  running: { token: '--status-active', label: 'Running' },
  waiting: { token: '--status-waiting', label: 'Waiting' },
  attention: { token: '--status-attention', label: 'Needs attention' },
  blocked: { token: '--status-blocked', label: 'Blocked' },
  failed: { token: '--status-failed', label: 'Failed' },
  critical: { token: '--status-failed', label: 'Critical' },
  planning: { token: '--status-planning', label: 'Planning' },
  specification: { token: '--status-planning', label: 'Specification' },
  ready: { token: '--status-ready', label: 'Ready' },
  implementation: { token: '--status-active', label: 'Implementation' },
  testing: { token: '--status-testing', label: 'Testing' },
  review: { token: '--status-review', label: 'Review' },
  repair: { token: '--status-repair', label: 'Repair' },
  validation: { token: '--status-validation', label: 'Validation' },
  pr: { token: '--status-pr', label: 'PR' },
  cancelled: { token: '--status-waiting', label: 'Cancelled' },
  unavailable: { token: '--status-waiting', label: 'Unavailable' },
  stopped: { token: '--status-waiting', label: 'Stopped' },
  unknown: { token: '--status-waiting', label: 'Unknown' },
});

// Mission stage -> status lookup (Spec §10). Unknown stages map to Unknown.
export function statusForStage(stage) {
  return STATUS_META[String(stage || 'unknown').toLowerCase()] ||
    STATUS_META.unknown;
}

export function statusLabel(stage) {
  return statusForStage(stage).label;
}

// Resolve a CSS custom-property token to its variable reference, e.g.
// '--status-ok' -> 'var(--status-ok)'. Used by the <herdr-status> component.
export function tokenVar(token) {
  return token ? `var(${token})` : 'var(--status-waiting)';
}

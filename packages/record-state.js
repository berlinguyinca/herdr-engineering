// Shared lifecycle classification for mission/session tabs. Freshness is separate:
// missing or stale telemetry never implies a terminal outcome.
const normalize = value => typeof value === 'string' ? value.trim().toLowerCase().replaceAll('-', '_').replaceAll(' ', '_') : '';
const failed = new Set(['failed', 'error', 'crashed']);
const finished = new Set(['finished', 'complete', 'completed', 'closed', 'stopped', 'canceled', 'cancelled', 'succeeded', 'done', 'archived', 'merged']);
const current = new Set(['active', 'running', 'executing', 'pending', 'queued', 'planned', 'planning', 'implementing', 'testing', 'reviewing', 'waiting', 'blocked', 'stalled', 'paused', 'open', 'idle', 'starting', 'created', 'ready', 'in_progress']);
const terminal = status => failed.has(status) ? 'failed' : finished.has(status) ? 'finished' : null;

export function statusOutcome(status, stage) {
  const state = normalize(status), phase = normalize(stage);
  // A declared terminal lifecycle wins over any leftover activity/stage. Generic
  // active status may coexist with a more specific terminal lifecycle stage.
  const outcome = terminal(state);
  if (outcome) return outcome;
  if (!state || state === 'active' || state === 'unknown') {
    const stageOutcome = terminal(phase);
    if (stageOutcome) return stageOutcome;
  }
  return current.has(state) || state.startsWith('paused_') || (!state && current.has(phase)) ? 'current' : 'unknown';
}

export function recordOutcome(record = {}) {
  return statusOutcome(record?.status, record?.stage);
}

// Shared lifecycle classification for mission/session tabs. Freshness is separate:
// missing or stale telemetry never implies a terminal outcome.
const normalize = value => typeof value === 'string' ? value.trim().toLowerCase().replaceAll('-', '_').replaceAll(' ', '_') : '';
const failed = new Set(['failed', 'error', 'crashed']);
const finished = new Set(['finished', 'complete', 'completed', 'closed', 'stopped', 'canceled', 'cancelled', 'succeeded', 'done', 'archived', 'merged']);
const current = new Set(['active', 'working', 'busy', 'running', 'executing', 'pending', 'queued', 'planned', 'planning', 'specification', 'implementation', 'review', 'repair', 'validation', 'pr', 'implementing', 'testing', 'reviewing', 'waiting', 'blocked', 'stalled', 'paused', 'open', 'idle', 'starting', 'created', 'ready', 'in_progress']);
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

// Match the service reporter's 15 minute grace period. This classifies display
// visibility only; an expired observation never becomes a stopped/failed record.
export const REPORT_FRESH_MS = 15 * 60 * 1000;
export function reportTimestamp(value) {
  if (typeof value === 'number') return Number.isFinite(value) && value > 0 ? (value < 1e11 ? value * 1000 : value) : NaN;
  // Require an explicit timezone so host/browser timezone cannot change freshness.
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/i.test(value)) return NaN;
  return Date.parse(value);
}
export function reportFreshness(timestamp, now = Date.now(), maxAgeMs = REPORT_FRESH_MS) {
  const at = reportTimestamp(timestamp);
  // A small skew allowance avoids flicker between distributed clocks.
  if (!Number.isFinite(at) || !Number.isFinite(now) || at > now + 5000 || !Number.isFinite(maxAgeMs) || maxAgeMs < 0) return 'unknown';
  return now - at > maxAgeMs ? 'stale' : 'current';
}
export function recordVisibility(record = {}, now = Date.now()) {
  const outcome = recordOutcome(record);
  if (outcome === 'failed' || outcome === 'finished') return outcome;
  return outcome === 'current' && reportFreshness(record?.last_seen_at ?? record?.updated_at, now) === 'current' ? 'current' : 'unconfirmed';
}

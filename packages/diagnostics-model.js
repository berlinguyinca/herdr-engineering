// The diagnostics view model (H02): the shape of host/session identity, incidents, timeline events and drift decisions.
// No API, no rendering, no DOM, no clock, no I/O — and nothing invented: a record that cannot be read says it cannot be read.

export const SEVERITIES = ['info', 'warning', 'error', 'critical'];   // the only severities a record may carry; anything else is invalid
const INCIDENT_KEYS = ['id', 'kind', 'severity', 'hostId', 'sessionId', 'message', 'at'];
const EVENT_KEYS = ['id', 'missionKey', 'at', 'eventType', 'hostId', 'sessionId', 'payload'];
const UNUSABLE = 'drift is undecidable: the numbers given are unusable';
const text = (v) => (typeof v === 'string' ? v : '');
const finite = (v) => typeof v === 'number' && Number.isFinite(v);
const plain = (v) => typeof v === 'object' && v !== null && !Array.isArray(v);
const extraKeys = (raw, allowed) => Object.keys(raw).filter((k) => !allowed.includes(k)).map((k) => `unknown key ${k}`);
const refs = (raw, errors) => ['hostId', 'sessionId'].forEach((k) => { if (raw[k] !== undefined && !text(raw[k])) errors.push(`${k} must be a non-empty string when present`); });

/** An incident: identity, a kind, a severity from the fixed vocabulary — and no key that could smuggle in rendering or I/O. */
export function normalizeIncident(raw) {
  if (!plain(raw)) return { ok: false, errors: ['incident must be a plain object'] };
  const errors = extraKeys(raw, INCIDENT_KEYS);
  if (!text(raw.id)) errors.push('id must be a non-empty string');
  if (!text(raw.kind)) errors.push('kind must be a non-empty string');
  if (!SEVERITIES.includes(raw.severity)) errors.push(`severity must be one of ${SEVERITIES.join('|')}`);
  if (raw.at !== undefined && !finite(raw.at)) errors.push('at must be a finite number when present');
  refs(raw, errors);
  return errors.length ? { ok: false, errors } : { ok: true, incident: { ...raw } };
}

/**
 * One point on the timeline: identity, when it happened (a number, never a clock reading), what kind of thing it
 * is, and — only when the feed binds one — the mission it belongs to. missionKey is optional because host-scoped
 * records carry none: measured on the real control plane, `GET /api/v1/events?entity_type=host&entity_id=hive`
 * answers envelopes with `mission_id: null` (recorded in tests/fixtures/diagnostics_events_host.json), so a
 * required mission would discard every record the host diagnostics page can read. A mission filter still
 * excludes a mission-less record; it is simply not identity.
 */
export function normalizeEvent(raw) {
  if (!plain(raw)) return { ok: false, errors: ['event must be a plain object'] };
  const errors = extraKeys(raw, EVENT_KEYS);
  if (!text(raw.id)) errors.push('id must be a non-empty string');
  if (raw.missionKey !== undefined && !text(raw.missionKey)) errors.push('missionKey must be a non-empty string when present');
  if (!finite(raw.at)) errors.push('at must be a finite number');
  if (!text(raw.eventType)) errors.push('eventType must be a non-empty string');
  if (raw.payload !== undefined && !plain(raw.payload)) errors.push('payload must be a plain object when present');
  refs(raw, errors);
  return errors.length ? { ok: false, errors } : { ok: true, event: { ...raw } };
}

/** Filter and order events by { missionKey, kinds, since, until }. A record that fails normalization is dropped, never coerced, and
 * counted — `invalid` failed validation, `dropped` was excluded while valid, so nothing goes missing silently. Events sharing an
 * instant keep the order they arrived in: the tiebreaker is input position, never the id or the payload. */
export function buildTimeline(items, filter = {}) {
  const kinds = Array.isArray(filter?.kinds) ? filter.kinds : null;
  const within = (e) => (filter?.missionKey ? e.missionKey === filter.missionKey : true) && (!kinds || kinds.includes(e.eventType)) && (!finite(filter?.since) || e.at >= filter.since) && (!finite(filter?.until) || e.at <= filter.until);
  const rows = []; let invalid = 0;
  for (const [position, raw] of (Array.isArray(items) ? items : []).entries()) { const result = normalizeEvent(raw); if (!result.ok) invalid += 1; else rows.push({ event: result.event, position, kept: within(result.event) }); }
  const timeline = rows.filter((r) => r.kept).sort((a, b) => a.event.at - b.event.at || a.position - b.position).map((r) => r.event);
  return { timeline, dropped: rows.length - timeline.length, invalid };
}

/** Compare two numbers only when both can be read; an unreadable comparison says so instead of reporting drift either way. */
export function decideDrift(record = {}) {
  const { expected, actual, tolerance } = plain(record) ? record : {};
  if (!finite(expected) || !finite(actual)) return { drifted: false, reason: UNUSABLE };
  const allowed = tolerance === undefined ? 0 : tolerance;
  if (!finite(allowed) || allowed < 0) return { drifted: false, reason: 'drift is undecidable: the tolerance is unusable (a finite number at or above zero is needed)' };
  const delta = Math.abs(actual - expected);
  if (delta > allowed) return { drifted: true, reason: `actual is ${delta} away from expected, above the tolerance of ${allowed}` };
  return { drifted: false, reason: `actual is within the tolerance of ${allowed}` };
}

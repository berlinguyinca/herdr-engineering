/** WC-001 pure work projection. No API writes. All statuses are evidence-based. */
export function workKey(source, value) {
  const id = String(value ?? '').trim();
  if (!id || !['mission', 'session', 'github'].includes(source)) return null;
  return `${source}:${id}`;
}
export function fromMission(m) {
  const id = String(m.mission_key || m.mission_id || '').trim();
  if (!id) return null;
  return {
    key: workKey('mission', id), source: 'mission', sourceId: id,
    title: String(m.title || m.name || id),
    startedAt: m.created_at || null, completedAt: m.completed_at || null,
    updatedAt: m.updated_at || null,
    state: normalizeState(m.status || m.outcome),
    sessionIds: (Array.isArray(m.sessions) ? m.sessions : []).map(x => typeof x === 'string' ? x : x?.session_id).filter(Boolean),
    progress: measuredProgress(m.activity || m),
    href: `#/missions/${encodeURIComponent(id)}`,
  };
}
export function fromSession(s) {
  const id = String(s.session_id || '').trim();
  if (!id) return null;
  return {
    key: workKey('session', id), source: 'session', sourceId: id,
    title: String(s.display_name || s.agent_role || s.summary || id),
    startedAt: s.started_at || s.created_at || null,
    completedAt: s.ended_at || null, updatedAt: s.updated_at || null,
    state: normalizeState(s.status), missionId: s.mission_key || s.mission_id || null,
    progress: null,
    href: `#/herd/session/${encodeURIComponent(id)}`,
  };
}
export function normalizeState(value) {
  const s = String(value || '').toLowerCase();
  if (/(quota|usage.?limit)/.test(s)) return 'quota_paused';
  if (/(blocked|waiting|paused)/.test(s)) return 'blocked';
  if (/(done|complete|finished|merged|closed|success)/.test(s)) return 'finished';
  if (/(fail|error|cancel)/.test(s)) return 'failed';
  if (/(running|active|working|doing|in.progress)/.test(s)) return 'running';
  return 'unconfirmed';
}
export function measuredProgress(activity = {}) {
  // Mirrors the acceptanceCoverage / weighted_dag rules in mission-catalog-view.js.
  const valid = v => v && Number.isFinite(v.completed) && Number.isFinite(v.total) && v.total > 0 && v.completed >= 0 && v.completed <= v.total;
  const c = activity.acceptanceCoverage;
  if (valid(c)) return {percent: Math.floor(c.completed / c.total * 100), basis: 'acceptance_coverage'};
  const w = activity.workflowProgress;
  if (w?.basis === 'weighted_dag' && valid(w)) return {percent: Math.floor(w.completed / w.total * 100), basis: 'weighted_dag'};
  return null;
}
/** Group session activity under a linked mission. Unlinked sessions remain separate. */
export function projectWork({missions = [], sessions = []} = {}) {
  const map = new Map();
  for (const raw of missions) { const m = fromMission(raw); if (m) map.set(m.key, m); }
  for (const raw of sessions) {
    const s = fromSession(raw); if (!s) continue;
    const parent = s.missionId ? map.get(workKey('mission', s.missionId)) : null;
    if (parent) {
      if (!parent.sessionIds.includes(s.sourceId)) parent.sessionIds.push(s.sourceId);
    } else map.set(s.key, s);
  }
  return [...map.values()];
}
function within(stamp, start, end) { const ms = Date.parse(stamp || ''); return Number.isFinite(ms) && ms >= start && ms < end; }
export function weeklySummary(items, startInclusive, endExclusive) {
  const start = Date.parse(startInclusive), end = Date.parse(endExclusive);
  if (!(Number.isFinite(start) && Number.isFinite(end) && start < end)) throw new TypeError('Valid interval required');
  const rows = Array.isArray(items) ? items : [];
  return {
    started: rows.filter(i => within(i.startedAt, start, end)).length,
    finished: rows.filter(i => i.state === 'finished' && within(i.completedAt, start, end)).length,
    open: rows.filter(i => !['finished','failed'].includes(i.state)).length,
    paused: rows.filter(i => i.state === 'quota_paused' || i.state === 'blocked').length,
    unconfirmed: rows.filter(i => i.state === 'unconfirmed').length,
  };
}

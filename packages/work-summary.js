// WC-001: read-only WorkSummary v1 adapter. Reuses missionEntries/measuredProgress (mission-catalog-view),
// associatedMission/recordVisibility (sessions-view/record-state) and entityHref (entity-links); it never
// correlates by title and never invents progress, lifecycle or completion.
import { missionEntries, measuredProgress } from './mission-catalog-view.js';
import { associatedMission } from './sessions-view.js';
import { recordOutcome, recordVisibility } from './record-state.js';
import { entityHref } from './entity-links.js';

const text = v => typeof v === 'string' ? v.trim() : '';
const records = v => Array.isArray(v) ? v.filter(x => x && typeof x === 'object' && !Array.isArray(x)) : [];
const stamp = v => text(v) || undefined;
const compact = o => Object.fromEntries(Object.entries(o).filter(([, v]) => v !== undefined && v !== null));
const link = (kind, id) => { const href = entityHref(kind, id); return href ? {kind, entity_id: String(id), href} : null; };

// G8: Pi's own "nothing scheduled" marker (MissionObservability.ts) is an absence, never a next action.
const NO_NEXT_ACTION = 'no further action is scheduled';
const NEXT_ACTION_MAX = 500;
/** @returns {{text:string, at?:string}|undefined} only when Pi supplied non-empty text that is not its absence marker; never inferred. */
export function piNextAction(activity) {
  const t = text(activity?.nextAction);
  if (!t || t.toLowerCase() === NO_NEXT_ACTION) return undefined;
  const at = text(activity.nextActionAt);
  return {text: t.slice(0, NEXT_ACTION_MAX), ...(at && Number.isFinite(Date.parse(at)) ? {at} : {})};
}

const PI_TIMES = [['created_at', 'pi_reported_created'], ['completed_at', 'pi_reported_completed']];
function piTimes(projection) {
  const m = projection?.mission;
  return PI_TIMES.map(([field, kind]) => {
    const at = text(m?.[field]);
    return at && Number.isFinite(Date.parse(at)) ? {kind, ref: text(m.id) || 'mission', timestamp: at} : null;
  }).filter(Boolean);
}

function missionLifecycle(entry) {
  if (entry.outcome === 'failed' || entry.outcome === 'finished') return entry.outcome;
  if (entry.tab !== 'current') return 'unconfirmed'; // stale / disconnected / recorded-only is not live
  if (/QUOTA/.test(entry.status)) return 'paused_quota';
  return ['blocked', 'stalled'].includes(entry.health) ? 'blocked' : 'running';
}
function sessionLifecycle(record, now) {
  const outcome = recordOutcome(record);
  if (outcome === 'failed' || outcome === 'finished') return outcome;
  return recordVisibility(record, now) === 'current' ? 'running' : 'unconfirmed';
}
function paneRef(s) {
  const ref = {host_id: text(s.host_id), herdr_session: text(s.herdr_session), pane_id: text(s.pane_id), terminal_id: text(s.terminal_id), session_id: text(s.session_id)};
  return ref.host_id && ref.herdr_session && ref.terminal_id ? compact(Object.fromEntries(Object.entries(ref).map(([k, v]) => [k, v || null]))) : null;
}

/** @returns {object[]} WorkSummary v1 items: one per mission, plus one per session that does not belong unambiguously to a mission. */
export function workSummaries({projections = [], recorded = [], sessions = [], received = Date.now(), now = Date.now()} = {}) {
  const entries = missionEntries(projections, recorded, received, now);
  const candidates = entries.map(e => ({
    mission_key: e.key, mission_id: text(e.recorded?.mission_id) || text(e.projection?.mission?.id), title: e.title,
    host_id: e.host, sessions: e.recorded?.sessions,
  }));
  const items = new Map(entries.map(e => {
    const a = e.projection?.activity || {}, progress = measuredProgress(a);
    return [e.key, {
      work_id: `mission:${e.key}`, title: e.title, source: 'mission', source_ref: e.key, mission_key: e.key, session_ids: [], sessionHosts: [],
      pane_refs: [], links: [link('mission', e.key)].filter(Boolean), lifecycle: missionLifecycle(e),
      evidence: [e.projection && {kind: 'mission_projection', ref: e.key, timestamp: stamp(e.projection.activity_snapshot_at ?? e.projection.snapshot_at)},
        // G8: contract-v4 times are Pi's own claim. They are evidence only: never started_at/completed_at, never used to
        // assert history coverage (a mission Pi no longer reports still has no row here), so the G1 coverage rules are unchanged.
        ...piTimes(e.projection),
        e.recorded && {kind: 'mission_record', ref: e.key, timestamp: stamp(e.recorded.updated_at)},
        missionLifecycle(e) === 'paused_quota' && {kind: 'quota_status', ref: e.key, timestamp: stamp(e.projection?.activity_snapshot_at ?? e.projection?.snapshot_at ?? e.recorded?.updated_at)}].filter(Boolean),
      ...(progress ? {progress: {percent: progress.percent, label: progress.label, basis: 'measuredProgress', measured_at: stamp(e.projection?.activity_snapshot_at ?? e.projection?.snapshot_at)}} : {}),
      ...compact({started_at: stamp(e.recorded?.created_at), completed_at: e.outcome === 'finished' ? stamp(e.recorded?.completed_at) : undefined, updated_at: stamp(e.recorded?.updated_at)}),
      ...(e.projection && piNextAction(a) ? {nextAction: piNextAction(a)} : {}),
      coverage: e.projection && e.tab === 'current' ? 'complete' : 'unconfirmed',
    }];
  }));

  const idHosts = new Map(); // bare session_id -> distinct hosts, to detect ambiguous IDs
  for (const s of records(sessions)) if (text(s.session_id)) idHosts.set(s.session_id, new Set([...(idHosts.get(s.session_id) || []), text(s.host_id)]));
  const seen = new Set(), standalone = [];
  for (const s of records(sessions)) {
    const id = text(s.session_id);
    if (!id || seen.has(`${text(s.host_id)}\u0000${id}`)) continue;
    seen.add(`${text(s.host_id)}\u0000${id}`);
    const hasRef = Boolean(text(s.mission_id) || text(s.mission_key));
    const ambiguousId = idHosts.get(id).size > 1;
    const mission = hasRef && !(ambiguousId && !text(s.host_id)) ? associatedMission(s, candidates) : null;
    const parent = mission && items.get(mission.key);
    const pane = paneRef(s);
    if (parent) {
      if (!parent.session_ids.includes(id)) parent.session_ids.push(id);
      if (pane) parent.pane_refs.push(pane);
      parent.evidence.push({kind: 'session_report', ref: id, timestamp: stamp(s.last_seen_at ?? s.updated_at)});
      const l = link('session', id); if (l && !parent.links.some(x => x.href === l.href)) parent.links.push(l);
      if (ambiguousId) parent.coverage = 'partial';
      continue;
    }
    const sref = text(s.host_id) ? `${s.host_id}/${id}` : id;
    standalone.push(compact({
      work_id: `session:${sref}`, title: text(s.display_name) || text(s.agent_role) || id, source: 'session', source_ref: id,
      mission_key: undefined, session_ids: [id], pane_refs: pane ? [pane] : [], links: [link('session', id)].filter(Boolean),
      lifecycle: sessionLifecycle(s, now), evidence: [{kind: 'session_report', ref: id, timestamp: stamp(s.last_seen_at ?? s.updated_at)}],
      started_at: stamp(s.started_at ?? s.created_at), updated_at: stamp(s.updated_at),
      // A mission reference that did not resolve (unknown or ambiguous) is partial; an agent with no reference is simply unassociated.
      unassociated: true, coverage: hasRef || ambiguousId ? 'partial' : 'unconfirmed',
    }));
  }
  return [...items.values()].map(({sessionHosts, ...w}) => w).concat(standalone);
}

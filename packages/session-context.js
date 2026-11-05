// WC-004: "Work Context" header for the session page. A read-only view over workSummaries (WC-001): the parent
// mission is whatever the existing ID-based association resolves, never a title match. Anything unresolved,
// stale or ambiguous renders as an explicit "Not linked to a mission" state. No endpoint, credential or action is added.
import { workSummaries } from './work-summary.js';
import { entityHref } from './entity-links.js';
import { escapeHtml as esc } from './herdr-web-components/util.js';

const text = v => typeof v === 'string' ? v.trim() : '';
const SIBLING_LIMIT = 8;
const LIFECYCLE = {running: 'running', paused_quota: 'paused (quota)', blocked: 'blocked', review: 'in review', finished: 'finished', failed: 'failed', unconfirmed: 'unconfirmed'};
const SOURCE_NAME = {projections: 'mission activity', missions: 'missions', sessions: 'agents'};
const label = s => text(s?.display_name) || text(s?.agent_role) || text(s?.session_id) || 'Agent';

/** @returns {object} {state: 'linked'|'unlinked'|'unavailable', ...} */
export function sessionContextModel({session, projections = [], recorded = [], sessions = [], received = Date.now(), now = Date.now(), failed = []} = {}) {
  const id = text(session?.session_id);
  const base = {sessionId: id, failed};
  if (!id) return {...base, state: 'unavailable'};
  if (failed.includes('projections') && failed.includes('missions')) return {...base, state: 'unavailable'};
  const all = Array.isArray(sessions) ? sessions : [];
  const withSelf = all.some(s => s?.session_id === id && text(s.host_id) === text(session.host_id)) ? all : [session, ...all];
  const items = workSummaries({projections, recorded, sessions: withSelf, received, now});
  const owners = items.filter(i => i.source === 'mission' && i.session_ids.includes(id));
  const hasRef = Boolean(text(session.mission_id) || text(session.mission_key));
  if (owners.length !== 1) {
    return {...base, state: 'unlinked', reason: hasRef || owners.length > 1 ? 'unconfirmed' : 'none'};
  }
  const parent = owners[0], byId = new Map(withSelf.map(s => [s.session_id, s]));
  const siblingIds = parent.session_ids.filter(x => x !== id);
  return {
    ...base, state: 'linked',
    mission: {title: parent.title, href: entityHref('mission', parent.mission_key), lifecycle: parent.lifecycle, coverage: parent.coverage},
    ...(parent.progress && Number.isFinite(parent.progress.percent) ? {progress: parent.progress} : {}),
    siblings: siblingIds.slice(0, SIBLING_LIMIT).map(sid => ({id: sid, label: label(byId.get(sid)), href: entityHref('session', sid)})),
    moreSiblings: Math.max(0, siblingIds.length - SIBLING_LIMIT),
  };
}

export const sessionContextStyles = `<style>.wcx{margin:14px 0;padding:12px 14px;border:1px solid var(--border,#8884);border-radius:8px;overflow-wrap:anywhere}.wcx h2{font-size:15px;margin:0 0 8px}
.wcx dl{display:grid;grid-template-columns:minmax(90px,auto) minmax(0,1fr);gap:6px 14px;margin:0}.wcx dt{color:var(--text-secondary)}.wcx dd{margin:0}.wcx ul{margin:0;padding-left:18px}
.wcx-progress{display:block;width:100%;max-width:260px;height:8px;margin-top:4px;accent-color:var(--accent)}.wcx-note{color:var(--text-secondary);font-size:13px;margin:6px 0 0}
.wcx[data-state=unlinked]{border-style:dashed}.wcx-state{font-weight:600}@media(max-width:600px){.wcx dl{display:block}.wcx dd{margin:2px 0 10px}}</style>`;

function failedNote(failed) {
  const names = failed.map(k => SOURCE_NAME[k] || k);
  return names.length ? `<p class="wcx-note" role="status">Some sources could not be read (${names.map(esc).join(', ')}); the association may be incomplete.</p>` : '';
}

/** `model` is null while loading. */
export function renderSessionContext(model) {
  const wrap = (state, body) => `${sessionContextStyles}<section class="wcx" data-work-context data-state="${state}" aria-label="Work context"><h2>Work context</h2>${body}</section>`;
  if (!model) return wrap('loading', '<p class="wcx-note" role="status">Loading work context…</p>');
  if (model.state === 'unavailable') return wrap('unavailable', '<p class="wcx-note" role="status">Work context could not be read, so the parent mission is unknown. Terminal and agent details below are unaffected.</p>');
  if (model.state === 'unlinked') {
    const why = model.reason === 'unconfirmed'
      ? 'This agent names a mission, but the reference could not be confirmed (it is unknown, stale or ambiguous), so no mission is shown.'
      : 'This agent does not report a mission.';
    return wrap('unlinked', `<p class="wcx-state" data-unlinked>Not linked to a mission</p><p class="wcx-note">${why}</p>${failedNote(model.failed || [])}`);
  }
  const m = model.mission, p = model.progress;
  const percent = p ? Math.max(0, Math.min(100, Math.round(p.percent))) : null;
  const rows = [
    `<dt>Mission</dt><dd>${m.href ? `<a href="${esc(m.href)}" data-mission-link>${esc(m.title)}</a>` : esc(m.title)} <span class="wcx-note">${esc(LIFECYCLE[m.lifecycle] || m.lifecycle)}${m.coverage !== 'complete' ? ' · not fully confirmed' : ''}</span></dd>`,
    percent === null ? '' : `<dt>Progress</dt><dd><progress class="wcx-progress" max="100" value="${percent}" aria-label="${esc(`Measured progress for ${m.title}: ${p.label || `${percent}%`}`)}">${percent}%</progress><span class="wcx-note">${esc(p.label || `${percent}%`)}</span></dd>`,
    model.siblings.length ? `<dt>Other agents</dt><dd><ul>${model.siblings.map(s => `<li>${s.href ? `<a href="${esc(s.href)}" data-sibling-link>${esc(s.label)}</a>` : esc(s.label)}</li>`).join('')}</ul>${model.moreSiblings ? `<span class="wcx-note">+${model.moreSiblings} more</span>` : ''}</dd>` : '',
  ].join('');
  return wrap('linked', `<dl>${rows}</dl>${failedNote(model.failed || [])}`);
}

/** Fetches the three existing read-only lists and draws into `host`; returns a disposer. A failure shows its own state, never "unlinked". */
export function mountSessionContext(host, client, session, {signal, onLinked = () => {}} = {}) {
  if (!host) return () => {};
  let live = true;
  host.innerHTML = renderSessionContext(null);
  const read = (key, call) => call().then(value => ({key, value}), () => ({key, value: null, failed: true}));
  Promise.all([
    read('projections', () => client.listMissionActivity({signal})),
    read('missions', () => client.listMissions(undefined, {signal})),
    read('sessions', () => client.listSessions(undefined, {signal})),
  ]).then(results => {
    if (!live || !host.isConnected || signal?.aborted) return;
    const got = Object.fromEntries(results.map(r => [r.key, r.value]));
    const model = sessionContextModel({
      session, projections: got.projections?.activities || [], recorded: got.missions?.missions || [], sessions: got.sessions?.sessions || [],
      received: Date.now(), now: Date.now(), failed: results.filter(r => r.failed).map(r => r.key),
    });
    host.innerHTML = renderSessionContext(model);
    if (model.state === 'linked') onLinked(model);
  });
  return () => { live = false; };
}

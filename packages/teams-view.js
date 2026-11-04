// "Your teams" (unified workspace P06): the teams the signed-in person may see, each linking to its Team Map. A team is made on
// purpose (`herdr-eng teams create`); the people in its room are its members, so what is listed here is exactly what this person
// is allowed to see. Plain functions returning HTML so the states run under node; mountTeams adds the events.
//
// Gating: the graph routes answer 404 until control_plane.unified_workspace.enabled. A 404 is shown as a plain "not turned on"
// state, never an error, and does not mark the page unavailable.
import { escapeHtml as esc } from './herdr-web-components/util.js';
import { skeleton } from './motion.js';

const text = (v) => (typeof v === 'string' ? v : v == null ? '' : String(v));
const list = (v) => (Array.isArray(v) ? v : []);

/** The Team Map address for a team (the map's root reference is `team:<native id>`). */
export const teamMapHref = (team) => `#/team-map?root=${encodeURIComponent(`team:${text(team?.native_id)}`)}`;

/** { status: 'ok' | 'off' | 'unavailable' | 'error', items, hasMore, message } */
export async function loadTeams(client, { showArchived = false, signal, limit = 100 } = {}) {
  try {
    const page = await client.workGraphTeams({ status: showArchived ? 'all' : 'active', limit, signal });
    const items = list(page?.items).filter((t) => t && text(t.native_id) && !t.tombstone);
    return { status: 'ok', items, hasMore: Boolean(page?.has_more) };
  } catch (err) {
    if (err?.status === 404) return { status: 'off' };
    if (err?.status === 503) return { status: 'unavailable', message: text(err.message) };
    return { status: 'error', message: text(err?.message) || 'request failed' };
  }
}

const sortTeams = (items) => [...items].sort((a, b) => (a.status === 'archived') - (b.status === 'archived')
  || text(a.display_name).localeCompare(text(b.display_name)) || text(a.native_id).localeCompare(text(b.native_id)));

export function renderTeam(team) {
  const archived = team.status === 'archived';
  const name = text(team.display_name) || text(team.native_id);
  return `<li class="teams-card" data-team-id="${esc(team.native_id)}"><a class="teams-link" href="${esc(teamMapHref(team))}" aria-label="${esc(`${name}${archived ? ', archived' : ''}. Open its team map.`)}">`
    + `<span class="teams-name">${esc(name)}</span>`
    + `<span class="teams-meta">${archived ? '<span class="teams-chip">archived</span> ' : ''}${team.project_id ? `project ${esc(team.project_id)}` : 'no project linked'}</span></a></li>`;
}

/** The whole body for a state. Pure so the states can be tested without a page. */
export function renderTeams(state) {
  const msg = (kind, html) => `<p class="teams-state" data-teams-state="${kind}" role="status">${html}</p>`;
  if (state.phase === 'loading') return skeleton('teams', { rows: 3, height: 64 });
  if (state.phase === 'off') return msg('off', 'Teams are not turned on for this control plane. Nothing is wrong with your account or this page.');
  if (state.phase === 'unavailable') return msg('unavailable', `The work graph is not available right now${state.message ? ` (${esc(state.message)})` : ''}. Try again shortly.`);
  if (state.phase === 'error') return msg('error', `Your teams could not be loaded: ${esc(state.message || 'request failed')}`);
  const bar = `<div class="teams-bar"><label class="teams-check"><input type="checkbox" data-teams-archived${state.showArchived ? ' checked' : ''}> Show archived teams</label></div>`;
  if (!state.items.length) {
    return `${bar}${msg('empty', 'You are not in any team yet. Teams show up here as soon as one is linked to a room you are in. An operator makes one with <code>herdr-eng teams create "Team name" --room ROOM_ID</code>.')}`;
  }
  const more = state.hasMore ? '<p class="teams-note" data-teams-more>More teams exist than are shown here.</p>' : '';
  return `${bar}<ul class="teams-list" aria-label="Your teams">${sortTeams(state.items).map(renderTeam).join('')}</ul>${more}`;
}

export function mountTeams(container, client, { freshness = () => null } = {}) {
  const state = { phase: 'loading', items: [], hasMore: false, message: '', showArchived: false };
  let disposed = false, controller = null;
  container.innerHTML = '<section class="teams" data-teams></section>';
  const body = container.querySelector('[data-teams]');
  const paint = () => {
    const active = typeof document === 'undefined' ? null : document.activeElement;
    const hadFocus = Boolean(active && body.contains(active) && active.matches?.('[data-teams-archived]'));
    body.innerHTML = renderTeams(state);
    if (hadFocus) body.querySelector('[data-teams-archived]')?.focus();
  };

  async function load() {
    if (disposed) return;
    controller?.abort();
    const request = controller = new AbortController();
    const signal = AbortSignal.any([request.signal, AbortSignal.timeout(20000)]);
    const result = await loadTeams(client, { showArchived: state.showArchived, signal });
    if (disposed || request.signal.aborted) return;
    Object.assign(state, { phase: result.status === 'ok' ? 'ok' : result.status, items: result.items || [], hasMore: Boolean(result.hasMore), message: result.message || '' });
    if (result.status === 'ok' || result.status === 'off') freshness()?.touch(true); else freshness()?.touch(false, result.message || result.status);
    paint();
  }

  container.addEventListener('change', (event) => {
    if (!event.target.matches?.('[data-teams-archived]')) return;
    state.showArchived = event.target.checked;
    void load();
  });

  paint();
  void load();
  return { refresh: load, dispose() { disposed = true; controller?.abort(); } };
}

// Read-only list of the Herdr workspaces and missions linked to a room. Pure: returns escaped HTML.
// What is shown is exactly what the server decided this person may see; the page never asks for more
// and never offers a terminal from here (room access does not imply workspace terminal access).
import { escapeHtml as esc } from './herdr-web-components/util.js';

const GRANT_LABEL = { status: 'status', tabs: 'tabs', panes: 'panes', terminal: 'terminal (named people only)' };

export const grantSummary = (grants) => (grants && grants.length
  ? grants.map((g) => GRANT_LABEL[g] || g).join(', ') : 'nothing shared');

export function renderLinks(links, missions) {
  if (links == null) return '<div class="hd-empty">Workspace links are not shown to your role.</div>';
  const ws = links.length
    ? `<ul class="cx-list" aria-label="Linked workspaces">${links.map((l) => `<li><span class="grow"><b>${esc(l.workspace_id)}</b> on ${esc(l.host)}<br><span class="cx-msg">Room members can see: ${esc(grantSummary(l.grants))}</span></span>${l.host_verified === false ? '<span class="cx-pill">Host not verified</span>' : ''}${l.is_primary ? '<span class="cx-pill">Primary room</span>' : ''}${l.can_terminal ? '<span class="cx-pill">You may open the terminal</span>' : ''}</li>`).join('')}</ul>`
    : '<div class="hd-empty">No workspaces are linked to this room.</div>';
  const ms = (missions || []).length
    ? `<ul class="cx-list" aria-label="Linked missions">${missions.map((m) => `<li><span class="grow">${esc(m.mission_id)}</span></li>`).join('')}</ul>`
    : '<div class="hd-empty">No missions are linked to this room.</div>';
  return `<h3 class="hd-card__title">Workspaces</h3>${ws}<h3 class="hd-card__title">Missions</h3>${ms}`;
}

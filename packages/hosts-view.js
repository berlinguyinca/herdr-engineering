// Hosts page as a mounted view: the card grid is keyed by host id through patchList, the
// config notice and the fleet version matrix are morphed, so a live refresh writes only what
// changed (identical data: zero writes). The ?drift=1 filter is a navigation, so it is fixed per mount.
import { patchList, morph, patchText } from './patch.js';
import { snapshotText, animateChanges } from './motion.js';
import { liveRefresher, pollWhileVisible } from './refresh-bus.js';
import { renderFleetMatrix } from './fleet-view.js';
import { renderHostCard, HOST_CARD_STYLE, staleFor } from './host-view.js';
import { configNotice, indexConfigStatus } from './fleet-config-badge.js';
import { loadDiagnostics, SECTIONS } from './diagnostics-source.js';
import { renderIssueList } from './diagnostics-view.js';
import { renderSessionTimeline } from './diagnostics-timeline.js';
import { renderDriftComparison } from './task-drift-view.js';
import { escapeHtml } from './herdr-web-components/util.js';

// Motion keys: a meter by host and kind, a fleet cell by component row and host. Matrix rows are
// paired by position in morph, so the key is the row's component name, never the DOM node.
const meterKey = (el) => `${el.closest('[data-host-id]')?.getAttribute('data-host-id')}|${el.getAttribute('data-meter')}`;
const cellKey = (el) => `${el.parentElement?.querySelector('th[scope=row]')?.textContent}|${el.getAttribute('data-host-id')}`;
const STALE_TICK_MS = 15000;

// Host views take every live event (no interest filter) AND poll: the server keeps routine 30 s
// heartbeats in memory without emitting an event and never emits one for a host that goes silent,
// so on a quiet fleet only the poll moves telemetry forward and turns a dead host stale.
export const HOST_POLL_MS = 15000;

export async function loadHosts(client) {
  const [{ hosts }, matrix, configStatus] = await Promise.all([
    client.listHosts(),
    client.request('/api/v1/fleet/inventory').catch(() => ({ hosts: [], components: [] })),
    client.getFleetConfigStatus().catch(() => null),
  ]);
  return { hosts: hosts || [], matrix, configStatus };
}

export function mountHosts(container, client, { data = null, driftedOnly = false } = {}) {
  container.innerHTML = `${HOST_CARD_STYLE}<div class="hd-live-head" style="display:flex;flex-wrap:wrap;align-items:baseline;gap:12px"><h1>Hosts</h1><herdr-freshness interval="5000"></herdr-freshness></div>
    <div data-hosts-notice></div><div data-hosts-empty></div><div class="hd-grid" data-hosts-grid></div>
    <details class="hd-silent-hosts" data-hosts-silent-wrap hidden><summary data-hosts-silent-label></summary><div class="hd-grid" data-hosts-silent style="margin-top:12px"></div></details>
    <h2 style="margin-top:24px">Fleet versions</h2>
    <p class="hd-card__sub"><a href="#/hosts${driftedOnly ? '' : '?drift=1'}">${driftedOnly ? 'Show all components' : 'Show only components that differ'}</a></p>
    <div data-hosts-matrix></div>`;
  const notice = container.querySelector('[data-hosts-notice]');
  const empty = container.querySelector('[data-hosts-empty]');
  const grid = container.querySelector('[data-hosts-grid]');
  const silentWrap = container.querySelector('[data-hosts-silent-wrap]');
  const silentGrid = container.querySelector('[data-hosts-silent]');
  const matrixEl = container.querySelector('[data-hosts-matrix]');
  let first = true;

  function apply({ hosts, matrix, configStatus }) {
    const cfg = indexConfigStatus(configStatus);
    const now = Date.now();
    morph(notice, configNotice(cfg));
    morph(empty, hosts.length ? '' : '<herdr-empty message="No hosts reporting"></herdr-empty>');
    // The rendered card is the snapshot: identical markup skips the card entirely.
    const meters = first ? null : snapshotText(grid, '[data-meter]', meterKey);
    const cells = first ? null : snapshotText(matrixEl, 'td.hd-fleet__cell', cellKey);
    // A host that has never reported is noise on the main grid: keep it one click away, in its own list.
    const neverReported = (h) => !h?.last_seen && !(h?.latest && typeof h.latest === 'object');
    const toItem = (h) => ({ id: String(h?.host_id ?? ''), html: renderHostCard(h, { now, cfg }) });
    const reporting = hosts.filter((h) => !neverReported(h)).map(toItem);
    const silent = hosts.filter(neverReported).map(toItem);
    const patch = (el, items) => patchList(el, items, {
      key: (item) => item.id,
      render: (item) => item.html,
      update: (node, item) => { const t = document.createElement('template'); t.innerHTML = item.html.trim(); morph(node, t.content.firstElementChild); },
    });
    patch(grid, reporting);
    patch(silentGrid, silent);
    if (silentWrap.hidden === !silent.length) { /* unchanged: writing the same value would still count as a mutation */ } else silentWrap.hidden = !silent.length;
    patchText(container.querySelector('[data-hosts-silent-label]'), `${silent.length} host${silent.length === 1 ? '' : 's'} registered but never reported`);
    if (first) { for (const el of container.querySelectorAll('[data-entering]')) el.removeAttribute('data-entering'); }
    morph(matrixEl, renderFleetMatrix(matrix, { driftedOnly }));
    // Meters ease through their width transition; the changed row and a changed fleet cell flash.
    animateChanges(grid, '[data-meter]', meters, meterKey, { count: false });
    animateChanges(matrixEl, 'td.hd-fleet__cell', cells, cellKey, { count: false });
    first = false;
  }

  // "no report for N min" keeps counting between refreshes (minute resolution: one write a minute).
  const tick = setInterval(() => {
    const now = Date.now();
    for (const el of container.querySelectorAll('[data-stale-since]')) patchText(el, staleFor(Number(el.getAttribute('data-stale-since')), now));
  }, STALE_TICK_MS);

  const live = liveRefresher(() => loadHosts(client), apply, () => container.querySelector('herdr-freshness'));
  if (data) live.applied(data); else live.refresh();
  const stopPoll = pollWhileVisible(() => live.refresh(), HOST_POLL_MS);
  return { refresh: (hint) => live.refresh(hint), dispose: () => { stopPoll(); live.dispose(); clearInterval(tick); } };
}

// Host diagnostics is the one page here that is NOT live: H03 reads each feed once and the plan forbids polling it, so this mount starts no
// refresh bus, no poll, no timer and no control, writes what that single read could see, and returns nothing. What H03 could not read travels
// through as `unavailable`, so a section that failed renders its unavailable banner instead of an empty list that would read as "nothing is wrong".
// The events feed carries events, not incidents, and mapping one onto the other would mean inventing a severity, so H04 shows what it cannot use
// as a rejected record. The same feeds carry no drift report, so H06 prints unknown for every field rather than a value supplied here.
// The hosts the page actually read, named: host id, the status and instant the feed reported, and how many
// problems it reported with them. A host list that could not be read says so — an empty grid would otherwise
// read as "no hosts".
function renderHostsRead(hosts, unavailable) {
  if ((Array.isArray(unavailable) ? unavailable : []).includes('hosts')) return '<p class="diag-note" data-diagnostics-hosts-unavailable>the host list could not be read, so no host is named here</p>';
  const rows = (Array.isArray(hosts?.items) ? hosts.items : []).filter((host) => host && typeof host === 'object');
  if (!rows.length) return '<p class="diag-note" data-diagnostics-hosts-empty>the host list is empty</p>';
  const shown = (value) => (typeof value === 'string' && value.trim() ? escapeHtml(value) : 'unknown');
  return `<ul class="diag-list" role="list" aria-label="Hosts read">${rows.map((host) => `<li class="diag-card" data-diagnostics-host data-host-id="${escapeHtml(String(host.host_id ?? ''))}">`
    + `<h3>${shown(text(host.host_id))}</h3><div class="diag-fields">${hostField('status', text(host.status))}${hostField('problems', Number.isInteger(host.problem_count) ? String(host.problem_count) : 'unknown')}${hostField('last seen', text(host.last_seen))}</div></li>`).join('')}</ul>`;
}
const hostField = (label, value) => `<div class="diag-field"><b>${escapeHtml(value || 'unknown')}</b><span>${escapeHtml(label)}</span></div>`;
const text = (v) => (typeof v === 'string' ? v : '');

export async function mountDiagnostics(container, { source } = {}) {
  const read = await loadDiagnostics(source).catch(() => null);   // loadDiagnostics already swallows every feed failure; this covers a source that is not a source at all
  const diagnostics = read || { events: { timeline: [] }, issues: { items: [] }, hosts: { items: [] }, unavailable: [...SECTIONS], partial: true };
  const events = diagnostics.events?.timeline || [];
  // Task drift keeps its "unknown" reading: no feed on this page carries an expected-versus-actual pair, and
  // H06 says an unreported comparison is unknown, not zero. Passing null says that explicitly.
  container.innerHTML = `<h1>Diagnostics</h1>
    ${diagnostics.note ? `<p class="diag-note" role="status" data-diagnostics-note>${escapeHtml(diagnostics.note)}</p>` : ''}
    <section data-diagnostics-hosts><h2>Hosts read</h2>${renderHostsRead(diagnostics.hosts, diagnostics.unavailable)}</section>
    <section data-diagnostics-issues><h2>Error issues</h2>${renderIssueList(diagnostics.issues?.items || [], { state: diagnostics })}</section>
    <section data-diagnostics-timeline><h2>Session timeline</h2>${renderSessionTimeline(events, { unavailable: diagnostics.unavailable })}</section>
    <section data-diagnostics-drift><h2>Task drift</h2>${renderDriftComparison(null)}</section>`;
}

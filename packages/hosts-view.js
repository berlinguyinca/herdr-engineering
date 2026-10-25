// Hosts page as a mounted view: the card grid is keyed by host id through patchList, the
// config notice and the fleet version matrix are morphed, so a live refresh writes only what
// changed (identical data: zero writes). The ?drift=1 filter is a navigation, so it is fixed per mount.
import { patchList, morph, patchText } from './patch.js';
import { snapshotText, animateChanges } from './motion.js';
import { liveRefresher, pollWhileVisible } from './refresh-bus.js';
import { renderFleetMatrix } from './fleet-view.js';
import { renderHostCard, HOST_CARD_STYLE, staleFor } from './host-view.js';
import { configNotice, indexConfigStatus } from './fleet-config-badge.js';

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
    <h2 style="margin-top:24px">Fleet versions</h2>
    <p class="hd-card__sub"><a href="#/hosts${driftedOnly ? '' : '?drift=1'}">${driftedOnly ? 'Show all components' : 'Show only components that differ'}</a></p>
    <div data-hosts-matrix></div>`;
  const notice = container.querySelector('[data-hosts-notice]');
  const empty = container.querySelector('[data-hosts-empty]');
  const grid = container.querySelector('[data-hosts-grid]');
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
    const items = hosts.map((h) => ({ id: String(h?.host_id ?? ''), html: renderHostCard(h, { now, cfg }) }));
    patchList(grid, items, {
      key: (item) => item.id,
      render: (item) => item.html,
      update: (el, item) => { const t = document.createElement('template'); t.innerHTML = item.html.trim(); morph(el, t.content.firstElementChild); },
    });
    if (first) { for (const el of grid.querySelectorAll('[data-entering]')) el.removeAttribute('data-entering'); }
    morph(matrixEl, renderFleetMatrix(matrix, { driftedOnly }));
    // Meters ease through their width transition; the changed row and a changed fleet cell flash.
    animateChanges(grid, '[data-meter]', meters, meterKey, { count: false });
    animateChanges(matrixEl, 'td.hd-fleet__cell', cells, cellKey, { count: false });
    first = false;
  }

  // "no report for N min" keeps counting between refreshes (minute resolution: one write a minute).
  const tick = setInterval(() => {
    const now = Date.now();
    for (const el of grid.querySelectorAll('[data-stale-since]')) patchText(el, staleFor(Number(el.getAttribute('data-stale-since')), now));
  }, STALE_TICK_MS);

  const live = liveRefresher(() => loadHosts(client), apply, () => container.querySelector('herdr-freshness'));
  if (data) live.applied(data); else live.refresh();
  const stopPoll = pollWhileVisible(() => live.refresh(), HOST_POLL_MS);
  return { refresh: (hint) => live.refresh(hint), dispose: () => { stopPoll(); live.dispose(); clearInterval(tick); } };
}

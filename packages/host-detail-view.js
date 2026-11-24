// Host detail as a mounted view. renderHostDetail stays the pure renderer; a refresh morphs its
// sections (keyed by data-section) in place, so gauges, sparklines and top consumers update
// without touching an open outdated-packages <details> or the scroll position. The range tabs
// are in-page: setRange swaps the series and rewrites the hash without a navigation.
import { morph } from './patch.js';
import { liveRefresher, pollWhileVisible } from './refresh-bus.js';
import { snapshotText, animateChanges, skeleton } from './motion.js';
import { renderHostDetail, RANGES } from './host-view.js';
import { HOST_POLL_MS, retryError } from './hosts-view.js';
import { escapeHtml } from './herdr-web-components/util.js';

const NOT_FOUND = '<herdr-error message="Host not found"></herdr-error>';

// hostId is the URL-encoded id (safeHostId). The host fetch throws on failure (404 resolves to host
// null); a failed history read degrades to empty sparklines but is carried as telemetryError.
export async function loadHostDetail(client, hostId, range) {
  const [host, { telemetry, telemetryError }] = await Promise.all([
    client.request(`/api/v1/hosts/${hostId}`).catch((err) => { if (err?.status === 404) return null; throw err; }),
    loadTelemetry(client, hostId, range),
  ]);
  return { host: host && !host.error ? host : null, telemetry, telemetryError, range };
}

const loadTelemetry = (client, hostId, range) =>
  client.request(`/api/v1/hosts/${hostId}/telemetry?range=${range}`)
    .then((telemetry) => ({ telemetry, telemetryError: null }),
      (err) => ({ telemetry: { samples: [] }, telemetryError: String(err?.message || 'request failed') }));

export function mountHostDetail(container, client, hostId, range, { data = null } = {}) {
  let current = RANGES.includes(range) ? range : '1h';
  let last = null;
  let rangeSeq = 0;
  container.innerHTML = `<div class="hd-live-head" style="display:flex;justify-content:flex-end"><herdr-freshness interval="5000"></herdr-freshness></div><div data-host-detail>${data ? '' : skeleton('host', { rows: 4 })}</div>`;
  const body = container.querySelector('[data-host-detail]');

  function apply(next) {
    // A refresh that started before a range switch keeps the new range's series.
    if (next.range !== current && last) next = { ...next, telemetry: last.telemetry, telemetryError: last.telemetryError, range: current };
    last = next;
    const values = snapshotText(body, '.hv-value');
    const note = next.host && next.telemetryError ? `<p class="hd-card__sub" role="status">History could not be loaded (${escapeHtml(next.telemetryError)}); charts are empty.</p>` : '';
    morph(body, next.host ? note + renderHostDetail(next.host, next.telemetry, { range: current, now: Date.now() }) : NOT_FOUND);
    animateChanges(body, '.hv-value', values);
    // Draw-in runs once per line (CSS keys it on data-drawn, which morph preserves).
    for (const line of body.querySelectorAll('.hv-spark__line:not([data-drawn])')) line.setAttribute('data-drawn', '');
  }

  const load = () => loadHostDetail(client, hostId, current).catch((err) => {
    if (!last) body.innerHTML = retryError(`Host could not be loaded: ${String(err?.message || 'request failed')}`, 'data-host-retry');
    throw err;
  });
  const live = liveRefresher(load, apply, () => container.querySelector('herdr-freshness'));

  async function setRange(range) {
    if (!RANGES.includes(range) || live.disposed) return;
    const seq = ++rangeSeq;
    const { telemetry, telemetryError } = await loadTelemetry(client, hostId, range);
    if (seq !== rangeSeq || live.disposed) return; // a later tab click won
    current = range;
    history.replaceState(history.state, '', `#/hosts/${hostId}?range=${range}`);
    if (last) apply({ ...last, telemetry, telemetryError, range });
  }

  const onClick = (event) => {
    if (event.target.closest?.('[data-host-retry]')) { event.preventDefault(); live.refresh(); return; }
    const link = event.target.closest?.('.hv-range a[href]');
    if (!link || !container.contains(link) || event.button || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    const range = new URLSearchParams(link.getAttribute('href').split('?')[1] || '').get('range');
    if (!RANGES.includes(range)) return;
    event.preventDefault();
    setRange(range);
  };
  container.addEventListener('click', onClick);

  if (data) live.applied(data); else live.refresh();
  // Same as the Hosts page: any live event refreshes, and a visible-only poll covers a quiet fleet.
  const stopPoll = pollWhileVisible(() => live.refresh(), HOST_POLL_MS);
  return {
    refresh: (hint) => live.refresh(hint),
    setRange,
    dispose() { stopPoll(); live.dispose(); container.removeEventListener('click', onClick); },
  };
}

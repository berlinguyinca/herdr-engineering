// Host detail as a mounted view. renderHostDetail stays the pure renderer; a refresh morphs its
// sections (keyed by data-section) in place, so gauges, sparklines and top consumers update
// without touching an open outdated-packages <details> or the scroll position. The range tabs
// are in-page: setRange swaps the series and rewrites the hash without a navigation.
import { morph } from './patch.js';
import { liveRefresher, pollWhileVisible } from './refresh-bus.js';
import { snapshotText, animateChanges } from './motion.js';
import { renderHostDetail, RANGES } from './host-view.js';
import { HOST_POLL_MS } from './hosts-view.js';

const NOT_FOUND = '<herdr-error message="Host not found"></herdr-error>';

// hostId is the URL-encoded id (safeHostId). Only the host fetch may fail; missing history
// degrades to empty sparklines. A 404 resolves to host null.
export async function loadHostDetail(client, hostId, range) {
  const [host, telemetry] = await Promise.all([
    client.request(`/api/v1/hosts/${hostId}`).catch((err) => { if (err?.status === 404) return null; throw err; }),
    loadTelemetry(client, hostId, range),
  ]);
  return { host: host && !host.error ? host : null, telemetry, range };
}

const loadTelemetry = (client, hostId, range) =>
  client.request(`/api/v1/hosts/${hostId}/telemetry?range=${range}`).catch(() => ({ samples: [] }));

export function mountHostDetail(container, client, hostId, range, { data = null } = {}) {
  let current = RANGES.includes(range) ? range : '1h';
  let last = null;
  let rangeSeq = 0;
  container.innerHTML = '<div class="hd-live-head" style="display:flex;justify-content:flex-end"><herdr-freshness interval="5000"></herdr-freshness></div><div data-host-detail></div>';
  const body = container.querySelector('[data-host-detail]');

  function apply(next) {
    // A refresh that started before a range switch keeps the new range's series.
    if (next.range !== current && last) next = { ...next, telemetry: last.telemetry, range: current };
    last = next;
    const values = snapshotText(body, '.hv-value');
    morph(body, next.host ? renderHostDetail(next.host, next.telemetry, { range: current, now: Date.now() }) : NOT_FOUND);
    animateChanges(body, '.hv-value', values);
    // Draw-in runs once per line (CSS keys it on data-drawn, which morph preserves).
    for (const line of body.querySelectorAll('.hv-spark__line:not([data-drawn])')) line.setAttribute('data-drawn', '');
  }

  const live = liveRefresher(() => loadHostDetail(client, hostId, current), apply, () => container.querySelector('herdr-freshness'));

  async function setRange(range) {
    if (!RANGES.includes(range) || live.disposed) return;
    const seq = ++rangeSeq;
    const telemetry = await loadTelemetry(client, hostId, range);
    if (seq !== rangeSeq || live.disposed) return; // a later tab click won
    current = range;
    history.replaceState(history.state, '', `#/hosts/${hostId}?range=${range}`);
    if (last) apply({ ...last, telemetry, range });
  }

  const onClick = (event) => {
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

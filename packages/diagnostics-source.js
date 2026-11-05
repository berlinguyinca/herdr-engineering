// The read-only host diagnostics transport adapter (H03): the existing Herdr event and host feeds, adapted to the H02 view model. Nothing is
// fetched here — fetchEvents, fetchHosts and now are injected by the caller — and no endpoint, host, port, protocol, retry, cache or clock is
// invented: each feed is read once, and the part that could not be read says so instead of looking like nothing happened.
import { buildTimeline } from './diagnostics-model.js';

export const SECTIONS = ['events', 'hosts'];   // the only feeds this adapter reads; asking for another section is not a new endpoint
const plain = (v) => typeof v === 'object' && v !== null && !Array.isArray(v), finite = (v) => typeof v === 'number' && Number.isFinite(v);
const unreadable = (section) => ({ section, items: [], read: false, partial: false });   // empty AND unreadable: empty alone is not evidence that nothing happened

// What a feed hands back is a page: an array, or { items, truncated }. null or undefined means nothing came back — a different fact from an empty page — and truncated means what arrived is kept while the page is admitted, honestly, to be incomplete.
function readPage(section, value) {
  if (value === null || value === undefined) return unreadable(section);
  const items = Array.isArray(value) ? value : plain(value) ? value.items : undefined;
  if (!Array.isArray(items)) return unreadable(section);
  return { section, items, read: true, partial: Boolean(plain(value) && value.truncated) };
}

/** Adapt whatever transport the caller supplies — an existing Herdr feed today, an injected fake under node:test. `now` is the caller's instant (a finite number) so freshness can be labelled downstream without this module reading a clock; without one it is null. */
export function createDiagnosticsSource({ fetchEvents, fetchHosts, now } = {}) {
  const feeds = { events: fetchEvents, hosts: fetchHosts };
  return {
    now: finite(now) ? now : null, sections: [...SECTIONS],   // never Date.now: a source with no injected instant says null
    async read(section) {
      if (!SECTIONS.includes(section)) return unreadable(String(section));
      const feed = feeds[section];
      if (typeof feed !== 'function') return unreadable(section);   // no transport wired for it: unavailable, never a silent empty list
      try { return readPage(section, await feed()); } catch { return unreadable(section); }   // an offline host is the feed's own fact; no cause is invented or echoed here
    },
  };
}

/** Read every section once — no polling, no retry, no cache, no cross-host call — and return what arrived, counted through H02: buildTimeline runs
 * normalizeEvent over each record and counts { dropped, invalid }, so nothing goes missing silently. Hosts pass through as read (H02 defines no host
 * shape, and inventing one here would be inventing a schema). The promise never rejects: ok says the read finished, partial and unavailable say how much could not be read. */
export async function loadDiagnostics(source, filter = {}) {
  const sections = await Promise.all(SECTIONS.map(async (section) => {
    try { const read = await source?.read?.(section); return plain(read) && Array.isArray(read.items) ? { ...read, section, read: read.read !== false } : unreadable(section); }
    catch { return unreadable(section); }
  }));
  const [events, hosts] = sections, model = buildTimeline(events.read ? events.items : [], filter);
  return {
    ok: true,
    events: { timeline: model.timeline, dropped: model.dropped, invalid: model.invalid, partial: events.partial },
    hosts: { items: hosts.read ? [...hosts.items] : [], partial: hosts.partial },
    partial: sections.some((s) => !s.read || s.partial),
    unavailable: sections.filter((s) => !s.read).map((s) => s.section),
  };
}

/** One line for the view layer, so an offline host reads as "events are unavailable" instead of a blank that looks like "no incidents". */
export function describeUnavailable(unavailable) {
  const sections = (Array.isArray(unavailable) ? unavailable : []).map((s) => (typeof s === 'string' ? s.toLowerCase() : '')).filter((s) => SECTIONS.includes(s));
  if (!sections.length) return '';   // nothing was withheld, so the view has no cause to name
  return `Some host diagnostics are unavailable: ${sections.join(', ')} could not be read, so what is shown covers part of the history only`;
}

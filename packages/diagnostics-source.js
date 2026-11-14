// The read-only host diagnostics transport adapter (H03): the existing Herdr event and host feeds, adapted to the H02 view model. Nothing is
// fetched here — fetchEvents, fetchHosts and now are injected by the caller — and no endpoint, host, port, protocol, retry, cache or clock is
// invented: each feed is read once, and the part that could not be read says so instead of looking like nothing happened.
// The client-facing factory at the bottom is the only part that names a URL, and it names the one the control plane actually answers:
// GET /api/v1/events?entity_type=host&entity_id=<id>&limit=<n>. The bare /api/v1/events is a 400 ("entity_type and entity_id required",
// herdr_engineering/control_plane/api.py), and tests/control_plane/test_diagnostics_events_contract.py pins both of those facts against a live
// server, with the recorded answers in tests/fixtures/diagnostics_events_host.json.
import { buildTimeline, normalizeIncident } from './diagnostics-model.js';

export const SECTIONS = ['events', 'hosts'];   // the only feeds this adapter reads; asking for another section is not a new endpoint
export const HOST_EVENT_PAGE_SIZE = 50;        // rows asked for per host; the control plane takes limit as 1..200 and answers 400 outside that
export const HOST_READ_CAP = 20;               // hosts read per load; past the cap the read says so rather than looking complete
const plain = (v) => typeof v === 'object' && v !== null && !Array.isArray(v), finite = (v) => typeof v === 'number' && Number.isFinite(v);
const text = (v) => (typeof v === 'string' ? v : '');
const PAGE_ROWS = { events: 'events', hosts: 'hosts' };   // each feed names its own rows: /api/v1/events answers {events,[has_more]}, /api/v1/hosts answers {hosts}
const unreadable = (section) => ({ section, items: [], read: false, partial: false, note: '' });   // empty AND unreadable: empty alone is not evidence that nothing happened

// What a feed hands back is a page: an array, or an object whose rows are the section's own key (`items` for a section the feed names no other
// way), plus `truncated`/`has_more` when the sender says the page is short. null or undefined means nothing came back — a different fact from an
// empty page — and a short page keeps what arrived while admitting, in `note` if the caller supplies one, what is missing.
function readPage(section, value) {
  if (value === null || value === undefined) return unreadable(section);
  const rowsKey = PAGE_ROWS[section] || 'items';
  const items = Array.isArray(value) ? value
    : plain(value) ? (Array.isArray(value[rowsKey]) ? value[rowsKey] : Array.isArray(value.items) ? value.items : undefined)   // the section's own key wins; `items` stays the generic page key
    : undefined;
  if (!Array.isArray(items)) return unreadable(section);
  const partial = Boolean(plain(value) && (value.truncated === true || value.has_more === true));
  return { section, items, read: true, partial, note: plain(value) ? text(value.note) : '' };
}

/**
 * One `/api/v1/events` envelope to an H02 event. The real envelope (see the file header) carries `event_id`,
 * `event_type`, `source_timestamp`/`ingest_timestamp`, `entity_type`, `entity_id`, `mission_id`, `session_id`
 * and `payload`; `at` is the source instant, or the ingest instant when the row reports no source instant at
 * all — a record with no readable instant cannot be placed on a timeline, so a row whose source instant is
 * present but does not parse returns null and the caller counts it. Identity, type or an absent instant likewise
 * return null: the caller counts that row instead of dropping it silently.
 */
export function toTimelineRecord(row) {
  if (!plain(row)) return null;
  const id = text(row.event_id), eventType = text(row.event_type);
  const at = Date.parse(text(row.source_timestamp) || text(row.ingest_timestamp));
  if (!id.trim() || !eventType.trim() || !finite(at)) return null;
  const record = { id, at, eventType };
  if (row.entity_type === 'host' && text(row.entity_id).trim()) record.hostId = row.entity_id;
  if (text(row.session_id).trim()) record.sessionId = row.session_id;
  if (text(row.mission_id).trim()) record.missionKey = row.mission_id;
  if (plain(row.payload)) record.payload = row.payload;
  return record;
}

/**
 * One host from `GET /api/v1/hosts` to the H02 incidents it reported. The feed carries `problems[]` of
 * `{code, message, severity}` alongside `host_id` and `last_seen`; each becomes one incident whose instant is
 * the host's own `last_seen` (the reading the problem was computed against — the feed reports no separate
 * time, so the "last seen" H04 prints for such a card is the instant that host last reported, which the page
 * says under its heading), and whose fingerprint is the local convention `host_id:code` (the feed gives no
 * fingerprint of its own, and H02 requires one to group repeats). A host reporting the same code twice is two
 * incidents: the repeats carry `#2`, `#3` …, because H02 keys a record by its id and two cards sharing an id
 * would read as one problem. That suffix is positional within one feed response (the server emits `problems[]` in
 * a fixed check order), so it groups what is on screen now; it is not an incident identity that survives the
 * first problem clearing. A problem with no code, or a severity outside H02's vocabulary, is passed through
 * anyway: H04 shows it as a rejected record rather than quietly discarding it. A host reporting no problems
 * returns nothing, which is the feed's fact, not an unreadable section.
 */
export function toIssueRecords(host) {
  if (!plain(host) || !text(host.host_id).trim()) return [];
  const at = Date.parse(text(host.last_seen));
  const seen = new Map();
  return (Array.isArray(host.problems) ? host.problems : []).filter(plain).map((problem) => {
    const base = `${host.host_id}:${text(problem.code).trim() || 'unspecified'}`;
    const repeat = (seen.get(base) || 0) + 1; seen.set(base, repeat);
    return {
      id: repeat === 1 ? base : `${base}#${repeat}`,
      kind: text(problem.code),
      severity: text(problem.severity),
      message: text(problem.message),
      hostId: host.host_id,
      ...(finite(at) ? { at } : {}),
    };
  });
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

/** The only events URL this adapter asks for, in one place so the contract is testable. The control plane requires both entity parameters (the
 * bare events path answers 400 "entity_type and entity_id required") and takes `limit` as 1..200 — `limit=0` and `limit=201` both answer 400
 * "limit must be an integer between 1 and 200" — so a limit outside that range is replaced by the page size rather than sending a request the
 * server will refuse. tests/control_plane/test_diagnostics_events_contract.py re-measures both rules against the running server. */
export function hostEventsPath(hostId, limit = HOST_EVENT_PAGE_SIZE) {
  if (!text(hostId).trim()) throw new TypeError('hostEventsPath needs a host id');
  const size = Number.isInteger(limit) && limit >= 1 && limit <= 200 ? limit : HOST_EVENT_PAGE_SIZE;
  return `/api/v1/events?entity_type=host&entity_id=${encodeURIComponent(hostId)}&limit=${size}`;
}

/**
 * The source `#/diagnostics` uses: the dashboard client's own surface, no invented endpoint. It reads
 * `GET /api/v1/hosts` (`client.listHosts`, herdr-web-client/client.js:750) and then one event page per host
 * through `hostEventsPath`, merging the rows into the events section. `request` and `listHosts` are injected —
 * the dashboard passes `client.request` and `client.listHosts` — and nothing else is called: no polling, no
 * retry, no cache, no cross-host fan-out beyond the host list itself.
 *
 * The event page's own shortfalls are named, not hidden: a host whose page fails appears in the events note
 * ("the event page for hive could not be read") and makes the read partial, and so does `has_more` on any page
 * or a host list longer than HOST_READ_CAP, in which case only the first HOST_READ_CAP hosts are read.
 */
export function createHostDiagnosticsSource({ request, listHosts, now } = {}) {
  // One host-list read is shared by both sections of a load: the two sections are read concurrently, so an
  // in-flight request is handed to the second caller instead of issuing a second GET. It is released when it
  // settles, so a later load asks the server again rather than reusing an answer — the page still reads each
  // feed exactly once, and now the control plane sees one host-list request per page load, not two that could
  // disagree.
  let inflight = null;
  function readHosts() {
    if (inflight) return inflight;
    const settled = (value, error) => { if (inflight === promise) inflight = null; if (error) throw error; return value; };
    const promise = (typeof listHosts === 'function' ? listHosts() : request('/api/v1/hosts'))
      .then((value) => settled(value), (error) => settled(undefined, error));
    inflight = promise;
    return promise;
  }
  async function fetchHosts() { return readHosts(); }
  async function fetchEvents() {
    const listing = await readHosts().catch(() => null);
    if (!plain(listing) || !Array.isArray(listing.hosts)) return null;   // the host list itself could not be read: nothing came back, which is a different fact from a host list with no events
    const all = listing.hosts.map((host) => text(host?.host_id)).filter(Boolean);
    const ids = all.slice(0, HOST_READ_CAP);
    const pages = await Promise.all(ids.map(async (hostId) => {
      try {
        const page = await request(hostEventsPath(hostId));
        return { hostId, rows: Array.isArray(page?.events) ? page.events : null, hasMore: page?.has_more === true };
      } catch { return { hostId, rows: null, hasMore: false }; }
    }));
    const failed = pages.filter((page) => page.rows === null).map((page) => page.hostId);
    const notes = [];
    if (failed.length) notes.push(`the event page for ${failed.join(', ')} could not be read`);
    if (all.length > ids.length) notes.push(`${all.length} hosts reported; only the first ${ids.length} were read`);
    if (pages.some((page) => page.hasMore)) notes.push('at least one host has events older than the page shown');
    return { events: pages.flatMap((page) => page.rows || []), has_more: notes.length > 0, note: notes.join('; ') };
  }
  return createDiagnosticsSource({ fetchEvents, fetchHosts, now });
}

/** Read every section once — no polling, no retry, no cache, no cross-host call beyond what the source itself says it did — and return what
 * arrived, counted through H02. Event envelopes (rows naming `event_id`/`event_type`) go through `toTimelineRecord` first: one that carries no
 * identity or no instant is counted as `unmapped`, never dropped silently; a row that names neither is taken to be an already-adapted H02 record
 * and is counted by buildTimeline ({dropped, invalid}). Host problems become H02 incidents for the
 * issue list (H04 validates them itself and shows a rejected record for any it refuses). The promise never rejects: partial and unavailable say
 * how much could not be read, and `note` carries the source's own account of what is missing. */
export async function loadDiagnostics(source, filter = {}) {
  const sections = await Promise.all(SECTIONS.map(async (section) => {
    try { const read = await source?.read?.(section); return plain(read) && Array.isArray(read.items) ? { ...read, section, read: read.read !== false } : unreadable(section); }
    catch { return unreadable(section); }
  }));
  const [events, hosts] = sections;
  const records = []; let unmapped = 0;
  for (const row of events.read ? events.items : []) {
    // A row naming `event_id`/`event_type` is a transport envelope and is mapped. A row that names neither is
    // taken as an already-adapted H02 record — what an injected source hands — and buildTimeline counts it
    // `invalid` if H02 refuses it. A row that is neither (no identity to map, no record to validate) is
    // counted `unmapped`; nothing goes missing silently.
    if (plain(row) && (text(row.event_id).trim() || text(row.event_type).trim())) {
      const mapped = toTimelineRecord(row); if (mapped) records.push(mapped); else unmapped += 1;
    } else records.push(row);
  }
  const incidents = (hosts.read ? hosts.items : []).flatMap((host) => toIssueRecords(host));
  const model = buildTimeline(records, filter);
  const notes = [events.note, hosts.note].filter(Boolean).join('; ');
  return {
    ok: true,
    events: { timeline: model.timeline, dropped: model.dropped, invalid: model.invalid, unmapped, partial: events.partial, note: events.note },
    issues: { items: incidents, invalid: incidents.filter((incident) => !normalizeIncident(incident).ok).length },
    hosts: { items: [...hosts.items], partial: hosts.partial },
    partial: sections.some((s) => !s.read || s.partial),
    unavailable: sections.filter((s) => !s.read).map((s) => s.section),
    note: notes,
  };
}

/** One line for the view layer, so an offline host reads as "events are unavailable" instead of a blank that looks like "no incidents". */
export function describeUnavailable(unavailable) {
  const sections = (Array.isArray(unavailable) ? unavailable : []).map((s) => (typeof s === 'string' ? s.toLowerCase() : '')).filter((s) => SECTIONS.includes(s));
  if (!sections.length) return '';   // nothing was withheld, so the view has no cause to name
  return `Some host diagnostics are unavailable: ${sections.join(', ')} could not be read, so what is shown covers part of the history only`;
}

import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import {
  SECTIONS, createDiagnosticsSource, loadDiagnostics, describeUnavailable,
  toTimelineRecord, toIssueRecords, hostEventsPath, createHostDiagnosticsSource,
  HOST_EVENT_PAGE_SIZE, HOST_READ_CAP,
} from './diagnostics-source.js';

const FIXTURE = fileURLToPath(new URL('../tests/fixtures/diagnostics_events_host.json', import.meta.url));
const recorded = JSON.parse(await readFile(FIXTURE, 'utf8'));   // answers recorded from a live control plane, not written by hand
const recordedHosts = recorded.hosts.body;
const envelope = recorded.events_host_hive.body;
const listen = async (handler) => new Promise((resolve) => { const server = createServer(handler); server.listen(0, '127.0.0.1', () => resolve(server)); });
const close = (server) => new Promise((resolve) => server.close(resolve));
const json = (res, status, body) => { res.writeHead(status, { 'content-type': 'application/json' }); res.end(JSON.stringify(body)); };
let origin = 'http://127.0.0.1:0';

const event = (id, at, over = {}) => Object.freeze({ id, missionKey: 'm1', at, eventType: 'status', ...over });
const host = Object.freeze({ host_id: 'h1', state: 'online' });
// A fake transport, injected the way session-helpers.test.js injects collectTranscript: an async closure the test counts, never a mocking framework.
const feed = (value) => { const calls = []; return { calls, fn: async () => { calls.push(1); if (value instanceof Error) throw value; return value; } }; };
const source = ({ events, hosts, now = 1000 } = {}) => createDiagnosticsSource({ fetchEvents: events?.fn, fetchHosts: hosts?.fn, now });
const shown = (r) => ({ ok: r.ok, partial: r.partial, unavailable: r.unavailable });
// An adapter row the envelope does not identify (no event_id, no event_type, no parseable instant).
const junk = Object.freeze({ event_type: 'HostHeartbeat', entity_id: 'hive', payload: {} });   // names an event_type but carries no event_id and no instant
// What a section's read says about itself, less the note the newer tests assert separately.
const shape = (read) => ({ section: read.section, items: read.items, read: read.read, partial: read.partial });
const stripNote = ({ note, ...rest }) => rest;
test('each feed is read once and what arrives becomes an H02 timeline', async () => {
  const hostItems = [host], hosts = feed(hostItems), events = feed([event('b', 5), event('a', 1)]);
  const result = await loadDiagnostics(source({ events, hosts }));
  assert.deepEqual(stripNote(result.events), { timeline: [event('a', 1), event('b', 5)], dropped: 0, invalid: 0, unmapped: 0, partial: false });
  assert.deepEqual(result.hosts.items, [host]); assert.notEqual(result.hosts.items, hostItems);   // a copy, so the view cannot write into the feed's own array
  assert.deepEqual(shown(result), { ok: true, partial: false, unavailable: [] }); assert.deepEqual([events.calls.length, hosts.calls.length], [1, 1]);   // one read each: no polling loop, no retry
});
test('the injected instant is the only instant; without one a source says null rather than reading a clock', () => {
  assert.equal(createDiagnosticsSource({ now: 42 }).now, 42);
  assert.equal(createDiagnosticsSource({}).now, null); assert.equal(createDiagnosticsSource({ now: 'now' }).now, null);
  assert.deepEqual(createDiagnosticsSource({}).sections, SECTIONS);
});
test('a host that rejects leaves its section unavailable and partial — never a rejection, never a fabricated empty success', async () => {
  const events = feed(new Error('host offline <b>raw cause</b>')), hosts = feed([host]);
  const result = await loadDiagnostics(source({ events, hosts }));
  assert.deepEqual(shown(result), { ok: true, partial: true, unavailable: ['events'] });
  assert.deepEqual(stripNote(result.events), { timeline: [], dropped: 0, invalid: 0, unmapped: 0, partial: false });
  assert.deepEqual(result.hosts.items, [host]); assert.doesNotMatch(JSON.stringify(result), /offline|raw cause/);   // the section that could be read is still shown; the feed's own error text is neither echoed nor invented
});
test('a feed that returns nothing is unavailable; a feed that returns an empty page is not', async () => {
  for (const nothing of [null, undefined, {}, 'unreachable', 7, { truncated: true }]) {
    const result = await loadDiagnostics(source({ events: feed(nothing), hosts: feed([]) }));
    assert.deepEqual(shown(result), { ok: true, partial: true, unavailable: ['events'] }, JSON.stringify(nothing)); assert.deepEqual(result.events.timeline, []);
  }
  const empty = await loadDiagnostics(source({ events: feed([]), hosts: feed({ items: [] }) }));
  assert.deepEqual(shown(empty), { ok: true, partial: false, unavailable: [] });   // "no incidents" and "could not be read" stay different facts
});
test('a truncated page keeps what arrived and admits it is incomplete', async () => {
  const result = await loadDiagnostics(source({ events: feed({ items: [event('kept', 2), event('also', 3)], truncated: true }), hosts: feed({ items: [host] }) }));
  assert.deepEqual({ ids: result.events.timeline.map((e) => e.id), shown: shown(result), eventsPartial: result.events.partial, hostsPartial: result.hosts.partial },
    { ids: ['kept', 'also'], shown: { ok: true, partial: true, unavailable: [] }, eventsPartial: true, hostsPartial: false });
});
test('records H02 cannot use are counted, and a filter counts what it excludes', async () => {
  const mixed = Object.freeze([event('keep', 5), event('late', 50), Object.freeze({ id: 'broken', missionKey: 'm1', at: 'soon', eventType: 'status' }), event('bad', 6, { html: '<b>' })]);
  const all = (await loadDiagnostics(source({ events: feed(mixed) }))).events;
  assert.deepEqual({ ids: all.timeline.map((e) => e.id), dropped: all.dropped, invalid: all.invalid }, { ids: ['keep', 'late'], dropped: 0, invalid: 2 });
  const windowed = (await loadDiagnostics(source({ events: feed(mixed) }), { missionKey: 'm1', kinds: ['status'], since: 2, until: 10 })).events;
  assert.deepEqual({ ids: windowed.timeline.map((e) => e.id), dropped: windowed.dropped, invalid: windowed.invalid }, { ids: ['keep'], dropped: 1, invalid: 2 });
});
test('a source with no transport, or no source at all, says unavailable instead of throwing', async () => {
  assert.deepEqual(shown(await loadDiagnostics(source({}))), { ok: true, partial: true, unavailable: SECTIONS });
  assert.deepEqual(shown(await loadDiagnostics(undefined)), { ok: true, partial: true, unavailable: SECTIONS });
  const one = createDiagnosticsSource({ fetchHosts: async () => [host] });
  assert.deepEqual([shape(await one.read('events')), shape(await one.read('hosts')), shape(await one.read('metrics'))], [{ section: 'events', items: [], read: false, partial: false },
    { section: 'hosts', items: [host], read: true, partial: false }, { section: 'metrics', items: [], read: false, partial: false }]);   // an unknown section is never a new endpoint
});
test('unavailable sections are named in one line; nothing withheld says nothing and unknown names are not laundered in', () => {
  const line = describeUnavailable(['events', 'hosts']);
  assert.match(line, /unavailable/); assert.match(line, /events/); assert.match(line, /hosts/); assert.doesNotMatch(line, /[\n\r]/);
  assert.equal(describeUnavailable([]), ''); assert.equal(describeUnavailable(null), ''); assert.equal(describeUnavailable('events'), '');
  const filtered = describeUnavailable(['api.py', 7, 'EVENTS']); assert.match(filtered, /events/); assert.doesNotMatch(filtered, /api\.py|EVENTS|\d/);
});

test('an event page is the {events,[has_more]} body the control plane answers, and a host page is {hosts}', async () => {
  // The keys are the real ones: GET /api/v1/events?entity_type=host&... answers
  // {events, has_more, next_cursor, latest_sequence, last_event_id} and GET /api/v1/hosts answers {hosts}
  // (tests/fixtures/diagnostics_events_host.json, recorded by tests/control_plane/record_diagnostics_events.py).
  const result = await loadDiagnostics(source({
    events: feed({ events: [envelope.events[0], junk], has_more: true, next_cursor: 'c', latest_sequence: 2, last_event_id: 'event_recorded_2' }),
    hosts: feed(recordedHosts),
  }));
  assert.equal(result.events.timeline.length, 1, 'the identified envelope maps; the row without identity is counted, not dropped');
  assert.equal(result.events.unmapped, 1);
  assert.equal(result.events.partial, true);
  assert.equal(result.partial, true, 'a has_more page is partial, never a complete-looking list');
  assert.deepEqual(result.hosts.items.map((h) => h.host_id), recordedHosts.hosts.map((h) => h.host_id));
  assert.deepEqual(result.issues.items.map((i) => i.id), ['farm:never_reported', 'hive:never_reported']);
});

test('a real /api/v1/events envelope becomes an H02 record; a row without identity or instant is refused', () => {
  assert.deepEqual(toTimelineRecord(envelope.events[0]), {
    id: 'event_recorded_0', at: Date.parse('2026-11-14T02:00:00+00:00'), eventType: 'HostRegistered',
    hostId: 'hive', payload: { host_name: 'hive', status: 'healthy', tailnet_ip: null },
  });
  // No mission_id on the envelope and no session_id: neither is invented.
  assert.equal('missionKey' in toTimelineRecord(envelope.events[0]), false);
  assert.equal('sessionId' in toTimelineRecord(envelope.events[0]), false);
  for (const unusable of [null, 'row', 7, {}, { event_id: ' ', event_type: 'x', source_timestamp: '2026-11-14T02:00:00+00:00' },
    { event_id: 'e', event_type: ' ', source_timestamp: '2026-11-14T02:00:00+00:00' }, { event_id: 'e', event_type: 'x', source_timestamp: 'yesterday' }]) {
    assert.equal(toTimelineRecord(unusable), null, JSON.stringify(unusable));
  }
  // The ingest instant stands in when a record carries no source instant; a non-host entity does not borrow hostId.
  assert.equal(toTimelineRecord({ ...envelope.events[0], source_timestamp: '', ingest_timestamp: '2026-11-14T02:05:00+00:00' }).at, Date.parse('2026-11-14T02:05:00+00:00'));
  assert.equal('hostId' in toTimelineRecord({ ...envelope.events[0], entity_type: 'session' }), false);
});

test("a host problem becomes an incident stamped with the host's own last_seen; a host reporting none becomes nothing", () => {
  const hive = recordedHosts.hosts.find((h) => h.host_id === 'hive');
  assert.deepEqual(toIssueRecords(hive), [{ id: 'hive:never_reported', kind: 'never_reported', severity: 'critical', message: 'Host has never reported', hostId: 'hive', at: Date.parse(hive.last_seen) }]);
  assert.deepEqual(toIssueRecords({ host_id: 'h2' }), []);   // no problems array is the feed's fact, not an unreadable section
  assert.deepEqual(toIssueRecords({ host_id: 'h2', problems: [{ code: '', severity: 'nonsense', message: 'x<b>' }] }),
    [{ id: 'h2:unspecified', kind: '', severity: 'nonsense', message: 'x<b>', hostId: 'h2' }]);   // passed through; H04 shows it as rejected
  assert.deepEqual(toIssueRecords(null), []); assert.deepEqual(toIssueRecords({ host_id: ' ' }), []);
  assert.equal(toIssueRecords({ host_id: 'h3', last_seen: null, problems: [{ code: 'c', severity: 'info', message: 'm' }] })[0].at, undefined, 'no instant is invented');
});

test('loadDiagnostics hands the issue list incidents, not timeline records', async () => {
  const result = await loadDiagnostics(source({ events: feed([envelope.events[0]]), hosts: feed(recordedHosts) }));
  assert.equal(result.issues.items.every((item) => 'kind' in item && 'severity' in item), true, 'incidents carry the incident fields H04 validates');
  assert.equal(result.issues.invalid, 0);
  const broken = await loadDiagnostics(source({ hosts: feed({ hosts: [{ host_id: 'h', problems: [{ code: 'c', severity: 'impossible' }] }] }) }));
  assert.equal(broken.issues.invalid, 1, 'a severity outside the vocabulary is counted, and H04 still shows it as rejected');
});

test('hostEventsPath is the only events URL, and it carries both entity parameters', () => {
  assert.equal(hostEventsPath('hive'), '/api/v1/events?entity_type=host&entity_id=hive&limit=50');
  assert.equal(hostEventsPath('ws:00:1a:2b'), '/api/v1/events?entity_type=host&entity_id=ws%3A00%3A1a%3A2b&limit=50');
  assert.equal(hostEventsPath('hive', 100), '/api/v1/events?entity_type=host&entity_id=hive&limit=100');   // the server's ceiling
  assert.equal(hostEventsPath('hive', 0), '/api/v1/events?entity_type=host&entity_id=hive&limit=50');      // outside 1..100: fall back to the page size
  assert.equal(hostEventsPath('hive', 500), '/api/v1/events?entity_type=host&entity_id=hive&limit=50');
  assert.throws(() => hostEventsPath(''), TypeError);
});

test('the client source reads the host list then one event page per host, over real HTTP', async () => {
  const requests = [];
  const server = await listen(async (req, res) => {
    requests.push(req.url);
    if (req.url === '/api/v1/hosts') return json(res, 200, recordedHosts);
    const match = /entity_type=host&entity_id=([^&]*)&limit=\d+/.exec(req.url);
    if (match && decodeURIComponent(match[1]) === 'hive') return json(res, 200, envelope);
    return json(res, 500, { error: 'boom' });   // the farm page fails: the read keeps hive and names the host that is missing
  });
  origin = `http://127.0.0.1:${server.address().port}`;
  try {
    const request = async (path) => { const response = await fetch(`${origin}${path}`); const body = await response.json(); if (!response.ok) throw new Error(`HTTP ${response.status}`); return body; };
    const read = await loadDiagnostics(createHostDiagnosticsSource({ request }));
    assert.deepEqual([...requests].sort(), ['/api/v1/events?entity_type=host&entity_id=farm&limit=50', '/api/v1/events?entity_type=host&entity_id=hive&limit=50', '/api/v1/hosts'],
      'one host-list read shared by both sections, then one page per host, and no other URL: a second GET whose answer could disagree with the first is not a second opinion')
    assert.deepEqual(read.events.timeline.map((e) => e.id), envelope.events.map((e) => e.event_id), 'the host that answered is shown, in the page order the server gave');
    assert.match(read.events.note, /farm could not be read/);
    assert.equal(read.events.partial, true);
    assert.deepEqual(read.hosts.items.map((h) => h.host_id), ['farm', 'hive']);
    assert.deepEqual(read.issues.items.map((i) => i.id), ['farm:never_reported', 'hive:never_reported']);
    assert.equal(read.partial, true);
    assert.deepEqual(read.events.timeline.map((e) => e.at), envelope.events.map((e) => Date.parse(e.source_timestamp)), 'each record carries the source instant the server reported');
  } finally { await close(server); }
});

test('the client source survives a transport that is not there, and caps how many hosts it reads', async () => {
  assert.deepEqual(shown(await loadDiagnostics(createHostDiagnosticsSource({}))), { ok: true, partial: true, unavailable: SECTIONS });
  const many = { hosts: Array.from({ length: HOST_READ_CAP + 3 }, (_, i) => ({ host_id: `h${i}`, problems: [] })) };
  const asked = [];
  const read = await loadDiagnostics(createHostDiagnosticsSource({
    listHosts: async () => many,
    request: async (path) => { asked.push(path); return { events: [], has_more: false }; },
  }));
  assert.equal(asked.length, HOST_READ_CAP, 'one page per host, capped');
  assert.match(read.events.note, new RegExp(`only the first ${HOST_READ_CAP} were read`));
  assert.equal(read.partial, true, 'a capped read never reads as complete');
  assert.equal(read.hosts.items.length, HOST_READ_CAP + 3, 'the host grid still names every host the feed reported');
});

test('a page that reports has_more says so in its own note', async () => {
  const read = await loadDiagnostics(createHostDiagnosticsSource({
    listHosts: async () => ({ hosts: [{ host_id: 'hive', problems: [] }] }),
    request: async () => recorded.events_host_hive_limit_1.body,   // the recorded limit=1 page: has_more true with a next_cursor
  }));
  assert.match(read.events.note, /events older than the page shown/);
  assert.equal(read.partial, true);
});

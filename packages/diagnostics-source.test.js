import test from 'node:test';
import assert from 'node:assert/strict';
import { SECTIONS, createDiagnosticsSource, loadDiagnostics, describeUnavailable } from './diagnostics-source.js';

const event = (id, at, over = {}) => Object.freeze({ id, missionKey: 'm1', at, eventType: 'status', ...over });
const host = Object.freeze({ host_id: 'h1', state: 'online' });
// A fake transport, injected the way session-helpers.test.js injects collectTranscript: an async closure the test counts, never a mocking framework.
const feed = (value) => { const calls = []; return { calls, fn: async () => { calls.push(1); if (value instanceof Error) throw value; return value; } }; };
const source = ({ events, hosts, now = 1000 } = {}) => createDiagnosticsSource({ fetchEvents: events?.fn, fetchHosts: hosts?.fn, now });
const shown = (r) => ({ ok: r.ok, partial: r.partial, unavailable: r.unavailable });
test('each feed is read once and what arrives becomes an H02 timeline', async () => {
  const hostItems = [host], hosts = feed(hostItems), events = feed([event('b', 5), event('a', 1)]);
  const result = await loadDiagnostics(source({ events, hosts }));
  assert.deepEqual(result.events, { timeline: [event('a', 1), event('b', 5)], dropped: 0, invalid: 0, partial: false });
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
  assert.deepEqual(result.events, { timeline: [], dropped: 0, invalid: 0, partial: false });
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
  assert.deepEqual([await one.read('events'), await one.read('hosts'), await one.read('metrics')], [{ section: 'events', items: [], read: false, partial: false },
    { section: 'hosts', items: [host], read: true, partial: false }, { section: 'metrics', items: [], read: false, partial: false }]);   // an unknown section is never a new endpoint
});
test('unavailable sections are named in one line; nothing withheld says nothing and unknown names are not laundered in', () => {
  const line = describeUnavailable(['events', 'hosts']);
  assert.match(line, /unavailable/); assert.match(line, /events/); assert.match(line, /hosts/); assert.doesNotMatch(line, /[\n\r]/);
  assert.equal(describeUnavailable([]), ''); assert.equal(describeUnavailable(null), ''); assert.equal(describeUnavailable('events'), '');
  const filtered = describeUnavailable(['api.py', 7, 'EVENTS']); assert.match(filtered, /events/); assert.doesNotMatch(filtered, /api\.py|EVENTS|\d/);
});

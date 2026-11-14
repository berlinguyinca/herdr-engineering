import test from 'node:test';
import assert from 'node:assert/strict';
import { SEVERITIES, normalizeIncident, normalizeEvent, buildTimeline, decideDrift } from './diagnostics-model.js';

const incident = Object.freeze({ id: 'i1', kind: 'session_stalled', severity: 'warning', hostId: 'h1', sessionId: 's1', message: 'no heartbeat', at: 10 });
const event = (id, at, over = {}) => Object.freeze({ id, missionKey: 'm1', at, eventType: 'status', ...over });
const rejects = (normalize, raw, phrase) => { const result = normalize(raw); assert.equal(result.ok, false, phrase); assert.ok(result.errors.some((e) => e.includes(phrase)), `${phrase} absent from ${result.errors.join('; ')}`); };

test('a usable incident normalizes to a copy; a frozen input is never mutated', () => {
  assert.deepEqual(normalizeIncident(incident), { ok: true, incident: { ...incident } }); assert.notEqual(normalizeIncident(incident).incident, incident);
  assert.deepEqual(normalizeIncident({ id: 'i2', kind: 'host_unreachable', severity: 'critical' }).incident, { id: 'i2', kind: 'host_unreachable', severity: 'critical' });
});

test('an unusable incident is rejected with reasons, never repaired into looking valid', () => {
  const cases = [[null, 'incident must be a plain object'], [[incident], 'incident must be a plain object'], [5, 'incident must be a plain object'], [{ ...incident, id: '' }, 'id must be a non-empty string'],
    [{ ...incident, kind: 7 }, 'kind must be a non-empty string'], [{ id: 'i1', kind: 'k' }, 'severity must be one of'], [{ ...incident, severity: 'urgent' }, 'severity must be one of'],
    [{ ...incident, hostId: 5 }, 'hostId must be a non-empty string when present'], [{ ...incident, sessionId: '' }, 'sessionId must be a non-empty string when present'],
    [{ ...incident, at: 'soon' }, 'at must be a finite number when present'], [{ ...incident, renderer: () => '' }, 'unknown key renderer'], [{ ...incident, html: '<b>' }, 'unknown key html'], [{ ...incident, fetch: () => {} }, 'unknown key fetch']];
  for (const [raw, phrase] of cases) rejects(normalizeIncident, raw, phrase);
  assert.deepEqual(SEVERITIES, ['info', 'warning', 'error', 'critical']);
});

test('an event needs identity, a finite instant, a type, and a plain object payload at most', () => {
  const ok = event('e1', 3, { payload: { status: 'running' }, sessionId: 's1' });
  assert.deepEqual(normalizeEvent(ok), { ok: true, event: { ...ok } });
  const cases = [[event('e6', 1, { missionKey: '' }), 'missionKey must be a non-empty string when present'], [event('', 1), 'id must be a non-empty string'], [event('e1', '2026-11-08T12:00:00Z'), 'at must be a finite number'],
    [event('e1', Infinity), 'at must be a finite number'], [event('e2', 1, { eventType: '' }), 'eventType must be a non-empty string'], [event('e3', 1, { payload: [] }), 'payload must be a plain object when present'],
    [event('e4', 1, { missionKey: 9 }), 'missionKey must be a non-empty string when present'], [event('e5', 1, { html: '<b>' }), 'unknown key html']];
  for (const [raw, phrase] of cases) rejects(normalizeEvent, raw, phrase);
});

test('a record with no mission is a valid point, because host-scoped events carry none', () => {
  // Measured against the real control plane (`GET /api/v1/events?entity_type=host&entity_id=hive`, recorded in
  // tests/fixtures/diagnostics_events_host.json): every host-scoped envelope has `mission_id: null` — the
  // ingest path binds a host record to its host, not to a mission. A model that required a missionKey would
  // drop every record the host diagnostics page can actually read. A mission filter still excludes it.
  const hostScoped = Object.freeze({ id: 'event_recorded_1', at: 5, eventType: 'HostHeartbeat', hostId: 'hive' });
  assert.deepEqual(normalizeEvent(hostScoped), { ok: true, event: hostScoped });
  const items = [event('m1_record', 5), hostScoped];
  assert.deepEqual(buildTimeline(items).timeline.map((e) => e.id), ['m1_record', 'event_recorded_1']);
  const filtered = buildTimeline(items, { missionKey: 'm1' });
  assert.deepEqual(filtered.timeline.map((e) => e.id), ['m1_record']);
  assert.equal(filtered.dropped, 1);
});

test('events sharing an instant keep the order they arrived in', () => {
  const same = ['first', 'second', 'third', 'fourth'].map((id) => event(id, 100)); assert.deepEqual(buildTimeline(same).timeline.map((e) => e.id), ['first', 'second', 'third', 'fourth']);
  const mixed = [event('b', 5), event('last', 100), ...same, event('a', 1), event('c', 5)];
  assert.deepEqual(buildTimeline(mixed).timeline.map((e) => e.id), ['a', 'b', 'c', 'last', 'first', 'second', 'third', 'fourth']);
});

test('filters narrow the timeline and everything discarded is counted, not hidden', () => {
  const items = [event('keep', 40), event('other_mission', 20, { missionKey: 'm2' }), event('other_kind', 30, { eventType: 'incident' }), { id: 'broken', missionKey: 'm1', at: 'soon', eventType: 'status' }, event('bad_key', 45, { html: '<b>' }), event('late', 99)];
  const all = buildTimeline(items);
  assert.deepEqual(all.timeline.map((e) => e.id), ['other_mission', 'other_kind', 'keep', 'late']); assert.deepEqual({ dropped: all.dropped, invalid: all.invalid }, { dropped: 0, invalid: 2 });
  const kept = buildTimeline(items, { missionKey: 'm1', kinds: ['status'], since: 25, until: 45 });
  assert.deepEqual(kept.timeline.map((e) => e.id), ['keep']); assert.deepEqual({ dropped: kept.dropped, invalid: kept.invalid }, { dropped: 3, invalid: 2 });
  assert.deepEqual(buildTimeline([event('solo', 1)], { kinds: 'status', since: 'yesterday' }).timeline.map((e) => e.id), ['solo']);
  assert.deepEqual(buildTimeline(null), { timeline: [], dropped: 0, invalid: 0 });
});

test('drift comes only from usable numbers; an unusable comparison drifts nowhere and says why', () => {
  assert.deepEqual(decideDrift({ expected: 10, actual: 12, tolerance: 3 }), { drifted: false, reason: 'actual is within the tolerance of 3' });
  assert.deepEqual(decideDrift({ expected: 10, actual: 12, tolerance: 1 }), { drifted: true, reason: 'actual is 2 away from expected, above the tolerance of 1' });
  assert.equal(decideDrift({ expected: 10, actual: 10 }).drifted, false); assert.equal(decideDrift({ expected: 10, actual: 10.5 }).drifted, true);
  const unusable = { drifted: false, reason: 'drift is undecidable: the numbers given are unusable' };
  for (const raw of [undefined, null, {}, { expected: 1 }, { expected: '10', actual: 12 }, { expected: Infinity, actual: 12 }, { expected: 10, actual: NaN }]) assert.deepEqual(decideDrift(raw), unusable);
  for (const raw of [{ expected: 10, actual: 12, tolerance: -1 }, { expected: 10, actual: 12, tolerance: 'wide' }, { expected: 10, actual: 12, tolerance: Infinity }]) { assert.equal(decideDrift(raw).drifted, false, JSON.stringify(raw)); assert.match(decideDrift(raw).reason, /tolerance is unusable/); }
});

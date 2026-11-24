import test from 'node:test';
import assert from 'node:assert/strict';
import { freshnessState, STREAM_SILENCE_MS } from '../freshness.js';
import { connectEvents } from './events.js';

const T = 1_000_000;

test('a quiet fleet stays fresh while the stream keepalive keeps arriving', () => {
  // Last refresh 60 s ago (nothing changed), keepalive 8 s ago: still current, not amber.
  const contact = { connected: true, at: T - 8000 };
  assert.equal(freshnessState({ now: T, last: T - 60000, interval: 5000, contact }), 'fresh');
});

test('the chip goes amber once the stream falls silent or disconnects', () => {
  const silent = { connected: true, at: T - STREAM_SILENCE_MS - 1 };
  assert.equal(freshnessState({ now: T, last: T - 60000, interval: 5000, contact: silent }), 'stale');
  const down = { connected: false, at: T - 1000 };
  assert.equal(freshnessState({ now: T, last: T - 11000, interval: 5000, contact: down }), 'stale');
  assert.equal(freshnessState({ now: T, last: T - 9000, interval: 5000, contact: down }), 'fresh');
});

test('without stream contact the old 2x-interval rule applies; errors always win', () => {
  const none = { connected: false, at: null };
  assert.equal(freshnessState({ now: T, last: T - 9000, interval: 5000, contact: none }), 'fresh');
  assert.equal(freshnessState({ now: T, last: T - 11000, interval: 5000, contact: none }), 'stale');
  assert.equal(freshnessState({ now: T, last: null, contact: none }), 'fresh');
  assert.equal(freshnessState({ now: T, last: T, error: 'x', contact: { connected: true, at: T } }), 'error');
});

test('connectEvents reports contact for keepalives but never delivers them as events', () => {
  let es; let contacts = 0; const events = [];
  connectEvents({ url: '/s', createEventSource: () => (es = {}), onContact: () => contacts++, onEvent: (e) => events.push(e), autoReconnectMs: 0 });
  es.onopen();
  es.onmessage({ data: JSON.stringify({ event_type: 'StreamConnected' }) });
  es.onmessage({ data: JSON.stringify({ event_type: 'StreamKeepalive' }) });
  es.onmessage({ data: JSON.stringify({ event_type: 'SessionUpdated', event_id: 'a' }) });
  assert.equal(contacts, 4);
  assert.deepEqual(events.map((e) => e.event_type), ['SessionUpdated']);
});

test('freshness text splits into a short state label and a ticking detail, and the label only changes with the state', async () => {
  const { freshnessParts } = await import('../freshness.js');
  const base = { ago: 'updated 3s ago', error: null, detail: '', waiting: false };
  const a = freshnessParts({ ...base, state: 'fresh' });
  const b = freshnessParts({ ...base, ago: 'updated 4s ago', state: 'fresh' });
  assert.equal(a.label, 'live');
  assert.equal(a.label, b.label, 'ticking the age never changes the announced label');
  assert.notEqual(a.rest, b.rest);
  assert.equal(a.label + a.rest, 'live · updated 3s ago');
  assert.equal(freshnessParts({ ...base, state: 'stale', detail: 'partial' }).label, 'stale');
  const err = freshnessParts({ ...base, state: 'error', error: 'boom' });
  assert.equal(err.label + err.rest, 'error · boom · updated 3s ago');
  const wait = freshnessParts({ ...base, state: 'fresh', waiting: true });
  assert.equal(wait.label + wait.rest, 'waiting for first update');
});

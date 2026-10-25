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

import test from 'node:test';
import assert from 'node:assert/strict';
import { renderFleetMatrix, selectComponents } from '../fleet-view.js';

const now = Date.parse('2026-10-21T18:00:00Z');
const matrix = {
  hosts: [{ host: 'bender', collected_at: '2026-10-21T17:55:00Z' }, { host: 'mac', collected_at: '2026-10-20T01:00:00Z' }],
  components: [
    { group: 'tools', name: 'claude', reference: '2.1.0', drifted: 1, missing: 0,
      hosts: { bender: { version: '2.1.0', state: 'ok' }, mac: { version: '2.0.0', state: 'drifted' } } },
    { group: 'plugins', name: 'claude:superpowers', reference: '7.0.0', drifted: 0, missing: 1,
      hosts: { bender: { version: '7.0.0', state: 'ok' }, mac: { version: null, state: 'missing' } } },
    { group: 'tools', name: 'node', reference: '22', drifted: 0, missing: 0,
      hosts: { bender: { version: '22', state: 'ok' }, mac: { version: '22', state: 'ok' } } },
  ],
  summary: { drifted: 1, missing: 1 },
};

test('matrix shows drift as text, marks stale hosts, escapes hostnames', () => {
  const html = renderFleetMatrix(matrix, {}, now);
  assert.match(html, /hd-fleet__cell--drifted[^>]*>2\.0\.0 <span[^>]*>≠/);
  assert.match(html, /stale · /);
  assert.match(html, /superpowers/);
  const evil = renderFleetMatrix({ ...matrix, hosts: [{ host: '<img onerror=x>', collected_at: null }], components: [] }, {}, now);
  assert.ok(!evil.includes('<img'));
});

test('drifted-only filter hides components that match everywhere', () => {
  assert.deepEqual(selectComponents(matrix, { driftedOnly: true }).map(c => c.name), ['claude', 'claude:superpowers']);
  assert.match(renderFleetMatrix(matrix, { driftedOnly: true }, now), /claude/);
  assert.ok(!renderFleetMatrix(matrix, { driftedOnly: true }, now).includes('>node<'));
});

test('empty fleet explains how data arrives', () => {
  assert.match(renderFleetMatrix({ hosts: [], components: [] }), /No host has reported/);
});

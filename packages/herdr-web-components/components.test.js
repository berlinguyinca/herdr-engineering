import test from 'node:test';
import assert from 'node:assert/strict';

import {
  STATUS_META, statusForStage, statusLabel, tokenVar,
} from './status.js';
import {
  formatBytes, formatDuration, formatTimestamp, formatTokens,
} from './format.js';
import { shortenId } from './util.js';

test('statusForStage maps canonical stages to semantic tokens', () => {
  assert.equal(statusForStage('implementation').label, 'Implementation');
  assert.equal(statusForStage('implementation').token, '--status-active');
  assert.equal(statusForStage('planning').token, '--status-planning');
  assert.equal(statusForStage('failed').label, 'Failed');
  assert.equal(statusForStage('failed').token, '--status-failed');
  // every stage has a non-empty label (color is never the only signal)
  for (const meta of Object.values(STATUS_META)) {
    assert.ok(meta.label.length > 0, `label for ${meta.label}`);
  }
});

test('statusForStage falls back to unknown', () => {
  assert.equal(statusForStage('not-a-stage').label, 'Unknown');
  assert.equal(statusForStage(undefined).label, 'Unknown');
});

test('statusLabel and tokenVar helpers', () => {
  assert.equal(statusLabel('review'), 'Review');
  assert.equal(tokenVar('--status-ok'), 'var(--status-ok)');
  assert.equal(tokenVar(null), 'var(--status-waiting)');
});

test('formatBytes handles bytes through terabytes', () => {
  assert.equal(formatBytes(0), '0 B');
  assert.equal(formatBytes(512), '512 B');
  assert.equal(formatBytes(1024), '1 KB');
  assert.equal(formatBytes(1536), '1.5 KB');
  assert.equal(formatBytes(null), '—');
});

test('formatDuration renders ms/s/m/h/d', () => {
  assert.equal(formatDuration(500), '500ms');
  assert.equal(formatDuration(2000), '2s');
  assert.equal(formatDuration(125000), '2m 5s');
  assert.equal(formatDuration(3600000), '1h 0m');
  assert.equal(formatDuration(null), '—');
});

test('formatTimestamp and formatTokens', () => {
  assert.equal(formatTimestamp(''), '—');
  assert.equal(formatTimestamp('garbage'), '—');
  assert.equal(formatTokens(1234), '1,234');
  assert.equal(formatTokens(null), '—');
});

test('shortenId keeps short ids and abbreviates long ones', () => {
  assert.equal(shortenId('abc'), 'abc');
  const long = 'a'.repeat(32);
  const short = shortenId(long);
  assert.ok(short.length < long.length);
});

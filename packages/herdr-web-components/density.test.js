import test from 'node:test';
import assert from 'node:assert/strict';
import { sanitizeDensity } from './density.js';

test('density accepts only supported persisted values and defaults malformed storage', () => {
  for (const value of ['comfortable', 'compact', 'dense']) assert.equal(sanitizeDensity(value), value);
  for (const value of [null, undefined, '', 'Dense', '{}', 'wide', '<script>', {}, 1]) {
    assert.equal(sanitizeDensity(value), 'comfortable');
  }
});

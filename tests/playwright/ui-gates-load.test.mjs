// The wait scaling that keeps a loaded CI host from timing out the real-page gates (ui-gates-lib.mjs loadScale).
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { loadScale } from './ui-gates-lib.mjs';

test('an idle or lightly loaded host keeps the stock waits', () => {
  assert.equal(loadScale(0, 8), 1);
  assert.equal(loadScale(7.9, 8), 1);
});

test('a loaded host stretches waits in proportion, capped at 3x', () => {
  assert.equal(loadScale(12, 8), 1.5);
  assert.equal(loadScale(80, 8), 3);
});

test('nonsense input never shrinks or breaks the wait', () => {
  assert.equal(loadScale(NaN, 8), 1);
  assert.equal(loadScale(5, 0), 3);
  assert.equal(loadScale(-2, 8), 1);
});

test('scaled() stretches an explicit wait by the current load and never below the base', async () => {
  const { scaled } = await import('./ui-gates-lib.mjs');
  const wait = scaled(8000);
  assert.ok(wait >= 8000 && wait <= 24000, `scaled(8000) = ${wait}`);
});

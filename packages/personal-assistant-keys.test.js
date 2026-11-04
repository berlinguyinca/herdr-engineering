import test from 'node:test';
import assert from 'node:assert/strict';
import { matches, roveIndex, TABS } from './personal-assistant-keys.js';

test('roveIndex wraps arrows and jumps with Home/End', () => {
  assert.equal(roveIndex('ArrowRight', 9, 10), 0);
  assert.equal(roveIndex('ArrowLeft', 0, 10), 9);
  assert.equal(roveIndex('Home', 4, 10), 0);
  assert.equal(roveIndex('End', 4, 10), 9);
  assert.equal(roveIndex('x', 4, 10), null);
  assert.equal(roveIndex('ArrowRight', 0, 0), null);
});

test('matches needs every typed word, in any order, ignoring case', () => {
  assert.equal(matches('Configure: Diagnostics', 'diag config'), true);
  assert.equal(matches('Configure: Diagnostics', 'diag memory'), false);
  assert.equal(matches('Anything', ''), true);
});

test('there is a tab for each digit 1-9 and 0', () => assert.equal(TABS.length, 10));

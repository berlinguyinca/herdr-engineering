import test from 'node:test';
import assert from 'node:assert/strict';
import { mentionAt, suggest, applyMention, quiet } from '../collab-mention.js';

const agents = [
  { name: 'coder', actions: ['read', 'execute'] }, { name: 'code-review', actions: ['execute'] },
  { name: 'scribe', actions: ['read', 'post'] }, { name: 'decoder', actions: ['execute'] },
];

test('mentionAt finds a mention at the start or after whitespace', () => {
  assert.deepEqual(mentionAt('@co', 3), { start: 0, query: 'co' });
  assert.deepEqual(mentionAt('hi @Co', 6), { start: 3, query: 'co' });
  assert.deepEqual(mentionAt('hi @', 4), { start: 3, query: '' });
});

test('mentionAt ignores addresses, finished mentions and odd characters', () => {
  assert.equal(mentionAt('me@host', 7), null);
  assert.equal(mentionAt('@coder do it', 12), null);
  assert.equal(mentionAt('@co<script>', 11), null);
  assert.equal(mentionAt('plain text', 5), null);
  assert.deepEqual(mentionAt('@coder do it', 6), { start: 0, query: 'coder' });
});

test('suggest offers only agents that accept tasks, prefix matches first', () => {
  assert.deepEqual(suggest(agents, 'co').map((a) => a.name), ['code-review', 'coder', 'decoder']);
  assert.deepEqual(suggest(agents, 'scr'), []);
  assert.equal(suggest(agents, '', 2).length, 2);
});

test('applyMention swaps the typed fragment for the full name', () => {
  assert.deepEqual(applyMention('hi @co and more', 6, 3, 'coder'), { text: 'hi @coder  and more', caret: 10 });
  assert.deepEqual(applyMention('@', 1, 0, 'coder'), { text: '@coder ', caret: 7 });
});

test('quiet runs once after the caller stops', () => {
  const calls = []; const pending = [];
  const timers = { set: (f) => { pending.push(f); return pending.length; }, clear: (id) => { if (id) pending[id - 1] = null; } };
  const q = quiet((x) => calls.push(x), 120, timers);
  q(1); q(2); q(3);
  pending.forEach((f) => f && f());
  assert.deepEqual(calls, [3]);
});

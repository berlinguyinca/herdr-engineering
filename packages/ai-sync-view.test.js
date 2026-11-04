import test from 'node:test';
import assert from 'node:assert/strict';
import { mountAiSync, AI_SYNC_POLL_MS } from './ai-sync-view.js';

const fakeRoot = () => { const list = { innerHTML: '' }; return { root: { innerHTML: '', querySelector: () => list }, list }; };
const settle = () => new Promise((resolve) => setImmediate(resolve));

test('the freshness chip follows every poll: red when a poll fails after the first load, fresh again on recovery', async (t) => {
  t.mock.timers.enable({ apis: ['setInterval'] });
  let fail = false;
  const client = { async listAiSync() { if (fail) throw new Error('api down'); return { hosts: [] }; } };
  const { root, list } = fakeRoot();
  const reports = [];
  const stop = mountAiSync(root, client, { hosts: [] }, { freshness: (ok, message) => reports.push([ok, message]) });
  const before = list.innerHTML;
  fail = true;
  t.mock.timers.tick(AI_SYNC_POLL_MS);
  await settle();
  assert.deepEqual(reports, [[false, 'api down']]);
  assert.equal(list.innerHTML, before, 'the last list stays on screen');
  fail = false;
  t.mock.timers.tick(AI_SYNC_POLL_MS);
  await settle();
  assert.deepEqual(reports.at(-1), [true, undefined]);
  stop();
});

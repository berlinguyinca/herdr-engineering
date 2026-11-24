import test from 'node:test';
import assert from 'node:assert/strict';
import { createMissionTitles } from './mission-titles.js';

const missions = (...titles) => ({ missions: titles.map((title, i) => ({ mission_id: `m${i + 1}`, title })) });

test('a failed first load is not cached: the next call retries and resolves titles', async () => {
  let calls = 0;
  const titles = createMissionTitles(async () => { calls += 1; if (calls === 1) throw new Error('429'); return missions('Alpha'); });
  assert.deepEqual(await titles(), {});
  assert.deepEqual(await titles(), { m1: 'Alpha' });
  assert.equal(calls, 2);
});

test('titles are reused within the ttl and refreshed after it, so missions created later resolve', async () => {
  let clock = 1000;
  let listed = missions('Alpha');
  let calls = 0;
  const titles = createMissionTitles(async () => { calls += 1; return listed; }, { ttlMs: 60000, now: () => clock });
  assert.deepEqual(await titles(), { m1: 'Alpha' });
  listed = missions('Alpha', 'Beta');
  clock += 59000;
  assert.deepEqual(await titles(), { m1: 'Alpha' });
  assert.equal(calls, 1);
  clock += 2000;
  assert.deepEqual(await titles(), { m1: 'Alpha', m2: 'Beta' });
  assert.equal(calls, 2);
});

test('a refresh that fails keeps serving the last good titles and retries on the next call', async () => {
  let clock = 0;
  let fail = false;
  let calls = 0;
  const titles = createMissionTitles(async () => { calls += 1; if (fail) throw new Error('down'); return missions('Alpha'); }, { ttlMs: 10, now: () => clock });
  assert.deepEqual(await titles(), { m1: 'Alpha' });
  clock = 100; fail = true;
  assert.deepEqual(await titles(), { m1: 'Alpha' });
  fail = false;
  assert.deepEqual(await titles(), { m1: 'Alpha' });
  assert.equal(calls, 3);
});

test('concurrent callers share one request', async () => {
  let calls = 0;
  const titles = createMissionTitles(async () => { calls += 1; await new Promise((r) => { setTimeout(r, 20); }); return missions('Alpha'); });
  const [a, b] = await Promise.all([titles(), titles()]);
  assert.deepEqual(a, b);
  assert.equal(calls, 1);
});

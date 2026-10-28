import test from 'node:test';
import assert from 'node:assert/strict';
import { createResourcePoller } from '../refresh-bus.js';

const settle = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); };
function setup(count = 6, options = {}) {
  let now = 0;
  const timers = new Set(), calls = [], applied = [], failed = [];
  const loads = Object.fromEntries(Array.from({ length: count }, (_, key) => [String(key), signal => new Promise((resolve, reject) => calls.push({ key: String(key), signal, resolve, reject }))]));
  const poller = createResourcePoller(loads, {
    apply: (key, value) => applied.push([key, value]), onError: (key, error) => failed.push([key, error]),
    now: () => now, random: () => .5,
    setTimer: (fn, delay) => { const timer = { fn, at: now + delay }; timers.add(timer); return timer; },
    clearTimer: timer => timers.delete(timer), ...options,
  });
  async function advance(ms) {
    const until = now + ms;
    for (;;) {
      const timer = [...timers].filter(t => t.at <= until).sort((a, b) => a.at - b.at)[0];
      if (!timer) break;
      now = timer.at; timers.delete(timer); timer.fn(); await settle();
    }
    now = until; await settle();
  }
  return { poller, calls, applied, failed, advance, timers };
}

test('four resource slots, independent completion, and no duplicate refresh reads', async () => {
  const h = setup(); h.poller.refresh(); h.poller.refresh();
  assert.deepEqual(h.calls.map(c => c.key), ['0', '1', '2', '3']);
  h.calls[1].resolve('ready'); await settle();
  assert.deepEqual(h.applied, [['1', 'ready']]);
  assert.equal(h.calls[4].key, '4');
  h.poller.refresh(); assert.equal(h.calls.length, 5);
  h.calls[2].resolve('another'); await settle(); assert.equal(h.calls[5].key, '5');
  h.poller.dispose(); assert.equal(h.timers.size, 0);
});

test('hidden and disposed views cancel queued work and ignore late replies', async () => {
  const h = setup(); h.poller.refresh(); const stale = [...h.calls];
  h.poller.pause(); assert.ok(stale.every(c => c.signal.aborted));
  stale.forEach(c => c.resolve('late')); await settle();
  await h.advance(60000); assert.equal(h.calls.length, 4); assert.deepEqual(h.applied, []);
  h.poller.resume(); assert.equal(h.calls.length, 8);
  h.poller.dispose(); h.calls.slice(4).forEach(c => c.resolve('disposed')); await settle();
  await h.advance(60000); assert.equal(h.calls.length, 8); assert.deepEqual(h.applied, []);
});

test('a hung client cannot occupy slots past its deadline', async () => {
  const h = setup(); h.poller.refresh(); await h.advance(15000);
  assert.equal(h.failed.length, 4); assert.ok(h.failed.every(([, e]) => e.name === 'TimeoutError'));
  assert.deepEqual(h.calls.slice(4).map(c => c.key), ['4', '5']);
  assert.ok(h.calls.slice(0, 4).every(c => c.signal.aborted));
  h.poller.dispose();
});

test('failure backoff grows, honors Retry-After across refresh/visibility, and resets on success', async () => {
  const h = setup(1); h.poller.refresh();
  h.calls[0].reject(new Error('down')); await settle();
  h.poller.refresh(); await h.advance(4999); assert.equal(h.calls.length, 1);
  await h.advance(1); h.calls[1].reject(new Error('down again')); await settle();
  await h.advance(9999); assert.equal(h.calls.length, 2);
  await h.advance(1); h.calls[2].reject(Object.assign(new Error('rate limit'), { retryAfterMs: 120000 })); await settle();
  h.poller.pause(); h.poller.resume(); h.poller.refresh();
  await h.advance(119999); assert.equal(h.calls.length, 3);
  await h.advance(1); h.calls[3].resolve('recovered'); await settle();
  await h.advance(5000); h.calls[4].reject(new Error('new failure')); await settle();
  await h.advance(5000); assert.equal(h.calls.length, 6);
  h.poller.dispose();
});

test('backoff is jittered, bounded, and retries stay single flight', async () => {
  for (const random of [() => 0, () => 1]) {
    const h = setup(1, { random, maxBackoffMs: 10000 }); h.poller.refresh();
    h.calls[0].reject(new Error('down')); await settle();
    const delay = random() === 0 ? 4000 : 6000;
    await h.advance(delay - 1); assert.equal(h.calls.length, 1);
    await h.advance(1); assert.equal(h.calls.length, 2);
    h.poller.refresh(); assert.equal(h.calls.length, 2);
    h.calls[1].reject(new Error('down')); await settle();
    await h.advance(10000); assert.equal(h.calls.length, 3);
    h.poller.dispose();
  }
});

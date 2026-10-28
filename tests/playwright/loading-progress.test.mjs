import { test } from 'node:test';
import assert from 'node:assert/strict';
import { withPage } from './helpers/fixture.mjs';

test('slow skeletons expose named indeterminate progress after 300ms and cancel on removal', async () => withPage(async (page, base) => {
  await page.goto(`${base}/__blank`); await page.clock.install({ time: new Date('2026-01-01T00:00:00Z') });
  await page.clock.pauseAt(new Date('2026-01-01T00:00:01Z'));
  await page.addStyleTag({ url: `${base}/motion.css` });
  await page.evaluate(async () => {
    const { skeleton } = await import('/motion.js');
    document.body.innerHTML = skeleton('reports <untrusted>');
  });
  await page.clock.runFor(299); assert.equal(await page.locator('progress').count(), 0);
  await page.clock.runFor(1);
  const progress = page.getByRole('progressbar', { name: 'Loading reports <untrusted>' });
  assert.equal(await progress.isVisible(), true);
  assert.equal(await progress.getAttribute('value'), null);
  assert.equal(await progress.getAttribute('aria-valuenow'), null);
  assert.equal(await page.locator('untrusted').count(), 0);
  await page.evaluate(async () => {
    const { skeleton } = await import('/motion.js');
    document.body.innerHTML = skeleton('fast response');
    window.removed = document.querySelector('herdr-loading-progress');
    document.body.textContent = 'Loaded';
  });
  await page.clock.runFor(1000);
  assert.equal(await page.evaluate(() => window.removed.querySelector('progress')), null);
}));

test('freshness waiting progress stops on success/error and calm/reduced motion keeps it static', async () => withPage(async (page, base) => {
  await page.goto(`${base}/__blank`); await page.clock.install({ time: new Date('2026-01-01T00:00:00Z') });
  await page.clock.pauseAt(new Date('2026-01-01T00:00:01Z'));
  await page.addStyleTag({ url: `${base}/motion.css` });
  await page.evaluate(async () => { await import('/freshness.js'); document.documentElement.dataset.calm = 'on'; document.body.innerHTML = '<herdr-freshness></herdr-freshness>'; });
  await page.clock.runFor(300);
  const progress = page.getByRole('progressbar', { name: 'Loading first update' });
  assert.equal(await progress.isVisible(), true);
  assert.deepEqual(await progress.evaluate(element => ({ position: element.position, appearance: getComputedStyle(element).appearance, animations: element.getAnimations().length })), { position: -1, appearance: 'none', animations: 0 });
  await page.evaluate(() => document.querySelector('herdr-freshness').touch(true));
  assert.equal(await page.locator('progress').count(), 0);
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await page.evaluate(() => { delete document.documentElement.dataset.calm; document.body.innerHTML = '<herdr-freshness></herdr-freshness>'; });
  await page.clock.runFor(300);
  assert.equal(await progress.evaluate(element => element.getAnimations().length), 0);
  await page.evaluate(() => document.querySelector('herdr-freshness').touch(false, 'Unavailable'));
  assert.equal(await page.locator('progress').count(), 0);
}));

test('Overview caps active resources at four and shows slow progress and useful errors independently', async () => withPage(async (page, base) => {
  await page.goto(`${base}/__blank`); await page.clock.install({ time: new Date('2026-01-01T00:00:00Z') });
  await page.clock.pauseAt(new Date('2026-01-01T00:00:01Z'));
  await page.addStyleTag({ url: `${base}/motion.css` });
  await page.evaluate(async () => {
    const { mountOverview } = await import('/overview-view.js');
    await import('/freshness.js');
    window.reads = [];
    const read = (key, signal) => new Promise((resolve, reject) => window.reads.push({ key, signal, resolve, reject }));
    const client = {
      attention: (_, { signal }) => read('attention', signal), listMissionActivity: ({ signal }) => read('live', signal),
      listMissions: (_, { signal }) => read('missions', signal), listSessions: (_, { signal }) => read('sessions', signal),
      listServices: ({ signal }) => read('services', signal), listHosts: ({ signal }) => read('hosts', signal),
      request: (_, { signal }) => read('fleet', signal), activity: (_, { signal }) => read('activity', signal),
      health: ({ signal }) => read('health', signal), getCi: ({ signal }) => read('ci', signal), listReviews: ({ signal }) => read('reviews', signal),
    };
    document.body.innerHTML = '<herdr-freshness></herdr-freshness><main></main>';
    window.overview = mountOverview(document.querySelector('main'), client, { freshness: () => document.querySelector('herdr-freshness') });
  });
  assert.equal(await page.evaluate(() => window.reads.length), 4);
  await page.clock.runFor(300);
  assert.equal(await page.getByRole('progressbar', { name: 'Loading agents' }).isVisible(), true);
  await page.evaluate(() => window.reads.find(r => r.key === 'sessions').resolve({ sessions: [] }));
  assert.equal(await page.locator('[data-overview="sessions"] progress').count(), 0);
  assert.equal(await page.evaluate(() => window.reads.length), 5, 'one independent completion opens one slot');
  await page.evaluate(() => window.reads.find(r => r.key === 'services').reject(Object.assign(new Error('database unavailable'), { elapsedMs: 12050, requestId: 'req-123', retryAfterMs: 60000 })));
  assert.match(await page.locator('[data-overview="services"]').innerText(), /database unavailable after 12.1s · Request req-123/);
  assert.match(await page.locator('herdr-freshness').innerText(), /Services: database unavailable/);
  await page.evaluate(() => window.overview.dispose());
  const count = await page.evaluate(() => window.reads.length);
  await page.clock.runFor(120000);
  assert.equal(await page.evaluate(() => window.reads.length), count, 'disposed Overview never starts queued or timed retries');
}));

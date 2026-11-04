import { test } from 'node:test';
import assert from 'node:assert/strict';
import { withPage } from './helpers/fixture.mjs';
import { boot, until } from './helpers/live.mjs';

const defaults = { items: [], status: 'ready', hosts: [], nodes: [], edges: [], artifacts: [], publications: [] };
const fail = (page, message) => page.evaluate((m) => document.querySelector('#view').dispatchEvent(new CustomEvent('hd-freshness', { bubbles: true, detail: { ok: false, message: m } })), message);
const entries = (page) => page.locator('#shell').getAttribute('routes').then((r) => JSON.parse(r).map((e) => e.path));
const stored = (page) => page.evaluate(() => JSON.parse(localStorage.getItem('herdr-unavailable-pages') || '[]'));
const go = async (page, hash) => { await page.evaluate((h) => { location.hash = h; }, hash); await page.waitForTimeout(400); };

test('a detail route or an optional tab failing never hides its section; only a whole missing section is dropped', async () => {
  await withPage(async (page, base) => {
    await boot(page, base, '/handoffs', () => ({ ...defaults, capabilities: [] }));
    await until(() => entries(page).then((e) => e.includes('/reviews')), 'nav listed');

    await go(page, '#/missions/gone');
    await fail(page, 'HTTP 404 not found');
    assert.deepEqual(await stored(page), [], 'a missing mission says nothing about the Agents section');
    assert.ok((await entries(page)).includes('/herd'));

    await go(page, '#/review-gate');
    await fail(page, 'HTTP 404 not found');
    assert.deepEqual(await stored(page), ['/review-gate']);
    await go(page, '#/reviews');
    assert.ok((await entries(page)).includes('/reviews'), 'Reviews stays: Plan reviews still works');
    assert.equal(await page.locator('#subnav a', { hasText: 'Review gate' }).count(), 0, 'the failed tab leaves the strip');

    await fail(page, 'HTTP 404 not found');   // now Plan reviews too: every page of the section is marked
    await go(page, '#/hosts');
    assert.ok(!(await entries(page)).includes('/reviews'), 'a section whose pages are all missing is dropped');
    await page.locator('#show-unavailable').click();
    assert.ok((await entries(page)).includes('/reviews'), 'Show unavailable lists it again');

    await go(page, '#/hosts');
    await fail(page, 'Request timed out');
    assert.ok(!(await stored(page)).includes('/hosts'), 'a timeout is transient and marks nothing');
  });
});

// Project intelligence view: timeline, evidence pane, URL state, escaping, scope hiding, phone width, dark/light.
// The API is stubbed with page.route() so this test needs no server and never touches a real control plane.
import { test } from 'node:test';
import assert from 'node:assert';
import fs from 'node:fs';
import path from 'node:path';
import { withPage } from './helpers/fixture.mjs';

const SHOTS = process.env.PI_SHOTS || '';
const EVIDENCE = { source_ref: 'gmail:acct:m1', anchor_type: 'email_part', anchor_value: '1.2' };
const TL = { total: 4, items: [
  { event_id: 'e1', title: 'Peak plot attached', category: 'email', occurred_at: '2026-10-01T12:00:00+00:00', observed_at: '2026-10-01T12:05:00+00:00',
    actor: 'email:alex.a@lab-a.example', evidence_refs: [EVIDENCE], involved_entities: ['email:m1'], late_arrival: false, superseded: false, tombstoned: false, label: 'verified' },
  { event_id: 'e2', title: '<img src=x onerror=window.__xss=1>PR #7 opened', category: 'work', occurred_at: '2026-10-02T09:00:00+00:00', observed_at: '2026-10-03T09:00:00+00:00',
    actor: 'github:bot', evidence_refs: [{ source_ref: 'github:acme/app', anchor_type: 'object', anchor_value: 'pr/7', git_sha: 'a'.repeat(40) }],
    involved_entities: ['pr:acme/app#7'], late_arrival: true, superseded: false, tombstoned: false, label: 'verified' },
  { event_id: 'e3', title: 'Inferred follow-up', category: 'work', occurred_at: '2026-10-02T10:00:00+00:00', observed_at: '2026-10-02T10:00:00+00:00',
    actor: null, evidence_refs: [], involved_entities: ['issue:acme/app#12'], late_arrival: false, superseded: false, tombstoned: false, label: 'unverified' },
  { event_id: 'e4', title: '[removed at source]', category: 'email', occurred_at: '2026-10-02T11:00:00+00:00', observed_at: '2026-10-02T11:00:00+00:00',
    actor: null, evidence_refs: [], involved_entities: [], late_arrival: false, superseded: false, tombstoned: true, label: 'unverified' }] };

async function stub(page, { capabilities = ['project_intelligence_v1'], projects = [{ id: 'p-binbase', name: 'LC-BinBase', version: 1 }], timeline = TL, fail = false, seen = [] } = {}) {
  await page.route('**/api/v1/**', (route) => {
    const url = new URL(route.request().url());
    seen.push(url.pathname + url.search);
    const json = (b, s = 200) => route.fulfill({ status: s, contentType: 'application/json', body: JSON.stringify(b) });
    if (url.pathname === '/api/v1/health') return json({ status: 'ok', capabilities });
    if (url.pathname === '/api/v1/intelligence/projects') return fail ? json({ error: 'boom' }, 500) : json({ items: projects });
    if (url.pathname.endsWith('/timeline')) return json(timeline);
    return json({ items: [], hosts: [], missions: [], sessions: [] });
  });
}

const shot = async (page, name) => { if (SHOTS) { fs.mkdirSync(SHOTS, { recursive: true }); await page.screenshot({ path: path.join(SHOTS, name), fullPage: true }); } };

test('nav entry appears only with the capability', async () => {
  await withPage(async (page, base) => {
    await stub(page, { capabilities: [] });
    await page.goto(`${base}/#/overview`);
    await page.waitForSelector('text=Handoffs');   // the nav (a shadow-DOM element) has rendered
    assert.equal(await page.locator('text=Project intelligence').count(), 0);
  });
  await withPage(async (page, base) => {
    await stub(page);
    await page.goto(`${base}/#/overview`);
    await page.waitForSelector('text=Project intelligence', { timeout: 8000 });
  });
});

test('timeline renders, escapes untrusted titles, and shows evidence on selection', async () => {
  await withPage(async (page, base) => {
    const seen = [];
    await stub(page, { seen });
    await page.goto(`${base}/#/intelligence`);
    await page.waitForSelector('.pi-item');
    assert.equal(await page.evaluate(() => window.__xss), undefined, 'markup in a title must not execute');
    assert.equal(await page.locator('.pi-item').count(), 4);
    assert.match(await page.locator('.pi-pane').innerText(), /Select an event/);
    await page.locator('.pi-item[data-event="e2"]').click();
    const pane = await page.locator('.pi-pane').innerText();
    assert.match(pane, /github:acme\/app/); assert.match(pane, /aaaaaaaaaaaa/);
    assert.equal(await page.locator('.pi-pane a[href^="#/reviews?search="]').count(), 1, 'PR entity links through entity-links.js');
    assert.match(await page.locator('.pi-list').innerText(), /late arrival/);
    await page.locator('.pi-item[data-event="e3"]').click();
    assert.match(await page.locator('.pi-pane').innerText(), /No evidence reference/);
    assert.match(await page.locator('.pi-list').innerText(), /Unverified/);
    assert.match(await page.locator('.pi-list').innerText(), /Removed at source/);
    assert.ok(seen.some((u) => u.includes('/projects/p-binbase/timeline')));
    await shot(page, 'timeline-desktop-light.png');
  });
});

test('filters persist in the URL and drive the request', async () => {
  await withPage(async (page, base) => {
    const seen = [];
    await stub(page, { seen });
    await page.goto(`${base}/#/intelligence`);
    await page.waitForSelector('.pi-item');
    await page.fill('#pi-source', 'gmail'); await page.locator('#pi-source').blur();
    await page.waitForFunction(() => location.hash.includes('source=gmail'));
    assert.ok(seen.some((u) => u.includes('source=gmail')));
    await page.reload();
    await page.waitForSelector('.pi-item');
    assert.equal(await page.inputValue('#pi-source'), 'gmail');
  });
});

test('keyboard: events are real buttons reachable by Tab and activated by Enter', async () => {
  await withPage(async (page, base) => {
    await stub(page);
    await page.goto(`${base}/#/intelligence`);
    await page.waitForSelector('.pi-item');
    await page.locator('.pi-item').first().focus();
    await page.keyboard.press('Enter');
    assert.match(await page.locator('.pi-pane').innerText(), /Evidence/);
    assert.equal(await page.locator('.pi-item[aria-current="true"]').count(), 1);
    assert.equal(await page.locator('.pi-pane[aria-live="polite"]').count(), 1);
  });
});

test('empty, no-project and error states are explicit, never partial data', async () => {
  await withPage(async (page, base) => {
    await stub(page, { projects: [] });
    await page.goto(`${base}/#/intelligence`);
    await page.waitForSelector('text=No projects are visible');
  });
  await withPage(async (page, base) => {
    await stub(page, { timeline: { items: [], total: 0 } });
    await page.goto(`${base}/#/intelligence`);
    await page.waitForSelector('text=No events match');
  });
  await withPage(async (page, base) => {
    await stub(page, { fail: true });
    await page.goto(`${base}/#/intelligence`);
    await page.waitForSelector('[role=alert]');
  });
});

test('phone width: no horizontal scroll, detail stacks above the list; dark mode', async () => {
  await withPage(async (page, base) => {
    await stub(page);
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(`${base}/#/intelligence`);
    await page.waitForSelector('.pi-item');
    await page.locator('.pi-item[data-event="e1"]').click();
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    assert.ok(overflow <= 1, `horizontal overflow ${overflow}px`);
    const order = await page.evaluate(() => { const p = document.querySelector('.pi-pane').getBoundingClientRect().top; const l = document.querySelector('.pi-list').getBoundingClientRect().top; return p < l; });
    assert.ok(order, 'detail pane precedes the list on a phone');
    await shot(page, 'timeline-phone-light.png');
    await page.emulateMedia({ colorScheme: 'dark' });
    await shot(page, 'timeline-phone-dark.png');
    await page.setViewportSize({ width: 1280, height: 900 });
    await shot(page, 'timeline-desktop-dark.png');
  });
});

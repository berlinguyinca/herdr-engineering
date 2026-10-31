import { test } from 'node:test';
import assert from 'node:assert/strict';
import { withPage } from './helpers/fixture.mjs';
import { boot, until, sleep } from './helpers/live.mjs';

const defaults = { items: [], status: 'ready', hosts: [], nodes: [], edges: [], artifacts: [], publications: [], workspaces: [] };

test('artifact and publication tables contain wide data on mobile', async () => {
  for (const route of ['/artifacts', '/publications']) await withPage(async (page, base) => {
    await boot(page, base, route, () => ({ ...defaults,
      artifacts: [{ artifact_id: 'a1', display_name: 'Artifact-with-a-long-unbroken-name-'.repeat(5), created_at: '2026-10-26T12:34:56Z' }],
      publications: [{ publication_id: 'p1', resource_id: 'a1', pinned_version_id: 'version-'.repeat(20), state: 'requested' }],
    }));
    await page.setViewportSize({ width: 390, height: 844 });
    await page.locator('.hd-table').waitFor();
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false, route);
    assert.equal(await page.locator('.hd-table-wrap').evaluate(element => element.scrollWidth > element.clientWidth), true, 'wide tables scroll inside the card');
  });
});

test('restored navigation retains upstream routes alongside the fabric routes', async () => withPage(async (page, base) => {
  const errors = []; page.on('pageerror', error => errors.push(error.message));
  await boot(page, base, '/handoffs', () => ({ ...defaults, capabilities: ['architecture_v1'] }));
  await until(() => page.locator('[data-handoff-list]').textContent().then(text => text.includes('No handoffs')), 'handoffs loaded');
  const routes = await page.locator('#shell').getAttribute('routes').then(JSON.parse);
  for (const path of ['/attention', '/architecture', '/ci', '/reviews', '/review-gate', '/llm', '/collab', '/local-services', '/system-services', '/artifacts', '/publications', '/scheduling', '/handoffs']) {
    assert.ok(routes.some(route => route.path === path || (route.match || []).includes(path)), `navigation preserves ${path} (as an entry or a tab of one)`);
  }
  assert.deepEqual(errors, []);
}));

test('artifact filters discard stale reads and cancel their debounce when navigating away', async () => withPage(async (page, base) => {
  let release, reads = 0;
  const delayed = new Promise(resolve => { release = resolve; });
  await boot(page, base, '/artifacts', async (path, url) => {
    if (path !== '/api/v1/artifacts') return defaults;
    reads++;
    if (url.searchParams.get('q') === 'old') await delayed;
    return { artifacts: [{ artifact_id: 'a/1', display_name: url.searchParams.get('q') || 'Initial' }] };
  });
  await until(() => page.locator('[data-artifact-results]').textContent().then(text => text.includes('Initial')), 'registry loaded');
  await page.locator('[data-artifact-query]').fill('old'); await page.clock.runFor(250); await until(() => reads === 2, 'old search');
  await page.locator('[data-artifact-query]').fill('new'); await page.clock.runFor(250);
  await until(() => page.locator('[data-artifact-results]').textContent().then(text => text.includes('new')), 'new search');
  release(); await sleep(100);
  assert.match(await page.locator('[data-artifact-results]').innerText(), /new/);
  assert.doesNotMatch(await page.locator('[data-artifact-results]').innerText(), /old/);
  await page.locator('[data-artifact-query]').fill('never');
  await page.evaluate(() => { location.hash = '#/handoffs'; });
  await page.locator('[data-handoff-list]').waitFor(); await page.clock.runFor(60000); await sleep(100);
  assert.equal(reads, 3, 'disposed registry neither searches nor polls');
}));

test('publication actions show progress, recover from errors and remain available for retry', async () => withPage(async (page, base) => {
  let action, attempts = 0;
  await boot(page, base, '/publications', path => path === '/api/v1/publications'
    ? { publications: [{ publication_id: 'p1', resource_id: 'a1', state: 'requested' }] } : defaults);
  await page.route('**/api/v1/publications/p1/approve', route => { attempts++; action = route; });
  const button = page.locator('[data-approve-publication]');
  await button.click(); await until(() => attempts === 1, 'approval request');
  assert.equal(await button.isDisabled(), true);
  assert.match(await page.locator('[data-publication-state]').innerText(), /Approving/);
  await action.fulfill({ status: 503, json: { error: 'approval unavailable' } });
  await until(() => button.isEnabled(), 'retry enabled');
  assert.match(await page.locator('[data-publication-state]').innerText(), /approval unavailable/);
  await button.click(); await until(() => attempts === 2, 'approval retry');
  await action.fulfill({ json: { state: 'active' } });
  await until(() => button.isEnabled(), 'approval settled');
}));

test('scheduler preview keeps edited inputs and exposes progress and errors', async () => withPage(async (page, base) => {
  let action;
  await boot(page, base, '/scheduling', () => defaults);
  await page.route('**/api/v1/scheduler/plan', route => { action = route; });
  const form = page.locator('[data-scheduler-plan]'); await form.waitFor();
  await form.locator('[name=request_id]').fill('my-preview');
  await page.clock.runFor(31000); await sleep(80);
  assert.equal(await form.locator('[name=request_id]').inputValue(), 'my-preview');
  await form.locator('button').click(); await until(() => Boolean(action), 'placement request');
  assert.equal(action.request().postDataJSON().request_id, 'my-preview');
  assert.equal(await form.locator('button').isDisabled(), true);
  assert.match(await form.locator('output').innerText(), /Planning/);
  await action.fulfill({ status: 500, json: { error: 'scheduler unavailable' } });
  await until(() => form.locator('button').isEnabled(), 'preview retry');
  assert.match(await form.locator('output').innerText(), /scheduler unavailable/);
}));

test('a stalled handoff read is bounded, retries, and stops polling on disposal', async () => withPage(async (page, base) => {
  let stalled = true;
  const reads = await boot(page, base, '/handoffs', path => path === '/api/v1/handoffs/overview' ? (stalled ? { __hang: true } : { workspaces: [] }) : defaults);
  await until(() => reads['/api/v1/handoffs/overview'] === 1, 'handoff request started');
  await page.clock.runFor(16000);
  await until(() => page.locator('[data-handoff-retry]').isVisible(), 'bounded timeout');
  assert.equal(await page.locator('#view [data-skeleton]').count(), 0);
  assert.equal(reads['/api/v1/handoffs/overview'], 1, 'one request while stalled');
  stalled = false; await page.locator('[data-handoff-retry]').click();
  await until(() => page.locator('[data-handoff-summary]').innerText().then(text => text === '0 handoffs · 0 workspaces'), 'recovered graph');
  await page.evaluate(() => { location.hash = '#/scheduling'; }); await page.locator('[data-scheduler-plan]').waitFor();
  await page.clock.runFor(90000); await sleep(100);
  assert.equal(reads['/api/v1/handoffs/overview'], 2);
}));

test('artifact detail errors are visible and late responses cannot replace another route', async () => withPage(async (page, base) => {
  let release;
  const gate = new Promise(resolve => { release = resolve; });
  await boot(page, base, '/artifacts/a%2Fb', async path => {
    if (path === '/api/v1/artifacts/a%2Fb') { await gate; return { artifact_id: 'a/b', display_name: 'Late artifact' }; }
    return { ...defaults, versions: [], reviews: [] };
  });
  await page.locator('#artifact-detail [data-skeleton]').waitFor();
  await page.evaluate(() => { location.hash = '#/handoffs'; }); await page.locator('[data-handoff-list]').waitFor();
  release(); await sleep(100);
  assert.equal(await page.locator('#view h1').innerText(), 'Handoffs');
  assert.doesNotMatch(await page.locator('#view').innerText(), /Late artifact/);
}));

test('artifact preview survives optional version errors and recovers on retry', async () => withPage(async (page, base) => {
  let failed = true;
  await boot(page, base, '/artifacts/a1', path => {
    if (path.endsWith('/a1/versions')) return failed ? { __status: 503, json: { error: 'versions unavailable' } } : { versions: [] };
    if (path.endsWith('/a1/reviews')) return { reviews: [] };
    if (path.endsWith('/artifacts/a1')) return { artifact_id: 'a1', display_name: 'Design notes' };
    return defaults;
  });
  await page.locator('[data-route-retry]').waitFor();
  assert.equal(await page.getByRole('heading', { name: 'Design notes', exact: true }).isVisible(), true);
  assert.equal(await page.getByRole('heading', { name: 'Preview', exact: true }).isVisible(), true);
  assert.match(await page.locator('#artifact-detail').innerText(), /versions unavailable/);
  assert.match(await page.locator('[data-artifact-reviews]').innerText(), /No review requested/);
  assert.equal(await page.locator('#view [data-skeleton]').count(), 0);
  failed = false; await page.locator('[data-route-retry]').click();
  await page.getByRole('heading', { name: 'Design notes', exact: true }).waitFor();
  await until(() => page.locator('#view herdr-freshness').getAttribute('data-state').then(state => state === 'fresh'), 'metadata retry recovers');
}));

test('artifact preview loads before optional metadata and navigation cancels late sections', async () => withPage(async (page, base) => {
  let release;
  const gate = new Promise(resolve => { release = resolve; });
  await boot(page, base, '/artifacts/a1', async path => {
    if (path.endsWith('/a1/versions')) { await gate; return { versions: [{ version_id: 'late', version_number: 1 }] }; }
    if (path.endsWith('/a1/reviews')) { await gate; return { __status: 503, json: { error: 'late review error' } }; }
    if (path.endsWith('/artifacts/a1')) return { artifact_id: 'a1', display_name: 'Early preview' };
    return defaults;
  });
  await page.getByRole('heading', { name: 'Early preview', exact: true }).waitFor();
  assert.equal(await page.locator('[data-artifact-versions] [data-skeleton]').count(), 1);
  await page.evaluate(() => { location.hash = '#/handoffs'; });
  await page.locator('[data-handoff-list]').waitFor();
  release(); await sleep(100);
  assert.equal(await page.locator('#view h1').innerText(), 'Handoffs');
  assert.doesNotMatch(await page.locator('#view').innerText(), /late review error|Early preview/);
}));

test('optional artifact metadata has a deadline without hiding its preview', async () => withPage(async (page, base) => {
  await boot(page, base, '/artifacts/a1', path => {
    if (path.endsWith('/a1/versions')) return { __hang: true };
    if (path.endsWith('/a1/reviews')) return { reviews: [] };
    if (path.endsWith('/artifacts/a1')) return { artifact_id: 'a1', display_name: 'Legacy artifact' };
    return defaults;
  });
  await page.getByRole('heading', { name: 'Legacy artifact', exact: true }).waitFor();
  await page.clock.runFor(16000);
  await page.locator('[data-artifact-versions] [data-route-retry]').waitFor();
  assert.match(await page.locator('[data-artifact-versions]').innerText(), /timed out/i);
  assert.equal(await page.getByRole('link', { name: 'Download artifact', exact: true }).isVisible(), true);
  assert.equal(await page.locator('#view [data-skeleton]').count(), 0);
}));

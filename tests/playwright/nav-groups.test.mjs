// Sidebar: links sit under group headings, related pages are one link with a tab strip above the page, and the display
// controls are icon buttons. Every route stays reachable.
import { test } from 'node:test';
import assert from 'node:assert';
import { withPage } from './helpers/fixture.mjs';

const links = (page) => page.locator('herdr-app-shell a.hd-shell__link');

test('the sidebar is grouped, merged pages share one link with tabs, and the current page is marked', async () => {
  await withPage(async (page, base) => {
    await page.goto(`${base}/#/overview`);
    await links(page).first().waitFor({ timeout: 10000 });
    const labels = await links(page).allTextContents();
    assert.ok(labels.length <= 14, `sidebar has ${labels.length} links: ${labels.join(', ')}`);
    for (const merged of ['Local services', 'System services', 'Review gate', 'Publications', 'Project boards']) {
      assert.ok(!labels.some((l) => l.startsWith(merged)), `${merged} should be a tab, not a sidebar link`);
    }
    assert.ok((await page.locator('herdr-app-shell .hd-shell__group-label').count()) >= 4);

    await page.goto(`${base}/#/local-services`);
    const strip = page.locator('#subnav:not([hidden]) a');
    await strip.first().waitFor({ timeout: 10000 });
    const tabs = await strip.allTextContents();
    assert.deepEqual(tabs.slice(0, 1), ['Catalog']);
    assert.ok(tabs.includes('Local') && tabs.includes('System'));
    assert.equal(await page.locator('#subnav a[aria-current=page]').textContent(), 'Local');
    assert.equal(await page.locator('herdr-app-shell a.hd-shell__link.active').textContent(), 'Services');

    await page.goto(`${base}/#/github/projects`);        // the most specific tab wins over its /github prefix
    await page.locator('#subnav a[aria-current=page]').waitFor({ timeout: 10000 });
    assert.equal(await page.locator('#subnav a[aria-current=page]').count(), 1);
    assert.equal(await page.locator('#subnav a[aria-current=page]').textContent(), 'Boards');

    await page.goto(`${base}/#/collab`);                  // a page with no tabs shows no tab strip
    await page.locator('herdr-app-shell a.hd-shell__link.active').waitFor({ timeout: 10000 });
    assert.equal(await page.locator('#subnav:not([hidden])').count(), 0);
  });
});

test('with every optional capability advertised the sidebar still fits, and Deployments is a CI tab', async () => {
  await withPage(async (page, base) => {
    const capabilities = ['mission_activity_v1', 'ci_v1', 'plannotator_reviews_v1', 'github_v1', 'collab_v1', 'assistant_v1',
      'project_intelligence_v1', 'architecture_v1', 'architecture_explain_v1', 'deployments_v1'];
    await page.route('**/api/v1/**', (route) => route.fulfill({ json: new URL(route.request().url()).pathname === '/api/v1/health'
      ? { status: 'ok', capabilities } : {} }));
    await page.goto(`${base}/#/overview`);
    await links(page).filter({ hasText: 'Assistant' }).waitFor({ timeout: 10000 });   // the capabilities have applied
    const labels = await links(page).allTextContents();
    for (const optional of ['Assistant', 'Architecture', 'Project intelligence']) {
      assert.ok(labels.some((l) => l.startsWith(optional)), `${optional} is listed: ${labels.join(', ')}`);
    }
    assert.ok(labels.length <= 14, `sidebar has ${labels.length} links with every capability: ${labels.join(', ')}`);
    assert.ok(!labels.some((l) => l.startsWith('Deployments')), 'Deployments is a tab, not a sidebar link');

    await page.goto(`${base}/#/deployments`);
    await page.locator('#subnav a[aria-current=page]').waitFor({ timeout: 10000 });
    assert.deepEqual(await page.locator('#subnav a').allTextContents(), ['CI', 'Deployments']);
    assert.equal(await page.locator('#subnav a[aria-current=page]').textContent(), 'Deployments');
    assert.equal(await page.locator('herdr-app-shell a.hd-shell__link.active').textContent(), 'CI');
  });
});

test('display controls are icon buttons with tooltips', async () => {
  await withPage(async (page, base) => {
    await page.goto(`${base}/#/overview`);
    const density = page.locator('herdr-app-shell [data-density-button]');
    await density.waitFor({ timeout: 10000 });
    assert.match(await density.getAttribute('title'), /^Density: /);
    assert.equal(await page.locator('herdr-app-shell select').count(), 0);
  });
});

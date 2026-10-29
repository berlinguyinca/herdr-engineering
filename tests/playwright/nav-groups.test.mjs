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

test('display controls are icon buttons with tooltips', async () => {
  await withPage(async (page, base) => {
    await page.goto(`${base}/#/overview`);
    const density = page.locator('herdr-app-shell [data-density-button]');
    await density.waitFor({ timeout: 10000 });
    assert.match(await density.getAttribute('title'), /^Density: /);
    assert.equal(await page.locator('herdr-app-shell select').count(), 0);
  });
});

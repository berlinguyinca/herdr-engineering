// Sidebar: related pages are one link with tabs, links sit under group headings, and the Display menu holds
// density, theme and calm. Every route stays reachable.
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
    assert.ok((await page.locator('herdr-app-shell details.hd-shell__group > summary').count()) >= 4);

    await page.goto(`${base}/#/local-services`);
    await page.locator('herdr-app-shell nav.hd-shell__tabs:not([hidden])').waitFor({ timeout: 10000 });
    const tabs = await page.locator('herdr-app-shell nav.hd-shell__tabs:not([hidden]) a').allTextContents();
    assert.deepEqual(tabs.slice(0, 1), ['Catalog']);
    assert.ok(tabs.includes('Local') && tabs.includes('System'));
    assert.equal(await page.locator('herdr-app-shell a.hd-shell__tab.active').textContent(), 'Local');
    assert.equal(await page.locator('herdr-app-shell a.hd-shell__link.active').textContent(), 'Services');

    await page.goto(`${base}/#/github/projects`);        // the most specific tab wins over its /github prefix
    await page.locator('herdr-app-shell a.hd-shell__tab.active').waitFor({ timeout: 10000 });
    assert.equal(await page.locator('herdr-app-shell a.hd-shell__tab.active').count(), 1);
    assert.equal(await page.locator('herdr-app-shell a.hd-shell__tab.active').textContent(), 'Boards');

    await page.goto(`${base}/#/missions`);                // a page with no tabs shows no tab bar
    await page.locator('herdr-app-shell a.hd-shell__link.active').waitFor({ timeout: 10000 });
    assert.equal(await page.locator('herdr-app-shell nav.hd-shell__tabs:not([hidden])').count(), 0);
  });
});

test('display controls sit in one collapsed menu that opens on demand', async () => {
  await withPage(async (page, base) => {
    await page.goto(`${base}/#/overview`);
    const display = page.locator('herdr-app-shell details.hd-shell__display');
    await display.waitFor({ timeout: 10000 });
    assert.equal(await display.evaluate((d) => d.open), false);
    await page.locator('herdr-app-shell details.hd-shell__display > summary').click();
    await page.getByRole('combobox', { name: 'Density' }).waitFor({ timeout: 5000 });
  });
});

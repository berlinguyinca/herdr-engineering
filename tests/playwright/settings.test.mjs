// Settings: the one page that lists every optional service. A service the server does not advertise is hidden from the
// sidebar and shown only here, with the config key that enables it.
import { test } from 'node:test';
import assert from 'node:assert';
import { withPage } from './helpers/fixture.mjs';

const links = (page) => page.locator('herdr-app-shell a.hd-shell__link');
const advertise = (page, capabilities) => page.route('**/api/v1/**', (route) => route.fulfill({
  json: new URL(route.request().url()).pathname === '/api/v1/health' ? { status: 'ok', capabilities } : {} }));

test('Settings is always in the sidebar and lists every service with its state and how to enable it', async () => {
  await withPage(async (page, base) => {
    await advertise(page, ['collab_v1', 'share_v1']);
    await page.goto(`${base}/#/settings`);
    await page.locator('[data-service="share_v1"]').waitFor({ timeout: 10000 });
    assert.ok((await links(page).allTextContents()).some((l) => l.startsWith('Settings')), 'Settings has a sidebar link');
    assert.equal(await page.locator('[data-service="share_v1"]').getAttribute('data-state'), 'on');
    assert.equal(await page.locator('[data-service="members_v1"]').getAttribute('data-state'), 'off');
    assert.match(await page.locator('[data-service="members_v1"]').textContent(), /control_plane\.collab\.members\.enabled/);
    assert.doesNotMatch(await page.locator('[data-service="share_v1"]').textContent(), /To enable/);
    for (const capability of ['review_gate_v1', 'artifact_delete_v1', 'ci_actions_v1', 'deployment_actions_v1', 'collab_artifacts_v1']) {
      assert.equal(await page.locator(`[data-service="${capability}"]`).count(), 1, `${capability} is listed`);
    }
    assert.equal(await page.locator('[data-service="share_v1"] a, [data-service="members_v1"] a').count(), 0, 'no service links out of Settings');
  });
});

test('a service the server does not advertise has no sidebar entry, and appears once it is advertised', async () => {
  await withPage(async (page, base) => {
    await advertise(page, ['mission_activity_v1', 'ci_v1', 'github_v1']);
    await page.goto(`${base}/#/overview`);
    await links(page).filter({ hasText: 'Settings' }).waitFor({ timeout: 10000 });
    assert.ok(!(await links(page).allTextContents()).some((l) => l.startsWith('Collaboration')), 'Collaboration is hidden while off');
    await page.goto(`${base}/#/settings`);
    await page.locator('[data-service="collab_v1"]').waitFor({ timeout: 10000 });
    assert.equal(await page.locator('[data-service="collab_v1"]').getAttribute('data-state'), 'off');
  });
  await withPage(async (page, base) => {
    await advertise(page, ['collab_v1']);
    await page.goto(`${base}/#/overview`);
    await links(page).filter({ hasText: 'Collaboration' }).waitFor({ timeout: 10000 });
  });
});

test('a failed probe says so on Settings instead of listing everything as off', async () => {
  await withPage(async (page, base) => {
    await page.route('**/api/v1/health*', (route) => route.fulfill({ status: 500, json: { error: 'down' } }));
    await page.goto(`${base}/#/settings`);
    await page.getByText('Could not read which services are enabled').waitFor({ timeout: 10000 });
    assert.equal(await page.locator('[data-service]').count(), 0);
  });
});

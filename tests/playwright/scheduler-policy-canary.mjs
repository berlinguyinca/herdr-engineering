// Invoked by the PostgreSQL/HTTP integration test; no API responses are intercepted.
import assert from 'node:assert/strict';
import path from 'node:path';
import { chromium } from 'playwright';
import { cachedChromiums } from './helpers/fixture.mjs';

const base = process.env.SCHEDULER_CANARY_URL;
const headers = { 'X-API-Key': process.env.SCHEDULER_CANARY_OPERATOR, 'Content-Type': 'application/json' };
const host = process.env.SCHEDULER_CANARY_HOST;
const mac = process.env.SCHEDULER_CANARY_MAC;
assert.ok(base && host && mac && headers['X-API-Key']);
async function api(route, body, key) {
  const response = await fetch(base + '/api/v1/scheduler' + route, {
    method: body ? 'POST' : 'GET', headers: key ? { ...headers, 'X-API-Key': key } : headers,
    ...(body ? { body: JSON.stringify(body) } : {}),
  });
  return { status: response.status, body: await response.json() };
}
const preview = () => api('/plan', { request_id: 'canary-preview', agent_family: 'codex',
  resource_profile: { class: 'coding' }, required_tags: ['coding'] });
let browser;
try {
  try { browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] }); }
  catch (error) {
    const executablePath = cachedChromiums()[0];
    if (!executablePath) throw error;
    browser = await chromium.launch({ executablePath, headless: true, args: ['--no-sandbox'] });
  }
  const page = await browser.newPage({ extraHTTPHeaders: headers });
  const pageErrors = [];
  page.on('pageerror', error => pageErrors.push(error.message));
  await page.goto(base + '/#/scheduling');
  const form = page.locator('[data-scheduler-policy]'); await form.waitFor();
  assert.equal((await preview()).body.selected_host_uid, host);
  assert.ok((await preview()).body.explanations[mac].reason_codes.includes('HOST_MANUAL_ONLY'));
  await form.getByLabel('Host', { exact: true }).selectOption(host);
  async function change(mode) {
    await form.getByLabel('Placement policy').selectOption(mode);
    await form.getByLabel('Reason for change').fill('Isolated dashboard canary');
    await form.getByRole('checkbox').check();
    await form.getByRole('button', { name: 'Save policy' }).click();
    await page.waitForFunction(() => /Saved/.test(document.querySelector('[data-scheduler-policy] output').textContent));
    await page.waitForFunction(() => !document.querySelector('[data-policy-reload]').disabled);
  }
  await change('disabled');
  const denied = (await preview()).body;
  assert.equal(denied.state, 'queued');
  assert.ok(denied.explanations[host].reason_codes.includes('HOST_DISABLED'));
  await form.getByRole('button', { name: 'Reload policy' }).click();
  await page.waitForFunction(() => /Latest policy loaded/.test(document.querySelector('[data-scheduler-policy] output').textContent));
  await change('automatic');
  assert.equal((await preview()).body.selected_host_uid, host);
  const overview = (await api('')).body;
  assert.equal(overview.hosts.find(row => row.host_uid === mac).policy.mode, 'manual_only');
  assert.equal(overview.admission_enabled, false);
  assert.ok(overview.history.some(row => row.request_id === 'queued-canary'));
  const revision = overview.hosts.find(row => row.host_uid === host).policy.revision;
  assert.equal((await api('/host-policy', { host_uid: host, mode: 'disabled', expected_revision: revision },
    process.env.SCHEDULER_CANARY_VIEWER)).status, 403);
  await page.locator('[data-scheduler-history] summary').first().focus(); await page.keyboard.press('Enter');
  assert.ok(await page.locator('[data-scheduler-history] details[open]').count());
  assert.equal(await form.locator('input,select').evaluateAll(elements => elements.every(element => element.labels.length || element.getAttribute('aria-label'))), true);
  if (process.env.SCHEDULER_CANARY_SCREENSHOTS) {
    await page.setViewportSize({ width: 1280, height: 900 });
    await page.screenshot({ path: path.join(process.env.SCHEDULER_CANARY_SCREENSHOTS, 'scheduler-desktop.png'), fullPage: true });
    await page.setViewportSize({ width: 390, height: 844 });
    await page.locator('[data-scheduler-history] details').evaluateAll(elements => elements.forEach(element => { element.open = false; }));
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    await page.screenshot({ path: path.join(process.env.SCHEDULER_CANARY_SCREENSHOTS, 'scheduler-mobile.png'), fullPage: true });
  }
  assert.deepEqual(pageErrors, []);
  console.log('Real HTTP/PostgreSQL dashboard canary passed: policy disable/rollback, Mac exclusion, viewer denial, history and keyboard controls.');
} finally { await browser?.close(); }

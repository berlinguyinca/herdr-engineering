import test from 'node:test';
import assert from 'node:assert/strict';
import { withPage } from './helpers/fixture.mjs';
import { boot, until, sleep } from './helpers/live.mjs';

const host = (revision = 1, mode = 'automatic') => ({ host_uid: 'host-a', host_name: 'Bender',
  trusted: true, owned: true, reachable: true, platform: 'linux', policy: { mode, revision, allow_tags: ['coding'], deny_tags: ['benchmark'] },
  telemetry: { freshness_seconds: 1 }, capacity: { total_cpu: 16, total_memory_gib: 64 } });
const record = id => ({ request_id: id, reservation_id: id, state: 'queued', agent_family: 'codex',
  created_at: '2026-11-04T10:00:00Z', explanations: { 'host-a': { reason_codes: ['RESERVATION_CAPACITY'], score: 0 } } });
const defaults = { items: [], workspaces: [], artifacts: [], publications: [] };

test('older backend policy summaries cannot accidentally clear unknown restrictions', async () => withPage(async (page, base) => {
  await boot(page, base, '/scheduling', path => path === '/api/v1/scheduler'
    ? { hosts: [{ ...host(), policy: { mode: 'automatic', revision: 1 } }], history: [], queue: [] } : defaults);
  const policy = page.locator('[data-scheduler-policy]'); await policy.waitFor();
  await policy.getByLabel('Host', { exact: true }).selectOption('host-a');
  await policy.getByLabel('Placement policy').selectOption('disabled');
  await policy.getByRole('checkbox').check();
  assert.equal(await policy.getByRole('button', { name: 'Save policy' }).isDisabled(), true);
  assert.match(await policy.locator('output').innerText(), /complete policy data/);
}));

test('Mac automatic eligibility requires a visible warning and renewed explicit confirmation', async () => withPage(async (page, base) => {
  await boot(page, base, '/scheduling', path => path === '/api/v1/scheduler'
    ? { hosts: [{ ...host(0, 'manual_only'), platform: 'darwin', host_name: 'Mac laptop' }], history: [], queue: [] } : defaults);
  const policy = page.locator('[data-scheduler-policy]'); await policy.waitFor();
  await policy.getByLabel('Host', { exact: true }).selectOption('host-a');
  await policy.getByRole('checkbox').check();
  await policy.getByLabel('Placement policy').selectOption('automatic');
  assert.equal(await policy.locator('[data-policy-warning]').isVisible(), true);
  assert.equal(await policy.getByRole('checkbox').isChecked(), false);
  assert.equal(await policy.getByRole('button', { name: 'Save policy' }).isDisabled(), true);
  await policy.getByRole('checkbox', { name: /explicitly allow automatic placements/ }).check();
  assert.equal(await policy.getByRole('button', { name: 'Save policy' }).isEnabled(), true);
}));

test('policy drafts survive blur and live refresh; conflicts require explicit reload', async () => withPage(async (page, base) => {
  let current = host(), records = [record('first')], action;
  await boot(page, base, '/scheduling', path => path === '/api/v1/scheduler'
    ? { hosts: [current], history: records, queue: records, queued_agents: 1, admission_enabled: false } : defaults);
  const policy = page.locator('[data-scheduler-policy]');
  await policy.waitFor({ timeout: 5000 });
  await policy.getByLabel('Host', { exact: true }).selectOption('host-a');
  await policy.getByLabel('Placement policy').selectOption('disabled');
  await policy.getByLabel('Reason for change').fill('Maintenance');
  await policy.getByRole('checkbox').check();
  await page.locator('[data-scheduler-plan] [name=request_id]').fill('keep-preview');
  await page.locator('h1').click();
  current = host(2, 'manual_only'); records = [record('new-request')];
  await page.clock.runFor(31000); await sleep(80);
  assert.equal(await policy.getByLabel('Placement policy').inputValue(), 'disabled');
  assert.equal(await policy.getByLabel('Reason for change').inputValue(), 'Maintenance');
  assert.equal(await policy.getByRole('checkbox').isChecked(), true);
  assert.equal(await policy.getAttribute('data-revision'), '1');
  assert.equal(await policy.getByRole('button', { name: 'Save policy' }).isDisabled(), true);
  assert.match(await policy.innerText(), /changed|revision/i);
  assert.match(await page.locator('[data-scheduler-history]').innerText(), /new-request/);
  await policy.getByRole('button', { name: 'Reload policy' }).click();
  await until(() => policy.getAttribute('data-revision').then(value => value === '2'), 'latest policy loaded');
  assert.equal(await policy.getByLabel('Placement policy').inputValue(), 'manual_only');
  assert.equal(await policy.getByRole('checkbox').isChecked(), false);
  await policy.getByLabel('Placement policy').selectOption('disabled');
  await policy.getByRole('checkbox').check();
  await page.route('**/api/v1/scheduler/host-policy', route => { action = route; });
  await policy.getByRole('button', { name: 'Save policy' }).click();
  await until(() => Boolean(action), 'policy request');
  assert.equal(action.request().postDataJSON().expected_revision, 2);
  assert.deepEqual(action.request().postDataJSON().allow_tags, ['coding']);
  assert.deepEqual(action.request().postDataJSON().deny_tags, ['benchmark']);
  assert.equal(await policy.getByRole('button', { name: 'Save policy' }).isDisabled(), true);
  current = host(3, 'disabled');
  await action.fulfill({ json: current.policy });
  await until(() => policy.locator('output').innerText().then(value => /Saved/.test(value)), 'save acknowledged');
  assert.equal(await page.locator('[data-scheduler-plan] [name=request_id]').inputValue(), 'keep-preview');
}));

test('policy conflict and failure preserve the draft; history is accessible on mobile', async () => withPage(async (page, base) => {
  let current = host(4), attempt = 0;
  await boot(page, base, '/scheduling', path => path === '/api/v1/scheduler'
    ? { hosts: [current], history: [record('<hostile-request>')], queue: [] } : defaults);
  const policy = page.locator('[data-scheduler-policy]'); await policy.waitFor({ timeout: 5000 });
  await policy.getByLabel('Host', { exact: true }).selectOption('host-a');
  await policy.getByLabel('Placement policy').selectOption('draining');
  await policy.getByRole('checkbox').check();
  await page.route('**/api/v1/scheduler/host-policy', async route => {
    attempt++;
    if (attempt === 1) { current = host(5); return route.fulfill({ status: 409, json: { error: 'policy revision conflict' } }); }
    return route.fulfill({ status: 503, json: { error: 'storage unavailable' } });
  });
  await policy.getByRole('button', { name: 'Save policy' }).click();
  await until(() => policy.locator('output').innerText().then(value => /changed|conflict/i.test(value)), 'conflict visible');
  assert.equal(await policy.getByLabel('Placement policy').inputValue(), 'draining');
  assert.equal(await policy.getByRole('button', { name: 'Save policy' }).isDisabled(), true);
  await policy.getByRole('button', { name: 'Reload policy' }).click();
  await until(() => policy.getAttribute('data-revision').then(value => value === '5'), 'conflict reloaded');
  await policy.getByLabel('Placement policy').selectOption('draining'); await policy.getByRole('checkbox').check();
  await policy.getByRole('button', { name: 'Save policy' }).click();
  await until(() => policy.locator('output').innerText().then(value => /storage unavailable/.test(value)), 'failure visible');
  assert.equal(await policy.getByRole('button', { name: 'Save policy' }).isEnabled(), true);
  await page.setViewportSize({ width: 390, height: 844 });
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
  await page.locator('[data-scheduler-history] summary').first().focus();
  await page.keyboard.press('Enter');
  assert.match(await page.locator('[data-scheduler-history]').innerText(), /RESERVATION_CAPACITY/);
  assert.equal(await page.locator('[data-scheduler-history] img').count(), 0);
  assert.match(await page.locator('[data-scheduler-history]').innerText(), /<hostile-request>/);
}));

test('failed reload preserves the draft and a late save cannot overwrite a disposed view', async () => withPage(async (page, base) => {
  let failed = false, action;
  await boot(page, base, '/scheduling', path => path === '/api/v1/scheduler'
    ? (failed ? { __status: 503, json: { error: 'read unavailable' } } : { hosts: [host()], history: [], queue: [] }) : defaults);
  const policy = page.locator('[data-scheduler-policy]'); await policy.waitFor();
  await policy.getByLabel('Host', { exact: true }).selectOption('host-a');
  await policy.getByLabel('Placement policy').selectOption('disabled');
  await policy.getByLabel('Reason for change').fill('Keep this draft');
  await policy.getByRole('checkbox').check();
  failed = true;
  await policy.getByRole('button', { name: 'Reload policy' }).click();
  await until(() => policy.locator('output').innerText().then(value => /draft is preserved/.test(value)), 'failed reload');
  assert.equal(await policy.getByLabel('Reason for change').inputValue(), 'Keep this draft');
  assert.equal(await policy.getByLabel('Placement policy').inputValue(), 'disabled');
  assert.equal(await policy.getByRole('checkbox').isChecked(), true);
  failed = false;
  await page.route('**/api/v1/scheduler/host-policy', route => { action = route; });
  await policy.getByRole('button', { name: 'Save policy' }).click();
  await until(() => Boolean(action), 'pending save');
  await page.evaluate(() => { location.hash = '#/handoffs'; });
  await page.locator('[data-handoff-list]').waitFor();
  await action.fulfill({ json: { mode: 'disabled', revision: 2 } }).catch(() => {}); // the aborted transport may already be closed
  await sleep(80);
  assert.equal(await page.locator('[data-scheduler-policy]').count(), 0);
  assert.doesNotMatch(await page.locator('#view').innerText(), /Saved policy/);
}));

test('an overview begun before a save cannot restore the previous policy', async () => withPage(async (page, base) => {
  let current = host(), reads = 0, action, release;
  const delayed = new Promise(resolve => { release = resolve; });
  await boot(page, base, '/scheduling', async path => {
    if (path !== '/api/v1/scheduler') return defaults;
    reads++;
    if (reads === 2) { await delayed; return { hosts: [host(0)], history: [], queue: [] }; }
    return { hosts: [current], history: [], queue: [] };
  });
  const policy = page.locator('[data-scheduler-policy]'); await policy.waitFor();
  await policy.getByLabel('Host', { exact: true }).selectOption('host-a');
  await policy.getByLabel('Placement policy').selectOption('disabled'); await policy.getByRole('checkbox').check();
  await page.clock.runFor(31000); await until(() => reads === 2, 'delayed overview');
  await page.route('**/api/v1/scheduler/host-policy', route => { action = route; });
  await policy.getByRole('button', { name: 'Save policy' }).click();
  await until(() => Boolean(action), 'save sent');
  current = host(2, 'disabled'); release();
  await action.fulfill({ json: current.policy });
  await until(() => policy.getAttribute('data-revision').then(value => value === '2'), 'saved revision retained');
  assert.equal(await policy.getByLabel('Placement policy').inputValue(), 'disabled');
  assert.equal(await policy.getByRole('checkbox').isChecked(), false);
}));

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { withPage } from './helpers/fixture.mjs';
import { boot, until } from './helpers/live.mjs';

test('bulk agent selection and family filters retain upstream controls and report partial failures', async () => withPage(async (page, base) => {
  const errors = [], calls = [];
  let first;
  let agents = ['pi', 'codex'].map((family, i) => ({ agent_id: `a${i}`, name: family, harness_type: family, status: 'active', rooms: [], expires_at: 1900000000 }));
  page.on('pageerror', error => errors.push(error.message));
  page.on('dialog', dialog => dialog.accept());
  await boot(page, base, '/collab?agent=a0', path => {
    if (path.endsWith('/collab/config')) return { client_id: 'test', hosted_ui_domain: 'test', public_url: 'https://example.test', operator: { user_id: 'owner' } };
    if (path.endsWith('/collab/agents')) return { agents };
    return { rooms: [], users: [], hosts: [], profiles: [], spawns: [], sessions: [], items: [] };
  });
  await page.route('**/api/v1/collab/agents/*/revoke', async route => {
    const id = new URL(route.request().url()).pathname.split('/').at(-2); calls.push(id);
    if (id === 'a0') { first = route; return; }
    await route.fulfill({ status: 503, json: { error: 'host offline' } });
  });
  await page.locator('[data-agent-select=a0]').check();
  await page.locator('[data-agent-select=a1]').check();
  await page.locator('[data-agent-family-filter]').selectOption('pi');
  assert.equal(await page.locator('[data-agent-card=a1]').isVisible(), false);
  assert.match(await page.locator('[data-agent-selected-count]').innerText(), /2 selected/);
  await page.locator('[data-agent-stop-selected]').click();
  await until(() => Boolean(first), 'first revoke');
  assert.equal(await page.locator('[data-agent-stop-selected]').isDisabled(), true);
  assert.match(await page.locator('[data-agent-selected-count]').innerText(), /Stopping 1 of 2/);
  agents = agents.filter(agent => agent.agent_id !== 'a0');
  await first.fulfill({ json: { revoked: true } });
  await until(() => page.locator('[data-agent-selected-count]').innerText().then(text => text.includes('host offline')), 'partial failure reported');
  assert.deepEqual(calls, ['a0', 'a1']);
  assert.match(await page.locator('[data-agent-selected-count]').innerText(), /1 stopped/);
  assert.equal(await page.locator('[data-agent-stop-all]').isEnabled(), true);
  assert.deepEqual(errors, []);
}));

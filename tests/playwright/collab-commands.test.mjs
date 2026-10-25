import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import path from 'node:path';
import { chromium } from 'playwright';

// Same shape as the shared helper planned in tests/playwright/helpers/fixture.mjs (withPage(run(page, baseUrl)));
// kept local until that helper lands so the two PRs do not collide.
async function withPage(run) {
  const root = path.resolve(import.meta.dirname, '../../packages');
  const server = createServer(async (req, res) => {
    if (req.url === '/') { res.setHeader('Content-Type', 'text/html'); res.end('<!doctype html><link rel="stylesheet" href="/herdr-design-system/theme.css"><div id="host"></div>'); return; }
    const file = path.resolve(root, `.${req.url.split('?')[0]}`);
    if (!file.startsWith(root + path.sep)) { res.writeHead(404); res.end(); return; }
    try { res.setHeader('Content-Type', file.endsWith('.css') ? 'text/css' : 'application/javascript'); res.end(await readFile(file)); }
    catch { res.writeHead(404); res.end(); }
  });
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  let browser;
  try {
    const fallback = '/home/wohlgemuth/.cache/ms-playwright/chromium-1243/chrome-linux64/chrome';
    browser = await chromium.launch({ headless: true, args: ['--no-sandbox'], executablePath: process.env.PLAYWRIGHT_CHROMIUM || (existsSync(fallback) ? fallback : undefined) });
    const page = await browser.newPage({ viewport: { width: 1200, height: 900 } });
    const errors = []; page.on('pageerror', (e) => errors.push(e.message));
    const baseUrl = `http://127.0.0.1:${server.address().port}`;
    await page.goto(`${baseUrl}/`);
    await run(page, baseUrl);
    assert.deepEqual(errors, []);
  } finally { await browser?.close(); await new Promise((r) => server.close(r)); }
}

const PEOPLE = [{ user_id: 'owner', handle: 'owner', online: true }, { user_id: 'bob', handle: 'bob', online: true }];
const at = () => new Date().toISOString();
const EVENTS = [
  { event_type: 'message', event_id: 'm1', sender: { type: 'user', id: 'bob' }, created_at: at(), text: 'the chart breaks', mentions: [] },
  { event_type: 'message', event_id: 'm2', sender: { type: 'user', id: 'bob' }, created_at: at(), text: 'on NaN too', mentions: [] },
  { event_type: 'handoff', event_id: 'h-ev', sender: { type: 'user', id: 'bob' }, created_at: at(), mentions: [],
    handoff: { handoff_id: 'h1', from: { kind: 'person', name: 'bob' }, to: { kind: 'agent', name: 'owner.pi' }, note: 'take <b>it</b>', status: 'pending', can_answer: false } },
  { event_type: 'decision', event_id: 'd-ev', sender: { type: 'user', id: 'bob' }, created_at: at(), text: 'ship Friday' },
  { event_type: 'issue_draft', event_id: 'i-ev', sender: { type: 'user', id: 'owner' }, created_at: at(),
    draft: { draft_id: 'draft-1', kind: 'issue', owner: 'acme', repo: 'app', title: 'Chart breaks', body: '> the chart breaks', status: 'draft', can_confirm: true } },
];

async function openRoom(page, later = [], { events = EVENTS, agents = [], unread = undefined } = {}) {
  const requests = [], events0 = events;
  let stateCount = 0;
  await page.route('**/api/v1/collab/**', async (route) => {
    const request = route.request(), url = new URL(request.url()), where = url.pathname.replace('/api/v1/collab', '');
    if (request.method() === 'POST') { requests.push({ path: where, body: request.postDataJSON() }); await route.fulfill({ json: {} }); return; }
    let body = {};
    if (where === '/config') body = { client_id: 'test', hosted_ui_domain: 'test', public_url: 'https://example.test', operator: { user_id: 'owner', email: 'owner@example.test' } };
    else if (where === '/rooms') body = { rooms: [{ room_id: 'room-one', name: 'Review', role: 'owner', unread_mentions: unread }] };
    else if (where === '/admin/users') body = { users: [] };
    else if (where === '/rooms/room-one') body = { room_id: 'room-one', name: 'Review', role: 'owner', members: [] };
    else if (where === '/rooms/room-one/state') {
      stateCount++;
      const events = stateCount === 1 ? events0 : (stateCount === 2 ? later : []);
      body = { changed: stateCount <= 2, seq: stateCount, next_cursor: stateCount, agents, people: PEOPLE, tasks: [], events };
      if (stateCount > 2) await new Promise((r) => setTimeout(r, 200));
    }
    await route.fulfill({ json: body });
  });
  await page.evaluate(async () => {
    const { mountCollab } = await import('/collab-view.js');
    window.collab = mountCollab(document.querySelector('#host'));
  });
  if (unread !== undefined) await page.locator('[data-room="room-one"]').waitFor();
  const badge = unread !== undefined ? await page.locator('[data-room="room-one"] .cx-unread').allInnerTexts() : null;
  await page.locator('[data-room="room-one"]').click();
  await page.locator('.cx-bubble').first().waitFor();
  return { requests, badge };
}

test('command events render as escaped cards; a draft reaches GitHub only after the explicit confirm', async () => withPage(async (page) => {
  const { requests } = await openRoom(page);
  const handoff = page.locator('[data-handoff="h1"]');
  assert.match(await handoff.innerText(), /@bob → @owner\.pi/);
  assert.match(await handoff.innerText(), /take <b>it<\/b>/);                 // shown as text
  assert.equal(await handoff.locator('b', { hasText: 'it' }).count(), 0);
  assert.match(await page.locator('.cx-cmdcard--decision').innerText(), /ship Friday/);
  const draft = page.locator('[data-draft="draft-1"]');
  const button = draft.getByRole('button', { name: /Create issue on GitHub/ });
  page.once('dialog', (d) => d.dismiss());                                      // first: cancel the confirm
  await button.click();
  await page.waitForTimeout(100);
  assert.equal(requests.filter((r) => r.path.includes('/drafts/')).length, 0);
  page.once('dialog', (d) => { assert.match(d.message(), /acme\/app/); d.accept(); });
  await button.click();
  await page.waitForFunction(() => document.querySelector('[data-draft-confirm]')?.disabled);
  assert.deepEqual(requests.filter((r) => r.path.includes('/drafts/')).map((r) => r.path), ['/rooms/room-one/drafts/draft-1/confirm']);
  await page.evaluate(() => window.collab.dispose());
}));

test('selected messages travel with /issue; any other message carries no selection', async () => withPage(async (page) => {
  const { requests } = await openRoom(page);
  await page.locator('[data-select="m1"]').click();
  await page.locator('[data-select="m2"]').click();
  assert.equal(await page.locator('[data-select="m1"]').getAttribute('aria-pressed'), 'true');
  const input = page.getByRole('combobox', { name: 'Message' });
  await input.click();
  await input.fill('/issue acme/app Chart breaks');
  await input.press('Escape');
  await input.press('Enter');
  await page.waitForFunction(() => !document.querySelector('[aria-label="Message"]').value);
  const sent = requests.filter((r) => r.path.endsWith('/messages'));
  assert.equal(sent.at(-1).body.text, '/issue acme/app Chart breaks');           // the text goes out as typed
  assert.deepEqual(sent.at(-1).body.selection, ['m1', 'm2']);
  assert.equal(await page.locator('[data-select="m1"]').getAttribute('aria-pressed'), 'false');
  await input.fill('see /issue later');
  await input.press('Enter');
  await page.waitForFunction(() => !document.querySelector('[aria-label="Message"]').value);
  assert.equal(requests.filter((r) => r.path.endsWith('/messages')).at(-1).body.selection, undefined);
  await page.evaluate(() => window.collab.dispose());
}));

test('a GitHub result settles its draft card', async () => withPage(async (page) => {
  await openRoom(page, [{ event_type: 'github_written', event_id: 'g1', created_at: at(),
    github: { draft_id: 'draft-1', kind: 'issue', owner: 'acme', repo: 'app', number: 12, url: 'https://github.com/acme/app/issues/12' } }]);
  await page.locator('.cx-cmdcard--github').waitFor();
  const draft = page.locator('[data-draft="draft-1"]');
  assert.equal(await draft.locator('[data-draft-confirm]').count(), 0);
  assert.match(await draft.locator('[data-status]').innerText(), /created/i);
  assert.equal(await page.locator('.cx-cmdcard--github a').getAttribute('href'), 'https://github.com/acme/app/issues/12');
  await page.evaluate(() => window.collab.dispose());
}));

test('the recipient answers a handoff from its card; the rooms list shows your unread mentions', async () => withPage(async (page) => {
  const mine = { ...EVENTS[2], handoff: { ...EVENTS[2].handoff, handoff_id: 'h2', to: { kind: 'person', name: 'owner' }, can_answer: true } };
  const { requests, badge } = await openRoom(page, [], { events: [EVENTS[0], mine], unread: 3 });
  assert.deepEqual(badge, ['3']);
  const card = page.locator('[data-handoff="h2"]');
  await card.getByRole('button', { name: 'Accept' }).click();
  await page.waitForFunction(() => document.querySelector('[data-handoff-answer="decline"]')?.disabled);
  assert.deepEqual(requests.filter((r) => r.path.includes('/handoffs/')).map((r) => r.path), ['/rooms/room-one/handoffs/h2/accept']);
  await page.evaluate(() => window.collab.dispose());
}));

test('an agent holding a task shows as working in the room roster', async () => withPage(async (page) => {
  const agents = [{ agent_id: 'a1', name: 'owner.pi', harness_type: 'pi', owner_user_id: 'owner', actions: ['read', 'execute'], presence: 'working', working: true, online: true, address: 'owner.pi', aliases: [], skills: [] },
    { agent_id: 'a2', name: 'bob.codex', harness_type: 'codex', owner_user_id: 'bob', actions: ['read'], presence: 'offline', working: false, online: false, address: 'bob.codex', aliases: [], skills: [] }];
  await openRoom(page, [], { events: [EVENTS[0]], agents });
  await page.locator('[data-sub="agents"]').click();
  const rows = page.locator('#hcn-roomagents .cx-agentrow');
  await rows.first().waitFor();
  assert.match(await rows.nth(0).innerText(), /working…/);
  assert.doesNotMatch(await rows.nth(1).innerText(), /working|online/);
  await page.evaluate(() => window.collab.dispose());
}));

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

const PEOPLE = [{ user_id: 'gert-id', handle: 'gert', online: true }, { user_id: 'owner', handle: 'owner', online: true }];
const AGENTS = [
  { agent_id: 'pi-id', name: 'lead', address: 'gert.pi', aliases: [], harness_type: 'pi', owner_user_id: 'gert-id', actions: ['read', 'post', 'execute'], skills: ['skill:review', 'skill:pdf-tools'] },
  { agent_id: 'cl-id', name: 'helper', address: 'gert.claude', aliases: [], harness_type: 'claude-code', owner_user_id: 'gert-id', actions: ['read', 'post'], skills: [] },
];
const MESSAGE = { event_type: 'message', sender: { type: 'user', id: 'owner' }, created_at: new Date().toISOString(),
  text: 'hi @gert, ask @gert.pi or `@gert.pi` and @nobody',
  mentions: [{ kind: 'person', id: 'gert-id', name: 'gert' }, { kind: 'agent', id: 'pi-id', name: 'gert.pi' }] };

async function openRoom(page, agents = AGENTS) {
  const requests = [];
  let stateCount = 0;
  await page.route('**/api/v1/collab/**', async (route) => {
    const request = route.request(), where = new URL(request.url()).pathname.replace('/api/v1/collab', '');
    if (request.method() === 'POST') { requests.push({ path: where, body: request.postDataJSON() }); await route.fulfill({ json: {} }); return; }
    let body = {};
    if (where === '/config') body = { client_id: 'test', hosted_ui_domain: 'test', public_url: 'https://example.test', operator: { user_id: 'owner', email: 'owner@example.test' } };
    else if (where === '/rooms') body = { rooms: [{ room_id: 'room-one', name: 'Review', role: 'owner' }] };
    else if (where === '/admin/users') body = { users: [] };
    else if (where === '/rooms/room-one') body = { room_id: 'room-one', name: 'Review', role: 'owner', members: [] };
    else if (where === '/rooms/room-one/state') {
      stateCount++;
      body = { changed: stateCount === 1, seq: 1, next_cursor: 1, agents, people: PEOPLE, tasks: [], events: stateCount === 1 ? [MESSAGE] : [] };
      if (stateCount > 1) await new Promise((r) => setTimeout(r, 200));
    }
    await route.fulfill({ json: body });
  });
  await page.evaluate(async () => {
    const { mountCollab } = await import('/collab-view.js');
    window.collab = mountCollab(document.querySelector('#host'));
  });
  await page.locator('[data-room="room-one"]').click();
  await page.locator('.cx-bubble').first().waitFor();
  return requests;
}

const focused = (page) => page.evaluate(() => document.activeElement?.getAttribute('aria-label'));

test('room messages render mentions as person and agent chips; code and unknown names stay text', async () => withPage(async (page) => {
  await openRoom(page);
  const bubble = page.locator('.cx-bubble').first();
  const person = bubble.locator('.cx-mention[data-mention-kind="person"]'), agent = bubble.locator('.cx-mention[data-mention-kind="agent"]');
  assert.equal(await person.count(), 1);
  assert.equal(await agent.count(), 1);
  assert.match(await person.innerText(), /@gert$/);
  assert.match(await agent.innerText(), /@gert\.pi$/);
  assert.equal(await person.locator('[data-icon="person"]').count(), 1);
  assert.equal(await agent.locator('[data-icon="robot"]').count(), 1);
  assert.equal(await bubble.locator('code .cx-mention').count(), 0);
  const literal = await bubble.evaluate((el) => { const c = el.cloneNode(true); c.querySelectorAll('[aria-hidden="true"]').forEach((n) => n.remove()); return c.textContent; });
  assert.equal(literal.trim(), 'hi @gert, ask @gert.pi or @gert.pi and @nobody');      // chips keep the literal text
  await page.evaluate(() => window.collab.dispose());
}));

test('the @ menu opens, moves with arrows, selects with Enter, closes with Esc and keeps focus in the textarea', async () => withPage(async (page) => {
  await openRoom(page);
  const input = page.getByRole('combobox', { name: 'Message' });
  await input.click();
  await input.pressSequentially('@ge');
  const menu = page.getByRole('listbox', { name: 'References' });
  await menu.waitFor();
  const options = menu.getByRole('option');
  assert.deepEqual(await options.locator('b').allTextContents(), ['@gert', '@gert.claude', '@gert.pi']);
  assert.deepEqual(await menu.locator('.conversation-suggestions-section').allTextContents(), ['People', 'Agents']);
  assert.deepEqual(await menu.getByRole('group', { name: 'People' }).getByRole('option').locator('b').allTextContents(), ['@gert']);
  assert.equal(await menu.getByRole('group', { name: 'Agents' }).getByRole('option').count(), 2);
  assert.equal(await input.getAttribute('maxlength'), '8000');
  await input.press('ArrowDown'); await input.press('ArrowDown');
  assert.equal(await options.nth(2).getAttribute('aria-selected'), 'true');
  await input.press('Enter');
  assert.equal(await input.inputValue(), '@gert.pi ');
  assert.equal(await focused(page), 'Message');
  await input.pressSequentially('/');
  await menu.getByRole('option', { name: /\/skill:review/ }).waitFor();
  await input.press('Escape');
  assert.equal(await menu.isHidden(), true);
  assert.equal(await input.inputValue(), '@gert.pi /');
  assert.equal(await focused(page), 'Message');
  await page.evaluate(() => window.collab.dispose());
}));

test('a literal "@gert.pi /skill" message is sent byte-for-byte', async () => withPage(async (page) => {
  const requests = await openRoom(page);
  const input = page.getByRole('combobox', { name: 'Message' });
  await input.click();
  await input.pressSequentially('@gert.p');
  await page.getByRole('option', { name: /@gert\.pi/ }).waitFor();
  await input.press('Enter');
  await input.pressSequentially('/sk');
  await page.getByRole('option', { name: /\/skill:review/ }).waitFor();
  await input.press('ArrowDown'); await input.press('ArrowUp');
  await page.getByRole('option', { name: /\/skill:review/ }).click();
  await input.pressSequentially('src/app.js');
  assert.equal(await input.inputValue(), '@gert.pi /skill:review src/app.js');
  await input.press('Enter');
  await page.waitForFunction(() => !document.querySelector('[aria-label="Message"]').value);
  assert.equal(requests.length, 1);
  assert.equal(requests[0].body.text, '@gert.pi /skill:review src/app.js');
  assert.equal(requests[0].body.mentions, undefined);
  await page.evaluate(() => window.collab.dispose());
}));

test('an unresolved @name is flagged before send and then sent as plain text; / at the start lists room commands', async () => withPage(async (page) => {
  const requests = await openRoom(page);
  const input = page.getByRole('combobox', { name: 'Message' });
  await input.fill('/');
  await page.getByRole('option', { name: /\/handoff/ }).waitFor();
  await input.fill('hi @zzz, see @gert now');
  await page.locator('.conversation-status').filter({ hasText: '@zzz' }).waitFor();
  assert.doesNotMatch(await page.locator('.conversation-status').innerText(), /@gert\b/);
  await input.press('Enter');
  await page.getByRole('status').filter({ hasText: 'Message posted' }).waitFor();
  assert.deepEqual(requests.map((r) => [r.path, r.body.text]), [['/rooms/room-one/messages', 'hi @zzz, see @gert now']]);
  await page.evaluate(() => window.collab.dispose());
}));

test('the session workspace composer uses the same menu and sends its text verbatim', async () => withPage(async (page) => {
  await page.evaluate(async () => {
    const { mountSessionWorkspace } = await import('/session-workspace.js');
    window.sent = [];
    const data = { events: [], rev: 1, epoch: 'e', has_more: false, summary: { agents: [{ agent_id: 'main' }, { agent_id: 'helper', parent_agent_id: 'main' }], files: [{ path: 'src/app.js' }], association: {} } };
    const client = { getInspector: async () => structuredClone(data), inspectorStreamUrl: () => '/stream', listSessions: async () => ({ sessions: [] }),
      sendMessage: async (id, text) => { window.sent.push(text); return { status: 'delivered' }; } };
    window.view = mountSessionWorkspace(document.querySelector('#host'), client, { session_id: 's', status: 'working' }, { connect: () => () => {} });
  });
  const input = page.getByRole('combobox', { name: 'Message' });
  await input.click();
  await input.pressSequentially('@app');
  const menu = page.getByRole('listbox', { name: 'References' });
  await menu.getByRole('option', { name: /src\/app\.js/ }).waitFor();
  assert.deepEqual(await menu.locator('.conversation-suggestions-section').allTextContents(), ['Files']);
  await input.press('Escape');
  assert.equal(await menu.isHidden(), true);
  assert.equal(await input.inputValue(), '@app');
  await input.fill('');
  await input.pressSequentially('@src');
  await menu.getByRole('option', { name: /src\/app\.js/ }).waitFor();
  await input.press('Enter');
  assert.equal(await input.inputValue(), '@src/app.js ');
  assert.equal(await focused(page), 'Message');
  await input.pressSequentially('/skill:review now');
  await input.press('Enter');
  await page.waitForFunction(() => window.sent.length === 1);
  assert.deepEqual(await page.evaluate(() => window.sent), ['@src/app.js /skill:review now']);
  await page.evaluate(() => window.view.dispose?.() ?? window.view());
}));

test('a leading name follows the server precedence: an old agent name never beats a person or a group', async () => withPage(async (page) => {
  const squatter = { agent_id: 'sq-id', name: 'squat', address: 'owner.codex', aliases: ['gert', 'all'], harness_type: 'codex', owner_user_id: 'owner', actions: ['read', 'post', 'execute'], skills: [] };
  const requests = await openRoom(page, [...AGENTS, squatter]);
  const input = page.getByRole('combobox', { name: 'Message' });
  for (const text of ['@gert please review', '@all heads up', '@owner.codex run it']) {
    await input.fill(text);
    await input.press('Escape');
    await input.press('Enter');
    await page.waitForFunction(() => !document.querySelector('[aria-label="Message"]').value);
  }
  assert.deepEqual(requests.map((r) => [r.path, r.body.text, r.body.target_agent_id]), [
    ['/rooms/room-one/messages', '@gert please review', undefined],
    ['/rooms/room-one/messages', '@all heads up', undefined],
    ['/rooms/room-one/tasks', '@owner.codex run it', 'sq-id']]);
  await page.evaluate(() => window.collab.dispose());
}));

test('an ambiguous name is flagged with the unambiguous forms and a leading one is refused as a task', async () => withPage(async (page) => {
  const twins = [
    { ...AGENTS[0], name: 'p1', aliases: ['lead'] },
    { agent_id: 'ana-id', name: 'gert', address: 'ana.codex', aliases: ['lead'], harness_type: 'codex', owner_user_id: 'owner', actions: ['read', 'post', 'execute'], skills: [] },
  ];
  const requests = await openRoom(page, twins);
  const input = page.getByRole('combobox', { name: 'Message' });
  await input.fill('@lead fix it');
  await page.locator('.conversation-status').filter({ hasText: '@lead is ambiguous — use @ana.codex or @gert.pi' }).waitFor();
  await input.press('Enter');
  await page.getByRole('alert').filter({ hasText: 'More than one agent or person is called @lead' }).waitFor();
  assert.equal(await input.inputValue(), '@lead fix it');
  await input.fill('@gert look');                       // person gert and agent "gert": neither silently wins
  await page.locator('.conversation-status').filter({ hasText: '@gert is ambiguous — use @ana.codex' }).waitFor();
  await input.press('Enter');
  await page.getByRole('alert').filter({ hasText: 'More than one agent or person is called @gert' }).waitFor();
  assert.deepEqual(requests, []);
  await page.evaluate(() => window.collab.dispose());
}));

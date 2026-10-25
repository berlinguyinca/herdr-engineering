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

const PEOPLE = [{ user_id: 'owner', handle: 'owner', online: true }];
const MESSAGE = { event_type: 'message', sender: { type: 'user', id: 'owner' }, created_at: new Date().toISOString(),
  text: 'fixes #binview-44 and acme/secret#7, not `#binview-44` or #other-1', mentions: [],
  refs: [{ kind: 'issue', owner: 'acme', repo: 'binview', number: 44, forms: ['short'], title: 'Fix <img src=x onerror=alert(1)>', state: 'closed' },
    { kind: 'issue', owner: 'acme', repo: 'secret', number: 7, forms: ['long'] }] };
const ISSUES = [
  { kind: 'issue', owner: 'acme', repo: 'binview', number: 44, title: 'Fix the chart', state: 'open', insert: '#binview-44' },
  { kind: 'issue', owner: 'acme', repo: 'app', number: 3, title: 'A', state: 'open', insert: 'acme/app#3' },
  { kind: 'issue', owner: 'other', repo: 'app', number: 3, title: 'B', state: 'open', insert: 'other/app#3' },
];

async function openRoom(page, refs) {
  const requests = [], queries = [];
  let stateCount = 0;
  await page.route('**/api/v1/collab/**', async (route) => {
    const request = route.request(), url = new URL(request.url()), where = url.pathname.replace('/api/v1/collab', '');
    if (request.method() === 'POST') { requests.push({ path: where, body: request.postDataJSON() }); await route.fulfill({ json: {} }); return; }
    let body = {};
    if (where === '/config') body = { client_id: 'test', hosted_ui_domain: 'test', public_url: 'https://example.test', operator: { user_id: 'owner', email: 'owner@example.test' } };
    else if (where === '/rooms') body = { rooms: [{ room_id: 'room-one', name: 'Review', role: 'owner' }] };
    else if (where === '/admin/users') body = { users: [] };
    else if (where === '/rooms/room-one') body = { room_id: 'room-one', name: 'Review', role: 'owner', members: [] };
    else if (where === '/rooms/room-one/refs') { queries.push(url.searchParams.get('q')); body = refs(url.searchParams.get('q')); }
    else if (where === '/rooms/room-one/state') {
      stateCount++;
      body = { changed: stateCount === 1, seq: 1, next_cursor: 1, agents: [], people: PEOPLE, tasks: [], events: stateCount === 1 ? [MESSAGE] : [] };
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
  return { requests, queries };
}

test('# lists cached issues in their shortest unambiguous form and Enter inserts it literally', async () => withPage(async (page) => {
  const { requests, queries } = await openRoom(page, (q) => ({ ready: true, items: ISSUES.filter((i) => `${i.repo}-${i.number} ${i.owner}/${i.repo}#${i.number}`.includes(q)) }));
  const input = page.getByRole('combobox', { name: 'Message' });
  await input.click();
  await input.pressSequentially('fixes #binv');
  const menu = page.getByRole('listbox', { name: 'References' });
  await menu.getByRole('option', { name: /#binview-44/ }).waitFor();
  assert.deepEqual(await menu.locator('.conversation-suggestions-section').allTextContents(), ['Issues']);
  assert.match(await menu.getByRole('option').first().innerText(), /Fix the chart · open/);
  await input.press('Enter');
  assert.equal(await input.inputValue(), 'fixes #binview-44 ');
  await input.pressSequentially('and #app-3');
  await menu.getByRole('option', { name: /other\/app#3/ }).waitFor();
  assert.deepEqual((await menu.getByRole('option').locator('b').allTextContents()).sort(), ['acme/app#3', 'other/app#3']);
  await input.press('Escape');
  await input.press('Enter');
  await page.waitForFunction(() => !document.querySelector('[aria-label="Message"]').value);
  assert.equal(requests.at(-1).body.text, 'fixes #binview-44 and #app-3');
  assert.equal(requests.at(-1).body.refs, undefined);                         // the server derives refs
  assert.ok(queries.includes('binv'));
  await page.evaluate(() => window.collab.dispose());
}));

test('with no GitHub cache the # menu says so and Enter still sends the message', async () => withPage(async (page) => {
  const { requests } = await openRoom(page, () => ({ ready: false, items: [] }));
  const input = page.getByRole('combobox', { name: 'Message' });
  await input.click();
  await input.pressSequentially('fixes #binview-44');
  const notice = page.getByRole('option', { name: /GitHub cache not ready/ });
  await notice.waitFor();
  assert.equal(await notice.getAttribute('aria-disabled'), 'true');
  await input.press('Enter');
  await page.waitForFunction(() => !document.querySelector('[aria-label="Message"]').value);
  assert.equal(requests.length, 1);
  assert.equal(requests[0].body.text, 'fixes #binview-44');
  await page.evaluate(() => window.collab.dispose());
}));

test('resolved references render as chips with the cached title and state; code and unresolved text stay text', async () => withPage(async (page) => {
  await openRoom(page, () => ({ ready: true, items: [] }));
  const bubble = page.locator('.cx-bubble').first();
  const chips = bubble.locator('.cx-ref');
  assert.equal(await chips.count(), 2);
  const closed = chips.first();
  assert.equal(await closed.getAttribute('data-ref-state'), 'closed');
  assert.match(await closed.innerText(), /#binview-44/);
  assert.match(await closed.locator('.cx-ref__title').innerText(), /Fix <img src=x onerror=alert\(1\)>/);   // shown as text
  assert.equal(await bubble.locator('img').count(), 0);
  assert.equal(await chips.nth(1).locator('.cx-ref__title').count(), 0);                // coordinates only
  assert.equal(await bubble.locator('code').innerText(), '#binview-44');
  assert.match(await bubble.innerText(), /#other-1/);
  const box = await closed.boundingBox();
  assert.ok(box && box.height < 40);
  await page.evaluate(() => window.collab.dispose());
}));

import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import { chromium } from 'playwright';

async function withComposer(fn) {
  const server = createServer(async (req, res) => {
    if (req.url === '/') { res.setHeader('Content-Type', 'text/html'); res.end('<div id="host"></div>'); return; }
    try { const file = new URL(`../../packages/${req.url.slice(1)}`, import.meta.url); res.setHeader('Content-Type', req.url.endsWith('.css') ? 'text/css' : 'application/javascript'); res.end(await readFile(file)); }
    catch { res.writeHead(404); res.end(); }
  });
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  let browser;
  try {
    const fallback = '/home/wohlgemuth/.cache/ms-playwright/chromium-1243/chrome-linux64/chrome';
    const executablePath = process.env.PLAYWRIGHT_CHROMIUM || (existsSync(fallback) ? fallback : undefined);
    browser = await chromium.launch({ headless: true, args: ['--no-sandbox'], executablePath });
    const page = await browser.newPage();
    await page.goto(`http://127.0.0.1:${server.address().port}/`);
    await page.evaluate(async () => {
      const { mountComposer } = await import('/conversation-composer.js');
      window.mount = (overrides = {}) => {
        window.composer?.dispose();
        window.composer = mountComposer(document.querySelector('#host'), {
          draftKey: 'test-draft', suggestions: () => [{ id: 'coder', label: '@coder', insertText: '@coder', detail: 'Agent' }, { id: 'file', label: '@src/file.js', insertText: '@src/file.js', detail: 'File' }],
          onSend: async (text, attachments) => { window.sent.push({ text, attachments }); if (window.fail) throw new Error('Transport offline'); return { status: 'delivered' }; }, ...overrides,
        });
      };
      window.sent = []; window.mount();
    });
    await fn(page);
  } finally { await browser?.close(); await new Promise((r) => server.close(r)); }
}

test('shared composer completes references, protects IME, keeps multiline drafts and failed sends', async () => withComposer(async (page) => {
  const input = page.getByRole('combobox', { name: 'Message' });
  await input.fill('@');
  await input.press('ArrowDown');
  await input.press('Tab');
  assert.equal(await input.inputValue(), '@src/file.js ');
  await input.fill('@co');
  await input.dispatchEvent('keydown', { key: 'Enter', isComposing: true });
  assert.equal(await input.inputValue(), '@co');
  assert.deepEqual(await page.evaluate(() => window.sent), []);
  await input.press('Escape');
  await input.press('Shift+Enter');
  await input.press('x');
  assert.equal(await input.inputValue(), '@co\nx');
  await page.evaluate(() => window.mount());
  assert.equal(await input.inputValue(), '@co\nx');
  await page.evaluate(() => { window.fail = true; });
  await input.press('Enter');
  await page.getByRole('alert').filter({ hasText: 'Transport offline' }).waitFor();
  assert.equal(await input.inputValue(), '@co\nx');
  await page.evaluate(() => { window.fail = false; });
  await input.press('Enter');
  await page.getByRole('status').filter({ hasText: 'Delivered' }).waitFor();
  assert.equal(await input.inputValue(), '');
  await page.evaluate(() => window.mount());
  assert.equal(await input.inputValue(), '');
}));

test('uploads require an adapter and preserve attachments through failures until explicit success', async () => withComposer(async (page) => {
  assert.equal(await page.getByRole('button', { name: 'Attach files' }).isDisabled(), true);
  await page.getByText('Attachments are not supported by this connection.').waitFor();
  await page.evaluate(() => window.mount({ upload: async (file) => { if (window.uploadFail) throw new Error('Upload failed'); return { id: 'artifact-1', name: file.name }; } }));
  await page.locator('input[type=file]').setInputFiles({ name: 'notes.txt', mimeType: 'text/plain', buffer: Buffer.from('hello') });
  await page.getByText('Ready', { exact: true }).waitFor();
  await page.getByRole('combobox', { name: 'Message' }).fill('Read notes');
  await page.evaluate(() => { window.fail = true; });
  await page.getByRole('button', { name: 'Send', exact: true }).click();
  await page.getByRole('alert').filter({ hasText: 'Transport offline' }).waitFor();
  await page.getByRole('button', { name: 'Remove notes.txt' }).waitFor();
  await page.evaluate(() => { window.fail = false; });
  await page.getByRole('button', { name: 'Send', exact: true }).click();
  await page.getByRole('status').filter({ hasText: 'Delivered' }).waitFor();
  assert.equal(await page.getByRole('button', { name: 'Remove notes.txt' }).count(), 0);
  assert.deepEqual(await page.evaluate(() => window.sent.at(-1).attachments), [{ id: 'artifact-1', name: 'notes.txt' }]);
  await page.evaluate(() => { window.uploadFail = true; });
  await page.locator('input[type=file]').setInputFiles({ name: 'retry.txt', mimeType: 'text/plain', buffer: Buffer.from('retry') });
  await page.getByText('Upload failed', { exact: true }).waitFor();
  await page.evaluate(() => { window.uploadFail = false; });
  await page.getByRole('button', { name: 'Retry retry.txt' }).click();
  await page.getByText('Ready', { exact: true }).waitFor();
  await page.getByRole('button', { name: 'Remove retry.txt' }).click();
  assert.equal(await page.getByText('retry.txt', { exact: true }).count(), 0);
}));

test('paste and drop use the upload adapter, previews are removable and disabled sessions cannot send', async () => withComposer(async (page) => {
  await page.evaluate(() => window.mount({ upload: async (file) => ({ id: file.name, name: file.name }) }));
  await page.getByRole('combobox', { name: 'Message' }).evaluate((input) => {
    const clipboardData = new DataTransfer();
    clipboardData.items.add(new File(['image'], 'pasted.png', { type: 'image/png' }));
    input.dispatchEvent(new ClipboardEvent('paste', { clipboardData, bubbles: true, cancelable: true }));
  });
  await page.getByRole('img', { name: 'Preview of pasted.png' }).waitFor();
  await page.locator('.conversation-composer').evaluate((form) => {
    const dataTransfer = new DataTransfer();
    dataTransfer.items.add(new File(['notes'], 'dropped.txt', { type: 'text/plain' }));
    form.dispatchEvent(new DragEvent('drop', { dataTransfer, bubbles: true, cancelable: true }));
  });
  await page.getByRole('button', { name: 'Remove dropped.txt' }).waitFor();
  await page.getByRole('button', { name: 'Remove pasted.png' }).click();
  assert.equal(await page.getByRole('img').count(), 0);
  await page.evaluate(() => window.composer.setDisabled(true));
  assert.equal(await page.getByRole('combobox', { name: 'Message' }).isDisabled(), true);
  assert.equal(await page.getByRole('button', { name: 'Send', exact: true }).isDisabled(), true);
  await page.locator('form').evaluate((form) => form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })));
  assert.deepEqual(await page.evaluate(() => window.sent), []);
}));

test('Collaboration uses shared Markdown and composer while preserving authenticated agent task routing', async () => withComposer(async (page) => {
  const requests = [];
  let stateCount = 0;
  await page.route('**/api/v1/collab/**', async (route) => {
    const request = route.request(), path = new URL(request.url()).pathname.replace('/api/v1/collab', '');
    if (request.method() === 'POST') { requests.push({ path, auth: request.headers().authorization, body: request.postDataJSON() }); await route.fulfill({ json: {} }); return; }
    let body = {};
    if (path === '/config') body = { client_id: 'test', hosted_ui_domain: 'test', public_url: 'https://example.test', operator: { user_id: 'owner', email: 'owner@example.test' } };
    else if (path === '/rooms') body = { rooms: [{ room_id: 'room-one', name: 'Review', role: 'owner' }] };
    else if (path === '/admin/users') body = { users: [] };
    else if (path === '/rooms/room-one') body = { room_id: 'room-one', name: 'Review', role: 'owner', members: [] };
    else if (path === '/rooms/room-one/state') {
      stateCount++;
      body = { changed: stateCount === 1, seq: 1, next_cursor: 1, agents: [{ agent_id: 'coder-id', name: 'coder', actions: ['execute'], harness_type: 'codex', owner_user_id: 'owner' }], tasks: [], events: stateCount === 1 ? [{ event_type: 'message', sender: { type: 'user', id: 'owner' }, text: '**Formatted** <script>bad()</script>', created_at: new Date().toISOString() }] : [] };
      body.flow = { nodes: [{ id: 'coder-id', agent_id: 'coder-id', name: 'coder', kind: 'agent', actions: ['execute'], fabric: { session_id: 'session:one', status: 'active', display_name: 'Amber Finch' } }], edges: [], tasks: [] };
      if (stateCount > 1) await new Promise((r) => setTimeout(r, 100));
    }
    await route.fulfill({ json: body });
  });
  await page.evaluate(async () => {
    window.composer.dispose();
    const { mountCollab } = await import('/collab-view.js');
    window.collab = mountCollab(document.querySelector('#host'));
  });
  await page.locator('[data-room="room-one"]').click();
  await page.locator('.conversation-markdown strong').waitFor();
  assert.equal(await page.locator('.conversation-markdown strong').innerText(), 'Formatted');
  assert.equal(await page.locator('.conversation-markdown script').count(), 0);
  const input = page.getByRole('combobox', { name: 'Message' });
  await input.fill('@Amber'); await page.getByRole('option', { name: /Amber Finch/ }).waitFor(); await input.press('Tab');
  assert.equal(await input.inputValue(), '@coder ');
  await input.press('i'); await input.press('Enter');
  await page.getByRole('status').filter({ hasText: 'Task created' }).waitFor();
  assert.deepEqual(requests[0], { path: '/rooms/room-one/tasks', auth: 'Bearer operator', body: { target_agent_id: 'coder-id', text: 'i' } });
  await input.fill('hello'); await input.press('Enter');
  await page.getByRole('status').filter({ hasText: 'Message posted' }).waitFor();
  assert.equal(requests[1].path, '/rooms/room-one/messages');
  assert.equal(requests[1].auth, 'Bearer operator');
  assert.equal(requests[1].body.text, 'hello');
  await input.fill('@missing do work'); await input.press('Enter');
  await page.getByRole('alert').filter({ hasText: 'No agent named @missing' }).waitFor();
  assert.equal(requests.length, 2);
  assert.equal(await input.inputValue(), '@missing do work');
  await page.getByRole('tab', { name: 'Flow', exact: true }).click();
  await page.locator('[data-node="coder-id"]').click();
  assert.equal(await page.getByRole('link', { name: 'Open agent conversation' }).getAttribute('href'), '/#/herd/session/session%3Aone');
  await page.evaluate(() => window.collab.dispose());
}));

test('explicit @ references can insert a GitHub URL and Escape keeps typed text', async () => withComposer(async (page) => {
  await page.evaluate(() => window.mount({ suggestions: () => [{ id: 'issue', trigger: '@', label: 'Issue #12', insertText: 'https://github.com/org/repo/issues/12' }] }));
  const input = page.getByRole('combobox', { name: 'Message' });
  await input.fill('@12');
  await page.getByRole('option', { name: 'Issue #12' }).waitFor({ timeout: 3000 });
  await input.press('Escape');
  assert.equal(await input.inputValue(), '@12');
  await input.fill('@Issue'); await input.press('Enter');
  assert.equal(await input.inputValue(), 'https://github.com/org/repo/issues/12 ');
  assert.deepEqual(await page.evaluate(() => window.sent), []);
}));

test('embedded Collaboration deep links focus the selected agent and link only known native sessions', async () => withComposer(async (page) => {
  const nativeId = 'herdr:bender:default:w1Z%3ApF';
  await page.route('**/api/v1/collab/**', async (route) => {
    const path = new URL(route.request().url()).pathname.replace('/api/v1/collab', '');
    let body = { rooms: [], users: [], tokens: [], hosts: [], spawns: [] };
    if (path === '/config') body = { client_id: 'test', hosted_ui_domain: 'test', public_url: 'https://example.test', operator: { user_id: 'owner' } };
    if (path === '/agents') body = { agents: [
      { agent_id: 'coder-id', name: 'coder', status: 'active', harness_type: 'codex', instance_id: nativeId, rooms: [], expires_at: 2000000000 },
      { agent_id: 'unknown-id', name: 'unknown', status: 'active', harness_type: 'codex', instance_id: 'not-observed', rooms: [], expires_at: 2000000000 },
    ] };
    await route.fulfill({ json: body });
  });
  await page.evaluate(async (sessionId) => {
    window.composer.dispose();
    window.location.hash = '/collab?agent=coder-id';
    const { mountCollab } = await import('/collab-view.js');
    window.collab = mountCollab(document.querySelector('#host'), { fabricSessions: async () => [{ session_id: sessionId, display_name: 'Slate Owl', agent_role: 'coder', host_id: 'bender' }] });
  }, nativeId);
  const target = page.locator('[data-agent-card="coder-id"]');
  await target.waitFor({ timeout: 3000 });
  assert.match(await target.innerText(), /Slate Owl/);
  assert.equal(await target.evaluate((el) => el === document.activeElement), true);
  assert.equal(await target.getByRole('link', { name: 'Open agent conversation' }).getAttribute('href'), `/#/herd/session/${encodeURIComponent(nativeId)}`);
  assert.equal(await page.locator('[data-agent-card="unknown-id"] a').count(), 0);
  // Refreshing agent cards must not steal focus back from an active control.
  await target.getByRole('button', { name: 'Rename @coder' }).click();
  await page.getByRole('textbox', { name: 'New @ handle' }).press('Escape');
  await target.waitFor();
  assert.equal(await target.evaluate((el) => el === document.activeElement), false);
  await page.evaluate(async (sessionId) => {
    window.collab.dispose();
    const host = document.querySelector('#host'); host.dataset.standalone = '1';
    window.location.hash = '/agents';
    const { mountCollab } = await import('/collab-view.js');
    window.collab = mountCollab(host, { fabricSessions: async () => [{ session_id: sessionId }] });
  }, nativeId);
  await page.locator('.hd-shell .cx-agentcard').first().waitFor();
  assert.equal(await page.getByRole('link', { name: 'Open agent conversation' }).count(), 0);
  await page.evaluate(() => window.collab.dispose());
}));

test('expanded editor preserves textarea, attachment, selection and Escape completion priority', async () => withComposer(async (page) => {
  await page.evaluate(() => window.mount({ upload: async (file) => ({ name: file.name }) }));
  const input = page.getByRole('combobox', { name: 'Message' });
  await input.fill('hello @co');
  await input.evaluate((el) => { window.originalInput = el; el.setSelectionRange(6, 9); });
  await page.locator('input[type=file]').setInputFiles({ name: 'keep.txt', mimeType: 'text/plain', buffer: Buffer.from('x') });
  await page.getByRole('button', { name: 'Expand editor' }).click();
  assert.equal(await input.evaluate((el) => el === window.originalInput), true);
  assert.deepEqual(await input.evaluate((el) => [el.selectionStart, el.selectionEnd]), [6, 9]);
  await page.getByRole('button', { name: 'Remove keep.txt' }).waitFor();
  await input.fill('@co');
  await input.press('Escape');
  assert.equal(await page.locator('.conversation-composer--expanded').count(), 1);
  await input.press('Escape');
  assert.equal(await page.locator('.conversation-composer--expanded').count(), 0);
  assert.equal(await page.getByRole('button', { name: 'Expand editor' }).evaluate((el) => el === document.activeElement), true);
  assert.deepEqual(await page.evaluate(() => window.sent), []);
}));

test('editor JSON format and find replace are undoable and preview is safely rendered', async () => withComposer(async (page) => {
  const input = page.getByRole('combobox', { name: 'Message' });
  await input.fill('{"a":1}');
  await page.getByRole('button', { name: 'Expand editor' }).click();
  await page.getByRole('button', { name: 'Format JSON' }).click();
  assert.equal(await input.inputValue(), '{\n  "a": 1\n}');
  await page.getByRole('button', { name: 'Undo', exact: true }).click();
  assert.equal(await input.inputValue(), '{"a":1}');
  await page.getByRole('button', { name: 'Redo', exact: true }).click();
  assert.equal(await input.inputValue(), '{\n  "a": 1\n}');
  await page.getByRole('button', { name: 'Find and replace' }).click();
  await page.getByRole('textbox', { name: 'Find text', exact: true }).fill('a');
  await page.getByRole('textbox', { name: 'Replace with', exact: true }).fill('b');
  await page.getByRole('button', { name: 'Replace all', exact: true }).click();
  assert.ok((await input.inputValue()).includes('"b"'));
  await page.getByRole('button', { name: 'Undo', exact: true }).click();
  assert.ok((await input.inputValue()).includes('"a"'));
  await input.fill('{bad');
  await page.getByRole('button', { name: 'Format JSON' }).click();
  await page.getByRole('alert').filter({ hasText: 'Invalid JSON' }).waitFor();
  assert.equal(await input.inputValue(), '{bad');
  await input.fill('# Heading\n\n<script>bad()</script>');
  await page.getByRole('button', { name: 'Split preview', exact: true }).click();
  assert.equal(await page.locator('.conversation-editor-preview h1').innerText(), 'Heading');
  assert.equal(await page.locator('.conversation-editor-preview script').count(), 0);
  assert.deepEqual(await page.evaluate(() => window.sent), []);
}));

test('JSON viewer toggles raw content and handles clipboard denial', async () => withComposer(async (page) => {
  await page.evaluate(async () => {
    const { mountJsonViewer } = await import('/conversation-json.js');
    const host = document.createElement('div'); document.body.append(host);
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: async () => { throw new Error('denied'); } } });
    mountJsonViewer(host, '{"<script>":{"a":true}}');
  });
  await page.getByRole('button', { name: 'Raw JSON', exact: true }).click();
  assert.equal(await page.locator('[data-json-raw]').innerText(), '{"<script>":{"a":true}}');
  await page.getByRole('button', { name: 'Copy JSON', exact: true }).click();
  await page.getByText('Copy unavailable. Select and copy the raw JSON.').waitFor();
  assert.equal(await page.locator('[data-json-tree] script').count(), 0);
}));

test('expanded keyboard editing, indentation and previews never submit implicitly', async () => withComposer(async (page) => {
  const input = page.getByRole('combobox', { name: 'Message' });
  await input.fill('one\ntwo');
  await page.getByRole('button', { name: 'Expand editor' }).click();
  await input.press('Control+End'); await input.press('Enter'); await input.press('x');
  assert.equal(await input.inputValue(), 'one\ntwo\nx');
  await input.press('Control+z'); assert.equal(await input.inputValue(), 'one\ntwo\n');
  await input.press('Control+Shift+z'); assert.equal(await input.inputValue(), 'one\ntwo\nx');
  await input.press('Control+a'); await input.press('Tab');
  assert.equal(await input.inputValue(), '  one\n  two\n  x');
  await input.press('Shift+Tab'); assert.equal(await input.inputValue(), 'one\ntwo\nx');
  await page.getByRole('button', { name: 'Preview', exact: true }).click();
  assert.equal(await input.isVisible(), false);
  await page.getByRole('button', { name: 'Edit', exact: true }).click();
  assert.equal(await input.inputValue(), 'one\ntwo\nx');
  await page.getByRole('button', { name: 'Find and replace' }).click();
  await page.getByRole('textbox', { name: 'Find text', exact: true }).fill('two');
  await page.getByRole('textbox', { name: 'Find text', exact: true }).press('Enter');
  assert.deepEqual(await input.evaluate((el) => [el.selectionStart, el.selectionEnd]), [4,7]);
  assert.deepEqual(await page.evaluate(() => window.sent), []);
}));

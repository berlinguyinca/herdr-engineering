// Browser tests for T12: comments and steering in the lineage inspector. The collaboration API is mocked at the
// network layer (with a small in-memory model of the server's rules); the page code is the real collab-view.js.
import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import { chromium } from 'playwright';

const HOSTILE = '<img src=x onerror="window.__xss=1">"onmouseover="window.__xss=2"><script>window.__xss=3</script>';
const T = '2026-10-23T10:00:00+00:00';
const node = (id, extra = {}) => ({ id, node_type: id.split(':')[0], label: id.split(':')[1], state: null, state_reason: null, attrs: {}, visibility_scope: 'room', first_event_id: 'e1', last_event_id: 'e2', ...extra });
const GRAPH = { nodes: [node('agent:lead', { state: 'running' }), node('attempt:a1', { state: 'running' })], edges: [{ edge_type: 'spawn', from_id: 'agent:lead', to_id: 'attempt:a1', attrs: {}, stale: false, removed: false, binding: null }] };
const lineage = (step) => ({ as_of: { step: step ?? 2, visible_events: 2, head: step === null || step === undefined || step >= 2, event_id: 'e', received_at: T, window_trimmed: false }, ...GRAPH, total_nodes: 2, offset: 0, next_offset: null, truncated: false, truncated_reason: null });
const FENCE = 'f'.repeat(64);

function model(over = {}) {
  return { comments: [{ kind: 'comment', trust: 'untrusted', comment_id: 'c1', room_id: 'room-one', anchor: { kind: 'node', node_id: 'attempt:a1' }, author: { type: 'user', id: HOSTILE }, created_at: T,
    revision: 1, edited: false, retracted: false, moderated: false, text: HOSTILE }],
  steers: [{ kind: 'steering', steering_id: 's1', action: 'redirect', instruction: HOSTILE, status: 'pending', digest: 'd'.repeat(64), proposed_by: { type: 'user', id: 'bob' }, proposed_at: T, confirmed_by: null, reason: null, ack: null,
    target: { node_id: 'attempt:a1', node_type: 'attempt', fence: FENCE, pinned_source: 'key:abc', expected_digest: null } }],
  fence: FENCE, enabled: true, canConfirm: true, posts: [], ...over };
}

async function withPage(fn, over = {}) {
  const m = model(over);
  const server = createServer(async (req, res) => {
    if (req.url === '/') { res.setHeader('Content-Type', 'text/html'); res.end('<link rel="stylesheet" href="/collab.css"><div id="host"></div>'); return; }
    try { const file = new URL(`../../packages/${req.url.split('?')[0].slice(1)}`, import.meta.url); res.setHeader('Content-Type', req.url.endsWith('.css') ? 'text/css' : 'application/javascript'); res.end(await readFile(file)); }
    catch { res.writeHead(404); res.end(); }
  });
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  let browser;
  try {
    const fallback = '/home/wohlgemuth/.cache/ms-playwright/chromium-1243/chrome-linux64/chrome';
    const executablePath = process.env.PLAYWRIGHT_CHROMIUM || (existsSync(fallback) ? fallback : undefined);
    browser = await chromium.launch({ headless: true, args: ['--no-sandbox'], executablePath });
    const page = await browser.newPage({ viewport: { width: 1100, height: 1000 } });
    await page.route('**/api/v1/collab/**', async (route) => {
      const url = new URL(route.request().url());
      const path = url.pathname.replace('/api/v1/collab', '');
      const method = route.request().method();
      const post = method === 'POST' ? route.request().postDataJSON() : null;
      if (method === 'POST') m.posts.push({ path, body: post });
      let body = {};
      if (path === '/config') body = { client_id: 'test', hosted_ui_domain: 'test', public_url: 'https://example.test', operator: { user_id: 'owner', email: 'owner@example.test' } };
      else if (path === '/rooms') body = { rooms: [{ room_id: 'room-one', name: 'Review', role: 'owner' }] };
      else if (path === '/admin/users') body = { users: [] };
      else if (path === '/rooms/room-one') body = { room_id: 'room-one', name: 'Review', role: 'owner', members: [] };
      else if (path === '/rooms/room-one/state') body = { changed: true, seq: 1, next_cursor: 1, agents: [], tasks: [], events: [], flow: { nodes: [], edges: [], tasks: [] } };
      else if (path === '/graph/lineage') body = lineage(url.searchParams.has('step') ? Number(url.searchParams.get('step')) : null);
      else if (path === '/graph/lineage/timeline') body = { as_of: { step: 2, visible_events: 2, head: true }, lanes: [], markers: [], truncated: false, truncated_reason: null };
      else if (path === '/rooms/room-one/comments' && method === 'GET') body = { comments: m.comments, has_more: false };
      else if (path === '/rooms/room-one/comments' && method === 'POST') {
        m.comments.push({ kind: 'comment', trust: 'untrusted', comment_id: `c${m.comments.length + 1}`, room_id: 'room-one', anchor: post.anchor, author: { type: 'user', id: 'owner' }, created_at: T, revision: 1, edited: false, retracted: false, moderated: false, text: post.text });
        body = { comment: m.comments.at(-1) };
      } else if (path === '/rooms/room-one/steering/target') body = { node_id: 'attempt:a1', node_type: 'attempt', state: 'running', superseded: false, fence: m.fence, pinned_source: 'key:abc', steering_enabled: m.enabled,
        actions: m.enabled ? ['pause', 'resume', 'stop_after_step', 'redirect'] : [], can_propose: true, can_confirm: m.canConfirm, self_confirm: false };
      else if (path === '/rooms/room-one/steering' && method === 'GET') body = { steering: m.steers };
      else if (path === '/rooms/room-one/steering' && method === 'POST') {
        m.steers.unshift({ ...m.steers[0], steering_id: `s${m.steers.length + 1}`, action: post.action, instruction: post.instruction || null, proposed_by: { type: 'user', id: 'owner' } });
        body = { steering: m.steers[0] };
      } else if (path === '/rooms/room-one/steering/s1/confirm') {
        if (post.expected_fence !== m.fence) { await route.fulfill({ status: 409, json: { error: 'stale', code: 'stale_fence' } }); return; }
        const i = m.steers.findIndex((x) => x.steering_id === 's1');
        m.steers[i] = { ...m.steers[i], status: 'confirmed', confirmed_by: { type: 'user', id: 'owner' } };
        body = { steering: m.steers[i] };
      }
      await route.fulfill({ json: body });
    });
    await page.goto(`http://127.0.0.1:${server.address().port}/`);
    await page.evaluate(async () => { const { mountCollab } = await import('/collab-view.js'); window.collab = mountCollab(document.querySelector('#host')); });
    await page.locator('[data-room="room-one"]').click();
    await page.getByRole('tab', { name: 'Lineage', exact: true }).click();
    await page.locator('svg.lin-svg g[data-node]').first().waitFor();
    await fn(page, m);
    await page.evaluate(() => window.collab.dispose());
  } finally { await browser?.close(); await new Promise((r) => server.close(r)); }
}

const select = async (page, id = 'attempt:a1') => {
  await page.locator(`g[data-node="${id}"]`).focus();
  await page.keyboard.press('Enter');
  await page.locator('.wb').waitFor();
};

test('completed comment writes release controls even while refreshed metadata is delayed', async () => withPage(async page => {
  await select(page);
  await page.locator('.wb-comment').first().waitFor();
  let release;
  const pending = new Promise(resolve => { release = resolve; });
  await page.route('**/api/v1/collab/rooms/room-one/state?*', async route => { await pending; await route.fallback(); });
  await page.route('**/api/v1/collab/rooms/room-one/comments**', async route => {
    if (route.request().method() === 'GET') await pending;
    await route.fallback();
  });
  await page.locator('textarea[data-wb-field="comment"]').fill('Completed write');
  const button = page.getByRole('button', { name: 'Post comment', exact: true });
  await button.click();
  try {
    await page.waitForFunction(() => document.querySelector('form[data-wb-form="comment"] button[type="submit"]')?.disabled === false, null, { timeout: 2000 });
    assert.match(await page.locator('.wb').innerText(), /Comment posted/);
  } finally { release(); }
}));

test('hostile comment, author and instruction text never run and show as text', async () => withPage(async (page) => {
  await select(page);
  await page.locator('.wb-comment').first().waitFor();
  assert.equal(await page.evaluate(() => window.__xss), undefined);
  assert.equal(await page.locator('.wb img, .wb script').count(), 0);
  const t = await page.locator('.wb').innerText();
  assert.ok(t.includes('<img src=x') && t.includes('<script>'));
  assert.equal(await page.locator('.wb [onmouseover], .wb [onerror]').count(), 0);
}));

test('commenting by keyboard sends only anchor, text and a key; the draft survives a re-render', async () => withPage(async (page, m) => {
  await select(page);
  const box = page.locator('textarea[data-wb-field="comment"]');
  await box.focus();
  await page.keyboard.type('why did this retry?');
  await page.getByRole('button', { name: 'Show as table' }).click();       // forces a full re-render of the panel
  await page.getByRole('button', { name: 'Show graph' }).click();
  assert.equal(await page.locator('textarea[data-wb-field="comment"]').inputValue(), 'why did this retry?');
  await page.locator('textarea[data-wb-field="comment"]').focus();
  await page.keyboard.press('Tab');
  assert.equal(await page.evaluate(() => document.activeElement.textContent), 'Post comment');
  await page.keyboard.press('Enter');
  await page.locator('.wb-comment', { hasText: 'why did this retry?' }).waitFor();
  const sent = m.posts.find((p) => p.path === '/rooms/room-one/comments');
  assert.deepEqual(Object.keys(sent.body).sort(), ['anchor', 'idempotency_key', 'text']);
  assert.deepEqual(sent.body.anchor, { kind: 'node', node_id: 'attempt:a1' });
  assert.equal(await page.locator('textarea[data-wb-field="comment"]').inputValue(), '');
}));

test('a link can be discussed from the inspector and the anchor is an edge', async () => withPage(async (page, m) => {
  await select(page, 'agent:lead');
  await page.getByRole('button', { name: /Discuss the spawn link/ }).click();
  await page.locator('.wb', { hasText: 'Showing comments on a link' }).waitFor();
  await page.locator('textarea[data-wb-field="comment"]').fill('late spawn');
  await page.locator('textarea[data-wb-field="comment"]').dispatchEvent('input');
  await page.getByRole('button', { name: 'Post comment' }).click();
  await page.waitForFunction(() => document.querySelector('.wb')?.innerText.includes('late spawn'));
  assert.deepEqual(m.posts.find((p) => p.path === '/rooms/room-one/comments').body.anchor, { kind: 'edge', edge_type: 'spawn', from_id: 'agent:lead', to_id: 'attempt:a1' });
}));

test('steering: propose by keyboard, then a separate confirm step that restates the request', async () => withPage(async (page, m) => {
  await select(page);
  await page.locator('input[data-wb-field="action"][value="redirect"]').check();
  await page.locator('textarea[data-wb-field="instruction"]').fill('use the cache');
  await page.locator('textarea[data-wb-field="instruction"]').dispatchEvent('input');
  await page.getByRole('button', { name: 'Propose', exact: true }).click();
  await page.waitForFunction(() => document.querySelector('.wb')?.innerText.includes('A room owner must confirm'));
  // The notice appears BEFORE the list is reloaded; the reload then inserts the new request (s2) above s1 and shifts every button below it.
  // A click aimed at s1's button in that window landed on s2's Reject (seen in the failing runs: the POST list ended with .../s2/reject).
  // Wait for the reloaded list, so the layout is final before the test acts on it.
  await page.locator('[data-wb-steer="s2"]').waitFor();
  const prop = m.posts.find((p) => p.path === '/rooms/room-one/steering');
  assert.deepEqual(Object.keys(prop.body).sort(), ['action', 'expected_fence', 'idempotency_key', 'instruction', 'node_id']);
  assert.equal(prop.body.expected_fence, FENCE);
  // bob's pending request: the owner reviews, sees it restated, then confirms
  await page.locator('[data-wb-steer="s1"]').getByRole('button', { name: 'Review and confirm' }).click();
  const group = page.getByRole('group', { name: 'Confirm steering request' });
  await group.waitFor();
  assert.match(await group.innerText(), /Redirect with an instruction[\s\S]*attempt:a1[\s\S]*key:abc[\s\S]*bob/);
  assert.equal(m.posts.filter((p) => p.path.endsWith('/confirm')).length, 0, 'nothing is sent before the explicit second action');
  await group.getByRole('button', { name: 'Confirm and send' }).click();
  await page.locator('[data-wb-steer="s1"]', { hasText: 'Confirmed, waiting for the agent' }).waitFor();
  const conf = m.posts.find((p) => p.path.endsWith('/confirm'));
  assert.deepEqual(Object.keys(conf.body).sort(), ['digest', 'expected_fence']);
}));

test('if the target changed since the proposal, the confirm is not sent and the person is told', async () => withPage(async (page, m) => {
  await select(page);
  await page.locator('[data-wb-steer="s1"]').getByRole('button', { name: 'Review and confirm' }).click();
  m.fence = 'a'.repeat(64);                                             // the attempt moved on
  await page.getByRole('button', { name: 'Confirm and send' }).click();
  await page.getByRole('alert').filter({ hasText: /changed since/  }).waitFor();
  assert.equal(m.posts.filter((p) => p.path.endsWith('/confirm')).length, 0);
}));

test('steering off, viewer-only and replay states offer no controls', async () => {
  await withPage(async (page) => {
    await select(page);
    await page.locator('.wb', { hasText: 'switched off by the operator' }).waitFor();
    assert.equal(await page.locator('form[data-wb-form="propose"]').count(), 0);
  }, { enabled: false });
  await withPage(async (page) => {
    await select(page);
    await page.getByRole('button', { name: /Back/ }).click();
    await page.locator('.lin-replay__status', { hasText: 'Replay' }).waitFor();
    await page.locator('.wb', { hasText: 'A replay shows a past state' }).first().waitFor();
    assert.equal(await page.locator('.wb form').count(), 0);
  });
});

test('workbench controls are real buttons and fields reachable by Tab, with no horizontal overflow on a phone', async () => withPage(async (page) => {
  await page.setViewportSize({ width: 390, height: 900 });
  await select(page);
  assert.equal(await page.locator('.wb [data-wb-act]').evaluateAll((els) => els.every((e) => e.tagName === 'BUTTON')), true);
  assert.equal(await page.locator('.wb input, .wb textarea').evaluateAll((els) => els.every((e) => e.labels && e.labels.length > 0)), true);
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1), true);
}));

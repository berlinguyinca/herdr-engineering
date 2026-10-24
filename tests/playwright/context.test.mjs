// Browser tests for T14: context references in chat, questions and cross-workspace links in the lineage inspector. The collaboration API is mocked at the
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

const SECRET = 'task:top-secret-id';
const visibleCtx = { hidden: false, kind: 'node', node_id: 'attempt:a1', node_type: 'attempt', label: HOSTILE, state: 'running' };
const MSGS = [
  { sequence: 1, event_id: 'e1', event_type: 'message', created_at: T, room_id: 'room-one', sender: { type: 'user', id: 'bob', owner_user_id: 'bob' }, text: 'see this', trust: 'untrusted', context: visibleCtx },
  { sequence: 2, event_id: 'e2', event_type: 'message', created_at: T, room_id: 'room-one', sender: { type: 'user', id: 'bob', owner_user_id: 'bob' }, text: 'and this', trust: 'untrusted', context: { hidden: true, label: 'hidden reference' } },
];
const ASKED = { kind: 'consultation', consult_id: 'cq1', room_id: 'room-one', state: 'open', asked_by: { type: 'user', id: 'bob' }, asked_at: T, expires_at: T, target: { type: 'user', id: 'owner' },
  subject: { hidden: false, kind: 'node', node_id: 'attempt:a1', label: 'a1' }, question: HOSTILE, trust: 'untrusted', executable: false };
const LINK = { kind: 'cross_link', link_id: 'xl1', room_id: 'room-one', state: 'active', from: { node_id: 'attempt:a1', node_type: 'attempt', label: 'a1', workspaces: ['ws-1'] },
  to: { node_id: 'agent:lead', node_type: 'agent', label: 'lead', workspaces: ['ws-2'] }, created_by: { type: 'user', id: 'owner' }, created_at: T, note: HOSTILE, published: true };

async function withPage(fn, over = {}) {
  const m = { posts: [], consults: [ASKED], links: [LINK], features: { context_refs: true, consultation: true, cross_links: true }, ...over };
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
      else if (path === '/rooms/room-one/state') body = { changed: true, seq: 1, next_cursor: 2, agents: [], tasks: [], events: MSGS.filter((e) => e.sequence > Number(url.searchParams.get('cursor') || 0)), flow: { nodes: [], edges: [], tasks: [] } };
      else if (path === '/graph/lineage') body = lineage(null);
      else if (path === '/graph/lineage/timeline') body = { as_of: { step: 2, visible_events: 2, head: true }, lanes: [], markers: [], truncated: false, truncated_reason: null };
      else if (path === '/rooms/room-one/comments' && method === 'GET') body = { comments: [], has_more: false };
      else if (path === '/rooms/room-one/steering/target') body = { node_id: 'attempt:a1', node_type: 'attempt', state: 'running', superseded: false, fence: 'f'.repeat(64), pinned_source: 'key:abc', steering_enabled: false, actions: [], can_propose: true, can_confirm: true, self_confirm: false };
      else if (path === '/rooms/room-one/steering' && method === 'GET') body = { steering: [] };
      else if (path === '/rooms/room-one/consultations' && method === 'GET') body = { consultations: m.consults, targets: [{ kind: 'user', id: 'bob' }, { kind: 'agent', id: 'agent_1', label: "Dana's Claude" }], enabled: true, features: m.features };
      else if (path === '/rooms/room-one/xlinks' && method === 'GET') body = { links: m.links, enabled: true, features: m.features };
      else if (path === '/rooms/room-one/consultations' && method === 'POST') body = { consultation: ASKED };
      else if (path === '/rooms/room-one/xlinks' && method === 'POST') { m.links = [...m.links, { ...LINK, link_id: 'xl2' }]; body = { link: m.links.at(-1) }; }
      else if (path.endsWith('/revoke')) { m.links = m.links.map((l) => ({ ...l, state: 'revoked' })); body = { link_id: 'xl1', state: 'revoked' }; }
      else if (path.endsWith('/answer')) { m.consults = m.consults.map((c) => ({ ...c, state: 'answered', answer: post.answer })); body = { consult_id: 'cq1', state: 'answered' }; }
      else if (path === '/rooms/room-one/messages') body = { event: { ...MSGS[0], sequence: 3, event_id: 'e3', text: post.text } };
      await route.fulfill({ json: body });
    });
    await page.goto(`http://127.0.0.1:${server.address().port}/`);
    await page.evaluate(async () => { const { mountCollab } = await import('/collab-view.js'); window.collab = mountCollab(document.querySelector('#host')); });
    await page.locator('[data-room="room-one"]').click();
    await fn(page, m);
    await page.evaluate(() => window.collab.dispose());
  } finally { await browser?.close(); await new Promise((r) => server.close(r)); }
}

const openLineage = async (page) => {
  await page.getByRole('tab', { name: 'Lineage', exact: true }).click();
  await page.locator('svg.lin-svg g[data-node]').first().waitFor();
};
const select = async (page, id = 'attempt:a1') => {
  await openLineage(page);
  await page.locator(`g[data-node="${id}"]`).focus();
  await page.keyboard.press('Enter');
  await page.locator('.ctx').waitFor();
};

test('a visible reference is a labelled chip, a hidden one is a fixed phrase with nothing in it, hostile text never runs', async () => withPage(async (page) => {
  await page.locator('.ctx-chip').first().waitFor();
  assert.equal(await page.evaluate(() => window.__xss), undefined);
  assert.equal(await page.locator('#hcn-log img, #hcn-log script, #hcn-log [onerror], #hcn-log [onmouseover]').count(), 0);
  const hidden = page.locator('.ctx-chip--hidden');
  assert.ok(await hidden.count() >= 1);
  assert.match(await hidden.first().innerText(), /Hidden reference/);
  const html = await page.locator('#hcn-log').innerHTML();
  assert.ok(!html.includes(SECRET));
  assert.ok((await page.locator('button.ctx-chip').innerText()).includes('<img src=x'));        // shown as text
}));

test('opening a reference chip goes to that item in the Lineage tab', async () => withPage(async (page) => {
  await page.locator('button.ctx-chip').click();
  await page.locator('.ctx').waitFor();
  assert.equal(await page.getByRole('tab', { name: 'Lineage', exact: true }).getAttribute('aria-selected'), 'true');
  assert.match(await page.locator('.ctx').innerText(), /Chat, questions and links/);
}));

test('discussing an item attaches it to the next message, which is sent with only a node reference', async () => withPage(async (page, m) => {
  await select(page);
  await page.getByRole('button', { name: 'Discuss this in the room chat' }).click();
  await page.getByRole('tab', { name: 'Chat', exact: true }).click();
  await page.locator('#hcn-ctx-attached .ctx-chip').waitFor();
  await page.locator('#hcn-composer textarea').fill('what do you think?');
  await page.keyboard.press('Enter');
  await page.waitForFunction(() => true);
  await page.waitForTimeout(300);
  const sent = m.posts.find((p) => p.path === '/rooms/room-one/messages');
  assert.deepEqual(sent.body.context, { kind: 'node', node_id: 'attempt:a1' });
  assert.deepEqual(Object.keys(sent.body).sort(), ['context', 'idempotency_key', 'text']);
  assert.equal(await page.locator('#hcn-ctx-attached .ctx-chip').count(), 0);                 // used once
}));

test('asking sends the target, the node, the question and a key and nothing else', async () => withPage(async (page, m) => {
  await select(page);
  await page.locator('select[data-ctx-field="target"]').selectOption('agent:agent_1');
  await page.locator('textarea[data-ctx-field="question"]').fill('how long will it take?');
  await page.locator('textarea[data-ctx-field="question"]').dispatchEvent('input');
  await page.getByRole('button', { name: 'Ask', exact: true }).click();
  await page.waitForFunction(() => document.querySelector('.ctx')?.innerText.includes('Asked.'));
  const sent = m.posts.find((p) => p.path === '/rooms/room-one/consultations');
  assert.deepEqual(Object.keys(sent.body).sort(), ['idempotency_key', 'question', 'subject', 'target']);
  assert.deepEqual(sent.body.target, { kind: 'agent', id: 'agent_1' });
  assert.deepEqual(sent.body.subject, { kind: 'node', node_id: 'attempt:a1' });
}));

test('a question for you is shown as untrusted text with a single answer field and no way to run anything', async () => withPage(async (page, m) => {
  await openLineage(page);
  const inbox = page.locator('.ctx-inbox');
  await inbox.waitFor();
  assert.equal(await page.evaluate(() => window.__xss), undefined);
  assert.equal(await inbox.locator('img, script, [onerror], [onmouseover]').count(), 0);
  assert.match(await inbox.innerText(), /untrusted text/);
  assert.equal(await inbox.locator('input[type=file], [name=command], [name=shell]').count(), 0);
  await inbox.locator('textarea[name="answer"]').fill('about a day');
  await inbox.getByRole('button', { name: 'Answer', exact: true }).click();
  await page.waitForFunction(() => !document.querySelector('.ctx-inbox'));
  const sent = m.posts.find((p) => p.path.endsWith('/answer'));
  assert.deepEqual(sent.body, { action: 'answer', answer: 'about a day' });
}));

test('links across workspaces: listed with both workspaces, created and revoked with named buttons', async () => withPage(async (page, m) => {
  await select(page);
  const t = await page.locator('.ctx').innerText();
  assert.ok(t.includes('ws-1') && t.includes('ws-2') && t.includes('asserted by a person'));
  assert.equal(await page.locator('.ctx img, .ctx script').count(), 0);
  await page.locator('input[name="to"]').fill('agent:lead');
  await page.getByRole('button', { name: 'Link across workspaces' }).click();
  await page.waitForFunction(() => document.querySelector('.ctx')?.innerText.includes('Linked.'));
  const made = m.posts.find((p) => p.path === '/rooms/room-one/xlinks');
  assert.deepEqual(Object.keys(made.body).sort(), ['from', 'idempotency_key', 'to']);
  await page.getByRole('button', { name: 'Revoke' }).first().click();
  await page.waitForFunction(() => document.querySelector('.ctx')?.innerText.includes('Link revoked.'));
  assert.ok(m.posts.some((p) => p.path === '/rooms/room-one/xlinks/xl1/revoke'));
}));

test('the whole section is absent when the features are off, and works by Tab on a phone without overflow', async () => {
  await withPage(async (page) => {
    await openLineage(page);
    await page.locator(`g[data-node="attempt:a1"]`).focus();
    await page.keyboard.press('Enter');
    await page.locator('.lin-insp, .wb').first().waitFor();
    assert.equal(await page.locator('.ctx, .ctx-inbox').count(), 0);
  }, { features: { context_refs: false, consultation: false, cross_links: false } });
  await withPage(async (page) => {
    await page.setViewportSize({ width: 390, height: 900 });
    await select(page);
    assert.equal(await page.locator('.ctx button').evaluateAll((els) => els.every((e) => e.tagName === 'BUTTON')), true);
    assert.equal(await page.locator('.ctx input, .ctx textarea, .ctx select').evaluateAll((els) => els.every((e) => e.labels && e.labels.length > 0)), true);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1), true);
  });
});

// T16 UI gates on the MOCKED collaboration API (same approach as lineage.test.mjs / workbench.test.mjs): states the real server
// cannot be asked to produce on demand: server errors, empty data, a stale-fence conflict, slow responses and very long
// unbroken text. Each state is audited with the in-house checker and must be operable by keyboard.
//
// Run: node --test --test-concurrency=1 tests/playwright/ui-gates-mock.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { launch, watch } from './ui-gates-lib.mjs';
import { staticAudit, focusIndicatorAudit, overflowAudit, overflowFailure, summarize } from './a11y-checks.mjs';

const T = '2026-10-23T10:00:00+00:00';
const LONG = 'x'.repeat(300);
const FENCE = 'f'.repeat(64);
const node = (id, extra = {}) => ({ id, node_type: id.split(':')[0], label: id.split(':')[1], state: null, state_reason: null, attrs: {}, visibility_scope: 'room', first_event_id: 'e1', last_event_id: 'e2', ...extra });
const GRAPH = { nodes: [node('agent:lead', { state: 'running', label: LONG }), node('attempt:a1', { state: 'running' })], edges: [{ edge_type: 'spawn', from_id: 'agent:lead', to_id: 'attempt:a1', attrs: {}, stale: false, removed: false, binding: null }] };
const lineage = () => ({ as_of: { step: 2, visible_events: 2, head: true, event_id: 'e', received_at: T, window_trimmed: false }, ...GRAPH, total_nodes: 2, offset: 0, next_offset: null, truncated: false, truncated_reason: null });

async function withPage(behaviour, fn) {
  const server = createServer(async (req, res) => {
    if (req.url === '/') { res.setHeader('Content-Type', 'text/html'); res.end('<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>t</title><link rel="stylesheet" href="/herdr-design-system/theme.css"><link rel="stylesheet" href="/herdr-design-system/base.css"><link rel="stylesheet" href="/collab.css"></head><body class="cx-page"><div id="host"></div></body></html>'); return; }
    try { const file = new URL(`../../packages/${req.url.split('?')[0].slice(1)}`, import.meta.url); res.setHeader('Content-Type', req.url.endsWith('.css') ? 'text/css' : 'application/javascript'); res.end(await readFile(file)); }
    catch { res.writeHead(404); res.end(); }
  });
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  const browser = await launch();
  try {
    const page = await browser.newPage({ viewport: { width: 1100, height: 900 } });
    const w = watch(page);
    const state = { lineageCalls: 0, posts: [], ...behaviour };
    await page.route('**/api/v1/collab/**', async (route) => {
      const url = new URL(route.request().url());
      const path = url.pathname.replace('/api/v1/collab', '');
      const method = route.request().method();
      if (method === 'POST') state.posts.push({ path, body: route.request().postDataJSON() });
      let body = {};
      if (path === '/config') body = { client_id: 'test', hosted_ui_domain: 'test', public_url: 'https://example.test', operator: { user_id: 'owner', email: 'owner@example.test' } };
      else if (path === '/rooms') body = { rooms: [{ room_id: 'room-one', name: 'Review', role: 'owner' }] };
      else if (path === '/admin/users') body = { users: [] };
      else if (path === '/rooms/room-one') body = { room_id: 'room-one', name: 'Review', role: 'owner', members: [] };
      else if (path === '/rooms/room-one/state') body = { changed: true, seq: 1, next_cursor: 1, agents: [], tasks: [], events: [], flow: { nodes: [], edges: [], tasks: [] } };
      else if (path === '/graph/lineage') {
        state.lineageCalls += 1;
        if (state.delay) await new Promise((r) => setTimeout(r, state.delay));
        if (state.lineageFail && state.lineageCalls <= state.lineageFail) { await route.fulfill({ status: 500, json: { error: 'boom' } }); return; }
        body = state.empty ? { ...lineage(), nodes: [], edges: [], total_nodes: 0 } : lineage();
        if (state.truncated) body = { ...body, truncated: true, truncated_reason: state.truncated, next_offset: state.truncatedNext ?? null };
      } else if (path === '/graph/lineage/timeline') body = { as_of: { step: 2, visible_events: 2, head: true }, lanes: [], markers: [], truncated: false, truncated_reason: null };
      else if (path === '/rooms/room-one/comments' && method === 'GET') body = { comments: [{ kind: 'comment', trust: 'untrusted', comment_id: 'c1', room_id: 'room-one', anchor: { kind: 'node', node_id: 'attempt:a1' }, author: { type: 'user', id: 'bob' }, created_at: T, revision: 1, edited: false, retracted: false, moderated: false, text: LONG }], has_more: false };
      else if (path === '/rooms/room-one/steering/target') body = { node_id: 'attempt:a1', node_type: 'attempt', state: 'running', superseded: false, fence: FENCE, pinned_source: 'key:abc', steering_enabled: true, actions: ['pause', 'redirect'], can_propose: true, can_confirm: true, self_confirm: false };
      else if (path === '/rooms/room-one/steering' && method === 'GET') body = { steering: [{ kind: 'steering', steering_id: 's1', action: 'redirect', instruction: LONG, status: 'pending', digest: 'd'.repeat(64), proposed_by: { type: 'user', id: 'bob' }, proposed_at: T, confirmed_by: null, reason: null, ack: null, target: { node_id: 'attempt:a1', node_type: 'attempt', fence: FENCE, pinned_source: 'key:abc', expected_digest: null } }] };
      else if (path === '/rooms/room-one/steering/s1/confirm') { await route.fulfill({ status: 409, json: { error: 'stale', code: 'stale_fence' } }); return; }
      await route.fulfill({ json: body });
    });
    await page.goto(`http://127.0.0.1:${server.address().port}/`);
    await page.evaluate(async () => { const { mountCollab } = await import('/collab-view.js'); window.collab = mountCollab(document.querySelector('#host')); });
    await page.locator('[data-room="room-one"]').click();
    await page.locator('[data-sub="lineage"]').click();
    await fn(page, state, w);
    await page.evaluate(() => window.collab.dispose());
  } finally { await browser.close(); await new Promise((r) => server.close(r)); }
}
const audit = async (page, label) => {
  const f = [...await staticAudit(page, '#hcn-p-lineage'), ...await focusIndicatorAudit(page, '#hcn-p-lineage')];
  assert.deepEqual(f, [], `${label}\n${summarize(f)}`);
};

test('a server error shows an alert with a Retry that works from the keyboard, and focus is not lost', async () => withPage({ lineageFail: 1 }, async (page, state) => {
  await page.locator('#hcn-lineage [role=alert]').waitFor();
  assert.match(await page.locator('#hcn-lineage [role=alert]').innerText(), /Could not load|No lineage/);
  await audit(page, 'error state');
  await page.locator('[data-retry]').focus();
  await page.keyboard.press('Enter');
  await page.locator('svg.lin-svg g[data-node]').first().waitFor();
  assert.ok(state.lineageCalls >= 2, 'Retry asked again');
  assert.ok(await page.evaluate(() => document.activeElement !== document.body), 'focus is not dropped to <body> after Retry');
}));

test('an empty room explains itself, without a blank panel', async () => withPage({ empty: true }, async (page) => {
  await page.locator('#hcn-lineage .hd-empty').waitFor();
  assert.match(await page.locator('#hcn-lineage').innerText(), /No lineage is shown for this room yet/);
  await audit(page, 'empty state');
}));

test('a slow response announces that it is loading in a live region and stays operable', async () => withPage({ delay: 1500 }, async (page) => {
  await page.locator('.lin-replay__status').waitFor();
  assert.match(await page.locator('.lin-replay__status').innerText(), /Loading/);
  assert.equal(await page.locator('.lin-replay__status').getAttribute('aria-live'), 'polite');
  await page.locator('svg.lin-svg g[data-node]').first().waitFor({ timeout: 8000 });
  await audit(page, 'after loading');
}));

test('a stale-fence conflict on confirm is announced as an alert, keeps the request, and keeps focus', async () => withPage({}, async (page) => {
  await page.locator('svg.lin-svg g[data-node="attempt:a1"]').focus();
  await page.keyboard.press('Enter');
  await page.locator('[data-wb-act="confirm"]').waitFor();
  await page.locator('[data-wb-act="confirm"]').focus();
  await page.keyboard.press('Enter');
  await page.locator('[data-wb-act="confirm-go"]').focus();
  await page.keyboard.press('Enter');
  await page.locator('.wb [role=alert]').waitFor();
  assert.match(await page.locator('.wb [role=alert]').innerText(), /changed|reloaded|stale/i);
  assert.ok(await page.locator('.wb-steer').count() >= 1, 'the request is still listed');
  assert.ok(await page.evaluate(() => document.activeElement !== document.body), 'focus is not dropped to <body> after the conflict');
}));

test('300-character unbroken labels, comments and instructions never cause horizontal scrolling at 320px', async () => withPage({}, async (page) => {
  await page.setViewportSize({ width: 320, height: 568 });
  await page.locator('svg.lin-svg g[data-node="attempt:a1"]').focus();
  await page.keyboard.press('Enter');
  await page.locator('.wb-comment').first().waitFor();
  await page.locator('.wb-steer').first().waitFor();
  const o = await overflowAudit(page);
  assert.equal(overflowFailure(o), '', 'overflow');
}));

// What the server ran out of decides what the page may claim: `scan_budget` means nodes may be missing, `edge_budget` means every node
// came back but links were left out. A page that says "more nodes may exist" for an edges-only cut is wrong in the other direction.
for (const [reason, claims, denies] of [['scan_budget', /more nodes may exist/, /left out some links/], ['edge_budget', /left out some links/, /more nodes may exist/], ['response_bytes', /left out some links/, /more nodes may exist/]]) {
  test(`a graph cut at "${reason}" with no next page says the right thing`, async () => {
    await withPage({ truncated: reason }, async (page) => {
      await page.locator('svg.lin-svg g[data-node]').first().waitFor();
      const t = await page.locator('#hcn-lineage').innerText();
      assert.match(t, claims, `says: ${t.slice(0, 200)}`);
      assert.doesNotMatch(t, denies);
      assert.equal(await page.locator('[data-more]').count(), 0, 'no Load more when the server will give no more');
      assert.ok(await page.locator('#hcn-lineage [role=status]', { hasText: /Showing/ }).count() >= 1, 'it is announced in a status region');
    });
  });
}
test('a graph with more pages to load offers Load more, whatever the reason', async () => {
  await withPage({ truncated: 'limit', truncatedNext: 2 }, async (page) => {
    await page.locator('svg.lin-svg g[data-node]').first().waitFor();
    assert.equal(await page.locator('[data-more]').count(), 1);
    assert.doesNotMatch(await page.locator('#hcn-lineage').innerText(), /more nodes may exist|left out some links/);
  });
});

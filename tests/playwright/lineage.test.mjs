// Browser smoke test for the T09 lineage workbench (graph, Gantt, replay, inspector). The collaboration API is
// mocked at the network layer; the page code under test is the real collab-view.js / collab-lineage.js.
import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import { chromium } from 'playwright';

const HOSTILE = '<img src=x onerror="window.__xss=1">"onmouseover="window.__xss=2"';
const T = (s) => `2026-10-22T10:00:${String(s).padStart(2, '0')}+00:00`;
const node = (id, extra = {}) => ({ id, node_type: id.split(':')[0], label: id.split(':')[1], state: null, state_reason: null, attrs: {}, visibility_scope: 'room', first_event_id: 'e1', last_event_id: 'e2', ...extra });
const edge = (edge_type, from_id, to_id, extra = {}) => ({ edge_type, from_id, to_id, attrs: {}, stale: false, removed: false, binding: null, ...extra });

const FULL = {
  nodes: [node('agent:lead', { label: 'Lead', state: 'running' }), node('agent:kid', { label: HOSTILE, state: 'done' }), node('attempt:a1', { state: 'failed' }),
    node('pr:github.com/o/r#1', { label: 'PR 1', state: 'open' }), node('review:r1', { label: 'R1' })],
  edges: [edge('spawn', 'agent:lead', 'agent:kid'), edge('spawn', 'agent:kid', 'attempt:a1'),
    edge('reviewed', 'review:r1', 'pr:github.com/o/r#1', { binding: { commit: 'c1' }, stale: true })],
};
const lineage = (step) => {
  const n = step === null ? 5 : step;
  return { as_of: { step: n, visible_events: 5, head: n >= 5, event_id: 'e', received_at: T(n), window_trimmed: false },
    nodes: FULL.nodes.slice(0, Math.min(n, 5)), edges: FULL.edges.filter((e) => FULL.nodes.slice(0, n).some((x) => x.id === e.from_id) && FULL.nodes.slice(0, n).some((x) => x.id === e.to_id)),
    total_nodes: 5, offset: 0, next_offset: null, truncated: false, truncated_reason: null };
};
const TIMELINE = {
  as_of: { step: 5, visible_events: 5, head: true }, truncated: false, truncated_reason: null,
  lanes: [{ node_id: 'attempt:a1', node_type: 'attempt', label: 'a1', state: 'failed', first_at: T(1), last_at: T(9),
    spans: [{ state: 'running', start: T(1), end: T(5), open: false, terminal: false }, { state: 'failed', start: T(5), end: T(5), open: false, terminal: true, reason: 'boom' }] },
  { node_id: 'agent:lead', node_type: 'agent', label: 'Lead', state: 'running', first_at: T(0), last_at: T(0), spans: [{ state: 'running', start: T(0), end: null, open: true, terminal: false }] }],
  markers: [{ kind: 'head_change', step: 4, at: T(7), claimed: null, edge_type: 'reviewed', from_id: 'review:r1', to_id: 'pr:p', binding: { commit: 'c2' }, stale: false }],
};

async function withPage(fn, opts = {}) {
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
    const page = await browser.newPage({ viewport: { width: 1100, height: 900 } });
    const seen = [];
    await page.route('**/api/v1/collab/**', async (route) => {
      const url = new URL(route.request().url());
      const path = url.pathname.replace('/api/v1/collab', '');
      seen.push({ method: route.request().method(), path, q: Object.fromEntries(url.searchParams) });
      let body = {};
      if (path === '/config') body = { client_id: 'test', hosted_ui_domain: 'test', public_url: 'https://example.test', operator: { user_id: 'owner', email: 'owner@example.test' } };
      else if (path === '/rooms') body = { rooms: [{ room_id: 'room-one', name: 'Review', role: 'owner' }] };
      else if (path === '/admin/users') body = { users: [] };
      else if (path === '/rooms/room-one') body = { room_id: 'room-one', name: 'Review', role: 'owner', members: [] };
      else if (path === '/rooms/room-one/state') body = { changed: true, seq: 1, next_cursor: 1, agents: [], tasks: [], events: [], flow: { nodes: [], edges: [], tasks: [] } };
      else if (path === '/graph/lineage' && seen.failStep !== undefined && url.searchParams.get('step') === String(seen.failStep)) { await route.fulfill({ status: 500, json: { error: 'boom' } }); return; }
      else if (path === '/graph/lineage') body = opts.lineage ? opts.lineage(url) : lineage(url.searchParams.has('step') ? Number(url.searchParams.get('step')) : null);
      else if (path === '/graph/lineage/timeline') body = opts.timeline || TIMELINE;
      else if (path.startsWith('/graph/nodes')) body = { nodes: [], edges: [] };
      await route.fulfill({ json: body });
    });
    await page.goto(`http://127.0.0.1:${server.address().port}/`);
    await page.evaluate(async () => { const { mountCollab } = await import('/collab-view.js'); window.collab = mountCollab(document.querySelector('#host')); });
    await page.locator('[data-room="room-one"]').click();
    await fn(page, seen);
    await page.evaluate(() => window.collab.dispose());
  } finally { await browser?.close(); await new Promise((r) => server.close(r)); }
}

test('lineage graph: renders, hostile ids stay text, keyboard selects and the inspector follows', async () => withPage(async (page, seen) => {
  await page.getByRole('tab', { name: 'Lineage', exact: true }).click();
  await page.locator('svg.lin-svg g[data-node]').first().waitFor();
  assert.equal(await page.locator('svg.lin-svg g[data-node]').count(), 5);
  assert.equal(await page.evaluate(() => window.__xss), undefined, 'a hostile label never runs');
  assert.equal(await page.locator('#hcn-lineage img').count(), 0);
  assert.ok(await page.locator('#hcn-lineage').innerText().then((t) => t.includes('<img src=x')), 'the hostile label is shown as text');
  assert.equal(await page.locator('g[data-node][tabindex="0"]').count(), 1, 'one tab stop in the graph');
  await page.locator('g[data-node][tabindex="0"]').focus();
  await page.keyboard.press('ArrowRight');
  assert.ok(await page.evaluate(() => document.activeElement.getAttribute('data-node')) !== null);
  await page.keyboard.press('Enter');
  await page.locator('aside[aria-label="Inspector"] .lin-inspector').waitFor();
  assert.match(await page.locator('aside[aria-label="Inspector"]').innerText(), /Read-only\. This page offers no terminal/);
  assert.equal(await page.locator('g[data-node][aria-pressed="true"]').count(), 1);
  await page.keyboard.press('Escape');
  assert.equal(await page.locator('g[data-node][aria-pressed="true"]').count(), 0);
  assert.ok(seen.every((r) => r.method === 'GET' || !r.path.includes('/graph')), 'the lineage page only reads');
}));

test('search filters in place: Enter does not navigate and typing keeps focus', async () => withPage(async (page) => {
  await page.getByRole('tab', { name: 'Lineage', exact: true }).click();
  await page.locator('svg.lin-svg g[data-node]').first().waitFor();
  const before = page.url();
  const box = page.getByRole('searchbox');
  await box.fill('lead');
  await box.press('Enter');
  assert.equal(page.url(), before);
  assert.equal(await page.evaluate(() => document.activeElement.getAttribute('data-filter')), 'q', 'focus stays in the search box');
  assert.equal(await page.locator('svg.lin-svg g[data-node]').count(), 2, 'the hit and its one-hop neighbour');
  await box.fill('');
  assert.equal(await page.locator('svg.lin-svg g[data-node]').count(), 5);
}));

test('lineage text alternatives: table view lists the same nodes and links', async () => withPage(async (page) => {
  await page.getByRole('tab', { name: 'Lineage', exact: true }).click();
  await page.locator('svg.lin-svg').waitFor();
  await page.getByRole('button', { name: 'Show as table' }).click();
  await page.locator('table.lin-table').first().waitFor();
  assert.equal(await page.locator('svg.lin-svg').count(), 0);
  assert.equal(await page.locator('table.lin-table').first().locator('tbody tr').count(), 5);
  assert.match(await page.locator('table.lin-table').nth(1).innerText(), /reviewed[\s\S]*stale/);
  await page.getByRole('button', { name: 'Show graph' }).click();
  await page.locator('svg.lin-svg').waitFor();
}));

test('replay: the slider and step buttons load an earlier position; Live returns to now', async () => withPage(async (page, seen) => {
  await page.getByRole('tab', { name: 'Lineage', exact: true }).click();
  await page.locator('svg.lin-svg g[data-node]').first().waitFor();
  assert.match(await page.locator('.lin-replay__status').innerText(), /^Live/);
  await page.getByRole('button', { name: /Back/ }).click();
  await page.locator('.lin-replay__status', { hasText: 'Replay: event 4 of 5' }).waitFor();
  assert.equal(await page.locator('svg.lin-svg g[data-node]').count(), 4);
  assert.ok(seen.some((r) => r.path === '/graph/lineage' && r.q.step === '4'));
  assert.equal(await page.locator('input[data-replay="slider"]').getAttribute('aria-valuetext').then((v) => /event 4 of 5/.test(v)), true);
  await page.getByRole('button', { name: /Start/ }).click();
  await page.locator('.lin-replay__status', { hasText: 'before the first' }).waitFor();
  assert.equal(await page.locator('svg.lin-svg g[data-node]').count(), 0);
  await page.getByRole('button', { name: 'Live', exact: true }).click();
  await page.locator('.lin-replay__status', { hasText: 'Live' }).waitFor();
  assert.equal(await page.locator('svg.lin-svg g[data-node]').count(), 5);
}));

test('gantt: lanes with patterns, a head-change marker, ongoing work and a table alternative', async () => withPage(async (page, seen) => {
  await page.getByRole('tab', { name: 'Lineage', exact: true }).click();
  await page.locator('svg.lin-svg').waitFor();
  await page.getByRole('tab', { name: 'Gantt', exact: true }).click();
  await page.locator('svg[aria-label^="Gantt timeline"]').waitFor();
  assert.ok(seen.some((r) => r.path === '/graph/lineage/timeline'));
  assert.equal(await page.locator('svg[aria-label^="Gantt timeline"] g[data-lane]').count(), 2);
  assert.ok(await page.locator('svg[aria-label^="Gantt timeline"]').innerHTML().then((h) => h.includes('url(#lin-p-failed)') && h.includes('>H<')));
  const table = page.getByRole('region', { name: 'Timeline table' });
  assert.match(await table.innerText(), /ongoing/);
  assert.match(await table.innerText(), /boom/);
  await page.getByRole('button', { name: 'Show as table' }).click();
  assert.equal(await page.locator('svg[aria-label^="Gantt timeline"]').count(), 0);
  assert.equal(await page.getByRole('region', { name: 'Timeline table' }).count(), 1);
}));

test('lineage panel has no horizontal page overflow on a phone-width screen and honours reduced motion', async () => withPage(async (page) => {
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await page.setViewportSize({ width: 390, height: 800 });
  await page.getByRole('tab', { name: 'Lineage', exact: true }).click();
  await page.locator('svg.lin-svg').waitFor();
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1), true);
  assert.equal(await page.evaluate(() => [...document.querySelectorAll('.lin-svg *')].every((el) => getComputedStyle(el).animationName === 'none')), true);
}));

const active = (page) => page.evaluate(() => { const a = document.activeElement; return a ? (a.getAttribute('data-replay') ? `replay:${a.getAttribute('data-replay')}` : a.getAttribute('data-node') ? `node:${a.getAttribute('data-node')}` : a.getAttribute('data-view') ? `view:${a.getAttribute('data-view')}` : a.getAttribute('data-filter') ? `filter:${a.getAttribute('data-filter')}` : a.tagName) : null; });

test('focus survives replay actions and re-renders (keyboard users keep their place)', async () => withPage(async (page) => {
  await page.getByRole('tab', { name: 'Lineage', exact: true }).click();
  await page.locator('svg.lin-svg g[data-node]').first().waitFor();
  await page.getByRole('button', { name: /Back/ }).focus();
  await page.keyboard.press('Enter');
  await page.locator('.lin-replay__status', { hasText: 'event 4 of 5' }).waitFor();
  assert.equal(await active(page), 'replay:prev', 'Back keeps focus after the replay loads');
  await page.keyboard.press('Enter');
  await page.locator('.lin-replay__status', { hasText: 'event 3 of 5' }).waitFor();
  assert.equal(await active(page), 'replay:prev');
  assert.equal(await page.locator('.lin-replay button[disabled]').count(), 0, 'busy never disables the controls');
  await page.getByRole('button', { name: 'Live', exact: true }).focus();
  await page.keyboard.press('Enter');
  await page.locator('.lin-replay__status', { hasText: 'Live' }).waitFor();
  assert.equal(await active(page), 'replay:live');
  await page.locator('g[data-node]').first().focus();
  await page.keyboard.press('Enter');
  assert.match(await active(page), /^node:/, 'selecting a node keeps focus on it');
  await page.getByRole('tab', { name: 'Gantt' }).focus();
  await page.keyboard.press('ArrowLeft');
  assert.equal(await active(page), 'view:graph');
}));

test('a failed replay keeps the controls, offers Retry and Live, and recovers', async () => withPage(async (page, seen) => {
  await page.getByRole('tab', { name: 'Lineage', exact: true }).click();
  await page.locator('svg.lin-svg g[data-node]').first().waitFor();
  seen.failStep = 4;
  await page.getByRole('button', { name: /Back/ }).click();
  await page.getByRole('alert').filter({ hasText: 'Could not load the lineage' }).waitFor();
  assert.equal(await page.getByRole('button', { name: 'Live', exact: true }).count(), 1, 'Live is still offered');
  assert.equal(await page.getByRole('button', { name: 'Retry' }).count(), 1);
  assert.doesNotMatch(await page.locator('#hcn-lineage').innerText(), /will be retried/);
  seen.failStep = undefined;
  await page.getByRole('button', { name: 'Retry' }).click();
  await page.locator('.lin-replay__status', { hasText: 'event 4 of 5' }).waitFor();
  seen.failStep = 4;
  await page.getByRole('button', { name: /Start/ }).click();
  await page.locator('.lin-replay__status', { hasText: 'before the first' }).waitFor();
  seen.failStep = 0;
  await page.getByRole('button', { name: /Next/ }).click();
  await page.getByRole('button', { name: 'Live', exact: true }).click();
  await page.locator('.lin-replay__status', { hasText: 'Live' }).waitFor();
}));

const BAD = '<img src=x onerror="window.__xss=7">';
test('hostile text in EVERY field is shown as text and never runs', async () => withPage(async (page) => {
  await page.getByRole('tab', { name: 'Lineage', exact: true }).click();
  await page.locator('svg.lin-svg g[data-node]').first().waitFor();
  await page.getByRole('button', { name: 'Show as table' }).click();
  await page.getByRole('tab', { name: 'Gantt' }).click();
  await page.locator('table.lin-table').first().waitFor();
  assert.equal(await page.evaluate(() => window.__xss), undefined);
  assert.equal(await page.locator('#hcn-lineage img, #hcn-lineage script, #hcn-lineage [onerror]').count(), 0);
  assert.ok((await page.locator('#hcn-lineage').innerText()).includes(BAD));
}, {
  lineage: () => {
    const n = (id, e = {}) => ({ id, node_type: id.split(':')[0], label: BAD, state: BAD, state_reason: BAD, attrs: { [BAD]: BAD }, visibility_scope: 'room', first_event_id: BAD, last_event_id: BAD, ...e });
    return { as_of: { step: 2, visible_events: 2, head: true, event_id: BAD, received_at: BAD, window_trimmed: false },
      nodes: [n(`task:${BAD}`), n('agent:b')], edges: [{ edge_type: 'spawn', from_id: `task:${BAD}`, to_id: 'agent:b', attrs: { [BAD]: BAD }, stale: false, removed: false, binding: { [BAD]: BAD } }],
      total_nodes: 2, offset: 0, next_offset: null, truncated: false, truncated_reason: null };
  },
  timeline: { as_of: { step: 2, visible_events: 2, head: true }, truncated: false, truncated_reason: null,
    lanes: [{ node_id: `task:${BAD}`, node_type: 'task', label: BAD, state: BAD, first_at: T(1), last_at: T(2), spans: [{ state: BAD, start: T(1), end: T(2), open: false, terminal: false, reason: BAD }] }],
    markers: [{ kind: BAD, step: 1, at: T(1), claimed: BAD, edge_type: BAD, from_id: BAD, to_id: BAD, binding: { [BAD]: BAD }, stale: false }] },
}));

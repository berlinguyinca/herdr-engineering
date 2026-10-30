// The Handoffs page as a big picture: workspaces, attention, and click-through detail with the chain. The API is stubbed;
// the page code under test is the real app.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { withPage } from './helpers/fixture.mjs';
import { boot, until } from './helpers/live.mjs';

const HOSTILE = '<img src=x onerror="window.__xss=1">';
const sum = (id, extra = {}) => ({ handoff_id: id, state: 'queued', workspace: 'org/app', source: { harness: 'pi', agent_id: 'a1', session_id: 's1' },
  target: { harness: 'pi', agent_id: 'a2', session_id: 's2' }, goal: `goal ${id}`, updated_at: '2026-01-01T00:00:00Z', completed: 1, remaining: 2, attention: false, stalled: false, ...extra });
const OVERVIEW = { total: 4, states: { queued: 2, failed: 1, completed: 1 }, attention_total: 1,
  workspaces: [{ workspace: 'org/app', total: 3, states: { failed: 1, completed: 1, queued: 1 }, attention: 1, stalled: 0, latest_at: '2026-01-01T00:00:00Z' },
    { workspace: 'lib', total: 1, states: { queued: 1 }, attention: 0, stalled: 0, latest_at: '2026-01-01T00:00:00Z' }],
  workspaces_omitted: 0, attention: [sum('h3', { state: 'failed', attention: true, goal: HOSTILE })] };
const ROWS = { 'org/app': [sum('h3', { state: 'failed', attention: true, goal: HOSTILE }), sum('h2', { state: 'completed' }), sum('h1')], lib: [sum('l1', { workspace: 'lib' })] };
const FULL = (id) => ({ handoff_id: id, state: id === 'h3' ? 'failed' : 'queued', goal: id === 'h3' ? HOSTILE : `goal ${id}`, source: { harness: 'pi', agent_id: 'a1' },
  target: { harness: 'pi', agent_id: 'a2', model_hint: 'qwen' }, completed: ['wrote the parser', HOSTILE], remaining: ['add tests'], decisions: [], risks: [], validation: [],
  history: [{ state: 'queued', at: '2026-01-01T00:00:00Z', actor: { id: 'op' } }], mission_id: null, updated_at: '2026-01-01T00:00:00Z' });
const api = (calls) => (p, url) => {
  calls.push(p + url.search);
  if (p === '/api/v1/handoffs/overview') return OVERVIEW;
  if (p === '/api/v1/handoffs') return { handoffs: ROWS[url.searchParams.get('workspace')] || [], next_cursor: null };
  const chain = p.match(/^\/api\/v1\/handoffs\/([^/]+)\/chain$/);
  if (chain) return { handoff_id: chain[1], position: 1, chain: [sum('h1'), sum(chain[1]), sum('h4')] };
  const one = p.match(/^\/api\/v1\/handoffs\/([^/]+)$/);
  if (one) return FULL(one[1]);
  return { capabilities: [] };
};

test('overview shows workspaces and attention; a handoff opens accomplished, missing, next and the chain', async () => {
  await withPage(async (page, base) => {
    const calls = [];
    await boot(page, base, '/handoffs', api(calls));
    await until(() => page.locator('[data-handoff-summary]').innerText().then((t) => t === '4 handoffs · 2 workspaces · 1 need attention'), 'summary');
    assert.equal(await page.locator('[data-handoff-list] [data-workspace]').count(), 2);
    assert.equal(await page.locator('[data-handoff-attention] [data-handoff]').count(), 1);
    assert.ok(!calls.some((c) => c.startsWith('/api/v1/handoffs?')), 'no rows are fetched until a workspace opens');

    await page.locator('[data-workspace="org/app"] .hd-ws__head').click();
    await until(() => page.locator('[data-workspace="org/app"] [data-handoff]').count().then((n) => n === 3), 'workspace rows');
    assert.ok(await page.locator('[data-workspace="org/app"] [data-handoff-graph] svg').count(), 'the flow is drawn for the open workspace');

    await page.locator('[data-workspace="org/app"] [data-handoff="h2"]').click();
    await until(() => page.locator('[data-handoff-detail-body]').count().then((n) => n === 1), 'detail');
    const text = (await page.locator('[data-handoff-detail]').innerText()).toLowerCase();
    for (const part of ['accomplished', 'wrote the parser', 'missing / what happens next', 'add tests', 'model hint', 'qwen']) assert.ok(text.includes(part), part);
    assert.equal(await page.locator('.hd-chain .hd-hop').count(), 3);
    assert.equal(await page.locator('.hd-hop--here').getAttribute('data-handoff'), 'h2');
    await page.locator('[data-handoff-full] summary').click();
    const full = await page.locator('[data-handoff-full] pre').innerText();
    for (const part of ['HANDOFF h2', 'GOAL', 'goal h2', 'wrote the parser', 'add tests']) assert.ok(full.includes(part), `full text has ${part}`);
    assert.equal(await page.locator('[data-handoff-copy]').count(), 1);

    await page.locator('.hd-chain [data-handoff="h4"]').click();
    await until(() => page.locator('[data-handoff-detail] h2').count().then((n) => n === 1) && page.locator('[data-handoff-detail-body]').getAttribute('aria-label').then((a) => a === 'Handoff h4'), 'next hop');
  });
});

test('untrusted handoff text is escaped everywhere and arrows open the newest handoff of the pair', async () => {
  await withPage(async (page, base) => {
    await boot(page, base, '/handoffs', api([]));
    await until(() => page.locator('[data-handoff-attention] [data-handoff]').count().then((n) => n === 1), 'attention');
    await page.locator('[data-handoff-attention] [data-handoff="h3"]').click();
    await until(() => page.locator('[data-handoff-detail-body]').count().then((n) => n === 1), 'detail');
    assert.equal(await page.evaluate(() => window.__xss), undefined);
    assert.equal(await page.locator('#view img').count(), 0);

    await page.locator('[data-workspace="org/app"] .hd-ws__head').click();
    await until(() => page.locator('[data-workspace="org/app"] [data-edge]').count().then((n) => n >= 1), 'arrow');
    await page.locator('[data-workspace="org/app"] [data-edge]').first().dispatchEvent('click');   // a straight arrow has no height for a pointer click
    await until(() => page.locator('[data-handoff-detail-body]').getAttribute('aria-label').then((a) => a === 'Handoff h3'), 'arrow opens the newest handoff');
  });
});

test('a deep link opens that handoff straight away, so a PR trail or an agent story can point at it', async () => {
  await withPage(async (page, base) => {
    await boot(page, base, '/handoffs?open=h2', api([]));
    await until(() => page.locator('[data-handoff-detail-body]').getAttribute('aria-label').then((a) => a === 'Handoff h2'), 'deep-linked detail');
    assert.equal(await page.locator('.hd-chain .hd-hop').count(), 3);
  });
});

test('a workspace with many routes lists who hands to whom, and picking one filters the handoffs', async () => {
  await withPage(async (page, base) => {
    const pairs = Array.from({ length: 9 }, (_, i) => [`a${i}`, `b${i}`]);
    const rows = pairs.flatMap(([a, b], i) => [sum(`w${i}x`, { workspace: 'wide', source: { harness: 'pi', agent_id: a }, target: { harness: 'pi', agent_id: b } }),
      ...(i === 0 ? [sum('w0y', { workspace: 'wide', source: { harness: 'pi', agent_id: a }, target: { harness: 'pi', agent_id: b } })] : [])]);
    await boot(page, base, '/handoffs', (p, url) => p === '/api/v1/handoffs/overview' ? { ...OVERVIEW, workspaces: [{ workspace: 'wide', total: rows.length, states: { queued: rows.length }, attention: 0, stalled: 0, latest_at: '2026-01-01T00:00:00Z' }], attention: [] }
      : p === '/api/v1/handoffs' ? { handoffs: rows, next_cursor: null } : api([])(p, url));
    await page.locator('[data-workspace="wide"] .hd-ws__head').click();
    await until(() => page.locator('[data-handoff-routes] .hd-route').count().then((n) => n === 9), 'one row per route');
    assert.equal(await page.locator('[data-workspace="wide"] [data-handoff-graph]').count(), 0, 'no unreadable graph at this size');
    assert.equal(await page.locator('[data-workspace="wide"] .hd-row').count(), 10);
    await page.locator('[data-route="@a0 → @b0"]').click();
    await until(() => page.locator('[data-workspace="wide"] .hd-row').count().then((n) => n === 2), 'filtered to the route');
    assert.match(await page.locator('[data-handoff-filter]').innerText(), /@a0 → @b0/);
    await page.locator('[data-route-clear]').click();
    await until(() => page.locator('[data-workspace="wide"] .hd-row').count().then((n) => n === 10), 'show all');
  });
});

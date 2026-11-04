// The operator-only "Check terminal access" control in the lineage inspector (S5), on the REAL dashboard page over a stubbed API.
//   * the operator sees it for a node that maps to a workspace link the server marked can_terminal, and the click POSTs the real
//     route /collab/rooms/<room>/workspaces/<link>/authorize with {action: "terminal"}; an allowed and a denied answer are both shown;
//   * a node with no such link, or a link with can_terminal false, shows nothing;
//   * a viewer who is not the operator (no `operator` in the collab config, signed in with a token) sees nothing and the route is never called.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { withPage } from './helpers/fixture.mjs';
import { boot, until } from './helpers/live.mjs';

const node = (id, extra = {}) => ({ id, node_type: id.split(':')[0], label: id.split(':')[1], state: 'running', attrs: {}, visibility_scope: 'room', ...extra });
const GRAPH = {
  nodes: [node('workspace:ws-1'), node('workspace:ws-2'), node('workspace:ws-9'), node('task:plain')], edges: [],
  as_of: { step: 4, visible_events: 4, head: true }, total_nodes: 4, next_offset: null, truncated_reason: null,
};
const LINKS = [
  { link_id: 'wl_1', room_id: 'r1', workspace_id: 'ws-1', host: 'bender', grants: ['status'], is_primary: true, host_verified: true, can_terminal: true },
  { link_id: 'wl_2', room_id: 'r1', workspace_id: 'ws-2', host: 'bender', grants: ['status'], is_primary: false, host_verified: true, can_terminal: false },
];

async function open(page, base, { operator, deny = false }) {
  const authorize = [];
  const b64 = (o) => Buffer.from(JSON.stringify(o)).toString('base64url');
  const jwt = `${b64({ alg: 'none' })}.${b64({ sub: 'viewer', exp: 4102444800 })}.x`;       // a token with a far expiry; the stub API never checks it
  await page.addInitScript((t) => { try { window.sessionStorage.setItem('hcn.tokens', JSON.stringify({ access: t, id: t })); } catch { /* blocked */ } }, jwt);
  let polled = false;
  await boot(page, base, '/collab', (p, url) => {
    if (p.endsWith('/collab/config')) return { client_id: 'test', hosted_ui_domain: 'test.example', public_url: base + '/', ...(operator ? { operator: { user_id: 'owner' } } : {}) };
    if (p.endsWith('/collab/me')) return { user_id: 'owner', email: 'o@example.org', status: 'approved' };
    if (p.endsWith('/collab/rooms')) return { rooms: [{ room_id: 'r1', name: 'R1', role: 'owner' }] };
    if (p.endsWith('/collab/rooms/r1')) return { room_id: 'r1', name: 'R1', role: 'owner', members: [{ user_id: 'owner', email: 'o@example.org' }] };
    if (p.endsWith('/collab/rooms/r1/state')) { if (polled) return { __hang: true }; polled = true; return { changed: true, seq: 1, links: LINKS, missions: [], events: [], agents: [], people: [], tasks: [] }; }
    if (p.endsWith('/graph/lineage')) return GRAPH;
    if (p.endsWith('/workspaces/wl_1/authorize')) { return deny ? { __status: 403, json: { error: 'forbidden' } } : { allowed: true, link_id: 'wl_1', workspace_id: 'ws-1', host: 'bender', action: 'terminal' }; }
    return {};
  });
  await page.route('**/api/v1/collab/rooms/r1/workspaces/wl_1/authorize', async (r) => { authorize.push({ method: r.request().method(), body: r.request().postData() }); await r.fallback(); });
  await page.locator('[data-go="/room/r1"]').first().click();
  await page.locator('[data-sub="lineage"]').click();
  await page.locator('svg.lin-svg g[data-node]').first().waitFor({ timeout: 10000 });
  return authorize;
}
const pick = async (page, id) => { const g = page.locator(`g[data-node="${id}"]`); await g.focus(); await page.keyboard.press('Enter'); await page.locator('.lin-inspector').waitFor(); };

test('operator: the control appears only for a workspace with can_terminal, posts the real authorize route, and shows the answer', async () => withPage(async (page, base) => {
  const calls = await open(page, base, { operator: true });
  await pick(page, 'workspace:ws-1');
  const btn = page.locator('button[data-term-auth="wl_1"]');
  await until(async () => (await btn.count()) === 1, 'the control for ws-1');
  assert.match(await btn.innerText(), /Check terminal access for ws-1/);
  await btn.focus(); await page.keyboard.press('Enter');
  await until(async () => (await page.locator('.lin-termauth [role=status]').count()) === 1, 'the allowed answer');
  assert.match(await page.locator('.lin-termauth').innerText(), /The server allows you the terminal of ws-1 on bender/);
  const real = calls.filter((c) => c && c.method);
  assert.equal(real.length, 1, 'exactly one request');
  assert.equal(real[0].method, 'POST');
  assert.deepEqual(JSON.parse(real[0].body), { action: 'terminal' });
  for (const id of ['workspace:ws-2', 'workspace:ws-9', 'task:plain']) {
    await pick(page, id);
    assert.equal(await page.locator('[data-term-auth]').count(), 0, `${id}: no control`);
  }
}));

test('operator: a denial from the server is shown as a denial', async () => withPage(async (page, base) => {
  await open(page, base, { operator: true, deny: true });
  await pick(page, 'workspace:ws-1');
  await page.locator('button[data-term-auth="wl_1"]').click();
  await until(async () => (await page.locator('.lin-termauth [role=alert]').count()) === 1, 'the denial');
  assert.match(await page.locator('.lin-termauth').innerText(), /did not allow the terminal \(HTTP 403\)/);
}));

test('not the operator: no control for any node, and the authorize route is never called', async () => withPage(async (page, base) => {
  const calls = await open(page, base, { operator: false });
  for (const id of ['workspace:ws-1', 'workspace:ws-2', 'task:plain']) {
    await pick(page, id);
    assert.equal(await page.locator('[data-term-auth]').count(), 0, `${id}: no control`);
  }
  assert.equal(calls.filter((c) => c && c.method).length, 0, 'no request to the authorize route');
}));

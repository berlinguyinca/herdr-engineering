// The dashboard sitemap (docs/dashboard-sitemap.{json,md}) is generated from packages/app.js by scripts/gen-dashboard-sitemap.mjs. This
// test fails when the route table and the sitemap drift apart: a route added to app.js without a sitemap row, a row for a route that
// no longer exists, a nav or tab entry that points nowhere, or committed files that are not what the generator now writes.
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { generate, readRouteTable } from '../scripts/gen-dashboard-sitemap.mjs';
import { ROUTE_NOTES } from '../tests/playwright/dashboard-routes.mjs';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const committed = (f) => fs.readFileSync(path.join(ROOT, 'docs', f), 'utf8');
const table = readRouteTable();
const routes = table.routes.map((r) => r.route);

test('every route of app.js has a sitemap row and a notes entry, and nothing else does', async () => {
  const { map } = await generate();
  assert.deepEqual(map.routes.map((r) => r.route).sort(), [...routes].sort());
  assert.deepEqual(Object.keys(ROUTE_NOTES).sort(), [...routes].sort(), 'tests/playwright/dashboard-routes.mjs must describe exactly the routes in app.js');
  assert.equal(new Set(routes).size, routes.length, 'a route is declared twice in ROUTES');
});

test('every nav entry, nav alias and tab-strip page is a real route (or a prefix of one)', () => {
  const known = (p) => routes.some((r) => r === p || r.startsWith(p + '/'));
  for (const e of table.nav) for (const p of [e.path, ...(e.match || [])]) assert.ok(known(p), `NAV ${e.label}: ${p} is not a route`);
  for (const row of table.tabs) for (const [p] of row) assert.ok(routes.includes(p), `SECTION_TABS: ${p} is not a route`);
});

test('every route has a title, a proof selector, an example hash with no placeholder left, and surfaces that point at it', async () => {
  const { map } = await generate();
  const slugs = new Set();
  for (const r of map.routes) {
    assert.ok(r.title, `${r.route}: no title`);
    assert.ok(r.proof, `${r.route}: no proof selector`);
    assert.ok(!r.hash.includes('<'), `${r.route}: parameter without an example (${r.hash})`);
    assert.ok(r.surfaces.length, `${r.route}: no capture surface`);
    assert.ok(['fixture', 'skipped', 'pending'].includes(r.fixture.status), `${r.route}: bad fixture status`);
    if (r.fixture.status === 'skipped') assert.ok(r.fixture.reason, `${r.route}: skipped without a reason`);
    for (const s of r.surfaces) {
      assert.ok(!slugs.has(s.slug), `duplicate surface slug ${s.slug}`); slugs.add(s.slug);
      const pattern = new RegExp('^' + r.route.replace(/:[^/]+/g, '[^/]+') + '$');
      assert.match(s.hash.split('?')[0], pattern, `${s.slug}: hash ${s.hash} does not instantiate ${r.route}`);
    }
  }
});

test('the committed sitemap files are exactly what the generator writes now', async () => {
  const { json, md } = await generate();
  assert.equal(committed('dashboard-sitemap.json'), json, 'docs/dashboard-sitemap.json is stale: run node scripts/gen-dashboard-sitemap.mjs');
  assert.equal(committed('dashboard-sitemap.md'), md, 'docs/dashboard-sitemap.md is stale: run node scripts/gen-dashboard-sitemap.mjs');
});

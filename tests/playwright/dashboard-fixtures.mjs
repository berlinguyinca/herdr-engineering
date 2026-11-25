// Deterministic /api/v1 fixtures for every dashboard route (see docs/dashboard-sitemap.md). Fixed ids and timestamps, no network, no mocks of
// the app's own code: the pages run unchanged and only the HTTP answers are canned (the repo's page.route approach, visual-dashboard.mjs).
//
// One module per area under tests/playwright/fixtures/; each exports `answer(pathname, method, params, ctx)` and returns the JSON body, or
// undefined when the path is not its own. The dispatcher asks them in turn; nobody answers an unknown path with {} (visual-dashboard.mjs
// turns undefined into a 404 and records a MISS, which the capture reports as degraded).
//   ctx = { now: epoch ms of the fixed browser clock, ago(seconds) -> ISO string, ids }
import { IDS } from './fixtures/ids.mjs';
import * as core from './fixtures/core.mjs';
import * as agents from './fixtures/agents.mjs';
import * as fabric from './fixtures/fabric.mjs';
import * as delivery from './fixtures/delivery.mjs';

const AREAS = [core, agents, fabric, delivery];

export function routeFixture(pathname, method, params, ctx) {
  for (const area of AREAS) {
    const body = area.answer(pathname, method, params, { ...ctx, ids: IDS });
    if (body !== undefined) return body;
  }
  return undefined;
}

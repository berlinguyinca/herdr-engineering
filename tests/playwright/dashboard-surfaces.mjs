// Every dashboard screen the capture matrix takes, built from the route notes (dashboard-routes.mjs, the source of docs/dashboard-sitemap.json).
// A surface is { slug, route, hash, proof, steps?, fixtureStatus }; `proof` must appear or the capture is a failure, never a pass.
import { ROUTE_NOTES } from './dashboard-routes.mjs';
import { DASHBOARD_SURFACES, assistantTab } from './visual-dashboard.mjs';

const KNOWN = new Map(DASHBOARD_SURFACES.map((s) => [s.slug, s]));
const settle = (page) => page.evaluate(() => document.activeElement?.blur());

/** Named UI steps referenced by `step` in the route notes. */
export const STEPS = {
  missions: KNOWN.get('dashboard-missions').steps,
  'memory-open': async (page) => { await page.locator('[data-memory="mem-1"]').click(); await page.locator('[data-mem-drawer]:not([hidden]) .mem-drawer__head').waitFor(); await page.locator('[data-mem-drawer]:not([hidden]) .mem-text').waitFor(); await page.locator('[data-mem-drawer] [aria-busy="true"]').waitFor({ state: 'detached' }); await settle(page); },
  'assistant-tab': (name) => KNOWN.get(`dashboard-assistant-${name.toLowerCase()}`)?.steps || assistantTab(name, null),
};
function stepFor(ref) {
  if (!ref) return async (page) => { await settle(page); };
  const [kind, arg] = ref.split(':');
  const fn = STEPS[kind];
  if (!fn) throw new Error(`unknown step "${ref}"`);
  return typeof fn === 'function' && arg !== undefined ? fn(arg) : fn;
}

export function allSurfaces() {
  const out = [];
  for (const [route, note] of Object.entries(ROUTE_NOTES)) {
    for (const s of note.surfaces) {
      out.push({ slug: s.slug, route, hash: s.hash, proof: KNOWN.get(s.slug)?.proof || note.proof, steps: stepFor(s.step), fullPage: true, app: 'dashboard', fixtureStatus: note.fixture?.status || 'pending', skipReason: note.fixture?.reason || '' });
    }
  }
  return out;
}

// H07 wires a route into the existing SPA. app.js cannot be imported under node --test (it needs HTMLElement and the browser shell), so the
// navigation and the routing are asserted as text on packages/app.js — every path listed before H07 must still be listed — and the new mount is
// asserted functionally through H03's real adapter over an injected fake transport. No DOM shim, no jsdom, no new dependency, no clock.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { mountDiagnostics } from './hosts-view.js';
import { createDiagnosticsSource } from './diagnostics-source.js';

const APP = readFileSync(new URL('./app.js', import.meta.url), 'utf8');
const NAV_AT = APP.indexOf('const NAV = [');
const navPaths = [...APP.slice(NAV_AT, APP.indexOf('];', NAV_AT)).matchAll(/\bpath:\s*'([^']+)'/g)].map((m) => m[1]);
// Every page the sidebar listed before the diagnostics route existed; the delta adds no sidebar row and rewrites nothing.
const BEFORE = ['/overview', '/attention', '/herd', '/collab', '/personal-assistant', '/teams', '/team-map', '/hosts', '/services',
  '/llm', '/ci', '/reviews', '/github', '/architecture', '/artifacts', '/memory', '/intelligence'];
// Pages main gained between H07 being written and this series landing. They are named rather than tolerated:
// the assertion stays "nothing was added, dropped, renamed or moved out of NAV" and only grows deliberately.
const ADDED_ON_MAIN_SINCE_H07 = ['/settings'];
const SECTION_AT = APP.indexOf('const SECTION_TABS = [');
const sectionBlock = APP.slice(SECTION_AT, APP.indexOf('];', SECTION_AT));
const hostsRow = sectionBlock.split('\n').find((line) => line.includes("'/hosts', 'Hosts'")) || '';
const event = (id, at, over = {}) => Object.freeze({ id, missionKey: 'm1', at, eventType: 'error', ...over });
const feed = (value) => { const calls = []; return { calls, fn: async () => { calls.push(1); if (value instanceof Error) throw value; return value; } }; };
// The mount only writes markup, so a plain object is the whole container: no DOM is needed to read what a page shows.
const mount = async (source) => { const el = { innerHTML: '' }; const returned = await mountDiagnostics(el, { source }); return { html: el.innerHTML, returned }; };

// The sidebar is in EVERY dashboard screenshot the ui-gates compare (docs/operations/ui-gates.md): the five
// captured surfaces are full-page captures of #/missions and #/personal-assistant, so one new sidebar row
// re-baselines all of them at desktop light and dark, and the row count is already tight enough that a
// content change on Missions overflows a phone (pipeline 1243). Diagnostics describes hosts, so it is a tab
// of the Hosts section instead: one click from every Hosts page, highlighted in the sidebar with them, and
// the Hosts section has no committed baselines at all, so no captured surface moves.
test('no navigation regression: the sidebar lists exactly what it listed before H07, and Diagnostics is a Hosts tab', () => {
  for (const page of BEFORE) assert.ok(navPaths.includes(page), `NAV no longer lists ${page}`);
  assert.deepEqual([...navPaths].sort(), [...BEFORE, ...ADDED_ON_MAIN_SINCE_H07].sort());   // 17 unchanged + what main added since, nothing else
  assert.ok(!navPaths.includes('/diagnostics'), 'a sidebar row for /diagnostics would move every captured dashboard surface');
  // The sidebar marks Hosts as the current page while the operator reads diagnostics, the same way it does
  // for /scheduling, /ai-sync and /build-cache.
  assert.match(APP, /\{ group: 'Fabric', path: '\/hosts', label: 'Hosts', match: \[[^\]]*'\/diagnostics'[^\]]*\] \},/);
  // ...and the tab strip of that section offers it, which is what makes the route reachable by clicking.
  assert.match(hostsRow, /\['\/diagnostics', 'Diagnostics'\]/);
  // KNOWN_PAGES is built from NAV and SECTION_TABS together, so a tab is a page the shell knows.
  assert.match(APP, /const KNOWN_PAGES = new Set\(\[\.\.\.NAV\.flatMap\(.*\.\.\.SECTION_TABS\.flat\(\)\.map\(\(\[p\]\) => p\)\]\)/);
});

test('the route is wired: app.js imports the mount and routes the /diagnostics path over the feeds that already exist', () => {
  assert.match(APP, /import \{[^}]*\bmountDiagnostics\b[^}]*\} from '\.\/hosts-view\.js';/);
  assert.match(APP, /'\/diagnostics': viewDiagnostics,/);   // the same ROUTES map /hosts uses, and KNOWN_PAGES is built from NAV, so the page is known
  const at = APP.indexOf('async function viewDiagnostics');
  const view = APP.slice(at, APP.indexOf('\n}', at));
  assert.match(view, /await mountDiagnostics\(root\.querySelector\('\[data-diagnostics\]'\), \{ source \}\)/);
  assert.match(view, /fetchEvents: \(\) => client\.request\('\/api\/v1\/events'\)\.catch\(\(\) => null\)/);
  assert.match(view, /fetchHosts: \(\) => client\.listHosts\(\)\.catch\(\(\) => null\)/);
  assert.match(view, /now: Date\.now\(\)/);
  assert.doesNotMatch(view, /fetch\(|https?:\/\/|localhost|:\d{4}/);   // no endpoint, host or port beyond the two calls the client already has
});

test('one read renders the issue list, then the session timeline, then the drift comparison, and an unreadable host feed says so', async () => {
  const events = feed([event('e1', 5, { sessionId: 's1' }), event('e2', 9, { sessionId: 's2', payload: Object.freeze({ step: 3 }) })]);
  const hosts = feed(new Error('host offline'));
  const { html, returned } = await mount(createDiagnosticsSource({ fetchEvents: events.fn, fetchHosts: hosts.fn, now: 1700 }));
  assert.equal(returned, undefined);   // nothing meaningful comes back, because the page hands out no control to drive
  assert.deepEqual([events.calls.length, hosts.calls.length], [1, 1]);   // each feed read exactly once: no poll, no retry, no second read
  assert.match(html, /<ul class="diag-list" role="list"/);   // H04
  assert.match(html, /class="diag-tl-session" data-tl-session="s1"/);   // H05
  assert.match(html, /class="drift-comparison" data-drift-comparison/);   // H06
  const where = (re) => html.search(re);
  assert.ok(where(/data-diagnostics-issues/) < where(/data-diagnostics-timeline/) && where(/data-diagnostics-timeline/) < where(/data-diagnostics-drift/));   // the plan's order
  assert.match(html, /data-diag-unavailable>/); assert.match(html, /data-tl-unavailable>/); assert.match(html, /hosts could not be read/);   // H03's fact reaches both sections
  assert.doesNotMatch(html, /data-diag-empty|data-tl-empty|host offline/);   // an unreadable section is never drawn as "nothing happened", nor the cause echoed
  // The feed reports events and an event carries no severity: what H04 cannot use is shown as rejected instead of given one here.
  assert.match(html, /data-diag-rejected>/); assert.doesNotMatch(html, /data-diag-card/);
});

test('a missing source, or one whose feeds all fail, still renders the unavailable state and never throws', async () => {
  const broken = createDiagnosticsSource({ fetchEvents: async () => { throw new Error('unreachable'); }, fetchHosts: async () => { throw new Error('unreachable'); } });
  for (const source of [undefined, createDiagnosticsSource({}), broken]) {
    const { html } = await mount(source);
    assert.match(html, /data-diag-unavailable>/); assert.match(html, /data-tl-unavailable>/);
    assert.match(html, /events, hosts could not be read/); assert.doesNotMatch(html, /data-diag-empty|data-tl-empty|unreachable/);
  }
});

test('the rendered page is read-only markup: no control, no handler, no timer, no link the feeds did not provide', async () => {
  const { html } = await mount(createDiagnosticsSource({ fetchEvents: async () => [event('e1', 1, { sessionId: 's1' })], fetchHosts: async () => [Object.freeze({ host_id: 'h1' })], now: 1700 }));
  for (const banned of [/<button\b/, /<form\b/, /<input\b/, /<a\b/, /onclick=/, /setInterval/, /setTimeout/, /addEventListener/]) assert.doesNotMatch(html, banned);
  assert.doesNotMatch(html, /data-diag-unavailable|data-tl-unavailable/);   // every feed read, so no banner is claimed
  assert.match(html, /<h1>Diagnostics<\/h1>/); assert.match(html, /data-tl-trace="unavailable"/);   // no trace href was supplied, so none is invented
});

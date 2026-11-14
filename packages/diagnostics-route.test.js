// H07 wires a route into the existing SPA. app.js cannot be imported under node --test (it needs HTMLElement and the browser shell), so the
// navigation and the routing are asserted as text on packages/app.js — every path listed before H07 must still be listed — and the new mount is
// asserted functionally through H03's real adapter — over an injected fake transport, and once over a real
// socket replaying the recorded answers of a live control plane. No DOM shim, no jsdom, no new dependency, no clock.
import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { readFileSync } from 'node:fs';
import { mountDiagnostics } from './hosts-view.js';
import { createDiagnosticsSource, createHostDiagnosticsSource, hostEventsPath } from './diagnostics-source.js';

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
// What a page must never show is its own commentary. A `//` line written inside the markup template is not a
// comment there - it is text the operator reads, which is how "Host issues, not Error issues" nearly shipped.
test('the page shows no developer commentary: the rendered text carries no comment lines and no retired wording', async () => {
  const { html } = await mount(createDiagnosticsSource({
    fetchEvents: async () => [event('e1', 1700, { sessionId: 's1' })],
    fetchHosts: async () => [Object.freeze({ host_id: 'h1', last_seen: '2026-11-13T02:00:00Z', problems: [Object.freeze({ code: 'DISK_FULL', message: 'disk full', severity: 'warning' })] })],
    now: 1700,
  }));
  const shown = html.replace(/<[^>]*>/g, ' ');        // what a reader sees, markup stripped
  assert.ok(!shown.includes('//'), `a comment line is rendered as page text: ${shown.slice(0, 240)}`);
  assert.ok(!shown.includes('Error issues'), 'the retired heading must not appear anywhere on the page');
  assert.equal((shown.match(/Problems the host feed reported/g) || []).length, 1);   // the scope note is said once
});

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
  // The page hands the adapter the client's own two verbs and nothing else. It names no URL: the events query
  // the control plane accepts (entity_type and entity_id are required — the bare /api/v1/events is a 400) lives
  // in hostEventsPath, next to the adapter that maps the answer.
  assert.match(view, /createHostDiagnosticsSource\(\{ request: \(path\) => client\.request\(path\), listHosts: \(\) => client\.listHosts\(\), now: Date\.now\(\) \}\)/);
  assert.doesNotMatch(view, /\/api\/v1\/|fetch\(|https?:\/\/|localhost|:\d{4}/);   // no endpoint, host or port written into the page
  assert.match(hostEventsPath('h1'), /^\/api\/v1\/events\?entity_type=host&entity_id=h1&limit=\d+$/);
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
  // Issues are host problems as H02 incidents, and the host feed could not be read: nothing is listed, and
  // nothing is invented to fill the list. Events alone were never issue records — severity is not an event field.
  assert.match(html, /data-diagnostics-hosts-unavailable>/);
  assert.match(html, /data-diagnostics-issues-scope>Problems the host feed reported, at the severity it gave them\./);
  assert.doesNotMatch(html, /data-diag-card|data-diag-rejected|data-diagnostics-host\b/);
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
  // The hosts the page read are named, with what the feed said about them. This host reported no status, no
  // count and no instant, so each of those reads "unknown" — a count the feed did not report is never drawn as 0.
  assert.match(html, /<section data-diagnostics-hosts><h2>Hosts read<\/h2>/);
  assert.match(html, /data-diagnostics-host data-host-id="h1"/);
  assert.match(html, /<b>unknown<\/b><span>status<\/span>/); assert.match(html, /<b>unknown<\/b><span>problems<\/span>/);
  assert.match(html, /data-diag-empty>p?/);   // the host reported no problems, and that is said plainly instead of a silent list
});

// The recorded answers of a live control plane (tests/fixtures/diagnostics_events_host.json, written by
// tests/control_plane/record_diagnostics_events.py) replayed over a real socket: what the page shows when the
// reads are the ones that actually exist. Ids and timestamps in that file are stable stand-ins; every key,
// severity and event type is what the server answered.
const recorded = JSON.parse(readFileSync(new URL('../tests/fixtures/diagnostics_events_host.json', import.meta.url), 'utf8'));
const serve = () => new Promise((resolve) => {
  const server = createServer((req, res) => {
    const send = (body) => { res.writeHead(200, { 'content-type': 'application/json' }); res.end(JSON.stringify(body)); };
    if (req.url === '/api/v1/hosts') return send(recorded.hosts.body);
    const match = /entity_type=host&entity_id=([^&]*)/.exec(req.url || '');
    if (match && decodeURIComponent(match[1]) === 'hive') return send(recorded.events_host_hive.body);
    return send({ events: [], has_more: false });   // farm registered and never reported: an empty stream is what it has
  });
  server.listen(0, '127.0.0.1', () => resolve(server));
});

test('the page built from recorded control-plane answers names the hosts, their problems and their events', async () => {
  const server = await serve();
  try {
    const origin = `http://127.0.0.1:${server.address().port}`;
    const request = async (path) => {
      const response = await fetch(`${origin}${path}`);
      const body = await response.json();
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      return body;
    };
    const { html } = await mount(createHostDiagnosticsSource({ request }));
    // Both hosts the feed reported are named, with the problem count each one carried.
    assert.match(html, /data-diagnostics-host data-host-id="hive"/);
    assert.match(html, /data-diagnostics-host data-host-id="farm"/);
    assert.match(html, /<b>1<\/b><span>problems<\/span>/);
    // The problems became issue cards with the severity the feed gave them, fingerprinted host:code.
    assert.match(html, /data-diag-card data-severity="critical"/);
    assert.match(html, /<h3>hive:never_reported<\/h3>/); assert.match(html, /<h3>farm:never_reported<\/h3>/);
    assert.match(html, /<b>critical<\/b><span>severity<\/span>/); assert.match(html, /Host has never reported/);
    // The host events are on the timeline under the bucket they belong to: no session was reported, so none is invented.
    assert.match(html, /data-tl-session="unknown session"><h3>no session reported<\/h3>/);
    assert.match(html, /data-event="event_recorded_0"/); assert.match(html, /<b>HostRegistered<\/b>/);
    assert.match(html, /<b>HostHeartbeat<\/b>/);
    // Nothing is claimed as missing, nothing as empty, and nothing rejected: every read succeeded.
    assert.doesNotMatch(html, /data-diag-empty|data-diag-rejected|data-diag-unavailable|data-tl-unavailable/);
    assert.doesNotMatch(html, /<button\b|<form\b|<input\b|<a\b|onclick=|setInterval|setTimeout|addEventListener/);
  } finally { await new Promise((resolve) => server.close(resolve)); }
});

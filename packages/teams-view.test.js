import test from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import { readFileSync } from 'node:fs';
import { HerdrClient } from './herdr-web-client/client.js';
import { loadTeams, renderTeams, teamMapHref } from './teams-view.js';

const team = (id, name, over = {}) => ({ id: `urn:${id}`, kind: 'team', native_id: id, display_name: name, status: 'active', project_id: null, tombstone: false, ...over });

// A real HTTP server speaking the teams route's contract (404 while off, 503 without a database), and the real client.
function serve({ mode = 'ok', teams = [] } = {}) {
  const seen = [];
  const server = http.createServer((req, res) => {
    const url = new URL(req.url, 'http://x');
    seen.push(url.pathname + url.search);
    const send = (code, body) => { res.writeHead(code, { 'content-type': 'application/json' }); res.end(JSON.stringify(body)); };
    if (mode === 'off' || url.pathname !== '/api/v1/graph/work/teams') return send(404, { error: 'not found' });
    if (mode === 'down') return send(503, { error: 'work graph unavailable: no database' });
    if (mode === 'broken') return send(500, { error: 'internal error' });
    const status = url.searchParams.get('status') || 'active';
    return send(200, { items: teams.filter((t) => status === 'all' || t.status === status), has_more: false });
  });
  return new Promise((resolve) => server.listen(0, '127.0.0.1', () => resolve({
    client: new HerdrClient(`http://127.0.0.1:${server.address().port}`), seen, close: () => server.close(),
  })));
}

const TEAMS = [team('team-b', 'Beta crew', { project_id: 'p1' }), team('team-a', '<i>Alpha</i>'), team('team-z', 'Old crew', { status: 'archived' }), team('project:p1', 'p team')];

test('each team links to its own team map, with the id encoded', () => {
  assert.equal(teamMapHref(team('team-a', 'A')), '#/team-map?root=team%3Ateam-a');
  assert.equal(teamMapHref(team('project:p1', 'p')), '#/team-map?root=team%3Aproject%3Ap1');
});

test('the list is one keyboard-reachable link per team, sorted, escaped, archived last and labelled in words', () => {
  const html = renderTeams({ phase: 'ok', items: TEAMS, hasMore: false, showArchived: true });
  assert.equal((html.match(/<a class="teams-link"/g) || []).length, 4);
  assert.doesNotMatch(html, /tabindex/, 'plain links: native tab order, no custom focus handling');
  assert.doesNotMatch(html, /<i>Alpha/);
  assert.match(html, /&lt;i&gt;Alpha&lt;\/i&gt;/);
  assert.ok(html.indexOf('Beta crew') < html.indexOf('Old crew'), 'archived teams come after active ones');
  assert.match(html, /aria-label="Old crew, archived\. Open its team map\."/);
  assert.match(html, /<span class="teams-chip">archived<\/span>/);
  assert.match(html, /project p1/);
  assert.match(html, /no project linked/);
  assert.match(html, /href="#\/team-map\?root=team%3Aproject%3Ap1"/);
  assert.match(html, /<input type="checkbox" data-teams-archived checked>/);
});

test('the empty state says what to do, not just that nothing is there', () => {
  const html = renderTeams({ phase: 'ok', items: [], hasMore: false, showArchived: false });
  assert.match(html, /data-teams-state="empty"/);
  assert.match(html, /herdr-eng teams create/);
  assert.doesNotMatch(html, /<ul/);
});

test('loading, off, unavailable and error are four different states and none is a blank page', () => {
  assert.match(renderTeams({ phase: 'loading' }), /data-skeleton/);
  assert.match(renderTeams({ phase: 'off' }), /data-teams-state="off"[^>]*>Teams are not turned on/);
  assert.match(renderTeams({ phase: 'unavailable', message: 'no <db>' }), /data-teams-state="unavailable".*no &lt;db&gt;/);
  assert.match(renderTeams({ phase: 'error', message: 'boom' }), /data-teams-state="error".*boom/);
});

test('the real client against a real server: active by default, all with the toggle, off is 404, unavailable is 503', async () => {
  const on = await serve({ teams: TEAMS });
  try {
    const active = await loadTeams(on.client);
    assert.equal(active.status, 'ok');
    assert.deepEqual(active.items.map((t) => t.native_id).sort(), ['project:p1', 'team-a', 'team-b']);
    const all = await loadTeams(on.client, { showArchived: true });
    assert.equal(all.items.length, 4);
    assert.deepEqual(on.seen, ['/api/v1/graph/work/teams?status=active&limit=100', '/api/v1/graph/work/teams?status=all&limit=100']);
  } finally { on.close(); }
  for (const [mode, expected] of [['off', 'off'], ['down', 'unavailable'], ['broken', 'error']]) {
    const s = await serve({ mode });
    try { assert.equal((await loadTeams(s.client)).status, expected, mode); } finally { s.close(); }
  }
});

test('hidden rows the server should not send are not drawn: tombstones are dropped, ids are required', async () => {
  const s = await serve({ teams: [team('team-a', 'Kept'), team('team-t', 'Gone', { tombstone: true }), { display_name: 'No id', status: 'active' }] });
  try { assert.deepEqual((await loadTeams(s.client)).items.map((t) => t.display_name), ['Kept']); } finally { s.close(); }
});

test('the stylesheet for the page uses design tokens only', () => {
  const css = readFileSync(new URL('./team-map.css', import.meta.url), 'utf8');
  const teamsCss = css.slice(css.indexOf('Your teams (unified workspace P06)'));
  assert.doesNotMatch(teamsCss, /#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(|font-family\s*:\s*[^v]/);
  assert.match(teamsCss, /var\(--focus-ring\)/);
});

test('the app registers the page and its nav entry behind the capability', () => {
  const app = readFileSync(new URL('./app.js', import.meta.url), 'utf8');
  assert.match(app, /path: '\/teams', label: 'Your teams', capability: 'unified_workspace_v1'/);
  assert.match(app, /'\/teams': viewTeams,/);
  assert.match(app, /import \{ mountTeams \} from '\.\/teams-view\.js'/);
});

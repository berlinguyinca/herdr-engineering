import test from 'node:test';
import assert from 'node:assert/strict';
import { renderWeekCard, renderOverviewSection, weekCardModel, sanitizeOverviewPreferences, userTimeZone, sanitizeWeekPreferences, loadWeekPreferences, saveWeekPreferences } from './overview-view.js';

const now = Date.parse('2026-11-04T12:00:00Z'), iso = ms => new Date(ms).toISOString();
const loaded = {projections: {loaded: true}, missions: {loaded: true}, sessions: {loaded: true}};
const projection = (key = 'pi:MSN-1', title = 'Repair parser', o = {}) => ({mission_key: key, activity_snapshot_at: iso(now - 2000), host_id: 'beast',
  mission: {id: key.split(':').pop(), title, status: 'EXECUTING'}, freshness: {state: 'live', age_seconds: 2}, activity: {health: 'active'}, ...o});
const render = (data, states = loaded) => renderWeekCard({timeZone: 'UTC', ...data}, states, now, now);

test('week is registered as an overview card; saved layouts keep their order and gain the new card', () => {
  const saved = sanitizeOverviewPreferences({order: ['hosts', 'sessions'], hidden: ['ci']});
  assert.deepEqual(saved.order.slice(0, 2), ['hosts', 'sessions']);
  assert.ok(saved.order.includes('week'));
  assert.deepEqual(saved.hidden, ['ci']);
  assert.equal(sanitizeOverviewPreferences().order[0], 'week');
  assert.match(renderOverviewSection('week', {timeZone: 'UTC'}, loaded, now, now), /This week/);
});

test('hostile titles are escaped, never injected', () => {
  const html = render({projections: [projection('pi:MSN-1', '<img src=x onerror=alert(1)>"><script>alert(2)</script>')]});
  assert.doesNotMatch(html, /<img|<script/i);
  assert.match(html, /&lt;img/);
});

test('null counts render as unknown, never as zero', () => {
  const html = render({});
  assert.match(html, /<strong>—<\/strong><span>Started<\/span><small[^>]*>unknown/);
  assert.match(html, /aria-label="Finished: unknown"/);
  assert.match(html, /No work items are reported yet/);
});

test('partial counts say "at least N (partial)" and snapshot metrics are labelled', () => {
  const html = render({projections: [projection()], missions: [{mission_id: 'MSN-1', title: 'x', created_at: '2026-11-03T10:00:00Z', updated_at: '2026-11-03T11:00:00Z'}]});
  assert.match(html, /at least 1 \(partial\)/);
  assert.match(html, /snapshot/);
});

test('partial-data banner appears whenever any coverage is not complete, including a source still loading', () => {
  assert.match(render({projections: [projection()]}), /wk-banner/);
  assert.match(render({projections: [projection()]}, {projections: {loaded: true}}), /still waiting for recorded missions, agents/);
  assert.equal(weekCardModel({timeZone: 'UTC'}, loaded, now, now).partial, true);
});

test('no <progress> or percent without measured evidence; with evidence it is labelled', () => {
  const none = render({projections: [projection()]});
  assert.doesNotMatch(none, /<progress|\d%/);
  const some = render({projections: [projection('pi:MSN-1', 'Repair parser', {activity: {health: 'active', acceptanceCoverage: {completed: 1, total: 4}}})]});
  assert.match(some, /<progress[^>]*max="100" value="25"[^>]*aria-label="Measured progress for Repair parser: 1 \/ 4 acceptance/);
});

test('rows link to the mission detail route and sort attention first', () => {
  const html = render({projections: [projection('pi:A', 'Calm one'), projection('pi:B', 'Stuck one', {activity: {health: 'blocked'}})]});
  assert.match(html, /href="#\/missions\/pi%3AA"/);
  assert.ok(html.indexOf('Stuck one') < html.indexOf('Calm one'));
  assert.match(html, /<span class="ov-tag ov-alert">blocked/);
});

test('loading, error and no-timezone states', () => {
  assert.match(renderWeekCard({}, {}, now, now), /data-skeleton/);
  assert.match(renderWeekCard({}, {projections: {error: 'boom'}}, now, now), /Could not refresh live mission reports · retrying/);
  assert.match(render({projections: [projection()]}, {...loaded, sessions: {loaded: true, error: 'x'}}), /showing last data/);
  assert.equal(userTimeZone({timeZone: 'Not/AZone'}), Intl.DateTimeFormat().resolvedOptions().timeZone); // G4: unknown zone falls back to the browser zone
  assert.ok(userTimeZone({}).length > 0);
});

// ---- G1: server weekly history on the card ----
const startedEv = (id, at, o = {}) => ({kind: 'started', work_id: `mission:${id}`, source: 'mission', entity_id: id, occurred_at: at, evidence: {ref: `e-${id}`, event_type: 'MissionStarted'}, ...o});
const history = (events, coverage = {}) => ({events, coverage: {complete: false, horizon: '2026-10-20T00:00:00Z', truncated: false, reasons: ['x'], ...coverage}});

test('G1: server events drive Started and the badge shows the history horizon', () => {
  const html = render({weeklyHistory: history([startedEv('a', '2026-11-03T10:00:00Z'), startedEv('b', '2026-11-02T10:00:00Z')])});
  assert.match(html, /<strong>2<\/strong><span>Started<\/span><small[^>]*data-coverage="partial">at least 2 \(partial\) · history from 2026-10-20/);
  assert.match(html, /wk-banner/);
  assert.match(html, /History is read from stored start and finish events since 2026-10-20/);
});
test('G1: complete shows only when the server says complete, with no banner for history', () => {
  const html = render({weeklyHistory: history([startedEv('a', '2026-11-03T10:00:00Z')], {complete: true}), projections: [], missions: [], sessions: []});
  assert.match(html, /<span>Started<\/span><small[^>]*data-coverage="complete">complete/);
  assert.match(html, /<span>Finished<\/span><small[^>]*data-coverage="complete">complete/);
  assert.doesNotMatch(html, /Started and Finished only count work with recorded timestamps/);
});
test('G1: truncated or mid-week horizon stays partial', () => {
  const t = render({weeklyHistory: history([startedEv('a', '2026-11-03T10:00:00Z')], {complete: true, truncated: true})});
  assert.match(t, /<span>Started<\/span><small[^>]*data-coverage="partial">/);
  const h = render({weeklyHistory: history([startedEv('a', '2026-11-03T10:00:00Z')], {complete: true, horizon: '2026-11-03T00:00:00Z'})});
  assert.match(h, /<span>Started<\/span><small[^>]*data-coverage="partial">at least 1 \(partial\) · history from 2026-11-03/);
});
test('G1: failed weekly read keeps the old partial card and says the history could not be read', () => {
  const html = render({projections: [projection()], missions: [{mission_id: 'MSN-1', title: 'x', created_at: '2026-11-03T10:00:00Z', updated_at: '2026-11-03T11:00:00Z'}]}, {...loaded, weekly: {error: 'HTTP 500'}});
  assert.match(html, /at least 1 \(partial\)/);
  assert.doesNotMatch(html, /history from/);
  assert.match(html, /weekly history could not be read/i);
});
test('G1: model never hardcodes complete without server coverage', () => {
  assert.equal(weekCardModel({timeZone: 'UTC'}, loaded, now, now).week.started.coverage === 'complete', false);
});

// ---- G4: user-chosen time zone and week start ----
const browserZone = Intl.DateTimeFormat().resolvedOptions().timeZone;
test('G4: sanitizeWeekPreferences accepts only known zones and week starts, default unchanged', () => {
  assert.deepEqual(sanitizeWeekPreferences(undefined), {timeZone: null, weekStart: 'monday'});
  assert.deepEqual(sanitizeWeekPreferences({timeZone: 'America/Los_Angeles', weekStart: 'sunday'}), {timeZone: 'America/Los_Angeles', weekStart: 'sunday'});
  assert.deepEqual(sanitizeWeekPreferences({timeZone: 'Mars/Base', weekStart: 'friday'}), {timeZone: null, weekStart: 'monday'});
  assert.deepEqual(sanitizeWeekPreferences({timeZone: 42, weekStart: ['sunday']}), {timeZone: null, weekStart: 'monday'});
  assert.deepEqual(sanitizeWeekPreferences('x'), {timeZone: null, weekStart: 'monday'});
});
test('G4: an unknown zone falls back to the browser zone, not silently to UTC', () => {
  assert.equal(userTimeZone({timeZone: 'Mars/Base'}), browserZone);
});
test('G4: week start moves the window and is shown on the card', () => {
  const at = {timeZone: 'America/Los_Angeles'}; // Wed 2026-11-04 04:00 PST
  for (const [weekStart, first] of [['monday', '2026-11-02'], ['sunday', '2026-11-01'], ['saturday', '2026-10-31']]) {
    const m = weekCardModel({...at, weekStart}, loaded, now, now);
    assert.equal(m.weekStart, weekStart);
    assert.equal(new Date(m.week.window.start).toLocaleDateString('en-CA', {timeZone: at.timeZone}), first, weekStart);
    assert.match(renderWeekCard({...at, weekStart}, loaded, now, now), new RegExp(`week starts ${weekStart[0].toUpperCase()}${weekStart.slice(1)}`, 'i'));
  }
});
test('G4: the card names the zone in use and offers zone and week-start controls with the current values selected', () => {
  const html = render({timeZone: 'America/Los_Angeles', weekStart: 'sunday'});
  assert.match(html, /<select[^>]*data-week-zone/);
  assert.match(html, /<option value="America\/Los_Angeles" selected>/);
  assert.match(html, /<select[^>]*data-week-start/);
  assert.match(html, /<option value="sunday" selected>/);
  assert.match(html, /America\/Los_Angeles/);
});
test('G4: storage round trip validates, and throwing storage still works', () => {
  const mem = new Map(), ok = {getItem: k => mem.get(k) ?? null, setItem: (k, v) => mem.set(k, v)};
  saveWeekPreferences(ok, {timeZone: 'Asia/Tokyo', weekStart: 'saturday'});
  assert.deepEqual(loadWeekPreferences(ok), {timeZone: 'Asia/Tokyo', weekStart: 'saturday'});
  mem.set('herdr.overview.week.v1', '{not json');
  assert.deepEqual(loadWeekPreferences(ok), {timeZone: null, weekStart: 'monday'});
  mem.set('herdr.overview.week.v1', JSON.stringify({timeZone: 'Nope/Zone', weekStart: 'sunday'}));
  assert.deepEqual(loadWeekPreferences(ok), {timeZone: null, weekStart: 'sunday'});
  const bad = {getItem() { throw new Error('blocked'); }, setItem() { throw new Error('full'); }};
  assert.deepEqual(loadWeekPreferences(bad), {timeZone: null, weekStart: 'monday'});
  assert.doesNotThrow(() => saveWeekPreferences(bad, {timeZone: 'UTC', weekStart: 'monday'}));
  assert.deepEqual(loadWeekPreferences(undefined), {timeZone: null, weekStart: 'monday'});
  assert.match(render({}), /This week/);
});

// ---- G5: per-group expansion, metric filter chips, row details ----
const many = (n, o = () => ({})) => Array.from({length: n}, (_, i) => projection(`pi:M${i}`, `Mission ${i}`, o(i)));
const recorded = (id, created, updated, o = {}) => ({mission_id: id, title: `Done ${id}`, created_at: created, updated_at: updated, ...o});
const group = (m, key) => m.groups.find(g => g.key === key);
test('G5: a group over the cap lists 5 rows, with a Show all N button that lists them all and a Show fewer that restores the cap', () => {
  const data = {projections: many(8)};
  const m = weekCardModel({timeZone: 'UTC', ...data}, loaded, now, now);
  assert.equal(group(m, 'active').total, 8);
  assert.equal(group(m, 'active').shown.length, 5);
  const html = render(data);
  assert.equal((html.match(/class="ov-row wk-row"/g) || []).length, 5);
  assert.match(html, /<button type="button"[^>]*data-week-more="active"[^>]*aria-expanded="false"[^>]*>Show all 8<\/button>/);
  const open = render({...data, weekExpanded: ['active']});
  assert.equal((open.match(/class="ov-row wk-row"/g) || []).length, 8);
  assert.match(open, /data-week-more="active"[^>]*aria-expanded="true"[^>]*>Show fewer<\/button>/);
  assert.equal(group(weekCardModel({timeZone: 'UTC', ...data, weekExpanded: ['active']}, loaded, now, now), 'active').shown.length, 8);
});
test('G5: a group within the cap has no expand button', () => {
  assert.doesNotMatch(render({projections: many(5)}), /data-week-more/);
});
test('G5: expansion is per group', () => {
  const data = {projections: many(7), weekExpanded: ['paused']};
  assert.equal(group(weekCardModel({timeZone: 'UTC', ...data}, loaded, now, now), 'active').shown.length, 5);
});
const blocked = {activity: {health: 'blocked'}};
test('G5: the filter chips narrow rows by metric, are aria-pressed buttons, and a second press is a clear', () => {
  const data = {projections: [projection('pi:A', 'Calm'), projection('pi:B', 'Stuck', blocked)]};
  const html = render({...data, weekFilter: 'attention'});
  assert.match(html, /<button type="button"[^>]*data-week-filter="attention"[^>]*aria-pressed="true"/);
  assert.match(html, /data-week-filter="started"[^>]*aria-pressed="false"/);
  for (const k of ['started', 'finished', 'open', 'paused', 'attention']) assert.match(html, new RegExp(`data-week-filter="${k}"`));
  assert.match(html, /Stuck/);
  assert.doesNotMatch(html, /Calm/);
  assert.match(html, /Filter: Needs attention/);
  assert.match(render(data), /Calm/);
});
test('G5: a filter never changes the headline counts', () => {
  const data = {projections: [projection('pi:A', 'Calm'), projection('pi:B', 'Stuck', blocked)], missions: [recorded('MSN-9', '2026-11-03T10:00:00Z', '2026-11-03T11:00:00Z')]};
  const base = weekCardModel({timeZone: 'UTC', ...data}, loaded, now, now).week;
  for (const f of ['started', 'finished', 'open', 'paused', 'attention']) {
    const m = weekCardModel({timeZone: 'UTC', ...data, weekFilter: f}, loaded, now, now);
    assert.deepEqual(m.week, base, f);
    assert.equal(m.total, 3, f);
    assert.equal(m.partial, true, f);
  }
  const strip = h => h.match(/<div class="ov-metrics wk-metrics">.*?<\/div>\s*<p/s)?.[0];
  assert.equal(strip(render({...data, weekFilter: 'attention'})), strip(render(data)));
});
test('G5: the Unconfirmed group is never hidden by a filter, and the filter says so', () => {
  const stale = projection('pi:S', 'Old report', {freshness: {state: 'stale', age_seconds: 9000}, activity_snapshot_at: iso(now - 9000e3)});
  const data = {projections: [projection('pi:A', 'Calm'), stale]};
  const m = weekCardModel({timeZone: 'UTC', ...data, weekFilter: 'attention'}, loaded, now, now);
  assert.equal(group(m, 'unconfirmed')?.shown.length, 1, 'unconfirmed row is kept');
  assert.equal(group(m, 'active').items.length, 0, 'calm active row filtered out');
  assert.equal(group(m, 'active').total, 1, 'its total is still known');
  const html = render({...data, weekFilter: 'attention'});
  assert.match(html, /Old report/);
  assert.match(html, /Unconfirmed work is always listed/);
});
test('G5: a filter that matches nothing says so and keeps the counts and chips', () => {
  const html = render({projections: [projection('pi:A', 'Calm')], weekFilter: 'paused'});
  assert.match(html, /No listed work matches this filter/);
  assert.match(html, /data-week-filter="paused"[^>]*aria-pressed="true"/);
});
test('G5: filter and expansion combine: the cap applies to the filtered rows', () => {
  const data = {projections: many(9, i => i < 7 ? blocked : {}), weekFilter: 'attention'};
  const g = group(weekCardModel({timeZone: 'UTC', ...data}, loaded, now, now), 'active');
  assert.equal(g.total, 9);
  assert.equal(g.items.length, 7);
  assert.equal(g.shown.length, 5);
  assert.match(render(data), /data-week-more="active"[^>]*>Show all 7</);
});
test('G5: an unknown filter or expansion value is ignored', () => {
  const data = {projections: many(2), weekFilter: '__proto__', weekExpanded: 'nope'};
  const m = weekCardModel({timeZone: 'UTC', ...data}, loaded, now, now);
  assert.equal(m.filter, null);
  assert.equal(group(m, 'active').shown.length, 2);
});
test('G5: there is no repository filter because work summaries carry no repository', () => {
  assert.doesNotMatch(render({projections: many(2)}), /data-week-repo|Repository/i);
});
test('G5: row details show only fields that exist, with links from the work summary', () => {
  const data = {projections: [projection('pi:A', 'Repair parser')], sessions: [{session_id: 's-1', host_id: 'beast', status: 'running', updated_at: iso(now - 1000), mission_key: 'pi:A'}], missions: [recorded('A', '2026-11-03T10:00:00Z', '2026-11-03T11:00:00Z')]};
  const closed = render(data);
  assert.match(closed, /<button type="button"[^>]*data-week-detail="mission:pi:A"[^>]*aria-expanded="false"/);
  assert.doesNotMatch(closed, /wk-details/);
  const html = render({...data, weekDetails: ['mission:pi:A']});
  assert.match(html, /data-week-detail="mission:pi:A"[^>]*aria-expanded="true"/);
  const d = html.match(/<dl class="wk-details".*?<\/dl>/s)?.[0] || '';
  assert.match(d, /Status<\/dt><dd>running/);
  assert.match(d, /Coverage<\/dt><dd>complete/);
  assert.match(d, /href="#\/missions\/pi%3AA"/);
  assert.match(d, /href="#\/herd\/session\/s-1"/);
  assert.match(d, /Evidence<\/dt>/);
  assert.doesNotMatch(d, /Next action|Last event|Repository|Owner/i);
});
test('G5: details of an unconfirmed row say unconfirmed and omit fields the summary lacks', () => {
  const stale = projection('pi:S', 'Old report', {freshness: {state: 'stale', age_seconds: 9000}, activity_snapshot_at: iso(now - 9000e3)});
  const d = render({projections: [stale], weekDetails: ['mission:pi:S']}).match(/<dl class="wk-details".*?<\/dl>/s)?.[0] || '';
  assert.match(d, /Coverage<\/dt><dd>unconfirmed/);
  assert.doesNotMatch(d, /Started<\/dt>|Finished<\/dt>/);
});
test('G5: hostile ids and titles stay escaped in details', () => {
  const html = render({projections: [projection('pi:X', '<b onmouseover=1>t</b>')], weekDetails: ['mission:pi:X']});
  assert.doesNotMatch(html, /<b onmouseover/);
});
test('G5: the no-match message also shows when only Unconfirmed rows remain', () => {
  const stale = projection('pi:S', 'Old report', {freshness: {state: 'stale', age_seconds: 9000}, activity_snapshot_at: iso(now - 9000e3)});
  const html = render({projections: [projection('pi:A', 'Calm'), stale], weekFilter: 'paused'});
  assert.match(html, /No listed work matches this filter/);
  assert.match(html, /Old report/);
});

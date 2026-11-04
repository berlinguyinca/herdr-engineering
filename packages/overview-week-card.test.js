import test from 'node:test';
import assert from 'node:assert/strict';
import { renderWeekCard, renderOverviewSection, weekCardModel, sanitizeOverviewPreferences, userTimeZone } from './overview-view.js';

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
  assert.equal(userTimeZone({timeZone: 'Not/AZone'}), 'UTC');
  assert.ok(userTimeZone({}).length > 0);
});

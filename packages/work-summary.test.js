import test from 'node:test';
import assert from 'node:assert/strict';
import { workSummaries } from './work-summary.js';
import { entityHref } from './entity-links.js';

const now = Date.parse('2026-11-04T12:00:00Z'), iso = ms => new Date(ms).toISOString();
const projection = (key = 'pi:MSN-1', o = {}) => ({mission_key: key, activity_snapshot_at: iso(now - 2000), host_id: 'beast',
  mission: {id: key.split(':').pop(), title: 'Repair parser', status: 'EXECUTING'}, freshness: {state: 'live', age_seconds: 2}, activity: {health: 'active'}, ...o});
const agent = (id, o = {}) => ({session_id: id, host_id: 'beast', status: 'running', updated_at: iso(now - 1000), mission_key: 'pi:MSN-1', ...o});
const run = (input) => workSummaries({received: now, now, ...input});

test('one mission with three agents is one work item', () => {
  const out = run({projections: [projection()], sessions: [agent('a1'), agent('a2'), agent('a3')]});
  assert.equal(out.length, 1);
  assert.deepEqual(out[0].session_ids, ['a1', 'a2', 'a3']);
  assert.equal(out[0].work_id, 'mission:pi:MSN-1');
  assert.equal(out[0].lifecycle, 'running');
});
test('a completed agent does not complete its mission', () => {
  const out = run({projections: [projection()], sessions: [agent('a1', {status: 'finished'})]});
  assert.equal(out[0].lifecycle, 'running');
  assert.equal(out[0].completed_at, undefined);
});
test('a session for an unknown mission stays separate and visibly unassociated', () => {
  const out = run({projections: [projection()], sessions: [agent('x1', {mission_key: 'pi:NOPE'}), agent('x2', {mission_key: undefined})]});
  assert.equal(out.length, 3);
  const [x1, x2] = out.slice(1);
  assert.equal(x1.unassociated, true); assert.equal(x1.coverage, 'partial'); assert.equal(x1.mission_key, undefined);
  assert.equal(x2.unassociated, true); assert.equal(x2.coverage, 'unconfirmed');
});
test('evidence without a denominator yields no percent; valid evidence reuses measuredProgress', () => {
  const none = run({projections: [projection('pi:MSN-1', {activity: {acceptanceCoverage: {completed: 2, total: 0}, progress: {approximatePercent: 80}}})]});
  assert.equal(none[0].progress, undefined);
  const some = run({projections: [projection('pi:MSN-1', {activity: {acceptanceCoverage: {completed: 1, total: 4}}})]});
  assert.equal(some[0].progress.percent, 25);
  assert.match(some[0].progress.label, /1 \/ 4 acceptance/);
  assert.equal(some[0].progress.measured_at, iso(now - 2000));
});
test('mission_key namespaces are preserved and never merged', () => {
  const out = run({projections: [projection('pi:MSN-1'), projection('omx:MSN-1')], recorded: [{mission_id: 'MSN-1', title: 'plain herdr'}],
    sessions: [agent('s1', {mission_key: 'omx:MSN-1'})]});
  assert.deepEqual(out.map(w => w.mission_key).sort(), ['MSN-1', 'omx:MSN-1', 'pi:MSN-1']);
  assert.deepEqual(out.find(w => w.mission_key === 'omx:MSN-1').session_ids, ['s1']);
  assert.deepEqual(out.find(w => w.mission_key === 'pi:MSN-1').session_ids, []);
  assert.deepEqual(out.find(w => w.mission_key === 'MSN-1').session_ids, []);
});
test('mixed host and ambiguous session IDs never join', () => {
  const missions = [{mission_id: 'M1', host_id: 'beast', title: 'Beast M1', sessions: ['dup']}];
  const out = run({recorded: missions, sessions: [
    {session_id: 'dup', host_id: 'hive', mission_id: 'M1', status: 'running', updated_at: iso(now)},
    {session_id: 'dup', mission_id: 'M1', status: 'running', updated_at: iso(now)}]});
  const mission = out.find(w => w.source === 'mission');
  assert.deepEqual(mission.session_ids, []);
  assert.equal(out.filter(w => w.source === 'session').length, 2);
  assert.ok(out.filter(w => w.source === 'session').every(w => w.unassociated && w.coverage === 'partial'));
  assert.notEqual(out[1].work_id, out[2].work_id);
});
test('two sources sharing a bare mission_id do not capture a host-less session', () => {
  const recorded = [{mission_id: 'M1', host_id: 'beast', sessions: ['s']}, {source: 'pi', mission_id: 'M1', host_id: 'hive', sessions: ['s']}];
  const out = run({recorded, sessions: [{session_id: 's', mission_id: 'M1', status: 'running', updated_at: iso(now)}]});
  assert.equal(out.filter(w => w.source === 'mission').every(w => w.session_ids.length === 0), true);
  assert.equal(out.find(w => w.source === 'session').coverage, 'partial');
});
test('stale reports are not live', () => {
  const stale = run({projections: [projection('pi:MSN-1', {activity_snapshot_at: iso(now - 3600e3)})], sessions: [agent('old', {updated_at: iso(now - 3600e3)}), agent('o2', {mission_key: undefined, updated_at: iso(now - 3600e3)})]});
  assert.equal(stale[0].lifecycle, 'unconfirmed');
  assert.equal(stale[1].lifecycle, 'unconfirmed');
});
test('hostile strings pass through as data and links come only from entityHref', () => {
  const evil = '<img src=x onerror=alert(1)>', key = 'pi:"><script>';
  const out = run({projections: [projection(key, {mission: {id: 'x', title: evil, status: 'EXECUTING'}})],
    sessions: [agent("s'1<b>", {mission_key: key, display_name: evil}), agent('free', {mission_key: undefined, display_name: evil})]});
  assert.equal(out.length, 2);
  assert.equal(out[1].title, evil);
  for (const w of out) for (const l of w.links) assert.equal(l.href, entityHref(l.kind, l.entity_id));
  for (const w of out) for (const l of w.links) assert.match(l.href, /^#\/[a-z/]+[A-Za-z0-9%._~/-]*$/);
});
test('identical titles never correlate', () => {
  const out = run({projections: [projection()], sessions: [agent('t', {mission_key: undefined, display_name: 'Repair parser'})]});
  assert.equal(out.length, 2);
  assert.deepEqual(out[0].session_ids, []);
});

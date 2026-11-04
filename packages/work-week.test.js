import test from 'node:test';
import assert from 'node:assert/strict';
import { weeklyActivity, weekWindow } from './work-week.js';
import { workSummaries } from './work-summary.js';

const LA = 'America/Los_Angeles';
const now = Date.parse('2026-11-04T20:00:00Z'); // Wed, in the week starting Mon 2026-11-02 PST
const item = (id, o = {}) => ({work_id: `mission:${id}`, title: id, source: 'mission', source_ref: id, session_ids: [], links: [], evidence: [], lifecycle: 'running', coverage: 'complete', ...o});
const week = (items, o = {}) => weeklyActivity({items, now, timeZone: LA, weekStartsOn: 1, ...o});

test('window is half-open in the zone, Monday start', () => {
  const w = weekWindow({now, timeZone: LA, weekStartsOn: 1});
  assert.equal(new Date(w.start).toISOString(), '2026-11-02T08:00:00.000Z');
  assert.equal(new Date(w.end).toISOString(), '2026-11-09T08:00:00.000Z');
});
test('DST spring-forward week is 167h and fall-back week is 169h', () => {
  const spring = weekWindow({now: Date.parse('2026-03-10T12:00:00Z'), timeZone: LA, weekStartsOn: 0});
  assert.equal(new Date(spring.start).toISOString(), '2026-03-08T08:00:00.000Z'); // Sun 00:00 PST
  assert.equal(new Date(spring.end).toISOString(), '2026-03-15T07:00:00.000Z'); // Sun 00:00 PDT
  assert.equal((spring.end - spring.start) / 3600e3, 167);
  const fall = weekWindow({now: Date.parse('2026-11-03T12:00:00Z'), timeZone: LA, weekStartsOn: 0});
  assert.equal(new Date(fall.start).toISOString(), '2026-11-01T07:00:00.000Z'); // Sun 00:00 PDT
  assert.equal(new Date(fall.end).toISOString(), '2026-11-08T08:00:00.000Z');
  assert.equal((fall.end - fall.start) / 3600e3, 169);
});
test('window containing now on the DST day itself', () => {
  const w = weekWindow({now: Date.parse('2026-03-08T12:00:00Z'), timeZone: LA, weekStartsOn: 0});
  assert.equal(new Date(w.start).toISOString(), '2026-03-08T08:00:00.000Z');
});
test('boundary instants: start inclusive, end exclusive', () => {
  const w = weekWindow({now, timeZone: LA, weekStartsOn: 1});
  const at = ms => new Date(ms).toISOString();
  const out = week([
    item('first', {started_at: at(w.start)}), item('before', {started_at: at(w.start - 1)}),
    item('last', {started_at: at(w.end - 1)}), item('after', {started_at: at(w.end)})]);
  assert.equal(out.started.count, 2);
});
test('DST fall-back: an event in the repeated hour lands once, by instant', () => {
  const n = Date.parse('2026-11-03T12:00:00Z');
  const out = weeklyActivity({items: [item('a', {started_at: '2026-11-01T08:30:00Z'}), item('b', {started_at: '2026-11-01T06:59:59Z'})], now: n, timeZone: LA, weekStartsOn: 0});
  assert.equal(out.started.count, 1);
});
test('started and finished are counted independently by timestamp', () => {
  const out = week([
    item('a', {started_at: '2026-10-20T00:00:00Z', completed_at: '2026-11-03T00:00:00Z', lifecycle: 'finished'}),
    item('b', {started_at: '2026-11-03T00:00:00Z'}),
    item('c', {started_at: '2026-11-03T01:00:00Z', completed_at: '2026-11-03T02:00:00Z', lifecycle: 'finished'})]);
  assert.equal(out.started.count, 2);
  assert.equal(out.finished.count, 2);
});
test('finished without a completion timestamp is unknown, never 0', () => {
  const out = week([item('a', {lifecycle: 'finished', updated_at: '2026-11-03T00:00:00Z'})]);
  assert.equal(out.finished.coverage, 'unknown');
  assert.equal(out.finished.count, null);
});
test('some finished items lacking timestamps make the total partial', () => {
  const out = week([item('a', {lifecycle: 'finished', completed_at: '2026-11-03T00:00:00Z'}), item('b', {lifecycle: 'finished'})]);
  assert.equal(out.finished.coverage, 'partial');
  assert.equal(out.finished.count, 1);
});
test('a present snapshot is never a complete history; historyComplete must be asserted', () => {
  const items = [item('a', {started_at: '2026-11-03T00:00:00Z'})];
  assert.equal(week(items).started.coverage, 'partial');
  assert.equal(week(items, {historyComplete: true}).started.coverage, 'complete');
  assert.equal(week([], {historyComplete: false}).started.coverage, 'unknown');
});
test('no started timestamps at all is unknown', () => {
  const out = week([item('a')]);
  assert.equal(out.started.coverage, 'unknown');
  assert.equal(out.started.count, null);
});
test('stale or unconfirmed sources make metrics partial even with history asserted', () => {
  const out = week([item('a', {started_at: '2026-11-03T00:00:00Z', coverage: 'unconfirmed', lifecycle: 'unconfirmed'})], {historyComplete: true});
  assert.equal(out.started.coverage, 'partial');
  assert.equal(out.open.coverage, 'partial');
});
test('open is a labelled snapshot and excludes finished/failed/unconfirmed', () => {
  const out = week([item('a'), item('b', {lifecycle: 'blocked'}), item('c', {lifecycle: 'finished'}), item('d', {lifecycle: 'failed'}), item('e', {lifecycle: 'unconfirmed'})]);
  assert.equal(out.open.count, 2);
  assert.equal(out.open.basis, 'snapshot');
  assert.equal(out.open.as_of, new Date(now).toISOString());
});
test('paused_quota needs evidenced reason; unevidenced is not counted as paused', () => {
  const evid = [{kind: 'quota_status', ref: 'x', timestamp: '2026-11-03T00:00:00Z'}];
  const out = week([item('a', {lifecycle: 'paused_quota', evidence: evid}), item('b', {lifecycle: 'paused_quota'}), item('c', {lifecycle: 'failed', evidence: evid})]);
  assert.equal(out.paused_quota.count, 1);
  assert.equal(out.paused_quota.coverage, 'partial');
  assert.equal(out.paused_quota.basis, 'snapshot');
});
test('attention_needed counts blocked/review and failed-in-window, not quota pauses', () => {
  const out = week([item('a', {lifecycle: 'blocked'}), item('b', {lifecycle: 'review'}), item('c', {lifecycle: 'paused_quota'}),
    item('d', {lifecycle: 'failed', updated_at: '2026-11-03T00:00:00Z'}), item('e', {lifecycle: 'failed', updated_at: '2026-09-01T00:00:00Z'})]);
  assert.equal(out.attention_needed.count, 3);
});
test('duplicate work items are counted once', () => {
  const a = item('a', {started_at: '2026-11-03T00:00:00Z'});
  const out = week([a, {...a}, {...a, evidence: [{kind: 'x', ref: 'y'}]}]);
  assert.equal(out.started.count, 1);
  assert.equal(out.open.count, 1);
});
test('conflicting duplicate timestamps degrade coverage to partial', () => {
  const out = week([item('a', {started_at: '2026-11-03T00:00:00Z'}), item('a', {started_at: '2026-11-03T05:00:00Z'})], {historyComplete: true});
  assert.equal(out.started.count, 1);
  assert.equal(out.started.coverage, 'partial');
});
test('reassociated session is not counted beside its mission', () => {
  const m = item('M1', {session_ids: ['s1'], started_at: '2026-11-03T00:00:00Z'});
  const s = {...item('s1', {work_id: 'session:s1', source: 'session', source_ref: 's1', session_ids: ['s1'], started_at: '2026-11-03T00:00:00Z'}), unassociated: true};
  const out = week([s, m]);
  assert.equal(out.started.count, 1);
  assert.equal(out.open.count, 1);
  assert.equal(out.total_items, 1);
});
test('same bare session id on another host is not swallowed by a mission', () => {
  const m = item('M1', {session_ids: ['s1'], pane_refs: [{host_id: 'beast', herdr_session: 'h', terminal_id: 't', session_id: 's1'}]});
  const s = item('s1', {work_id: 'session:hive/s1', source: 'session', session_ids: ['s1'], pane_refs: [{host_id: 'hive', herdr_session: 'h', terminal_id: 't2', session_id: 's1'}]});
  assert.equal(week([m, s]).total_items, 2);
});
test('a mission with three agents counts once (end to end through the adapter)', () => {
  const t = '2026-11-03T00:00:00Z';
  const items = workSummaries({received: now, now,
    projections: [{mission_key: 'pi:M1', activity_snapshot_at: new Date(now - 1000).toISOString(), host_id: 'beast', mission: {id: 'M1', title: 'T', status: 'EXECUTING'}, freshness: {state: 'live', age_seconds: 2}, activity: {health: 'active'}}],
    recorded: [{mission_id: 'M1', source: 'pi', host_id: 'beast', created_at: t}],
    sessions: ['a', 'b', 'c'].map(id => ({session_id: id, host_id: 'beast', mission_key: 'pi:M1', status: 'running', updated_at: new Date(now - 1000).toISOString(), created_at: t}))});
  const out = week(items);
  assert.equal(out.total_items, items.length);
  assert.equal(out.open.count, items.length);
  assert.equal(out.started.count, items.length);
  assert.equal(items.filter(i => i.session_ids.length === 3).length, 1);
});
test('quota-paused mission through the adapter carries evidence', () => {
  const items = workSummaries({received: now, now, projections: [{mission_key: 'pi:Q', activity_snapshot_at: new Date(now - 1000).toISOString(), host_id: 'beast', mission: {id: 'Q', title: 'Q', status: 'PAUSED_QUOTA'}, freshness: {state: 'live', age_seconds: 2}, activity: {health: 'active'}}]});
  assert.equal(items[0].lifecycle, 'paused_quota');
  assert.equal(week(items).paused_quota.count, 1);
});
test('invalid timestamps and zone-less strings are treated as missing', () => {
  const out = week([item('a', {started_at: 'not a date'}), item('b', {started_at: '2026-11-03T00:00:00'})]);
  assert.equal(out.started.coverage, 'unknown');
});
test('rejects an invalid time zone', () => {
  assert.throws(() => weekWindow({now, timeZone: 'Mars/Base', weekStartsOn: 1}), RangeError);
});

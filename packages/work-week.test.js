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

// ---- G1: server lifecycle events (GET /api/v1/work/weekly-events) ----
const ev = (kind, work_id, occurred_at, o = {}) => ({kind, work_id, source: work_id.startsWith('session:') ? 'session' : 'mission', entity_id: work_id.split(':').slice(1).join(':'), occurred_at, evidence: {ref: `ev-${kind}-${work_id}`, event_type: 'X'}, ...o});
const cov = (o = {}) => ({complete: false, horizon: '2026-10-01T00:00:00Z', truncated: false, reasons: ['r'], ...o});
const hist = (events, coverage = cov()) => ({events, coverage});

test('G1: server started/finished events supply real timestamps, including work no longer in the snapshot', () => {
  const out = week([item('live')], {serverHistory: hist([
    ev('started', 'mission:live', '2026-11-03T10:00:00Z'),
    ev('started', 'mission:gone', '2026-11-03T11:00:00Z'), ev('finished', 'mission:gone', '2026-11-04T09:00:00Z'),
    ev('started', 'mission:old', '2026-10-20T11:00:00Z'), ev('finished', 'mission:old', '2026-11-03T12:00:00Z')])});
  assert.equal(out.started.count, 2);          // live + gone; old started last month
  assert.equal(out.finished.count, 2);         // gone + old
  assert.equal(out.started.coverage, 'partial');
  assert.equal(out.started.horizon, '2026-10-01T00:00:00.000Z');
});
test('G1: complete only when the server says complete, untruncated, and the horizon precedes the window', () => {
  const events = [ev('started', 'mission:a', '2026-11-03T10:00:00Z')];
  const ok = week([item('a')], {serverHistory: hist(events, cov({complete: true}))});
  assert.equal(ok.started.coverage, 'complete');
  assert.equal(ok.finished.coverage, 'complete');
  for (const bad of [cov({complete: true, truncated: true}), cov({complete: true, horizon: '2026-11-05T00:00:00Z'}), cov({complete: true, horizon: null}), cov({complete: 'true'}), cov({complete: false})]) {
    assert.equal(week([item('a')], {serverHistory: hist(events, bad)}).started.coverage, 'partial', JSON.stringify(bad));
  }
});
test('G1: historyComplete:true from the caller alone never makes server-less data complete', () => {
  assert.equal(week([item('a', {started_at: '2026-11-03T10:00:00Z'})], {serverHistory: hist([], cov({complete: false})), historyComplete: true}).started.coverage, 'partial');
});
test('G1: duplicate events and snapshot + event for the same work count once', () => {
  const out = week([item('a', {started_at: '2026-11-03T10:00:00Z'})], {serverHistory: hist([
    ev('started', 'mission:a', '2026-11-03T10:00:00Z'), ev('started', 'mission:a', '2026-11-03T10:00:00Z'),
    ev('started', 'mission:a', '2026-11-03T10:00:00Z', {evidence: {ref: 'other'}})])});
  assert.equal(out.started.count, 1);
});
test('G1: conflicting server timestamps for one item degrade to partial and keep the earliest', () => {
  const out = week([], {serverHistory: hist([ev('started', 'mission:a', '2026-11-03T10:00:00Z'), ev('started', 'mission:a', '2026-11-03T12:00:00Z')], cov({complete: true}))});
  assert.equal(out.started.count, 1);
  assert.equal(out.started.coverage, 'partial');
});
test('G1: sessions owned by a mission are not counted separately (reassociation), standalone ones are', () => {
  const out = week([], {serverHistory: hist([
    ev('started', 'mission:pi:m1', '2026-11-03T10:00:00Z', {mission_key: 'pi:m1'}),
    ev('started', 'session:h1/s-owned', '2026-11-03T10:00:01Z', {mission_key: 'pi:m1', host_id: 'h1'}),
    ev('started', 'session:h1/s-free', '2026-11-03T10:00:02Z', {host_id: 'h1'})])});
  assert.equal(out.started.count, 2);
});
test('G1: a snapshot standalone session that the server knows belongs to a mission is dropped', () => {
  const snapshot = {work_id: 'session:h1/s1', title: 's1', source: 'session', source_ref: 's1', session_ids: ['s1'], pane_refs: [], links: [], evidence: [], lifecycle: 'running', coverage: 'partial', started_at: '2026-11-03T10:00:00Z'};
  const out = week([snapshot], {serverHistory: hist([ev('started', 'session:h1/s1', '2026-11-03T10:00:00Z', {mission_key: 'pi:m1'})])});
  assert.equal(out.started.count, 0);
  assert.equal(out.total_items, 0);
});
test('G1: DST fall-back week window decides membership by instant (server events)', () => {
  const n = Date.parse('2026-11-03T12:00:00Z');
  const out = weeklyActivity({items: [], now: n, timeZone: LA, weekStartsOn: 0, serverHistory: hist([
    ev('started', 'mission:a', '2026-11-01T06:59:59Z'),   // 23:59:59 PDT Sat: previous week
    ev('started', 'mission:b', '2026-11-01T07:00:00Z'),   // 00:00 PDT Sun: in
    ev('started', 'mission:c', '2026-11-01T08:30:00Z'),   // repeated 01:30 PST hour: in, once
    ev('started', 'mission:d', '2026-11-08T08:00:00Z')])}); // next week start: out
  assert.equal(out.started.count, 2);
});
test('G1: horizon in the middle of the week stays partial and exposes the horizon', () => {
  const out = week([], {serverHistory: hist([ev('started', 'mission:a', '2026-11-04T10:00:00Z')], cov({complete: true, horizon: '2026-11-04T00:00:00Z'}))});
  assert.equal(out.started.coverage, 'partial');
  assert.equal(out.started.horizon, '2026-11-04T00:00:00.000Z');
});
test('G1: no server history (read failed) keeps the old partial behaviour and shows no horizon', () => {
  const out = week([item('a', {started_at: '2026-11-03T10:00:00Z'})], {serverHistory: null});
  assert.equal(out.started.coverage, 'partial');
  assert.equal(out.started.horizon, undefined);
});
test('G1: a successful read with no events is a bounded zero, not unknown', () => {
  const out = week([], {serverHistory: hist([])});
  assert.equal(out.started.count, 0);
  assert.equal(out.started.coverage, 'partial');
  assert.equal(out.finished.count, 0);
});
test('G1: malformed events are ignored (no title/shape guessing)', () => {
  const out = week([], {serverHistory: hist([null, 7, {kind: 'started'}, ev('started', 'mission:a', 'not-a-date'), ev('started', 'mission:b', '2026-11-03T10:00:00'), ev('bogus', 'mission:c', '2026-11-03T10:00:00Z'), ev('started', 'mission:ok', '2026-11-03T10:00:00Z')])});
  assert.equal(out.started.count, 1);
});
test('G1: server quota-pause event supplies evidence for a snapshot paused item', () => {
  const paused = item('q', {lifecycle: 'paused_quota'});
  const out = week([paused], {serverHistory: hist([ev('paused_quota', 'mission:q', '2026-11-03T10:00:00Z')])});
  assert.equal(out.paused_quota.count, 1);
  assert.equal(out.paused_quota.unevidenced, 0);
});

test('G4: Monday, Sunday and Saturday starts are half-open local-midnight windows in a non-UTC zone', () => {
  const at = Date.parse('2026-11-04T12:00:00Z'); // Wed 04:00 PST
  const local = ms => new Date(ms).toLocaleString('en-CA', {timeZone: LA, hour12: false});
  for (const [ws, day] of [[1, '2026-11-02'], [0, '2026-11-01'], [6, '2026-10-31']]) {
    const w = weekWindow({now: at, timeZone: LA, weekStartsOn: ws});
    assert.match(local(w.start), new RegExp(`^${day}, 00:00:00`), `start ${ws}`);
    assert.equal(weekWindow({now: w.start, timeZone: LA, weekStartsOn: ws}).start, w.start, 'start inclusive');
    assert.equal(weekWindow({now: w.end - 1, timeZone: LA, weekStartsOn: ws}).start, w.start, 'last ms inside');
    assert.equal(weekWindow({now: w.end, timeZone: LA, weekStartsOn: ws}).start, w.end, 'end exclusive');
  }
});
test('G4: Saturday start across the fall-back change (Nov 1 2026, America/Los_Angeles) is 169h and DST-safe', () => {
  const w = weekWindow({now: Date.parse('2026-11-02T20:00:00Z'), timeZone: LA, weekStartsOn: 6});
  assert.equal(new Date(w.start).toISOString(), '2026-10-31T07:00:00.000Z');
  assert.equal(new Date(w.end).toISOString(), '2026-11-07T08:00:00.000Z');
});

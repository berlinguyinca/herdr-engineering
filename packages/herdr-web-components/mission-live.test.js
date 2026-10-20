import test from 'node:test';
import assert from 'node:assert/strict';
import { missionCard, missionDetail, freshnessAt } from '../mission-live-view.js';
const now = Date.parse('2026-10-20T12:00:00Z');
const projection = {
  mission_key: 'pi:MSN-1', host_id: 'beast', instance_id: 'pi-1', session_id: 'native-123',
  mission: { id: 'MSN-1', title: 'Fix authentication', status: 'EXECUTING' },
  freshness: { state: 'live', age_seconds: 3, stale_after_seconds: 30, disconnect_after_seconds: 120 },
  activity: { health: 'active', currentActivity: { summary: 'Running tests' },
    acceptanceCoverage: { completed: 1, total: 4 }, progress: { approximatePercent: 80, basis: 'inferred' },
    workerDetails: [{ workerId: 'reviewer', health: 'stalled', state: 'running', route: 'flash', model: 'metabolomics/qwen3.8', lastOutputPreview: '<script>alert(1)</script>' }],
  },
};
test('mission cards show genuine activity and worst worker health, with namespaced links', () => {
  const html = missionCard(projection, now);
  assert.match(html, /Running tests/); assert.match(html, /stalled/);
  assert.match(html, /pi%3AMSN-1/); assert.match(html, /flash → metabolomics\/qwen3.8/);
  assert.match(html, /1 \/ 4 acceptance criteria verified/); assert.doesNotMatch(html, /80%/);
});
test('unknown progress never becomes a guessed percentage', () => {
  const html = missionCard({ ...projection, activity: { progress: { approximatePercent: 80, basis: 'inferred' } } }, now);
  assert.match(html, /Progress unknown/); assert.doesNotMatch(html, /<progress/);
});
test('freshness ages on the client and disconnection never implies stalled or crashed', () => {
  assert.equal(freshnessAt(projection, now, now + 30000).state, 'stale');
  assert.equal(freshnessAt(projection, now, now + 120000).state, 'disconnected');
  assert.equal(freshnessAt({ ...projection, connection: 'disconnected' }, now, now).state, 'disconnected');
});
test('detail escapes and bounds output and does not guess Herdr session links', () => {
  const html = missionDetail(projection, now);
  assert.match(html, /&lt;script&gt;/); assert.doesNotMatch(html, /<script>/);
  assert.match(html, /native-123/); assert.doesNotMatch(html, /#\/herd\/session/);
  const long = structuredClone(projection); long.activity.workerDetails[0].lastOutputPreview = 'x'.repeat(20000);
  assert.ok(missionDetail(long, now).length < 17000);
});

test('missing optional worker and log entries do not hide an otherwise valid mission', () => {
  const p = structuredClone(projection);
  p.activity.workerDetails.push(null);
  p.activity.activity = [null]; p.activity.errors = [null]; p.activity.recovery = [null];
  assert.match(missionDetail(p, now), /Fix authentication/);
});

test('workflow counts are labeled separately from verified acceptance', () => {
  const p = structuredClone(projection);
  p.activity.workflowProgress = {completed: 3, total: 5, basis: 'weighted_dag', approximatePercent: 60};
  const html = missionCard(p, now);
  assert.match(html, /Workflow tasks/); assert.match(html, /3 \/ 5 completed/);
  assert.match(html, /1 \/ 4 acceptance criteria verified/);
});

test('updating frames never relabel cached work as current activity', () => {
  const p = {...projection, connection: 'updating', mission_revision: 2, activity_revision: 1,
    freshness: {...projection.freshness, state: 'updating', age_seconds: 0}};
  assert.equal(freshnessAt(p, now, now + 1000).state, 'updating');
  const html = missionDetail(p, now);
  assert.match(html, /Updating telemetry · current details unavailable/);
  assert.match(html, /Last recorded measurements/);
  assert.match(html, /Last recorded workers/);
  assert.doesNotMatch(html, /Live telemetry/);
});

test('mission lifecycle takes precedence over obsolete worker health', () => {
  for (const [status, expected] of [['COMPLETE', 'complete'], ['FAILED', 'failed'], ['CANCELED', 'canceled'], ['PAUSED_INFRASTRUCTURE', 'paused: infrastructure'], ['PAUSED', 'paused']]) {
    const p = structuredClone(projection);
    p.mission.status = status;
    p.activity.health = 'stalled';
    p.activity.workerDetails = [{workerId: 'old-attempt', state: 'failed', health: 'failed', endedAt: '2026-10-20T11:00:00Z'}];
    assert.ok(missionCard(p, now).includes(`Live telemetry · ${expected}</div>`), status);
  }
});

test('active mission health excludes finished attempts while preserving current stalls', () => {
  const p = structuredClone(projection);
  p.activity.workerDetails = [
    {workerId: 'old-failed', state: 'failed', health: 'failed'},
    {workerId: 'old-complete', state: 'completed', health: 'stalled'},
    {workerId: 'ended-running', state: 'running', health: 'failed', endedAt: '2026-10-20T11:00:00Z'},
    {workerId: 'retry', state: 'running', health: 'active'},
  ];
  assert.match(missionCard(p, now), /Live telemetry · active<\/div>/);
  p.activity.workerDetails.push({workerId: 'current-stall', state: 'waiting', health: 'stalled'});
  assert.match(missionCard(p, now), /Live telemetry · stalled<\/div>/);
  p.activity.health = 'failed';
  assert.match(missionCard(p, now), /Live telemetry · failed<\/div>/);
});

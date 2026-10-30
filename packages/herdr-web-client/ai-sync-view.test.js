import test from 'node:test';
import assert from 'node:assert/strict';
import { ageState, aiSyncRows, renderAiSync } from '../ai-sync-view.js';
import { createServer } from 'node:http';
import { HerdrClient } from './client.js';

const NOW = Date.parse('2026-10-30T12:00:00Z');
const ago = (ms) => new Date(NOW - ms).toISOString();
const H = 3600e3;
const host = (over = {}) => ({ host_id: 'beast', ai_sync_version: '0.2.0', envs_enabled: ['claude', 'pi'], last_sync_ts: ago(10 * 60e3),
  ahead: 0, behind: 0, drifted_files_count: 0, drift_sample: [], skipped_links_count: 0, last_error: null, checked_at: ago(60e3), ...over });

test('age is fresh under an hour, warn over an hour, stale over a day, unknown without a timestamp', () => {
  assert.equal(ageState(ago(59 * 60e3), NOW), 'fresh');
  assert.equal(ageState(ago(61 * 60e3), NOW), 'warn');
  assert.equal(ageState(ago(25 * H), NOW), 'stale');
  assert.equal(ageState(null, NOW), 'unknown');
  assert.equal(ageState('not a date', NOW), 'unknown');
});

test('rows are sorted with the neediest hosts first and carry readable age text', () => {
  const rows = aiSyncRows({ hosts: [host({ host_id: 'a' }), host({ host_id: 'b', last_sync_ts: ago(30 * H) }), host({ host_id: 'c', last_sync_ts: ago(2 * H) })] }, NOW);
  assert.deepEqual(rows.map((r) => [r.host, r.state]), [['b', 'stale'], ['c', 'warn'], ['a', 'fresh']]);
  assert.equal(rows[2].age, '10 min ago');
  assert.equal(rows[0].age, '1 d ago');
});

test('table shows host, version, envs, age, ahead/behind, drift count and error', () => {
  const html = renderAiSync({ hosts: [host({ ahead: 2, behind: 1, drifted_files_count: 3, drift_sample: ['claude/CLAUDE.md'], last_error: 'git fetch failed' })] }, NOW);
  for (const text of ['beast', '0.2.0', 'claude, pi', '10 min ago', 'ahead 2', 'behind 1', '>3<', 'git fetch failed', 'claude/CLAUDE.md']) assert.ok(html.includes(text), text);
  assert.ok(html.includes('data-state="fresh"'));
});

test('hostile text is escaped and never becomes markup', () => {
  const html = renderAiSync({ hosts: [host({ host_id: '<img src=x onerror=alert(1)>', last_error: '<script>x</script>', drift_sample: ['"><b>'] })] }, NOW);
  assert.ok(!html.includes('<img') && !html.includes('<script') && !html.includes('<b>'));
});

test('hosts without ai-sync or data show dashes, and an empty fleet is explained', () => {
  const html = renderAiSync({ hosts: [host({ ai_sync_version: null, last_sync_ts: null, ahead: null, behind: null, envs_enabled: [] })] }, NOW);
  assert.ok(html.includes('data-state="unknown"') && html.includes('—'));
  assert.ok(renderAiSync({ hosts: [] }, NOW).includes('No hosts have reported'));
  assert.ok(renderAiSync(null, NOW).includes('No hosts have reported'));
});

test('client lists ai-sync status from a real HTTP server', async () => {
  const seen = [];
  const server = createServer((req, res) => { seen.push(req.url); res.setHeader('Content-Type', 'application/json'); res.end(JSON.stringify({ hosts: [host()] })); });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  try {
    const body = await new HerdrClient(`http://127.0.0.1:${server.address().port}`).listAiSync();
    assert.equal(body.hosts[0].host_id, 'beast');
    assert.deepEqual(seen, ['/api/v1/ai-sync']);
  } finally { server.close(); }
});

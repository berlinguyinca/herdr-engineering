import test from 'node:test';
import assert from 'node:assert/strict';

import {
  CLIENT_BUFFER, mergeEntry, pickLayout, renderDiff, renderEvent, renderFiles,
  renderPrefsForm, renderSummaryBar, renderView, telemetryHealth,
} from './inspector.js';

const ev = (over) => ({ seq: 1, rev: 1, kind: 'message', agent_id: 'main', ts: '2026-01-01T00:00:00Z', data: {}, ...over });

test('pickLayout honours explicit prefs and auto-switches on width', () => {
  assert.equal(pickLayout('split', 100), 'split');
  assert.equal(pickLayout('stacked', 4000), 'stacked');
  assert.equal(pickLayout('auto', 1200), 'split');
  assert.equal(pickLayout('auto', 600), 'stacked');
  assert.equal(pickLayout(undefined, 600), 'stacked');
});

test('mergeEntry replaces by seq only for newer rev, sorts newest-first and bounds the buffer', () => {
  const list = [];
  mergeEntry(list, ev({ seq: 1, rev: 1, data: { text: 'a' } }));
  mergeEntry(list, ev({ seq: 2, rev: 2 }));
  mergeEntry(list, ev({ seq: 1, rev: 5, data: { text: 'newer' } }));
  mergeEntry(list, ev({ seq: 1, rev: 3, data: { text: 'stale' } }));
  assert.deepEqual(list.map((e) => e.seq), [2, 1]);
  assert.equal(list[1].data.text, 'newer');
  for (let i = 10; i < 10 + CLIENT_BUFFER + 50; i++) mergeEntry(list, ev({ seq: i, rev: i }), CLIENT_BUFFER);
  assert.equal(list.length, CLIENT_BUFFER);
  assert.equal(list[0].seq, 10 + CLIENT_BUFFER + 49);
});

test('everything dynamic is escaped (XSS)', () => {
  const evil = '<img src=x onerror=alert(1)>';
  const html = [
    renderEvent(ev({ kind: 'message', data: { role: 'user', text: evil } })),
    renderEvent(ev({ kind: 'tool', data: { tool: evil, args_preview: evil, output: evil, status: 'ok' } })),
    renderEvent(ev({ kind: 'file', data: { op: 'edit', path: evil, diff: `+${evil}` } })),
    renderEvent(ev({ kind: 'test', data: { run_id: 'r', command: evil, failures: [{ name: evil, message: evil, file: evil }] } })),
    renderEvent(ev({ agent_id: evil })),
    renderFiles({ summary: { files: [{ path: evil, ops: ['edit'], additions: 1, deletions: 0, agents: [evil], last_seq: 1 }] } }),
  ].join('');
  assert.ok(!html.includes('<img'), html);
});

test('withheld bodies render a lock, never the content', () => {
  const html = renderEvent(ev({ kind: 'tool', data: { tool: 'bash', status: 'ok', output: null, withheld: true, withheld_reason: 'sensitive output requires a higher role' } }));
  assert.match(html, /🔒/);
  assert.ok(!html.includes('<pre class="hd-ins__out">'));
});

test('tool output honours line preference and show_tool_output=false', () => {
  const out = Array.from({ length: 50 }, (_, i) => `line${i}`).join('\n');
  const e = ev({ kind: 'tool', data: { tool: 'bash', status: 'ok', output: out } });
  const html = renderEvent(e, { tool_output_lines: 3 });
  assert.ok(html.includes('line49') && !html.includes('line10<') && html.includes('earlier lines'));
  assert.ok(!renderEvent(e, { show_tool_output: false }).includes('line49'));
});

test('partial/interrupted tool telemetry is labelled, not hidden', () => {
  const html = renderEvent(ev({ kind: 'tool', data: { tool: 'bash', status: 'interrupted', incomplete: true } }));
  assert.match(html, /interrupted/);
  assert.match(html, /partial telemetry/);
});

test('renderDiff classes lines and bounds rows', () => {
  const html = renderDiff('--- a\n+++ b\n@@ -1 +1 @@\n-x\n+y\n z\n' + 'k\n'.repeat(500), { maxLines: 10 });
  for (const c of ['meta', 'hunk', 'del', 'add', 'ctx']) assert.ok(html.includes(`hd-diff__${c}`), c);
  assert.match(html, /more lines/);
  assert.equal(renderDiff(null), '');
});

test('reasoning view: capability none / unknown / hidden-by-pref / shown', () => {
  const base = { events: [ev({ kind: 'reasoning', data: { text: 'thinking hard' } })], summary: { reasoning_withheld: false } };
  assert.match(renderView('reasoning', { ...base, capabilities: { declared: true, reasoning: 'none' } }, {}), /does not expose reasoning/);
  assert.match(renderView('reasoning', { ...base, capabilities: { declared: false, reasoning: 'unknown' } }, {}), /not reported yet/);
  const hidden = renderView('reasoning', { ...base, capabilities: { declared: true, reasoning: 'summary' } }, { show_reasoning: false });
  assert.match(hidden, /hidden by your preferences/);
  assert.ok(!hidden.includes('thinking hard'));
  const shown = renderView('reasoning', { ...base, capabilities: { declared: true, reasoning: 'summary' } }, { show_reasoning: true });
  assert.ok(shown.includes('thinking hard'));
  const roleBlocked = renderView('reasoning', { ...base, summary: { reasoning_withheld: true }, capabilities: { declared: true, reasoning: 'full' } }, { show_reasoning: true });
  assert.match(roleBlocked, /operator role/);
});

test('timeline hides reasoning unless opted in', () => {
  const data = { events: [ev({ seq: 2, kind: 'reasoning', data: { text: 'inner' } }), ev({ seq: 1, data: { role: 'user', text: 'outer' } })] };
  assert.ok(!renderView('all', data, { show_reasoning: false }).includes('inner'));
  assert.ok(renderView('all', data, { show_reasoning: true }).includes('inner'));
});

test('paging: shows at most `limit` rows plus a Show more control', () => {
  const events = Array.from({ length: 250 }, (_, i) => ev({ seq: i, data: { role: 'user', text: `m${i}` } }));
  const html = renderView('conversation', { events }, {}, 100);
  assert.equal((html.match(/hd-ins__row /g) || []).length, 100);
  assert.match(html, /Show more \(150\)/);
});

test('telemetry health: none / live / stale / ended', () => {
  assert.equal(telemetryHealth(null, 'none').state, 'none');
  assert.equal(telemetryHealth({ last_event_age_s: 1, ended: false }).state, 'live');
  assert.equal(telemetryHealth({ last_event_age_s: 999, ended: false }).state, 'stale');
  assert.equal(telemetryHealth({ last_event_age_s: 999, ended: true }).state, 'ended');
});

test('summary bar surfaces running tools, failures, context and dropped events', () => {
  const html = renderSummaryBar({
    dropped: 3,
    summary: { ended: false, last_event_age_s: 1, client_dropped: 2, running_tools: [{}, {}], tests: [{ status: 'failed' }],
      usage: { context_pct: 91, input_tokens: 1000, output_tokens: 10 } },
  });
  for (const needle of ['2 tools running', '1 failing test run', 'context 91%', '5 events dropped']) assert.ok(html.includes(needle), needle);
});

test('prefs form reflects current values', () => {
  const html = renderPrefsForm({ layout: 'split', density: 'compact', default_view: 'tools', show_reasoning: true, show_tool_output: false, follow_tail: true, tool_output_lines: 7, max_events_per_session: 100, retention_hours: 3 }, 'this session');
  assert.match(html, /<option selected>split<\/option>/);
  assert.match(html, /data-pref="show_reasoning" checked/);
  assert.match(html, /value="7"/);
});

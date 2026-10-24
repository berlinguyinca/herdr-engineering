import test from 'node:test';
import assert from 'node:assert/strict';
import { compact, disclosure, ms, pair, percent, rate, renderLlm, renderLlmSession, single, when, pickTotal } from '../llm-view.js';

const dist = (n, p50, p95) => ({ n, p50, p95, insufficient: n < 5 });
const group = (over = {}) => ({
  key: 'all', source: 'llm-proxy', requests: 120, errors: 6, error_rate: 0.05,
  tokens: { input: 1000, output: 2_500_000, reasoning: 0, cache_read: 0, lower_bound: false },
  ttft_ms: dist(40, 600, 2900), queue_ms: dist(40, 0, 10), speed_tps: dist(30, 41, 60), e2e_tps: dist(0, null, null),
  first_seen: '2026-10-24T10:00:00Z', last_seen: '2026-10-24T11:50:00Z', ...over,
});
const pts = (vals) => vals.map((v, i) => ({ t: `2026-10-24T${String(i).padStart(2, '0')}:00:00Z`, v, n: 1 }));
const data = (over = {}) => ({
  total: { window: '24h', since: '2026-10-23T12:00:00Z', retained_since: '2026-10-20T00:00:00Z', partial: false, groups: [group()] },
  models: { groups: [group({ key: 'qwen' })] },
  agents: { groups: [group({ key: 'herdr:beast:eng:w1:p1', last_seen: '2026-10-24T11:50:00Z' }), group({ key: 'unmatched:zz-9' })] },
  problems: { problems: [{ class: 'upstream_admission', model: 'qwen', count: 4, last_seen: '2026-10-24T11:00:00Z', sessions: ['herdr:beast:eng:w1:p1'], session_count: 1, statuses: { 429: 4 } }] },
  series: { requests: { step: '1h', points: pts([3, 5, 0, 7]) }, errors: { points: pts([0, 1, 0, 2]) },
    ttft: { points: pts([null, 600, null, 700]) }, speed: { points: pts([null, null, null, null]) } },
  ...over,
});
const state = (over = {}) => ({ window: '24h', filters: { project: '', model: '' }, projects: ['p'], modelNames: ['qwen'], names: {}, ...over });

test('number formats keep unknown apart from zero', () => {
  assert.equal(ms(null), '—'); assert.equal(ms(0), '0 ms'); assert.equal(ms(1500), '1.5 s');
  assert.equal(rate(undefined), '—'); assert.equal(rate(41.26), '41.3 t/s'); assert.equal(rate(250.4), '250 t/s');
  assert.equal(percent(0), '0%'); assert.equal(percent(null), '—');
  assert.equal(compact(2_500_000), '2.5 M'); assert.equal(compact(999), '999');
  assert.equal(when('2026-10-24T11:50:00Z'), '2026-10-24 11:50 UTC'); assert.equal(when(null), '—');
});

test('a percentile with fewer than five samples says so instead of showing a number', () => {
  assert.equal(pair(dist(40, 600, 2900), ms), '600 ms / 2.9 s');
  assert.equal(pair(dist(3, null, null), ms), 'insufficient data (n=3)');
  assert.equal(pair(dist(0, null, null), ms), 'not reported');
  assert.equal(single(dist(30, 41, 60), rate, 'p95'), '60 t/s');
});

test('tiles show the proxy figures; other sources are named and never added', () => {
  const d = data();
  d.total.groups.push(group({ source: 'transcript', requests: 9999 }));
  const html = renderLlm(state(), d);
  assert.match(html, /<div class="llm-tile__value">120<\/div>/);
  assert.doesNotMatch(html, /9,999<\/div>/);
  assert.match(html, /Other sources \(transcript\)/);
  assert.equal(pickTotal(d.total).source, 'llm-proxy');
});

test('an empty window renders placeholders, not zeros', () => {
  const html = renderLlm(state(), { total: { groups: [], retained_since: null }, models: { groups: [] }, agents: { groups: [] },
    problems: { problems: [] }, series: { requests: { points: [] }, errors: { points: [] }, ttft: { points: [] }, speed: { points: [] } } });
  assert.match(html, /No usage has been recorded yet\./);
  assert.match(html, /No requests recorded in this window\./);
  assert.match(html, /No problems in this window\./);
  assert.doesNotMatch(html, /undefined|null|NaN/);
});

test('partial windows and lower-bound token totals are disclosed', () => {
  assert.match(disclosure({ since: '2026-10-17T12:00:00Z', retained_since: '2026-10-20T00:00:00Z', partial: true }), /^Partial: records start 2026-10-20 00:00 UTC/);
  const d = data(); d.total.groups[0].tokens.lower_bound = true;
  assert.match(renderLlm(state(), d), /≥ 2\.5 M/);
});

test('latency buckets without enough samples are gaps in the line, and an all-empty chart says so', () => {
  const html = renderLlm(state(), data());
  assert.match(html, /aria-labelledby="llm-lat-t llm-lat-d"/);
  assert.match(html, /<circle /);                        // two isolated points, never joined across the gap
  assert.match(html, /Not reported in this window, or fewer than 5 samples per bucket\./);   // the speed chart
});

test('charts carry a title, a description and a table of the same numbers', () => {
  const html = renderLlm(state(), data());
  assert.equal((html.match(/<svg /g) || []).length, 2);
  assert.match(html, /<title id="llm-req-t">Requests over time<\/title>/);
  assert.match(html, /<details class="llm-data"><summary>Chart data<\/summary>/);
  assert.match(html, /<td>2026-10-24 01:00 UTC<\/td><td>5<\/td><td>1<\/td>/);
});

test('agent rows link to the agent, unmatched ids are plain text, names come from the dashboard', () => {
  const html = renderLlm(state({ names: { 'herdr:beast:eng:w1:p1': 'Amber Swift' } }), data());
  assert.match(html, /<a href="#\/llm\/session\/herdr%3Abeast%3Aeng%3Aw1%3Ap1">Amber Swift<\/a>/);
  assert.match(html, /zz-9 <em>\(unmatched\)<\/em>/);
  assert.doesNotMatch(html, /href="#\/herd\/session\/unmatched/);
});

test('every dynamic string is escaped, including names that collide with object properties', () => {
  const d = data();
  d.models.groups[0].key = '<img src=x onerror=alert(1)>';
  d.problems.problems[0].class = '"><script>x</script>';
  d.problems.problems[0].sessions = ['constructor', '__proto__'];
  const html = renderLlm(state({ names: { evil: '<b>' }, filters: { project: '"><x', model: '' }, projects: ['"><x'] }), d);
  assert.doesNotMatch(html, /<img src=x|<script>x|<b>|"><x/);
  assert.match(html, /<a href="#\/llm\/session\/constructor">constructor<\/a>/);
});

test('window links keep the filters and mark the current window', () => {
  const html = renderLlm(state({ window: '7d', filters: { project: 'p', model: 'qwen' } }), data());
  assert.match(html, /class="llm-window" href="#\/llm\?project=p&amp;model=qwen&amp;window=1h"/);
  assert.match(html, /href="#\/llm\?project=p&amp;model=qwen&amp;window=7d" aria-current="page"/);
});

test('the agent page links back to the agent, names the source, and scopes the window links to itself', () => {
  const html = renderLlmSession({ window: '7d', sessionId: 'herdr:beast:eng:w1:p1', name: 'Amber Swift', known: true }, data());
  assert.match(html, /<h1>Amber Swift <span class="llm-badge">llm-proxy<\/span><\/h1>/);
  assert.match(html, /<a class="llm-back" href="#\/herd\/session\/herdr%3Abeast%3Aeng%3Aw1%3Ap1">Open agent ↗<\/a>/);
  assert.match(html, /href="#\/llm\/session\/herdr%3Abeast%3Aeng%3Aw1%3Ap1\?window=1h"/);
  assert.match(html, /href="#\/llm\/session\/herdr%3Abeast%3Aeng%3Aw1%3Ap1\?window=7d" aria-current="page"/);
  assert.doesNotMatch(html, /data-llm-filter|<h2>Agents<\/h2>/);
});

test('an agent whose path reports no latency says so, and an unknown session has no agent link', () => {
  const d = data();
  d.total.groups = [group({ source: 'transcript', ttft_ms: dist(0, null, null), speed_tps: dist(0, null, null) })];
  const html = renderLlmSession({ window: '24h', sessionId: 'zz-9', name: '', known: false }, d);
  assert.match(html, /data-llm-latency-note>Latency and speed are not reported by this agent's path\./);
  assert.match(html, /Not a known dashboard session\./);
  assert.doesNotMatch(html, /Open agent/);
  assert.match(html, /<h1>zz-9 /);
});

test('an agent page with no records says so instead of showing zeros', () => {
  const html = renderLlmSession({ window: '24h', sessionId: 's', name: 'A', known: true },
    { total: { groups: [], retained_since: null }, models: { groups: [] }, problems: { problems: [] },
      series: { requests: { points: [] }, errors: { points: [] }, ttft: { points: [] }, speed: { points: [] } } });
  assert.match(html, /No usage has been recorded yet\./);
  assert.doesNotMatch(html, /undefined|null|NaN/);
});

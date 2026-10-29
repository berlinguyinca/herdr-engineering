import test from 'node:test';
import assert from 'node:assert/strict';
import { renderAttention, attentionBadge, SNOOZE_CHOICES } from '../attention-view.js';
import { HerdrClient } from '../herdr-web-client/client.js';
import { renderOverviewSection, serverAttention } from '../overview-view.js';

const now = Date.parse('2026-10-24T12:00:00Z');
const item = (over = {}) => ({
  key: 'session:a', fingerprint: 'f1', kind: 'agent_failed', severity: 'critical', title: 'Amber Swift', detail: 'Agent reported failed',
  href: '#/herd/session/a', actions: [{ id: 'open', label: 'Open agent', href: '#/herd/session/a' }], source: 'sessions',
  first_seen: '2026-10-24T11:50:00Z', last_seen: '2026-10-24T11:59:00Z', count: 1, state: 'open', snoozed_until: null, ...over,
});
const body = (items = [item()], over = {}) => ({
  items, counts: { open: items.filter(i => i.state === 'open').length, acknowledged: 0, snoozed: 0, resolved: 0, critical: 1 },
  partial: false, unavailable: [], truncated: false, generated_at: '2026-10-24T12:00:00Z', ...over,
});

test('renders an open item with its evidence link, owner link and controls', () => {
  const html = renderAttention({ body: body() }, now);
  assert.match(html, /<span class="at-sr">critical<\/span><strong>Amber Swift<\/strong>/);
  assert.doesNotMatch(html, /data-attention-inspector/, 'details are folded until asked for');
  const open = renderAttention({ body: body(), expanded: new Set(['session:a']) }, now);
  assert.match(open, /aria-expanded="true"/);
  assert.match(open, /data-attention-inspector="session:a"/);
  assert.match(open, /class="at-action" href="#\/herd\/session\/a">Open</);
  assert.equal((open.match(/href="#\/herd\/session\/a"/g) || []).length, 1, 'an action to the same place is not repeated');
  assert.match(html, /data-ack data-key="session:a" data-fingerprint="f1"/);
  for (const [seconds] of SNOOZE_CHOICES) assert.match(html, new RegExp(`<option value="${seconds}">`));
  assert.match(html, /<strong>1<\/strong> open/);
  assert.match(html, /first seen <span data-attention-stamp="2026-10-24T11:50:00Z">10m 0s<\/span> ago/);
});

test('loading, error and empty states are explicit and an empty list is only claimed when complete', () => {
  assert.match(renderAttention({ loading: true }, now), /data-skeleton aria-busy="true" role="status" aria-label="Loading attention"/);
  assert.match(renderAttention({ error: 'HTTP 500' }, now), /herdr-error message="HTTP 500"/);
  assert.match(renderAttention({ body: body([], { counts: { open: 0 } }) }, now), /No attention items\. Every source was read\./);
  const partial = renderAttention({ body: body([], { partial: true, unavailable: ['ci'], counts: { open: 0 } }) }, now);
  assert.doesNotMatch(partial, /No attention items/);
  assert.match(partial, /Partial: ci unavailable/);
  assert.match(partial, /some sources could not be read/);
  assert.match(renderAttention({ body: body(), error: 'HTTP 500' }, now), /Could not refresh; showing the last received list/);
});

test('acknowledged, snoozed and resolved items are grouped and acknowledged ones offer reopen', () => {
  const html = renderAttention({ body: body([item(), item({ key: 'k2', state: 'acknowledged' }), item({ key: 'k3', state: 'snoozed', snoozed_until: '2026-10-24T14:00:00Z' })],
    { resolved: [{ key: 'old', kind: 'host_stale', title: 'beast', resolved_at: '2026-10-24T11:00:00Z' }] }) }, now);
  assert.match(html, /<summary>Acknowledged \(1\)<\/summary>/);
  assert.match(html, /<summary>Snoozed \(1\)<\/summary>/);
  assert.match(html, /<summary>Recently resolved \(1\)<\/summary>/);
  assert.match(html, /data-clear data-key="k2"[^>]*>Reopen/);
  assert.match(html, /data-clear data-key="k3"[^>]*>Wake now/);
  assert.equal((html.match(/data-ack /g) || []).length, 1);
});

test('filters narrow the list by severity and kind', () => {
  const items = [item(), item({ key: 'w', severity: 'warning', kind: 'host_stale', title: 'host one' })];
  const html = renderAttention({ body: body(items), filters: { severity: 'warning' } }, now);
  assert.match(html, /host one/);
  assert.doesNotMatch(html, /Amber Swift/);
  assert.doesNotMatch(renderAttention({ body: body(items), filters: { kind: 'agent_failed' } }, now), /host one/);
});

test('remote text is escaped and only internal hash links become hrefs', () => {
  const evil = item({ title: '<img src=x onerror=alert(1)>', detail: '"><script>x</script>', href: 'javascript:alert(1)', key: 'k"><b>',
    actions: [{ label: 'Go', href: 'https://evil.example/' }, { label: 'Ok', href: '#/hosts/h' }] });
  const html = renderAttention({ body: body([evil]), expanded: new Set([evil.key]) }, now);
  assert.doesNotMatch(html, /<img|<script|<b>|javascript:|evil\.example/);
  assert.match(html, /&lt;img src=x onerror=alert\(1\)&gt;/);
  assert.match(html, /href="#\/hosts\/h">Ok/);
});

test('badge counts open items and flags critical ones; unusable answers give null', () => {
  assert.deepEqual(attentionBadge(body()), { count: 1, critical: 1 });
  assert.deepEqual(attentionBadge({ counts: { open: 0 } }), { count: 0, critical: 0 });
  assert.equal(attentionBadge(null), null);
  assert.equal(attentionBadge({ items: [] }), null);
});

test('the client sends ack, snooze and clear as JSON posts and refuses other actions', async () => {
  const calls = [];
  const client = new HerdrClient('', { fetchImpl: async (url, opts) => {
    calls.push({ url, method: opts.method, body: opts.body && JSON.parse(opts.body) });
    return { ok: true, status: 200, text: async () => '{}' };
  } });
  await client.attention({ state: 'all', kind: '' });
  await client.attentionAct('snooze', { key: 'k', fingerprint: 'f', seconds: 3600 });
  assert.deepEqual(calls.map(c => [c.method, c.url]), [['GET', '/api/v1/attention?state=all'], ['POST', '/api/v1/attention/snooze']]);
  assert.deepEqual(calls[1].body, { key: 'k', fingerprint: 'f', seconds: 3600 });
  assert.throws(() => client.attentionAct('delete', {}), /unknown attention action/);
});

// ------------------------------------------------------------ overview
const overview = (data, states) => renderOverviewSection('attention', data, states, now, now);

test('the overview shows the server list when it loaded, with a link to the page and partial coverage', () => {
  const server = body([item(), item({ key: 'k2', state: 'acknowledged' })], { partial: true, unavailable: ['ci'], counts: { open: 1, acknowledged: 1, snoozed: 0, critical: 1 } });
  const html = overview({ attention: server }, { attention: { loaded: true, error: false } });
  assert.match(html, /Amber Swift/);
  assert.match(html, /1\+ open items/);
  assert.match(html, /href="#\/attention">Open the Attention page/);
  assert.match(html, /1 acknowledged, 0 snoozed/);
  assert.match(html, /Partial coverage: CI builds/);
  assert.doesNotMatch(html, /computed in this browser/);
  assert.equal(serverAttention(server, now).items.length, 1);   // acknowledged items are not "open"
});

test('the overview falls back to the browser projection and says so when the server list fails', () => {
  const data = { sessions: [{ session_id: 's1', status: 'failed', title: 'Browser-only agent', updated_at: '2026-10-24T11:59:00Z' }] };
  const failed = overview(data, { attention: { loaded: true, error: true }, sessions: { loaded: true } });
  assert.match(failed, /Browser-only agent/);
  assert.match(failed, /computed in this browser and may differ from the Attention page/);
  const loading = overview(data, { attention: { loaded: false, error: false } });
  assert.match(loading, /role="status" aria-label="Loading attention"/);
  assert.doesNotMatch(loading, /Browser-only agent/);
  const disabled = overview(data, { attention: { loaded: true, error: false, disabled: true }, sessions: { loaded: true } });
  assert.match(disabled, /Browser-only agent/);
  assert.doesNotMatch(disabled, /computed in this browser/);
  assert.match(overview(data, { sessions: { loaded: true } }), /Browser-only agent/);   // legacy callers without the new source
});

test('a long open list folds into one section per severity and kind, worst first, with the rest behind Show all', () => {
  const many = [
    ...Array.from({ length: 12 }, (_, n) => item({ key: `ci:${n}`, kind: 'ci_failed', severity: 'critical', title: `CI ${n}` })),
    ...Array.from({ length: 2 }, (_, n) => item({ key: `h:${n}`, kind: 'host_silent', severity: 'warning', title: `Host ${n}` })),
  ];
  const html = renderAttention({ body: body(many) }, now);
  assert.equal((html.match(/class="at-kind at-kind-/g) || []).length, 2);
  assert.ok(html.indexOf('at-kind-critical') < html.indexOf('at-kind-warning'));
  assert.match(html, /12 × CI failed/);
  assert.equal((html.match(/data-attention-row="/g) || []).length, 14);      // nothing is dropped, only folded
  assert.ok(!html.includes('Show all 2<'));                                    // a section of three or fewer has nothing to fold
});

test('a short open list stays flat', () => {
  const html = renderAttention({ body: body(Array.from({ length: 8 }, (_, n) => item({ key: `k:${n}`, kind: `kind${n % 4}`, title: `T${n}` }))) }, now);
  assert.ok(!html.includes('<h2 class="at-kind-title"'));
});

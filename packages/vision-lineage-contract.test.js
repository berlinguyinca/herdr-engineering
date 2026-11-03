// The Handoffs page over the API rows produced by Pi's REAL vision lineage envelopes
// (tests/control_plane/fixtures/vision_lineage_envelopes.json replayed through the control plane's HTTP ingest and read back
// from GET /api/v1/handoffs; see docs/evidence/vision-delegation-dashboard.md).
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { causeFilterHtml, detailHtml, filterByCause, handoffCard, listHtml, roundTripHtml, visionRoundTrips, visionStats, visionStatsLine } from './handoffs-view.js';

const load = (name) => JSON.parse(readFileSync(new URL(`../tests/control_plane/fixtures/${name}`, import.meta.url), 'utf8'));
const { handoffs: rows, details } = load('vision_lineage_api_rows.json');
const { delegations } = load('vision_lineage_envelopes.json');
const now = Math.max(...rows.map((r) => Date.parse(r.updated_at))) + 60_000;
const rendered = (rs, ds) => [...rs.map(handoffCard), listHtml(rs), ...visionRoundTrips(rs).map(roundTripHtml), ...ds.map((d) => detailHtml(d, null)),
  visionStatsLine(visionStats(rs, now)), causeFilterHtml(rs, null), causeFilterHtml(rs, 'vision_return')];

test('Pi vision rows from the API pair into 4 closed round trips and 1 open delegation with the right outcomes', () => {
  const trips = visionRoundTrips(rows);
  assert.equal(trips.length, 5);
  const byId = Object.fromEntries(trips.map((t) => [t.requestId, t]));
  for (const d of delegations) {
    const t = byId[d.request_id];
    assert.ok(t, d.name);
    assert.equal(t.outcome, d.outcome, d.name);
    assert.equal(t.out?.handoff_id, `handoff:vision-${d.request_id}-out`);
    if (d.outcome === 'open') {
      assert.equal(t.open, true); assert.equal(t.back, undefined); assert.equal(t.durationMs, null);
    } else {
      assert.equal(t.open, false); assert.equal(t.back?.handoff_id, `handoff:vision-${d.request_id}-back`);
      assert.ok(t.durationMs > 0, `${d.name} has a duration`);
    }
  }
  assert.equal(trips.filter((t) => !t.open).length, 4);
});

test('stats, cause filter and list agree with the API rows', () => {
  assert.deepEqual(visionStats(rows, now), { total: 5, byOutcome: { open: 1, timeout: 1, failed: 1, partial: 1, ok: 1 }, last24h: 5, last7d: 5 });
  assert.match(visionStatsLine(visionStats(rows, now)), /5 vision delegations · 5 last 24h · 5 last 7d · 1 ok · 1 partial · 1 failed · 1 timed out · 1 open/);
  assert.equal(filterByCause(rows, 'vision_delegate').length, 5);
  assert.equal(filterByCause(rows, 'vision_return').length, 4);
  assert.equal(filterByCause(rows, null).length, 9);
  const list = listHtml(rows);
  assert.equal(list.match(/data-roundtrip/g).length, 5);              // the 9 rows collapse into one entry per delegation
  assert.equal(list.match(/>result</g).length, 4);
  for (const label of ['analysis complete', 'partial analysis', 'analysis failed', 'timed out', '>open<']) assert.ok(list.includes(label), label);
  assert.ok(list.includes('qwen3.6-35b-a3b → gemini-3-flash · '));
});

test('no rendered out row says analysis complete; each says it was sent to the vision model', () => {
  const outs = rows.filter((r) => r.vision.phase === 'out');
  assert.equal(outs.length, 5);
  for (const r of [...outs, ...details.filter((d) => d.vision.phase === 'out')]) {
    for (const html of [handoffCard(r), detailHtml(r, null)]) {
      assert.ok(!html.includes('analysis complete'), r.handoff_id);
      assert.match(html, /sent to vision model/);
    }
  }
});

test('nothing rendered from the API rows says undefined, null or NaN', () => {
  for (const html of rendered(rows, details)) assert.doesNotMatch(html, /undefined|\bnull\b|NaN/);
});

test('request ids, handoff ids and model names from the wire are escaped', () => {
  const bad = '"><img src=x onerror=alert(1)>';
  const taint = (r) => ({ ...r, handoff_id: `${r.handoff_id}${bad}`, from_model: `local/${bad}`, to_model: `google/${bad}`,
    vision: { ...r.vision, request_id: `${r.vision.request_id}${bad}` } });
  for (const html of rendered(rows.map(taint), details.map(taint))) {
    assert.doesNotMatch(html, /<img/);
    assert.ok(html.includes('&lt;img') || !html.includes('img'), 'escaped, not dropped');
  }
  assert.ok(listHtml(rows.map(taint)).includes('&lt;img src=x onerror=alert(1)&gt;'));
});

// H7: a filter picks which round trips show, never how they read: each shown trip is paired from the card's full loaded rows.
const sid = (s) => s?.session_id || 'unknown';
const routeOf = (r) => `${sid(r.source)} → ${sid(r.target)}`;
const assertWholeTrips = (list, ids, label) => {
  assert.equal((list.match(/data-roundtrip/g) || []).length, ids.length, `${label}: one entry per delegation`);
  for (const d of delegations.filter((x) => ids.includes(x.request_id))) {
    const entry = list.split('data-roundtrip').find((s) => s.includes(d.request_id));
    assert.ok(entry, `${label}: ${d.name} shown`);
    assert.match(entry, new RegExp(`data-handoff="handoff:vision-${d.request_id}-out"[^>]*>out<`), `${label}: ${d.name} keeps its out link`);
    if (d.outcome === 'open') {
      assert.match(entry, />open</, `${label}: ${d.name} is open`);
      assert.doesNotMatch(entry, />result</);
    } else {
      assert.doesNotMatch(entry, />open</, `${label}: ${d.name} is not open`);
      assert.match(entry, new RegExp(`data-handoff="handoff:vision-${d.request_id}-back"[^>]*>result<`), `${label}: ${d.name} keeps its result link`);
      assert.match(entry, / · \d+(\.\d)?s/, `${label}: ${d.name} keeps its duration`);
    }
  }
};

test('a cause filter shows each whole round trip once, paired from all loaded rows (H7)', () => {
  const all = delegations.map((d) => d.request_id), closed = delegations.filter((d) => d.outcome !== 'open').map((d) => d.request_id);
  assertWholeTrips(listHtml(filterByCause(rows, 'vision_delegate'), rows), all, 'vision_delegate');
  assertWholeTrips(listHtml(filterByCause(rows, 'vision_return'), rows), closed, 'vision_return');
  assertWholeTrips(listHtml(rows, rows), all, 'no filter');
  assert.equal(listHtml(rows, rows), listHtml(rows), 'unfiltered output is unchanged');
});

test('a route filter keeps each shown round trip whole (H7)', () => {
  const routes = [...new Set(rows.map(routeOf))];
  assert.equal(routes.length, 9);                                         // out text->vision, back vision->text: every pair splits
  for (const route of routes) {
    const shown = rows.filter((r) => routeOf(r) === route);
    assertWholeTrips(listHtml(shown, rows), [...new Set(shown.map((r) => r.vision.request_id))], route);
  }
});

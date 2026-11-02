import test from 'node:test';
import assert from 'node:assert/strict';
import { handoffGraphSummary, overviewSummary, detailHtml, handoffCard, workspaceCard, flowFromPage, routeTable, routesHtml, modelText, outcomeText,
  mountHandoffs, visionRoundTrips, filterByCause, visionStats, visionStatsLine, causeFilterHtml, roundTripHtml, listHtml } from './handoffs-view.js';

test('summarizes handoff lineage without trusting goal text as markup', () => {
  const summary = handoffGraphSummary({
    nodes: [{ id: 'a', harness: 'pi' }, { id: 'b', harness: 'claude' }],
    edges: [{ handoff_id: 'h1', from: 'a', to: 'b', state: 'queued', goal: '<script>' }],
  });
  assert.equal(summary.nodes, 2);
  assert.equal(summary.edges, 1);
  assert.equal(summary.states.queued, 1);
  assert.equal(summary.edgeText, 'h1 · queued');
});

const HOSTILE = '<img src=x onerror="window.__x=1">';

test('detail shows accomplished, missing and next, the chain, and escapes every field', () => {
  const html = detailHtml({ handoff_id: 'h<1>', state: 'in_progress', goal: HOSTILE, completed: [HOSTILE, 'wrote parser'], remaining: ['tests'],
    decisions: [], risks: ['r'], validation: [], history: [{ state: 'queued', at: '2026-10-30T10:00:00+00:00', actor: { id: HOSTILE } }],
    source: { harness: 'pi', agent_id: HOSTILE }, target: { harness: 'claude', model_hint: HOSTILE }, mission_id: 'm1' },
  { position: 1, chain: [{ handoff_id: 'a', state: 'completed', source: { harness: 'pi' }, target: { harness: 'pi' } }, { handoff_id: 'h<1>', state: 'in_progress', source: { harness: 'pi' }, target: { harness: 'claude' } }] });
  assert.ok(!html.includes('<img'), 'no raw markup survives');
  assert.match(html, /Accomplished/); assert.match(html, /Missing \/ what happens next/); assert.match(html, /wrote parser/);
  assert.match(html, /aria-current="true"/); assert.match(html, /data-handoff="a"/);
  assert.ok(!html.includes('<h3>Decisions</h3>'), 'empty sections are left out');
});

test('summaries and cards stay bounded and escaped', () => {
  assert.equal(overviewSummary({ total: 5000, workspaces: new Array(100), workspaces_omitted: 200, attention_total: 7 }), '5000 handoffs · 300 workspaces · 7 need attention');
  assert.equal(overviewSummary({}), '0 handoffs · 0 workspaces');
  assert.ok(!handoffCard({ handoff_id: HOSTILE, state: 'failed', goal: HOSTILE, source: { harness: HOSTILE }, target: {}, completed: 0, remaining: 1, attention: true }).includes('<img'));
  assert.ok(!workspaceCard({ workspace: HOSTILE, total: 1, states: { [HOSTILE]: 1 }, attention: 1, latest_at: '' }, false).includes('<img'));
});

test('flow nodes are endpoints, edges are handoffs, and an arrow remembers its newest handoff', () => {
  const rows = [{ handoff_id: 'n2', state: 'queued', source: { harness: 'pi', agent_id: 'a' }, target: { harness: 'pi', agent_id: 'b' } },
    { handoff_id: 'n1', state: 'failed', source: { harness: 'pi', agent_id: 'a' }, target: { harness: 'pi', agent_id: 'b' } }];
  const { flow, latest } = flowFromPage(rows);
  assert.equal(flow.nodes.length, 2); assert.equal(flow.edges.length, 1);
  assert.deepEqual(flow.edges[0].counts, { pending: 1, failed: 1 });
  assert.equal(latest.get('a>b'), 'n2');
});

import { agentStory, renderStory } from './agents-lineage.js';

test('an agent story names the handoff it started from and handed on, as links to the detail', () => {
  const tree = { nodes: [{ id: 'a', session_id: 'S', harness: 'pi' }, { id: 'p', session_id: 'P', harness: 'pi' }, { id: 'n', session_id: 'N', harness: 'claude' }],
    edges: [{ handoff_id: 'in<1>', from: 'p', to: 'a', state: 'completed', goal: 'g', updated_at: '2026-01-01T00:00:00Z' },
      { handoff_id: 'out2', from: 'a', to: 'n', state: 'queued', goal: 'h', updated_at: '2026-01-02T00:00:00Z' }] };
  const story = agentStory({ session_id: 'S', created_at: '2026-01-01T00:00:00Z', updated_at: '2026-01-03T00:00:00Z', status: 'active' }, tree);
  assert.deepEqual(story.steps.filter((s) => s.handoffId).map((s) => s.handoffId).sort(), ['in<1>', 'out2']);
  const html = renderStory(story);
  assert.match(html, /href="#\/handoffs\?open=out2"/); assert.match(html, /open=in%3C1%3E/); assert.ok(!html.includes('in<1>'));
});

test('a cut chain says so on both ends, and the state badge reads as words', () => {
  const hop = (id) => ({ handoff_id: id, state: 'completed', source: { harness: 'pi' }, target: { harness: 'pi' } });
  const html = detailHtml({ handoff_id: 'b', state: 'in_progress', goal: 'g', source: {}, target: {}, completed: [], remaining: [], history: [] },
    { position: 1, more_before: true, more_after: true, chain: [hop('a'), hop('b'), hop('c')] });
  assert.equal((html.match(/…/g) || []).length, 2);
  assert.match(html, /<span class="hd-badge">in progress<\/span>/);
});

const row = (id, a, b, state = 'completed', extra = {}) => ({ handoff_id: id, state, source: { harness: 'pi', agent_id: a }, target: { harness: 'pi', agent_id: b }, ...extra });

test('routes are one row per from-to pair, attention first, then busiest', () => {
  const routes = routeTable([row('1', 'a', 'b'), row('2', 'a', 'b', 'queued'), row('3', 'c', 'd'), row('4', 'c', 'd'), row('5', 'c', 'd'), row('6', 'x', 'y', 'failed', { attention: true })]);
  assert.deepEqual(routes.map((r) => [r.key, r.total]), [['@x → @y', 1], ['@c → @d', 3], ['@a → @b', 2]]);
  assert.deepEqual(routes[2].states, { completed: 1, queued: 1 });
  const html = routesHtml(routes, '@c → @d', 2);
  assert.match(html, /3 routes, top 2/); assert.match(html, /aria-pressed="true"/); assert.ok(!html.includes('@a → @b'));
});

test('a chain reads as a labelled path with agents, goals and the current step marked', () => {
  const hop = (id, a, b, goal) => ({ ...row(id, a, b), goal, updated_at: '2026-01-01T00:00:00Z' });
  const html = detailHtml({ handoff_id: 'b', state: 'completed', goal: 'g', source: {}, target: {}, completed: [], remaining: [], history: [] },
    { position: 1, chain: [hop('a', 'one', 'two', 'first leg'), hop('b', 'two', 'three', 'second leg'), hop('c', 'three', 'four', HOSTILE)] });
  assert.match(html, /<ol class="hd-chain"/); assert.match(html, /@one → @two/); assert.match(html, /first leg/);
  assert.equal((html.match(/aria-current="step"/g) || []).length, 1); assert.ok(!html.includes('<img'));
});

import { handoffText } from './handoffs-view.js';

test('the full handoff text carries every recorded field and nothing is trusted as markup', () => {
  const row = { handoff_id: 'h1', state: 'in_progress', goal: `Continue the drain work.\n\nContext: ${HOSTILE}`, mission_id: 'm1', created_at: '2026-01-01T00:00:00Z', created_by: { id: 'op' },
    source: { harness: 'pi', agent_id: 'a', session_id: 's1' }, target: { harness: 'pi', agent_id: 'b', model_hint: 'qwen' }, completed: ['parser'], remaining: ['tests'], decisions: [], risks: ['r'], validation: [],
    artifact_refs: [{ artifact_id: 'art_1', version_id: 'ver_2' }], provenance: { checkpoint: 'abc123', cause: 'context_pressure' }, history: [{ state: 'queued', at: '2026-01-01T00:00:00Z', actor: { id: 'op' } }] };
  const text = handoffText(row);
  for (const part of ['HANDOFF h1 · in progress', 'From: pi · @a · session s1', 'model qwen', 'Mission: m1', 'Continue the drain work.', '- parser', '- tests', 'art_1 @ ver_2', '- checkpoint: abc123', '- cause: context_pressure', 'queued at 2026-01-01T00:00:00Z by op']) assert.ok(text.includes(part), part);
  const html = detailHtml(row, null);
  assert.ok(!html.includes('<img'), 'escaped'); assert.match(html, /data-handoff-full/); assert.match(html, /data-handoff-copy/); assert.match(html, /Handoff text/);
});

test('a Pi handoff says what happened to the model, and the detail is honest about having no written summary', () => {
  assert.equal(modelText({ origin: 'lineage', model_change: 'same', to_model: 'metabolomics/qwen3.8-27b-q8' }), 'same model · qwen3.8-27b-q8');
  assert.equal(modelText({ origin: 'lineage', model_change: 'switched', from_model: 'a/x', to_model: 'b/y' }), 'model changed · x → y');
  assert.equal(modelText({ origin: 'lineage', model_change: 'not_pinned', from_model: 'qwen' }), 'not pinned · was qwen · routed normally');
  assert.equal(modelText({ origin: 'lineage', model_change: 'unknown' }), 'model not reported');
  assert.equal(modelText({ origin: 'registry' }), '');
  const lineage = { handoff_id: 'handoff:h1', origin: 'lineage', state: 'accepted', goal: 'Context pressure', cause: HOSTILE, checkpoint: 'abcdef1234567890', model_change: 'not_pinned', from_model: 'q',
    source: { harness: 'pi', session_id: 's1' }, target: { harness: 'pi', session_id: 's2' }, completed: [], remaining: [], history: [] };
  const html = detailHtml(lineage, null);
  assert.match(html, /data-handoff-model/); assert.match(html, /not pinned/); assert.match(html, /abcdef123456/); assert.ok(!html.includes('<img'));
  assert.ok(!html.includes('<h3>Accomplished</h3>'), 'no empty accomplished/missing columns for a handoff that has no written summary');
  assert.match(handoffCard({ ...lineage, attention: true, attention_reason: 'successor not pinned', completed: 0, remaining: 0 }), /not pinned · was q/);
});

const VROW = { handoff_id: 'handoff:vision-r1-out', origin: 'lineage', state: 'completed', goal: 'g', source: {}, target: {}, completed: 0, remaining: 0, cause: 'vision_delegate',
  model_change: 'switched', from_model: 'p/text-model', to_model: 'p/vision-model', vision: { request_id: 'r1', phase: 'out', outcome: 'ok' } };

test('a vision card names the cause, both models and the outcome', () => {
  const html = handoffCard(VROW);
  assert.match(html, /vision delegation · model changed · text-model → vision-model · analysis complete/);
  assert.match(handoffCard({ ...VROW, cause: 'vision_return' }), /vision result · model changed/);
  assert.ok(!html.includes('hd-row--attention'));
});

test('a partial outcome reads as partial analysis and is marked for attention', () => {
  const html = handoffCard({ ...VROW, vision: { ...VROW.vision, outcome: 'partial' }, attention: true, attention_reason: 'partial analysis' });
  assert.match(html, /· partial analysis/);
  assert.match(html, /hd-row--attention/);
  assert.equal(outcomeText({ outcome: 'failed' }), 'analysis failed');
  assert.equal(outcomeText({ outcome: 'timeout' }), 'timed out');
});

test('an unknown outcome reads as outcome unknown', () => {
  assert.match(handoffCard({ ...VROW, vision: { ...VROW.vision, outcome: 'unknown' } }), /outcome unknown/);
  assert.equal(outcomeText(undefined), 'outcome unknown');
  assert.equal(outcomeText({ outcome: 'weird' }), 'outcome unknown');
});

test('hostile markup in a model name and in the request id is escaped in the card and the detail', () => {
  const row = { ...VROW, to_model: HOSTILE, vision: { request_id: HOSTILE, phase: 'out', outcome: 'ok' } };
  assert.ok(!handoffCard(row).includes('<img'));
  const html = detailHtml(row, null);
  assert.ok(!html.includes('<img'));
  assert.match(html, /analysis complete/);
  assert.match(html, /&lt;img src=x/);
});

// Golden strings below were rendered by the PRE-CHANGE module (git show 14ded885:packages/handoffs-view.js), not by this one.
const GOLD_LINEAGE = { handoff_id: 'handoff:h1', origin: 'lineage', state: 'accepted', goal: 'Context pressure', cause: 'context_pressure', checkpoint: 'abcdef1234567890', model_change: 'not_pinned', from_model: 'q', attention: true, attention_reason: 'successor not pinned',
  source: { harness: 'pi', session_id: 's1' }, target: { harness: 'pi', session_id: 's2' }, completed: 0, remaining: 0, history: [] };
const GOLD_REGISTRY = { handoff_id: 'h1', state: 'in_progress', goal: 'Continue the drain work.', completed: ['parser'], remaining: ['tests'], decisions: ['d'], risks: ['r'], validation: [], history: [],
  source: { harness: 'pi', agent_id: 'a', session_id: 's1' }, target: { harness: 'claude', model_hint: 'qwen' }, mission_id: 'm1' };
const GOLD = { lc: "<button type=\"button\" class=\"hd-row hd-row--attention\" data-handoff=\"handoff:h1\" title=\"handoff:h1 — successor not pinned\">\n    <span class=\"hd-row__state hd-row__state--accepted\">accepted</span>\n    <strong class=\"hd-row__who\">s1 → s2</strong>\n    <span class=\"hd-row__goal\">context pressure · not pinned · was q · routed normally</span>\n    <small class=\"hd-row__meta\">successor not pinned</small></button>",
  ld: "<section class=\"hd-detail\" data-handoff-detail-body aria-label=\"Handoff handoff:h1\">\n    <header><h2>pi s1 → pi s2</h2>\n    <p><span class=\"hd-badge\">accepted</span> accepted</p>\n    </header>\n    <h3>Handoff text — what the next agent was told</h3><p class=\"hd-goal\" data-handoff-goal>Context pressure</p>\n    <h3>Model</h3><p data-handoff-model><strong>not pinned · was q · routed normally</strong> · cause <code>context_pressure</code> · checkpoint <code>abcdef123456</code></p>\n      <p class=\"hd-muted\">Pi records this handoff and its verified checkpoint, not a written summary: the work done so far is in the checkpoint, and the successor continues from it.</p>\n    \n    \n    \n    \n    \n    <details class=\"hd-full\" data-handoff-full><summary>Full handoff as text</summary><p><button type=\"button\" data-handoff-copy>Copy to clipboard</button> <small class=\"hd-muted\" data-handoff-copied hidden>Copied</small></p><pre class=\"hd-pre\">HANDOFF handoff:h1 · accepted\nFrom: pi · session s1\nTo:   pi · session s2\n\nCreated: ? · updated ?\n\nGOAL\nContext pressure\n\nACCOMPLISHED\n(none recorded)\n\nMISSING / NEXT\n(none recorded)\n\nDECISIONS\n(none recorded)\n\nRISKS\n(none recorded)\n\nVALIDATION\n(none recorded)\n\nARTIFACTS\n(none)\n\nPROVENANCE\n(none)\n\nHISTORY\n(none)</pre></details></section>",
  rc: "<button type=\"button\" class=\"hd-row\" data-handoff=\"h1\" title=\"h1\">\n    <span class=\"hd-row__state hd-row__state--in_progress\">in progress</span>\n    <strong class=\"hd-row__who\">@a → claude</strong>\n    <span class=\"hd-row__goal\">Continue the drain work.</span>\n    <small class=\"hd-row__meta\">1 done · 1 left · </small></button>",
  rd: "<section class=\"hd-detail\" data-handoff-detail-body aria-label=\"Handoff h1\">\n    <header><h2>pi @a → claude</h2>\n    <p><span class=\"hd-badge\">in progress</span> in progress · model hint <code>qwen</code></p>\n    <p class=\"hd-muted\">Mission <a href=\"#/missions/m1\">m1</a></p></header>\n    <h3>Handoff text — what the next agent was told</h3><p class=\"hd-goal\" data-handoff-goal>Continue the drain work.</p>\n    <div class=\"hd-cols\"><div><h3>Accomplished</h3><ul><li>parser</li></ul></div><div><h3>Missing / what happens next</h3><ul><li>tests</li></ul></div></div>\n    \n    <h3>Decisions</h3><ul><li>d</li></ul>\n    <h3>Risks</h3><ul><li>r</li></ul>\n    \n    \n    <details class=\"hd-full\" data-handoff-full><summary>Full handoff as text</summary><p><button type=\"button\" data-handoff-copy>Copy to clipboard</button> <small class=\"hd-muted\" data-handoff-copied hidden>Copied</small></p><pre class=\"hd-pre\">HANDOFF h1 · in progress\nFrom: pi · @a · session s1\nTo:   claude · model qwen\nMission: m1\n\nCreated: ? · updated ?\n\nGOAL\nContinue the drain work.\n\nACCOMPLISHED\n- parser\n\nMISSING / NEXT\n- tests\n\nDECISIONS\n- d\n\nRISKS\n- r\n\nVALIDATION\n(none recorded)\n\nARTIFACTS\n(none)\n\nPROVENANCE\n(none)\n\nHISTORY\n(none)</pre></details></section>" };

test('non-vision cards render exactly as before', () => {
  assert.equal(handoffCard(GOLD_LINEAGE), GOLD.lc);
  assert.equal(detailHtml(GOLD_LINEAGE, null), GOLD.ld);
  assert.equal(handoffCard({ ...GOLD_REGISTRY, completed: 1, remaining: 1 }), GOLD.rc);
  assert.equal(detailHtml(GOLD_REGISTRY, null), GOLD.rd);
  for (const row of [GOLD_LINEAGE, GOLD_REGISTRY]) {
    assert.equal(detailHtml({ ...row, vision: undefined }, null), detailHtml(row, null));
    assert.equal(handoffCard({ ...row, vision: undefined, completed: 1, remaining: 1 }), handoffCard({ ...row, completed: 1, remaining: 1 }));
  }
});

test('a vision row with missing fields never prints undefined or null and never throws', () => {
  const variants = [{ outcome: 'ok' }, { outcome: 'ok', request_id: '' }, { outcome: 'ok', request_id: 42 }, { request_id: 'r1' }, {}];
  for (const vision of variants) for (const row of [{ ...VROW, vision }, { ...VROW, vision, to_model: undefined }, { ...VROW, vision, from_model: null, to_model: null }]) {
    for (const html of [handoffCard(row), detailHtml(row, null)]) assert.ok(!/undefined|null/.test(html.replace(/data-handoff-[a-z-]+|hd-[a-z_-]+/g, '')), JSON.stringify(vision));
  }
  assert.ok(!detailHtml({ ...VROW, vision: { outcome: 'ok' } }, null).includes('request <code>'));
  assert.ok(!detailHtml({ ...VROW, vision: { outcome: 'ok', request_id: '' } }, null).includes('request <code>'));
  assert.match(detailHtml(VROW, null), /request <code>r1<\/code>/);
});

const vrow = (id, rid, phase, outcome, created, updated, extra = {}) => ({ handoff_id: id, cause: phase === 'out' ? 'vision_delegate' : 'vision_return',
  vision: { request_id: rid, phase, outcome }, created_at: created, updated_at: updated || created, state: 'completed', origin: 'lineage', ...extra });
const T0 = '2026-11-01T10:00:00Z';

test('an out and a back row with one request id pair into one round trip with a duration', () => {
  const t = visionRoundTrips([vrow('o1', 'r1', 'out', 'ok', T0), vrow('b1', 'r1', 'back', 'partial', '2026-11-01T10:00:05Z', '2026-11-01T10:00:07Z')]);
  assert.equal(t.length, 1);
  assert.equal(t[0].requestId, 'r1'); assert.equal(t[0].outcome, 'partial'); assert.equal(t[0].durationMs, 7000); assert.equal(t[0].open, false);
  assert.equal(t[0].out.handoff_id, 'o1'); assert.equal(t[0].back.handoff_id, 'b1');
});
test('an out row with no back row is an open round trip', () => {
  const [t] = visionRoundTrips([vrow('o1', 'r1', 'out', 'ok', T0)]);
  assert.equal(t.outcome, 'open'); assert.equal(t.open, true); assert.equal(t.durationMs, null);
});
test('a back row without its out row stands alone with no duration', () => {
  const [t] = visionRoundTrips([vrow('b1', 'r1', 'back', 'failed', T0)]);
  assert.equal(t.outcome, 'failed'); assert.equal(t.open, false); assert.equal(t.durationMs, null); assert.equal(t.out, undefined);
});
test('two delegations in the same second are two round trips', () => {
  const t = visionRoundTrips([vrow('o1', 'r1', 'out', 'ok', T0), vrow('o2', 'r2', 'out', 'ok', T0), vrow('b2', 'r2', 'back', 'ok', T0), vrow('b1', 'r1', 'back', 'ok', T0)]);
  assert.equal(t.length, 2); assert.ok(t.every((x) => x.out && x.back));
});
test('two rows with empty request ids stay two entries (H3)', () => {
  const rows = [vrow('o1', '', 'out', 'ok', T0), vrow('o2', '', 'out', 'ok', T0), vrow('o3', undefined, 'back', 'ok', T0), vrow('o4', 7, 'back', 'ok', T0)];
  const t = visionRoundTrips(rows);
  assert.equal(t.length, 4);
  assert.equal(t[0].open, true);
});
test('a negative duration is null', () => {
  const [t] = visionRoundTrips([vrow('o1', 'r1', 'out', 'ok', '2026-11-01T11:00:00Z'), vrow('b1', 'r1', 'back', 'ok', T0, T0)]);
  assert.equal(t.durationMs, null);
  const [u] = visionRoundTrips([vrow('o1', 'r1', 'out', 'ok', ''), vrow('b1', 'r1', 'back', 'ok', T0, 'junk')]);
  assert.equal(u.durationMs, null);
});
test('filterByCause returns only matching rows and null returns all', () => {
  const rows = [{ cause: 'a' }, { cause: 'b' }, { cause: 'a' }];
  assert.equal(filterByCause(rows, 'a').length, 2);
  assert.equal(filterByCause(rows, null).length, 3);
  assert.equal(filterByCause(rows, '').length, 3);
});
test('visionStats counts total, by outcome, the last 24 hours and the last 7 days from a fixed now', () => {
  const now = Date.parse('2026-11-08T12:00:00Z');
  const rows = [
    vrow('o1', 'a', 'out', 'ok', '2026-11-08T12:00:00Z'), vrow('b1', 'a', 'back', 'ok', '2026-11-08T12:00:00Z'),      // exactly now
    vrow('o2', 'b', 'out', 'ok', '2026-11-07T12:00:00Z'), vrow('b2', 'b', 'back', 'failed', '2026-11-07T12:00:01Z'),  // 24h boundary
    vrow('o3', 'c', 'out', 'ok', '2026-11-01T12:00:00Z'),                                                          // 7d boundary, open
    vrow('o4', 'd', 'out', 'ok', '2026-11-01T11:59:59Z'), vrow('b4', 'd', 'back', 'timeout', '2026-11-01T12:00:09Z'), // just outside
    vrow('o5', 'e', 'out', 'ok', '2026-11-09T00:00:00Z'), vrow('b5', 'e', 'back', 'ok', '2026-11-09T00:00:01Z'),      // future
    vrow('o6', 'f', 'out', 'ok', ''), vrow('b6', 'f', 'back', 'partial', ''),                                       // no usable time
  ];
  const s = visionStats(rows, now);
  assert.equal(s.total, 6);
  assert.deepEqual(s.byOutcome, { ok: 2, failed: 1, open: 1, timeout: 1, partial: 1 });
  assert.equal(s.last24h, 2); assert.equal(s.last7d, 3);
});
test('visionStats ignores non-vision rows', () => {
  const s = visionStats([{ handoff_id: 'x', cause: 'context_pressure', created_at: T0 }, null, vrow('o1', 'r', 'out', 'ok', T0)], Date.parse(T0));
  assert.equal(s.total, 1);
  assert.equal(visionStats([], 0).total, 0);
});
test('hostile request ids and model names are escaped where round trips and the stats line are rendered', () => {
  const rows = [vrow('o<1>', HOSTILE, 'out', 'ok', T0, T0, { from_model: HOSTILE, to_model: HOSTILE, cause: HOSTILE }), vrow('b"1', HOSTILE, 'back', 'ok', T0, T0, { from_model: HOSTILE, to_model: HOSTILE })];
  const [trip] = visionRoundTrips(rows);
  const html = roundTripHtml(trip) + visionStatsLine(visionStats(rows, Date.parse(T0))) + causeFilterHtml(rows, HOSTILE) + listHtml(rows);
  assert.ok(!html.includes('<img'), 'no raw markup survives');
  assert.match(roundTripHtml(trip), /data-handoff="o&lt;1&gt;"/);
  assert.match(roundTripHtml(trip), /data-handoff="b&quot;1"/);
});
test('stats line says it covers the loaded rows of this workspace; list shows one entry per round trip', () => {
  const rows = [vrow('o1', 'r1', 'out', 'ok', T0), vrow('b1', 'r1', 'back', 'ok', T0), { handoff_id: 'n1', state: 'queued', source: {}, target: {} }];
  const line = visionStatsLine(visionStats(rows, Date.parse(T0)));
  assert.match(line, /1 vision delegation/); assert.match(line, /loaded/); assert.match(line, /this workspace/);
  const html = listHtml(rows);
  assert.equal((html.match(/data-roundtrip/g) || []).length, 1);
  assert.match(html, /data-handoff="n1"/);
  assert.match(causeFilterHtml(rows, null), /data-cause="vision_delegate"/);
});

// ------------------------------------------------------------------ mountHandoffs wiring (fake DOM)
function mountFake(pagesRef) {
  const listeners = [];
  const body = { innerHTML: '', hidden: true, querySelector: () => null };
  const head = { setAttribute() {} };
  const card = { dataset: { workspace: 'ws' }, querySelector: (s) => (s === '.hd-ws__body' ? body : s === '.hd-ws__head' ? head : null) };
  head.closest = () => card;
  let listHtmlOut = '';
  const wsList = { set innerHTML(v) { listHtmlOut = v; }, get innerHTML() { return listHtmlOut; }, querySelectorAll: () => [], querySelector: () => card };
  const els = { '[data-handoff-summary]': { textContent: '' }, '[data-handoff-attention]': { innerHTML: '' }, '[data-handoff-list]': wsList,
    '[data-handoff-detail]': { innerHTML: '', scrollIntoView() {} }, '[data-handoff-retry]': { hidden: true, addEventListener() {} } };
  const container = { innerHTML: '', querySelector: (s) => els[s] || null, querySelectorAll: () => [], contains: () => true,
    addEventListener: (t, fn) => { if (t === 'click') listeners.push(fn); } };
  let total = 1;
  const calls = [];
  const client = {
    getHandoffOverview: async () => ({ workspaces: [{ workspace: 'ws', total: total++, states: {} }], total: 1 }),
    pageHandoffs: async () => ({ handoffs: pagesRef.rows, next_cursor: null }),
    getHandoff: async (id) => { calls.push(id); return { handoff_id: id, state: 'completed' }; },
    getHandoffChain: async () => null,
  };
  const click = (map) => { for (const fn of listeners) fn({ target: { closest: (s) => map[s] || null } }); };
  const tick = () => new Promise((r) => setTimeout(r, 5));
  return { mount: () => mountHandoffs(container, client), card, body, click, tick, calls, head };
}
const withGlobals = async (fn) => {
  const had = { CSS: globalThis.CSS, window: globalThis.window };
  globalThis.CSS = { escape: (x) => x }; globalThis.window = {};
  try { await fn(); } finally { globalThis.CSS = had.CSS; globalThis.window = had.window; }
};
const sourced = (r, n) => ({ ...r, source: { agent_id: `a${n}` }, target: { agent_id: `b${n}` } });
const visionRows = (cause = 'context_pressure') => [vrow('o1', 'r1', 'out', 'ok', T0), vrow('b1', 'r1', 'back', 'ok', T0)].map(sourced)
  .concat([2, 3, 4, 5, 6, 7, 8].map((n) => sourced({ handoff_id: `n${n}`, cause, state: 'queued', origin: 'lineage' }, n)));
const openCard = async (h) => { h.click({ '.hd-ws__head': h.head }); await h.tick(); };

test('mountHandoffs: cause filter click, round trip links, survival and stale reset', async () => {
  await withGlobals(async () => {
    const pages = { rows: visionRows() };
    const h = mountFake(pages);
    const m = h.mount(); await h.tick();
    try {
      await openCard(h);
      assert.match(h.body.innerHTML, /data-roundtrip/); assert.match(h.body.innerHTML, /data-handoff="n2"/);
      // (a) cause click filters
      h.click({ '[data-cause]': { dataset: { cause: 'context_pressure' }, closest: () => h.card } });
      assert.ok(!/data-roundtrip/.test(h.body.innerHTML)); assert.match(h.body.innerHTML, /data-handoff="n2"/);
      // (c) filter survives a reload whose rows still have the cause
      await m.refresh(); await h.tick();
      assert.equal(h.card.__cause, 'context_pressure'); assert.ok(!/data-roundtrip/.test(h.body.innerHTML));
      // (d) reload without that cause clears the filter, list shows rows, control still usable
      pages.rows = visionRows('quota_exhausted');
      await m.refresh(); await h.tick();
      assert.equal(h.card.__cause, null); assert.match(h.body.innerHTML, /data-roundtrip/);
      assert.match(h.body.innerHTML, /data-cause="vision_delegate"/);
      h.click({ '[data-cause]': { dataset: { cause: 'vision_return' }, closest: () => h.card } });
      assert.equal(h.card.__cause, 'vision_return'); assert.match(h.body.innerHTML, /data-roundtrip/);
      h.click({ '[data-cause]': { dataset: { cause: '' }, closest: () => h.card } });
      assert.equal(h.card.__cause, null);
      // (b) out and result buttons open the out then the back handoff
      h.click({ '[data-handoff]': { dataset: { handoff: 'o1' } } }); await h.tick();
      h.click({ '[data-handoff]': { dataset: { handoff: 'b1' } } }); await h.tick();
      assert.deepEqual(h.calls, ['o1', 'b1']);
      assert.match(h.body.innerHTML, /data-handoff="o1"[^>]*>out</); assert.match(h.body.innerHTML, /data-handoff="b1"[^>]*>result</);
    } finally { m.dispose(); }
  });
});

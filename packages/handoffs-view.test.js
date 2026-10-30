import test from 'node:test';
import assert from 'node:assert/strict';
import { handoffGraphSummary, overviewSummary, detailHtml, handoffCard, workspaceCard, flowFromPage, routeTable, routesHtml } from './handoffs-view.js';

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

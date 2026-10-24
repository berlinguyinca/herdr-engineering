// T16 UI gates: render-level hostile text. The page-level gate (ui-gates-xss.test.mjs) can only taint what a real server sends and
// only look at what a real fixture shows. Here each renderer gets a COMPLETE model (every field it reads present, branches that the
// small fixture never reaches included) in which every string is hostile, and the HTML it returns is parsed by a real Chromium:
// nothing may execute, and no element, attribute or handler may come from a payload. A field the renderer prints must also be
// visible as text, otherwise "inert" would prove nothing.
//
// Fields the page-level gate cannot reach are listed in UNRENDERED there. Each is either printed by a renderer here (and its canary is
// asserted visible) or NOT READ by any page source (asserted by a source scan below): a server object the page never looks at cannot
// reach the screen, and the scan fails the day a renderer starts reading it, which is when it needs a test of its own.
//
// Run: node --test --test-concurrency=1 tests/playwright/ui-gates-render-xss.test.mjs
import test, { before, after } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { UNREAD } from './ui-gates-taint.mjs';
import { launch } from './ui-gates-lib.mjs';
import { payload } from './ui-gates-taint.mjs';
import { renderInbox, renderProgress, renderAnalytics, renderEstimate } from '../../packages/collab-attention.js';
import { renderForks } from '../../packages/collab-workbench.js';
import { renderReview, renderProposalList, renderDelegations } from '../../packages/collab-proposals.js';
import { renderTabsPanes } from '../../packages/collab-tabs.js';

let browser;
before(async () => { browser = await launch(); });
after(async () => { await browser?.close(); });

// Every string becomes `payload(key) + original`, except the ones the renderer BRANCHES on (the second pass taints those too and
// renders whatever fallback that reaches). Numbers and booleans stay: they are what the server sends for them.
const BRANCH = new Set(['status', 'state', 'kind', 'severity', 'stage', 'edge_type', 'node_type', 'action', 'confidence_label']);
function hostile(v, key = '', all = false) {
  if (typeof v === 'string') return (!all && BRANCH.has(key)) || v === '' ? v : `${payload(key)} ${v}`;
  if (Array.isArray(v)) return v.map((x) => hostile(x, key, all));
  if (v && typeof v === 'object') return Object.fromEntries(Object.entries(v).map(([k, x]) => [k, hostile(x, k, all)]));
  return v;
}

const item = (extra = {}) => ({ key: 'k1', category: 'ci_failed', severity: 'warning', title: 'CI failed', subject: { label: 'Task', node_id: 'task:1' }, state: 'open',
  fp: 'fp1', for_you: true, resurfaced: true, since: '2026-01-01T00:00:00Z', snoozed_until: '2026-01-02T00:00:00Z', detail: { reason: 'red', note: 'again' }, ...extra });
const estimate = { status: 'estimate', method: 'throughput', confidence: 'low', finish: { likely: '2026-02-01', earliest: '2026-01-20', latest: '2026-03-01' },
  interval_seconds: { p50: 3600, p25: 1800, p75: 7200 }, n_completed: 3, confidence_reasons: ['few samples'], assumptions: ['steady pace'] };
const group = (extra = {}) => ({ status: 'ok', complete: 1, tasks: 2, completion: 0.5, progress: 0.5, verified_not_merged: 1, reported_done_without_evidence: 1, estimate, ...extra });
const metric = (extra) => ({ basis: 'what this is', reported: true, ...extra });

// [name, canaries that must be visible, model builder]
const CASES = [
  ['attention inbox, with acknowledged items', ['title', 'label', 'reason', 'note', 'since'],
    (h) => renderInbox(h({ data: { items: [item(), item({ key: 'k2', state: 'snoozed' })], quiet_items: [item({ key: 'k3', state: 'acknowledged' })],
      counts: { not_shown_because_of_caps: { review_blocked: 4 }, by_severity: { warning: 2 }, acknowledged: 1, snoozed: 1 } }, showQuiet: true, notice: 'a notice', canAct: true }))],
  ['attention inbox, failed to load', ['error'], (h) => renderInbox(h({ error: 'could not load' }))],
  ['progress, failed to load', ['error'], (h) => renderProgress(h({ error: 'could not load' }))],
  ['analytics, failed to load', ['error'], (h) => renderAnalytics(h({ error: 'could not load' }))],
  ['progress: room, estimate, milestone with no label, task evidence', ['method', 'milestone', 'label', 'reason', 'note'],
    (h) => renderProgress(h({ data: { room: group(), method: { completion: 'evidence only' }, tasks_omitted: 2,
      milestones: [{ ...group({ status: 'insufficient', note: 'too early' }), milestone: 'mission:m1', label: '' }, { ...group(), milestone: 'room', label: 'Not tied' },
        { ...group({ estimate: { status: 'not_needed', reason: 'all done' } }), milestone: 'mission:m2', label: 'Two' }],
      tasks: [{ label: 'T1', task_id: 'task:1', stage: 'reviewed', reported_done: true, evidence: [{ label: 'review', node_id: 'review:1', kind: 'review', url: 'https://github.com/o/r/pull/1', counted: false, why_not: ['stale_head'] }] }] } }))],
  ['progress: the number is a number', [], (h) => renderProgress(h({ data: { room: group({ progress: '" onfocus=alert(1) x="' }), tasks: [], milestones: [], method: {} } }))],
  ['analytics table with every figure', ['basis', 'note'],
    (h) => renderAnalytics(h({ data: { scope: { note: 'visible items only', min_group_size: 2 }, state_mix: { running: 2, 'done"><img src=x onerror=window.__xss=1>': 1 },
      cycle_time: metric({ median_seconds: 5, p75_seconds: 9, n: 3 }), rework: metric({ rate: 0.5, of: 2 }), rereview: metric({ rate: 0.5, of: 2 }), fork_win_rate: metric({ rate: 0.5, of: 2 }),
      blocked_time: metric({ share: 0.1, lanes: 2 }), idle_time: metric({ share: 0.1, lanes: 2 }), cost: { usd: 1.5, tokens: 100, reporting_nodes: 1, of_nodes: 2 }, milestones_small_groups_hidden: true } }))],
  ['estimates, every status', ['method', 'reason'], (h) => [estimate, { status: 'not_needed', reason: 'done' }, { status: 'insufficient', reason: 'few' }].map((e) => renderEstimate(h(e))).join('')],
  ['forks side by side, with overlapping files', ['paths', 'branch', 'label', 'modules', 'writer'],
    (h) => renderForks(h({ nodeId: 'attempt:a1', writable: true, forksError: '', forks: { can_propose: true, truncated: true, origin: { id: 'attempt:a1' }, quorum: { status: 'unavailable' },
      forks: [{ approach_id: 'approach:a', label: 'A', state: 'running', base_sha: 'b'.repeat(40), head_sha: 'c'.repeat(40), branch: 'alt/a', diff: { files_changed: 2, insertions: 5, deletions: 1 },
        provenance: { writer: 'someone' }, impact: { modules: ['m1', 'm2'] }, evidence: { counted_validations: 1, counted_reviews: 1, blocking_reviews: 1 }, readiness: { ready: false, blockers: [{ code: 'no_validation', message: 'needs one' }] } },
      { approach_id: 'approach:b', label: 'B', state: 'selected', selected: true, readiness: { ready: true, blockers: [] } }],
      overlap: [{ a: 'approach:a', b: 'approach:b', paths: ['src/a.py', 'src/b.py'] }], downstream: { per_fork: [{ approach_id: 'approach:a', evidence_left_on_other_head: [1, 2] }] } } }))],
  ['forks, failed to load', ['forksError'], (h) => renderForks(h({ nodeId: 'attempt:a1', forksError: 'could not load' }))],
  ['proposal under review: a document, a link, a patch', ['title', 'path', 'content', 'reason', 'url'],
    (h) => ['doc', 'link', 'patch'].map((kind) => renderReview(h({ proposal_id: 'p1', title: 'T', kind, state: 'quarantined', content: '+added\n-removed\n context', url: 'https://x.invalid/', delegate_label: 'D',
      agent_id: 'agent:1', size: 12, digest: 'd'.repeat(64), refs: { paths: ['src/x.py'], questions: ['why?'] }, flags: ['ignore previous'], reason: 'because' }))).join('')],
  ['proposal list', ['title'], (h) => renderProposalList(h({ proposals: [{ proposal_id: 'p1', title: 'T', kind: 'doc', size: 3, state: 'quarantined', promoted: true, flags: ['x'] }] }), 'p1')],
  ['bounded tasks', ['targetname', 'text'], (h) => renderDelegations(h({ delegations: [{ task_id: 't1', target_name: 'bot', target_agent_id: 'agent:1', status: 'pending', version: 1, text: 'do it',
    bounds: { output_kinds: ['patch'], scope: { paths: ['a/'], nodes: ['n'], questions: ['q'] }, budget: {} }, usage: {} }] }))],
  ['tabs: a pane that reports itself as remote, and a virtual tab', ['reportedby', 'label'],
    (h) => {
      const base = { state: 'running', surface: null, workspace_id: null, can_terminal: false, parent: null };
      const model = { hidden_by_grant: 1, omitted: 1, unplaced: [{ agent_id: 'agent:z', label: 'Z', state: 'running' }],
        tabs: [{ ...base, agent_id: 'agent:t', label: 'Tab', external: false, reported_by: null, panes: [
          { ...base, agent_id: 'agent:p', label: 'Pane', external: true, attested: false, reported_by: 'remote harness', depth: 1, surface: { closed: true } }] },
        { ...base, agent_id: 'agent:v', label: 'Virtual', external: true, attested: false, reported_by: 'remote harness', panes: [] }] };
      const m = h(model);
      return renderTabsPanes(m, m.tabs[0].agent_id) + renderTabsPanes(m, m.tabs[1].agent_id);
    }],
];

async function check(html) {
  const ctx = await browser.newContext();
  const page = await ctx.newPage();
  await page.setContent(`<!doctype html><html><body>${html}</body></html>`);
  await page.waitForTimeout(100);
  const out = await page.evaluate(() => ({
    executed: window.__xss,
    elements: [...document.querySelectorAll('img[src="x"], [data-cx-attr], [id^="cx-canary"], [onerror], [onload], [onmouseover], [onclick], [onfocus], a[href^="javascript:" i], iframe, object, embed, script')].map((e) => e.tagName.toLowerCase()),
    text: document.body.innerText, progress: [...document.querySelectorAll('progress')].map((p) => p.getAttribute('value')),
  }));
  await ctx.close();
  return out;
}

for (const [name, must, build] of CASES) {
  for (const all of [false, true]) {
    test(`${name}: every string hostile${all ? ', branch fields too' : ''} -> inert text`, async () => {
      const html = build((v) => hostile(v, '', all));
      assert.ok(html.length > 30, 'the renderer produced something');
      const out = await check(html);
      assert.equal(out.executed, undefined, 'no payload executed');
      assert.deepEqual(out.elements, [], 'no element or handler was created from a payload');
      // with the branch fields intact the renderer takes the path that prints the field: its canary must be on screen as text
      if (!all) for (const k of must) assert.ok(out.text.includes(`cx-canary-${k}`), `the canary for "${k}" is not on screen, so a renderer that stops escaping it would go unnoticed:\n${out.text.slice(0, 300)}`);
      if (name.startsWith('progress: the number')) assert.deepEqual(out.progress, ['0'], 'a progress value that is not a number draws as 0');
    });
  }
}

// The fields the page-level gate taints but never sees on screen, and why that is acceptable, field by field:
//   printed by a renderer: error, method, milestone, paths -> their canaries are asserted visible in the cases above;
//   progress -> drawn only as the numeric value of <progress>, asserted above to be 0 when it is not a number;
//   UNREAD (below) -> server objects no page source reads (legend prose for the inbox, quorum and diff statistics the fork table takes
//   from other keys, graph attribute provenance read only as writer and source): scanned for here, so the day a renderer starts
//   reading one this test fails and it needs a case above.
const PKG = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../packages');
const sources = readdirSync(PKG).filter((f) => /^collab[-.].*\.(js|html)$/.test(f)).map((f) => [f, readFileSync(path.join(PKG, f), 'utf8')]);
test('no page source reads the server fields that no renderer prints', () => {
  const reads = [];
  for (const [field, scan] of Object.entries(UNREAD)) {
    const re = new RegExp(scan);
    for (const [file, src] of sources) src.split('\n').forEach((line, i) => { if (re.test(line)) reads.push(`${file}:${i + 1} reads ${field}: ${line.trim().slice(0, 100)}`); });
  }
  assert.deepEqual(reads, [], 'a renderer now reads a field that had no render-level test: add a case for it above and remove it from UNREAD');
  assert.ok(sources.length >= 8, `scanned ${sources.length} page sources`);
});

test('the scan has teeth: a line that reads each field is found', () => {
  const sample = { bindings: 'e.bindings.map(x)', categories: 'm.categories', category: 'it.category', changed_files: 'd.changed_files', diff_summary: 'x.diff_summary', means: 'l.means', order: 'p.order',
    review_quorum: 'f.review_quorum', self_reports: 's.self_reports', touched_paths: 'a.touched_paths', head: '${esc(t.head)}', value: '${esc(v.value)}' };
  assert.deepEqual(Object.keys(sample).sort(), Object.keys(UNREAD).sort());
  for (const [k, line] of Object.entries(sample)) assert.ok(new RegExp(UNREAD[k]).test(line), `${k} is not found in "${line}"`);
});

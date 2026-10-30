import test from 'node:test';
import assert from 'node:assert/strict';
import { recordCards, renderEvolution, renderItem, evolutionModel, tabsHtml, wireTabs } from './agent-evolution-view.js';

const sig = (value, reason = '') => ({ value, reason });
const record = {
  outcome: sig('finished', 'Last reported status: finished.'), duration_s: sig(3600), tool_calls: { total: sig(4), failed: sig(0), retried: sig(null, 'arguments withheld') },
  handoffs_in: sig(1), handoffs_out: { total: sig(null, 'could not be read'), rejected: sig(null, 'could not be read'), failed: sig(null, '') },
  prs: sig([{ ref: 'org/app#7' }]), cost_usd_estimate: sig(0.004, ''),
};
const data = {
  sources: { handoffs: 'ok', tools: 'partial', ci: 'ok' }, learning_record: record,
  phases: [
    { key: 'assigned', title: 'Assigned', state: 'recorded', reason: '', omitted: 0, items: [
      { kind: 'session_record', at: '2026-10-29T12:00:00Z', name: '<b>Coder</b>', role: 'coder', runtime: 'pi', model: 'gpt-4o', mission_id: 'm 1', rule: 'session_record' },
      { kind: 'handoff_in', at: '2026-10-29T12:01:00Z', state: 'accepted', goal: '<img src=x onerror=alert(1)>', from: { harness: 'pi', session_id: 'a/b' }, to: { harness: 'claude', session_id: 's1' },
        completed: ['spec'], remaining: ['tests'], decisions: [], time_to_accept_s: 60, rule: 'session_id' }] },
    { key: 'worked', title: 'Worked', state: 'recorded', reason: 'older events were evicted', omitted: 3, undated: { tool_calls: 2, status_events: 0 }, items: [
      { kind: 'run', started_at: '2026-10-29T12:02:00Z', calls: 3, failed: 1, running: 0, tools: [{ tool: 'bash', calls: 3 }], idle_before_s: null, rule: 'tool_gap' },
      { kind: 'status', at: '2026-10-29T12:30:00Z', from: 'failed', to: 'running', restart: true, rule: 'event_stream' }] },
    { key: 'handed_off', title: 'Handed off', state: 'none', reason: 'No handoff from this agent is recorded.', items: [] },
    { key: 'produced', title: 'Produced', state: 'recorded', reason: '', items: [
      { kind: 'commit', sha: 'abcdef1234567890', run_number: 4, ci_url: 'javascript:alert(1)', rule: 'branch_window' },
      { kind: 'pull_request', number: 7, status: 'merged', url: 'http://insecure/x', trail: '#/pr/org/app/7?host=github.com', rule: 'session_commits' }] },
    { key: 'outcome', title: 'Outcome', state: 'recorded', reason: '', pr_signals_unreadable: true, items: [
      { kind: 'status', value: 'finished', last_reported_status: 'finished', at: '2026-10-29T13:00:00Z', rule: 'last_report' },
      { kind: 'pull_request_outcome', ref: 'org/app#7', status: 'merged', trail_lists_this_agent: false, signals: null, caveat: 'The pull request trail does not list this agent, so its signals are not carried here.', rule: 'pr_trail' }] },
    { key: 'cost', title: 'Cost', state: 'unknown', reason: 'No price table is configured.', items: [] },
  ],
  hints: { same_mission_sessions: [{ session_id: 's<2>', name: 'Other' }] }, derived: { retries: 'a repeat of an errored call' },
};

test('an unknown value says unknown with its reason, and a known zero is not unknown', () => {
  const cards = recordCards(record);
  assert.deepEqual(cards.find((c) => c.key === 'tool_calls_retried'), { key: 'tool_calls_retried', label: 'Retried tool calls', known: false, value: 'unknown', reason: 'arguments withheld' });
  assert.equal(cards.find((c) => c.key === 'tool_calls_failed').value, '0');
  assert.equal(cards.find((c) => c.key === 'cost_usd_estimate').value, '<$0.01');
  assert.equal(cards.find((c) => c.key === 'prs').value, '1');
  assert.equal(recordCards({}).every((c) => !c.known && c.value === 'unknown'), true);
});

test('the page shows every phase in order, with reasons for the ones without evidence', () => {
  const html = renderEvolution(data);
  const order = [...html.matchAll(/data-phase="([a-z_]+)"/g)].map((m) => m[1]);
  assert.deepEqual(order, ['assigned', 'worked', 'handed_off', 'produced', 'outcome', 'cost']);
  assert.match(html, /nothing recorded/);
  assert.match(html, /No handoff from this agent is recorded\./);
  assert.match(html, /3 more not listed\./);
  assert.match(html, /2 tool calls/);
  assert.match(html, /data-ev-sources>Not all records could be read — tools: partial/);
  assert.match(html, /data-ev-derived/);
  assert.match(html, /\(restart, derived\)/);
});

test('everything from the server is escaped and only https or in-app links are made', () => {
  const html = renderEvolution(data);
  assert.doesNotMatch(html, /<b>Coder<\/b>/);
  assert.doesNotMatch(html, /<img src=x/);
  assert.doesNotMatch(html, /href="javascript:/i);
  assert.doesNotMatch(html, /href="http:\/\/insecure/);
  assert.match(html, /href="#\/pr\/org\/app\/7\?host=github\.com"/);
  assert.match(html, /href="#\/herd\/session\/a%2Fb"/);
  assert.match(html, /href="#\/missions\/m%201"/);
  assert.match(html, /href="#\/herd\/session\/s%3C2%3E"/);
});

test('a pull request trail that does not list this agent is flagged', () => {
  assert.match(renderItem(data.phases[4].items[1]), /does not list this agent/);
});

test('a problem handoff, failed run and restart are marked as problems', () => {
  assert.match(renderItem({ kind: 'handoff_out', problem: true, state: 'rejected', from: {}, to: {} }), /data-problem="true"/);
  assert.match(renderItem(data.phases[1].items[0]), /data-problem="true"/);
  assert.match(renderItem(data.phases[1].items[1]), /data-problem="true"/);
  assert.match(renderItem({ kind: 'ci_run', state: 'success' }), /data-problem="false"/);
});

test('an odd or missing body does not throw', () => {
  for (const body of [null, undefined, 5, 'x', [], { phases: 'no', learning_record: 5 }, { phases: [null, 3, { items: [null, 'x', { kind: 'nope' }] }] }]) {
    assert.doesNotThrow(() => renderEvolution(body));
  }
  assert.deepEqual(evolutionModel(null).phases, []);
});

test('the tabs are a proper tablist and arrows, Home and End move between them', () => {
  const html = tabsHtml('evolution');
  assert.match(html, /role="tablist"/);
  assert.match(html, /id="agent-tab-evolution" aria-controls="agent-panel-evolution" aria-selected="true" tabindex="0"/);
  assert.match(html, /id="agent-tab-overview" aria-controls="agent-panel-overview" aria-selected="false" tabindex="-1"/);
  const handlers = {}, panels = { overview: { hidden: false }, evolution: { hidden: true } };
  const mk = (key) => ({ dataset: { agentTab: key }, tabIndex: 0, attrs: {}, setAttribute(k, v) { this.attrs[k] = v; }, focus() { this.focused = true; }, addEventListener(type, fn) { handlers[`${key}:${type}`] = fn; } });
  const tabs = [mk('overview'), mk('evolution')];
  const root = { querySelectorAll: () => tabs, querySelector: (sel) => panels[sel.replace('#agent-panel-', '')] };
  const picked = [], queries = [];
  wireTabs(root, { onSelect: (k) => picked.push(k), setQuery: (k) => queries.push(k) });
  handlers['overview:keydown']({ key: 'ArrowRight', preventDefault() {} });
  assert.deepEqual(picked, ['evolution']);
  assert.equal(panels.evolution.hidden, false);
  assert.equal(panels.overview.hidden, true);
  assert.equal(tabs[1].attrs['aria-selected'], 'true');
  assert.equal(tabs[1].tabIndex, 0);
  assert.equal(tabs[0].tabIndex, -1);
  assert.equal(tabs[1].focused, true);
  handlers['evolution:keydown']({ key: 'ArrowRight', preventDefault() {} });
  assert.deepEqual(picked, ['evolution', 'overview']);
  handlers['overview:keydown']({ key: 'End', preventDefault() {} });
  handlers['evolution:keydown']({ key: 'Home', preventDefault() {} });
  assert.deepEqual(picked.slice(-2), ['evolution', 'overview']);
  handlers['overview:click']();
  assert.equal(queries.at(-1), 'overview');
});


test('withheld signals show the caveat and no cards; a hint without a session id is not a link', () => {
  const html = renderItem({ kind: 'pull_request_outcome', ref: 'o/a#1', status: 'merged', signals: null, caveat: 'Not listed.', trail_lists_this_agent: false });
  assert.match(html, /data-ev-caveat/);
  assert.doesNotMatch(html, /data-signal=/);
  const page = renderEvolution({ hints: { same_mission_sessions: [{ name: 'nameless' }] }, phases: [], learning_record: {} });
  assert.doesNotMatch(page, /href=""/);
  assert.match(page, /nameless/);
});

test('a stale outcome is labelled as reported, not as an outcome; a self handoff and a handoff out read correctly', () => {
  const html = renderEvolution({ phases: [{ key: 'outcome', title: 'Outcome', state: 'recorded', items: [{ kind: 'status', value: 'running', last_reported_status: 'running', stale: true }] }], learning_record: {} });
  assert.match(html, /Reported/);
  assert.match(html, /not counted as an outcome/);
  assert.doesNotMatch(html, /Handed on/);
  assert.match(renderItem({ kind: 'handoff_out', self_handoff: true, state: 'accepted', from: {}, to: {} }), /to itself/);
});

test('tab panels are focusable only through the page markup; arrow keys move focus and the tablist keeps one tab stop', () => {
  const html = tabsHtml('overview');
  assert.match(html, /tabindex="0"[^>]*data-agent-tab="overview"/);
  assert.match(html, /tabindex="-1"[^>]*data-agent-tab="evolution"/);
});

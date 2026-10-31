import test from 'node:test';
import assert from 'node:assert/strict';

import {
  createSupervisionTreeState,
  mountSupervisionTree,
  normalizeSupervisionTree,
  renderSupervisionTree,
  updateSupervisionTreeState,
} from '../supervision-tree.js';

const projection = {
  room_id: 'room-1',
  workspace_id: 'workspace-1',
  jobs: [{
    job_id: 'job-1', tab_id: 'tab-1', root_agent_id: 'lead',
    agents: [
      {agent_id: 'lead', supervisor_id: null, name: 'Lead', role: 'supervisor', responsibility: 'Integrate results', status: 'running', health: 'healthy', used_tokens: 25, token_limit: 100, progress: 2, evidence_ref: 'ev:root'},
      {agent_id: 'review', supervisor_id: 'lead', name: 'Reviewer', role: 'reviewer', responsibility: 'Review implementation', status: 'running', health: 'healthy', used_tokens: 10, token_limit: 30, progress: 1, evidence_ref: 'https://example.invalid/not-a-link', binding: {adapter: 'pi', pane_id: 'w:p2'}},
      {agent_id: 'worker', supervisor_id: 'lead', name: 'Worker', role: 'executor', responsibility: 'Build parser', status: 'queued', health: 'queued', used_tokens: 0, token_limit: 40, progress: 0},
    ],
  }],
  unassigned: [],
};

test('normalizes only explicit stable supervision edges', () => {
  const model = normalizeSupervisionTree(projection);
  assert.equal(model.jobs[0].root.agent_id, 'lead');
  assert.deepEqual(model.jobs[0].root.children.map(n => n.agent_id), ['review', 'worker']);
  assert.equal(model.jobs[0].agentsById.get('review').binding.pane_id, 'w:p2');
});

test('unknown parents and cycles stay unassigned instead of becoming roots', () => {
  const model = normalizeSupervisionTree({
    ...projection,
    jobs: [{...projection.jobs[0], agents: [
      ...projection.jobs[0].agents,
      {agent_id: 'unknown', supervisor_id: 'missing', name: 'Unknown'},
      {agent_id: 'cycle-a', supervisor_id: 'cycle-b', name: 'A'},
      {agent_id: 'cycle-b', supervisor_id: 'cycle-a', name: 'B'},
    ]}],
    unassigned: [{agent_id: 'legacy', name: 'Legacy agent', provenance: 'unknown'}],
  });
  assert.deepEqual(model.jobs[0].root.children.map(n => n.agent_id), ['review', 'worker']);
  assert.deepEqual(model.unassigned.map(n => n.agent_id).sort(), ['cycle-a', 'cycle-b', 'legacy', 'unknown']);
  assert.equal(model.unassigned.find(n => n.agent_id === 'unknown').reason, 'unknown_parent');
  assert.equal(model.unassigned.find(n => n.agent_id === 'cycle-a').reason, 'cycle');
});

test('renders collapsed jobs, semantic disclosures and opaque evidence text', () => {
  const html = renderSupervisionTree(projection, {selectedId: 'review'});
  assert.match(html, /<details class="sv-job" data-job-id="job-1"[^>]*>/);
  assert.doesNotMatch(html, /data-job-id="job-1"[^>]* open/);
  assert.match(html, /Reviewer/);
  assert.match(html, /Evidence: https:\/\/example\.invalid\/not-a-link/);
  assert.doesNotMatch(html, /href="https:\/\/example\.invalid/);
  assert.match(html, /aria-label="Select Reviewer"/);
});

test('escapes hostile identifiers, labels, responsibilities and evidence', () => {
  const hostile = structuredClone(projection);
  hostile.jobs[0].job_id = 'job\"><script>alert(1)</script>';
  hostile.jobs[0].agents[0].name = '<img src=x onerror=alert(1)>';
  hostile.jobs[0].agents[0].responsibility = '<script>bad()</script>';
  hostile.jobs[0].agents[0].evidence_ref = '" onclick="bad()';
  const html = renderSupervisionTree(hostile, {expanded: new Set([hostile.jobs[0].job_id])});
  assert.doesNotMatch(html, /<script>|<img|onclick="bad/);
  assert.match(html, /&lt;img src=x onerror=alert\(1\)&gt;/);
  assert.match(html, /&lt;script&gt;bad\(\)&lt;\/script&gt;/);
});

test('search matching a child reveals its ancestor chain and excludes siblings', () => {
  const html = renderSupervisionTree(projection, {query: 'review implementation'});
  assert.match(html, /data-job-id="job-1"[^>]* open/);
  assert.match(html, /Lead/);
  assert.match(html, /Reviewer/);
  assert.doesNotMatch(html, />Worker</);
});

test('selected agent shows bounded operational detail without controls', () => {
  const html = renderSupervisionTree(projection, {selectedId: 'review', expanded: new Set(['job-1'])});
  assert.match(html, /Agent details/);
  assert.match(html, /10 \/ 30 tokens/);
  assert.match(html, /<dt>Adapter<\/dt><dd>pi<\/dd>/);
  assert.match(html, /<dt>Pane id<\/dt><dd>w:p2<\/dd>/);
  assert.doesNotMatch(html, /Launch|Cancel|Stop/);
});

test('render cap is explicit and traversal remains bounded', () => {
  const agents = [{agent_id: 'lead', supervisor_id: null, name: 'Lead'}];
  for (let i = 0; i < 50; i += 1) agents.push({agent_id: `a-${i}`, supervisor_id: 'lead', name: `Agent ${i}`});
  const html = renderSupervisionTree({...projection, jobs: [{...projection.jobs[0], agents}]}, {limit: 10, query: 'agent'});
  assert.match(html, /Showing 10 of 51 agents/);
  assert.ok((html.match(/data-agent-id=/g) || []).length <= 10);
});

test('unknown provenance has a separate disclosure and no invented parent', () => {
  const html = renderSupervisionTree({...projection, unassigned: [{agent_id: 'legacy', name: 'Legacy', provenance: 'unknown'}]});
  assert.match(html, /Unknown or unadopted provenance/);
  assert.match(html, /Legacy/);
  assert.doesNotMatch(html, /supervisor_id.*lead/);
});

test('state updates preserve expansion and selection across refreshed data', () => {
  const initial = createSupervisionTreeState({expanded: ['job-1', 'lead'], selectedId: 'review'});
  const searched = updateSupervisionTreeState(initial, {query: 'review'});
  const refreshed = updateSupervisionTreeState(searched, {data: structuredClone(projection)});
  assert.deepEqual([...refreshed.expanded].sort(), ['job-1', 'lead']);
  assert.equal(refreshed.selectedId, 'review');
  assert.equal(refreshed.query, 'review');
});

test('accepts the canonical nested projection contract', () => {
  const nested = structuredClone(projection);
  const reviewer = nested.jobs[0].agents[1];
  delete reviewer.responsibility;
  delete reviewer.used_tokens;
  delete reviewer.token_limit;
  delete reviewer.progress;
  delete reviewer.evidence_ref;
  delete reviewer.binding;
  reviewer.responsibilities = [{task_id: 't1', status: 'accepted', text: 'Review parser'}];
  reviewer.budget = {used_tokens: 12, token_limit: 31};
  reviewer.checkpoint = {progress: 4, evidence_ref: 'ev:4'};
  reviewer.runtime = {adapter: 'codex', pane_id: 'w:p9'};
  const model = normalizeSupervisionTree(nested);
  const agent = model.jobs[0].agentsById.get('review');
  assert.equal(agent.responsibility, 'Review parser');
  assert.equal(agent.used_tokens, 12);
  assert.equal(agent.progress, 4);
  assert.equal(agent.binding.pane_id, 'w:p9');
});

test('shared-room responsibilities retain task identity and status when text is hidden', () => {
  const shared = structuredClone(projection);
  delete shared.jobs[0].agents[1].responsibility;
  shared.jobs[0].agents[1].responsibilities = [{task_id: 'task-7', status: 'accepted'}];
  const agent = normalizeSupervisionTree(shared).jobs[0].agentsById.get('review');
  assert.equal(agent.responsibility, 'task-7 · accepted');
});

test('a stable collaboration agent identity appears only once across jobs and unknown records', () => {
  const duplicate = structuredClone(projection.jobs[0].agents[1]);
  const model = normalizeSupervisionTree({
    ...projection,
    jobs: [projection.jobs[0], {job_id: 'job-2', tab_id: 'tab-2', root_agent_id: 'review', agents: [duplicate]}],
    unassigned: [duplicate],
  });
  const ids = [...model.jobs.flatMap(job => job.agents), ...model.unassigned].map(agent => agent.agent_id);
  assert.equal(ids.filter(id => id === 'review').length, 1);
});

test('identical polling data does not replace the tree DOM', () => {
  const listeners = new Map();
  let writes = 0;
  const container = {
    contains: () => false,
    querySelector: () => null,
    querySelectorAll: () => [],
    addEventListener: (name, handler) => listeners.set(name, handler),
    removeEventListener: name => listeners.delete(name),
    set innerHTML(value) { this.html = value; writes += 1; },
    get innerHTML() { return this.html; },
  };
  const mounted = mountSupervisionTree(container, projection);
  mounted.update(structuredClone(projection));
  assert.equal(writes, 1);
  mounted.dispose();
  assert.equal(listeners.size, 0);
});

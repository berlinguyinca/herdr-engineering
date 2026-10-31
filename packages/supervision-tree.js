// Shared Collaboration / Agents / Assistants supervision tree.
// Parentage comes only from durable agent_id + supervisor_id records. Runtime
// bindings and visual layout are display facts and never create tree edges.
import {escapeHtml as esc} from './herdr-web-components/util.js';

const DEFAULT_LIMIT = 250;
const MAX_INPUT_AGENTS = 2000;

const records = value => Array.isArray(value)
  ? value.filter(item => item && typeof item === 'object' && !Array.isArray(item))
  : [];
const text = value => typeof value === 'string' ? value.trim() : '';
const identity = value => {
  const result = text(value);
  return result && result.length <= 512 && !/[\u0000-\u001f\u007f]/.test(result) ? result : '';
};
const number = value => typeof value === 'number' && Number.isFinite(value) ? value : null;
const queryText = node => [node.agent_id, node.name, node.role, node.responsibility, node.status, node.health]
  .map(text).join(' ').toLocaleLowerCase();

function copyAgent(raw, reason = null) {
  const responsibilities = records(raw.responsibilities).map(item => text(item.text)
    || [text(item.task_id), text(item.status)].filter(Boolean).join(' · ')).filter(Boolean);
  const budget = raw.budget && typeof raw.budget === 'object' ? raw.budget : {};
  const checkpoint = raw.checkpoint && typeof raw.checkpoint === 'object' ? raw.checkpoint : {};
  const runtime = raw.runtime && typeof raw.runtime === 'object' ? raw.runtime : raw.binding;
  return {
    agent_id: identity(raw.agent_id),
    supervisor_id: identity(raw.supervisor_id) || null,
    name: text(raw.name) || identity(raw.agent_id) || 'Unknown agent',
    role: text(raw.role),
    responsibility: text(raw.responsibility) || responsibilities.slice(0, 3).join(' · '),
    status: text(raw.status),
    health: text(raw.health),
    used_tokens: number(raw.used_tokens) ?? number(budget.used_tokens),
    token_limit: number(raw.token_limit) ?? number(budget.token_limit),
    progress: number(raw.progress) ?? number(checkpoint.progress),
    evidence_ref: text(raw.evidence_ref) || text(checkpoint.evidence_ref),
    binding: runtime && typeof runtime === 'object' && !Array.isArray(runtime)
      ? Object.fromEntries(Object.entries(runtime).filter(([, value]) => typeof value === 'string').map(([key, value]) => [key, value]))
      : null,
    reason: reason || text(raw.reason) || text(raw.provenance) || null,
    children: [],
  };
}

function classifyAgent(agent, rootId, byId) {
  if (agent.agent_id === rootId) return agent.supervisor_id === null ? 'root' : 'invalid_root';
  const visited = new Set([agent.agent_id]);
  let cursor = agent;
  while (cursor.supervisor_id !== null) {
    if (visited.has(cursor.supervisor_id)) return 'cycle';
    visited.add(cursor.supervisor_id);
    cursor = byId.get(cursor.supervisor_id);
    if (!cursor) return 'unknown_parent';
    if (cursor.agent_id === rootId) return 'attached';
  }
  return 'unknown_parent';
}

export function normalizeSupervisionTree(data = {}) {
  const jobs = [];
  const unassigned = [];
  const assigned = new Set();
  const seen = new Set();
  let accepted = 0;
  let ignored = 0;
  for (const rawJob of records(data.jobs)) {
    const jobId = identity(rawJob.job_id);
    const tabId = identity(rawJob.tab_id);
    const rootId = identity(rawJob.root_agent_id);
    if (!jobId || !tabId || !rootId) { ignored += records(rawJob.agents).length; continue; }
    const byId = new Map();
    for (const raw of records(rawJob.agents)) {
      if (accepted >= MAX_INPUT_AGENTS) { ignored += 1; continue; }
      const agent = copyAgent(raw);
      if (!agent.agent_id || byId.has(agent.agent_id) || seen.has(agent.agent_id)) { ignored += 1; continue; }
      accepted += 1;
      seen.add(agent.agent_id);
      byId.set(agent.agent_id, agent);
    }
    const root = byId.get(rootId);
    const attached = [];
    for (const agent of byId.values()) {
      const reason = classifyAgent(agent, rootId, byId);
      if (reason === 'root' || reason === 'attached') attached.push(agent);
      else unassigned.push({...agent, reason});
    }
    if (!root || classifyAgent(root, rootId, byId) !== 'root') {
      for (const agent of attached) unassigned.push({...agent, reason: 'missing_root'});
      continue;
    }
    const attachedIds = new Set(attached.map(agent => agent.agent_id));
    for (const agent of attached) {
      if (agent.agent_id !== rootId && attachedIds.has(agent.supervisor_id)) {
        byId.get(agent.supervisor_id).children.push(agent);
      }
      assigned.add(agent.agent_id);
    }
    jobs.push({
      job_id: jobId,
      tab_id: tabId,
      root_agent_id: rootId,
      title: text(rawJob.title) || text(rawJob.name) || jobId,
      status: text(rawJob.status),
      root,
      agents: attached,
      agentsById: byId,
    });
  }
  for (const raw of records(data.unassigned)) {
    if (accepted >= MAX_INPUT_AGENTS) { ignored += 1; continue; }
    const agent = copyAgent(raw, text(raw.reason) || text(raw.provenance) || 'unknown');
    if (!agent.agent_id || seen.has(agent.agent_id) || assigned.has(agent.agent_id)) {
      ignored += 1;
      continue;
    }
    accepted += 1;
    seen.add(agent.agent_id);
    unassigned.push(agent);
  }
  return {
    room_id: identity(data.room_id),
    workspace_id: identity(data.workspace_id),
    jobs,
    unassigned,
    total: jobs.reduce((sum, job) => sum + job.agents.length, 0) + unassigned.length,
    ignored,
  };
}

export function createSupervisionTreeState({data = {}, expanded = [], query = '', selectedId = null, limit = DEFAULT_LIMIT} = {}) {
  return {
    data,
    expanded: expanded instanceof Set ? new Set(expanded) : new Set(Array.isArray(expanded) ? expanded.filter(identity) : []),
    query: text(query),
    selectedId: identity(selectedId) || null,
    limit: Number.isInteger(limit) && limit > 0 ? Math.min(limit, MAX_INPUT_AGENTS) : DEFAULT_LIMIT,
  };
}

export function updateSupervisionTreeState(state, patch = {}) {
  const next = {...state, ...patch};
  next.expanded = patch.expanded === undefined
    ? new Set(state.expanded || [])
    : patch.expanded instanceof Set ? new Set(patch.expanded) : new Set(Array.isArray(patch.expanded) ? patch.expanded.filter(identity) : []);
  next.query = text(next.query);
  next.selectedId = identity(next.selectedId) || null;
  next.limit = Number.isInteger(next.limit) && next.limit > 0 ? Math.min(next.limit, MAX_INPUT_AGENTS) : DEFAULT_LIMIT;
  return next;
}

function matchesTree(node, query) {
  if (!query) return true;
  if (queryText(node).includes(query)) return true;
  return node.children.some(child => matchesTree(child, query));
}

function countMatches(node, query) {
  if (!matchesTree(node, query)) return 0;
  return 1 + node.children.reduce((sum, child) => sum + countMatches(child, query), 0);
}

function statusText(agent) {
  return [agent.role, agent.status, agent.health].filter(Boolean).join(' · ') || 'Status not reported';
}

function agentCard(agent, state, context, {branch = false} = {}) {
  if (context.rendered >= state.limit) return '';
  if (!matchesTree(agent, context.query)) return '';
  context.rendered += 1;
  const children = agent.children.filter(child => matchesTree(child, context.query));
  const open = context.query || state.expanded.has(agent.agent_id);
  const name = esc(agent.name);
  const summary = `<span class="sv-agent-main"><strong>${name}</strong><span>${esc(statusText(agent))}</span>${agent.responsibility ? `<small>${esc(agent.responsibility)}</small>` : ''}</span>`;
  const inspect = `<button type="button" class="sv-inspect" data-select-agent="${esc(agent.agent_id)}" aria-label="Select ${name}">Inspect</button>`;
  const body = children.length
    ? `<ul class="sv-children">${children.map(child => agentCard(child, state, context, {branch: child.children.length > 0})).join('')}</ul>`
    : '';
  if (children.length || branch) {
    return `<li class="sv-agent" data-agent-id="${esc(agent.agent_id)}"><details class="sv-branch" data-expand-id="${esc(agent.agent_id)}"${open ? ' open' : ''}><summary>${summary}</summary>${inspect}${body}</details></li>`;
  }
  return `<li class="sv-agent sv-agent--leaf" data-agent-id="${esc(agent.agent_id)}"><div class="sv-leaf">${summary}${inspect}</div></li>`;
}

function findAgent(model, id) {
  if (!id) return null;
  for (const job of model.jobs) {
    const found = job.agentsById.get(id);
    if (found) return found;
  }
  return model.unassigned.find(agent => agent.agent_id === id) || null;
}

function detailPanel(agent) {
  if (!agent) return '';
  const tokenUsage = agent.used_tokens !== null || agent.token_limit !== null
    ? `${agent.used_tokens ?? 'unknown'} / ${agent.token_limit ?? 'unknown'} tokens`
    : 'Token usage not reported';
  const binding = agent.binding ? Object.entries(agent.binding)
    .filter(([, value]) => text(value))
    .map(([key, value]) => `<dt>${esc(key.replaceAll('_', ' ').replace(/^./, c => c.toUpperCase()))}</dt><dd>${esc(value)}</dd>`).join('') : '';
  return `<aside class="sv-detail" aria-label="Agent details"><h3>Agent details</h3><dl><dt>Agent ID</dt><dd>${esc(agent.agent_id)}</dd><dt>Role</dt><dd>${esc(agent.role || 'Not reported')}</dd><dt>Responsibility</dt><dd>${esc(agent.responsibility || 'Not reported')}</dd><dt>Status</dt><dd>${esc(agent.status || 'Not reported')}</dd><dt>Health</dt><dd>${esc(agent.health || 'Not reported')}</dd><dt>Budget</dt><dd>${esc(tokenUsage)}</dd><dt>Checkpoint</dt><dd>${esc(agent.progress === null ? 'Not reported' : String(agent.progress))}</dd>${agent.evidence_ref ? `<dt>Evidence</dt><dd>Evidence: ${esc(agent.evidence_ref)}</dd>` : ''}${binding}</dl></aside>`;
}

const styles = `<style>
.sv-root{min-width:0}.sv-toolbar{display:flex;flex-wrap:wrap;gap:10px;align-items:end;margin:10px 0}.sv-toolbar label{display:flex;flex:1;min-width:180px;flex-direction:column;gap:4px;font-size:12px}.sv-toolbar input{box-sizing:border-box;max-width:100%;padding:7px 9px;border:1px solid var(--border);border-radius:var(--radius-sm);background:var(--surface);color:var(--text);font:inherit}.sv-context,.sv-count{color:var(--text-secondary);font-size:12px;overflow-wrap:anywhere}.sv-job,.sv-unknown{margin:8px 0;border:1px solid var(--border);border-radius:var(--radius);background:var(--surface);overflow:hidden}.sv-job>summary,.sv-unknown>summary,.sv-branch>summary{cursor:pointer;overflow-wrap:anywhere}.sv-job>summary,.sv-unknown>summary{padding:10px 12px;font-weight:600}.sv-job-meta{font-weight:400;color:var(--text-secondary);font-size:12px}.sv-agents,.sv-children,.sv-unassigned{list-style:none;margin:0;padding:0 0 0 18px}.sv-agents{padding:0 12px 12px}.sv-agent{min-width:0;margin:6px 0;border-left:2px solid var(--border-subtle);padding-left:8px}.sv-branch>summary,.sv-leaf{display:flex;min-width:0;gap:8px;align-items:start;padding:6px}.sv-agent-main{display:flex;min-width:0;flex:1;flex-direction:column;gap:2px;overflow-wrap:anywhere}.sv-agent-main span,.sv-agent-main small{color:var(--text-secondary);font-size:12px}.sv-inspect{margin:4px 6px;padding:5px 8px;border:1px solid var(--border);border-radius:var(--radius-sm);background:var(--surface-2);color:var(--text);cursor:pointer}.sv-detail{min-width:0;margin:12px 0;padding:12px;border:1px solid var(--border);border-radius:var(--radius);background:var(--surface)}.sv-detail h3{margin:0 0 8px}.sv-detail dl{display:grid;grid-template-columns:minmax(100px,auto) minmax(0,1fr);gap:6px 12px;margin:0}.sv-detail dt{color:var(--text-secondary)}.sv-detail dd{margin:0;overflow-wrap:anywhere;word-break:break-word}.sv-truncated{color:var(--text-secondary);font-size:12px}.sv-job:focus-within,.sv-unknown:focus-within{border-color:var(--accent)}
@media(max-width:650px){.sv-agents,.sv-children,.sv-unassigned{padding-left:8px}.sv-detail dl{display:block}.sv-detail dd{margin:2px 0 9px}.sv-branch>summary,.sv-leaf{align-items:stretch;flex-direction:column}.sv-inspect{align-self:start}}
</style>`;

export function renderSupervisionTree(data = {}, options = {}) {
  const state = createSupervisionTreeState({...options, data});
  const model = normalizeSupervisionTree(data);
  const context = {query: state.query.toLocaleLowerCase(), rendered: 0};
  const eligible = model.jobs.reduce((sum, job) => sum + countMatches(job.root, context.query), 0)
    + model.unassigned.filter(agent => !context.query || queryText(agent).includes(context.query)).length;
  const jobs = model.jobs.map(job => {
    if (context.query && !matchesTree(job.root, context.query) && ![job.job_id, job.tab_id, job.title].join(' ').toLocaleLowerCase().includes(context.query)) return '';
    const open = context.query || state.expanded.has(job.job_id);
    const agents = agentCard(job.root, state, context, {branch: job.root.children.length > 0});
    return `<details class="sv-job" data-job-id="${esc(job.job_id)}" data-expand-id="${esc(job.job_id)}"${open ? ' open' : ''}><summary>${esc(job.title)} <span class="sv-job-meta">Tab ${esc(job.tab_id)} · ${job.agents.length} agent${job.agents.length === 1 ? '' : 's'}</span></summary><ul class="sv-agents">${agents}</ul></details>`;
  }).join('');
  const unknownMatches = model.unassigned.filter(agent => !context.query || queryText(agent).includes(context.query));
  const unknown = unknownMatches.length ? `<details class="sv-unknown" data-expand-id="unknown"${context.query || state.expanded.has('unknown') ? ' open' : ''}><summary>Unknown or unadopted provenance <span class="sv-job-meta">${unknownMatches.length} agent${unknownMatches.length === 1 ? '' : 's'}</span></summary><ul class="sv-unassigned">${unknownMatches.map(agent => agentCard(agent, state, context)).join('')}</ul></details>` : '';
  const hidden = Math.max(0, eligible - context.rendered);
  const shownTotal = context.query ? eligible : model.total;
  const count = hidden > 0 ? `<p class="sv-truncated">Showing ${context.rendered} of ${eligible} agents. Narrow the search to inspect more.</p>` : `<p class="sv-count">${context.query ? `${eligible} matching` : model.total} agent${shownTotal === 1 ? '' : 's'} across ${model.jobs.length} job${model.jobs.length === 1 ? '' : 's'}.</p>`;
  const empty = !jobs && !unknown ? '<p class="sv-count">No supervision records match this view.</p>' : '';
  const selected = findAgent(model, state.selectedId);
  return `${styles}<section class="sv-root" aria-label="Agent supervision tree"><div class="sv-toolbar"><label>Search agent tree<input type="search" data-supervision-search value="${esc(state.query)}" placeholder="Agent, role or responsibility"></label><span class="sv-context">Workspace ${esc(model.workspace_id || 'not linked')} · Room ${esc(model.room_id || 'not linked')}</span></div>${count}${empty}${jobs}${unknown}${detailPanel(selected)}</section>`;
}

export function mountSupervisionTree(container, data = {}, options = {}) {
  let state = createSupervisionTreeState({...options, data});
  let dataSignature = JSON.stringify(data);
  const focusKey = () => {
    const active = globalThis.document?.activeElement;
    if (!active || !container.contains?.(active)) return null;
    if (active.matches?.('[data-supervision-search]')) return {type: 'search', cursor: active.selectionStart};
    const button = active.closest?.('[data-select-agent]');
    if (button) return {type: 'agent', id: button.dataset.selectAgent};
    const details = active.closest?.('details[data-expand-id]');
    if (details && active.tagName === 'SUMMARY') return {type: 'disclosure', id: details.dataset.expandId};
    return null;
  };
  const restoreFocus = key => {
    if (!key) return;
    if (key.type === 'search') {
      const input = container.querySelector('[data-supervision-search]'); input?.focus();
      if (Number.isInteger(key.cursor)) input?.setSelectionRange(key.cursor, key.cursor);
      return;
    }
    if (key.type === 'agent') {
      const button = [...container.querySelectorAll('[data-select-agent]')].find(node => node.dataset.selectAgent === key.id);
      button?.focus(); return;
    }
    const details = [...container.querySelectorAll('details[data-expand-id]')].find(node => node.dataset.expandId === key.id);
    details?.querySelector(':scope > summary')?.focus();
  };
  const draw = () => { const key = focusKey(); container.innerHTML = renderSupervisionTree(state.data, state); restoreFocus(key); };
  const onInput = event => {
    if (!event.target.matches('[data-supervision-search]')) return;
    const cursor = event.target.selectionStart;
    state = updateSupervisionTreeState(state, {query: event.target.value});
    draw();
    const input = container.querySelector('[data-supervision-search]');
    input?.focus();
    if (Number.isInteger(cursor)) input?.setSelectionRange(cursor, cursor);
  };
  const onClick = event => {
    const button = event.target.closest('[data-select-agent]');
    if (!button) return;
    state = updateSupervisionTreeState(state, {selectedId: button.dataset.selectAgent});
    draw();
  };
  const onToggle = event => {
    const details = event.target.closest?.('details[data-expand-id]');
    if (!details || event.target !== details) return;
    const expanded = new Set(state.expanded);
    if (details.open) expanded.add(details.dataset.expandId); else expanded.delete(details.dataset.expandId);
    state = updateSupervisionTreeState(state, {expanded});
  };
  container.addEventListener('input', onInput);
  container.addEventListener('click', onClick);
  container.addEventListener('toggle', onToggle, true);
  draw();
  return {
    update(nextData, patch = {}) {
      const signature = JSON.stringify(nextData);
      if (signature === dataSignature && !Object.keys(patch).length) return;
      dataSignature = signature;
      state = updateSupervisionTreeState(state, {data: nextData, ...patch}); draw();
    },
    state() {
      const expanded = new Set(state.expanded);
      for (const details of container.querySelectorAll('details[data-expand-id]')) {
        if (details.open) expanded.add(details.dataset.expandId);
        else expanded.delete(details.dataset.expandId);
      }
      return {expanded, query: state.query, selectedId: state.selectedId, limit: state.limit};
    },
    dispose() {
      container.removeEventListener('input', onInput);
      container.removeEventListener('click', onClick);
      container.removeEventListener('toggle', onToggle, true);
    },
  };
}

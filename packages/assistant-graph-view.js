// The assistant graph on the Personal Assistant page: you -> your assistants -> what they delegated -> the specialist it
// runs on. Read-only; it shows what exists and what delegation could use, and changes nothing. Plain functions returning
// HTML so the states run under node.
import { escapeHtml as esc } from './herdr-web-components/util.js';
import { skeleton } from './motion.js';

const text = (v) => (typeof v === 'string' ? v : v == null ? '' : String(v));
const list = (v) => (Array.isArray(v) ? v : []);

/** { status: 'ok' | 'off' | 'error', nodes, edges, message } */
export async function loadAssistantGraph(client, { signal } = {}) {
  try {
    const graph = await client.assistantGraph({ signal });
    return { status: 'ok', nodes: list(graph?.nodes), edges: list(graph?.edges) };
  } catch (err) {
    if (err?.status === 404 || err?.status === 503) return { status: 'off' };
    return { status: 'error', message: text(err?.message) || 'request failed' };
  }
}

const chip = (cls, label) => `<span class="pa-graph-chip pa-graph-chip--${cls}">${esc(label)}</span>`;

function renderDelegation(node, specialist) {
  return `<li class="pa-graph-node" data-graph-kind="delegation" data-graph-id="${esc(node.id)}">`
    + `${chip(text(node.status) || 'unknown', text(node.status) || 'unknown')} ${esc(text(node.name) || 'Task')}`
    + `${specialist ? ` <span class="pa-muted">on ${esc(specialist)}</span>` : ''}</li>`;
}

function renderAssistant(node, delegations) {
  const scopes = list(node.delegate_scopes);
  const can = node.can_delegate
    ? chip('ok', `can delegate${scopes.length ? `: ${scopes.join(', ')}` : ''}`)
    : chip('off', 'cannot delegate yet');
  return `<li class="pa-graph-node" data-graph-kind="assistant" data-graph-id="${esc(node.id)}">`
    + `<strong>${esc(text(node.name))}</strong> ${chip(text(node.status) || 'unknown', text(node.status) || 'unknown')} ${can}`
    + (delegations.length ? `<ul class="pa-graph-list">${delegations.join('')}</ul>` : '') + '</li>';
}

/** The whole body for a state. Pure so the states can be tested without a page. */
export function renderAssistantGraph(state) {
  const msg = (kind, html) => `<p class="pa-muted" data-graph-state="${kind}" role="status">${html}</p>`;
  if (state.phase === 'loading') return skeleton('graph', { rows: 3, height: 40 });
  if (state.phase === 'off') return msg('off', 'The assistant graph is not available on this control plane.');
  if (state.phase === 'error') return msg('error', `The assistant graph could not be loaded: ${esc(state.message || 'request failed')}`);
  const nodes = list(state.nodes);
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const edges = list(state.edges);
  const runsOn = new Map(edges.filter((e) => e.kind === 'runs_on').map((e) => [e.from, byId.get(e.to)]));
  const delegationsOf = (id) => edges.filter((e) => e.kind === 'delegated' && e.from === id)
    .map((e) => byId.get(e.to)).filter(Boolean)
    .map((d) => renderDelegation(d, text(runsOn.get(d.id)?.name)));
  const assistants = nodes.filter((n) => n.kind === 'assistant');
  const specialists = nodes.filter((n) => n.kind === 'specialist');
  const tree = assistants.length
    ? `<ul class="pa-graph-list" role="tree">${assistants.map((a) => renderAssistant(a, delegationsOf(a.id))).join('')}</ul>`
    : msg('empty', 'No assistants yet.');
  const spec = specialists.map((s) => `<li class="pa-graph-node" data-graph-kind="specialist" data-graph-id="${esc(s.id)}">`
    + `<strong>${esc(text(s.name))}</strong> ${s.bound ? chip('ok', 'ready') : chip('off', 'not set up')}`
    + `${s.role ? ` <span class="pa-muted">${esc(text(s.role))}</span>` : ''}</li>`).join('');
  return `<div class="pa-graph"><h3 class="pa-graph-title">You</h3>${tree}`
    + `<h3 class="pa-graph-title">Specialists</h3>${spec ? `<ul class="pa-graph-list">${spec}</ul>` : msg('none', 'No specialists are configured.')}</div>`;
}

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

// Where a specialist would run now. "planned" is the resolver's current choice, never a claim that it is running there.
function renderPlacement(node, byId) {
  const p = node.placement;
  if (!p) return '';
  if (p.state === 'planned') {
    const host = byId.get(p.host);
    return ` ${chip('ok', `planned: ${text(host?.name) || text(p.host)}`)}`
      + (host?.backend === 'slurm' ? ` ${chip('off', 'slurm')}` : '');
  }
  const reasons = list(p.reasons).slice(0, 3).map((r) => esc(text(r))).join('; ');
  return ` ${chip('failed', "can't be placed")}${reasons ? ` <span class="pa-muted" data-graph-reasons>${reasons}</span>` : ''}`;
}

// What a specialist still needs, step by step; nothing is shown for a finished or non-specialist assistant.
function renderSetup(node) {
  const open = list(node.setup).filter((st) => st && st.state !== 'done');
  if (!open.length) return '';
  return `<ul class="pa-graph-setup" data-graph-setup>${open.map((st) =>
    `<li data-setup-step="${esc(text(st.id))}">${chip(st.state === 'blocked' ? 'error' : 'off', text(st.state))} ${esc(text(st.detail))}</li>`).join('')}</ul>`;
}

function renderAssistant(node, inner, byId) {
  const scopes = list(node.delegate_scopes);
  const can = node.can_delegate
    ? chip('ok', `can delegate${scopes.length ? `: ${scopes.join(', ')}` : ''}`)
    : chip('off', 'cannot delegate yet');
  const kind = text(node.type) && node.type !== 'rakazo' ? ` ${chip('off', text(node.type))}` : '';
  return `<li class="pa-graph-node" data-graph-kind="assistant" data-graph-id="${esc(node.id)}">`
    + `<strong>${esc(text(node.name))}</strong>${kind} ${chip(text(node.status) || 'unknown', text(node.status) || 'unknown')} ${can}`
    + renderSetup(node)
    + renderPlacement(node, byId)
    + (inner.length ? `<ul class="pa-graph-list">${inner.join('')}</ul>` : '') + '</li>';
}

function renderHost(node) {
  const caps = list(node.capabilities).map((c) => chip('off', text(c))).join(' ');
  const free = node.free || {};
  const room = [free.cpus ? `${free.cpus} cpu` : '', free.memory_mb ? `${Math.round(free.memory_mb / 1024)} GiB` : '',
    free.gpus ? `${free.gpus} gpu` : ''].filter(Boolean).join(', ');
  return `<li class="pa-graph-node" data-graph-kind="host" data-graph-id="${esc(node.id)}">`
    + `<strong>${esc(text(node.name))}</strong> ${chip(text(node.status) || 'unknown', text(node.status) || 'unknown')}`
    + ` <span class="pa-muted">${esc(text(node.backend) || 'host')}${room ? ` · free: ${esc(room)}` : ''}</span> ${caps}</li>`;
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
  const linkedParent = new Set(edges.filter((e) => e.kind === 'linked' && byId.has(e.from)).map((e) => e.to));
  const seen = new Set();
  // An explicit link nests the child under its parent; what actually ran (delegations) follows the assistant's own links.
  const subtree = (id) => {
    if (seen.has(id)) return '';
    seen.add(id);
    const kids = edges.filter((e) => e.kind === 'linked' && e.from === id).map((e) => byId.get(e.to)).filter(Boolean)
      .map((c) => subtree(c.id));
    const ran = edges.filter((e) => e.kind === 'delegated' && e.from === id).map((e) => byId.get(e.to)).filter(Boolean)
      .map((d) => renderDelegation(d, text(runsOn.get(d.id)?.name)));
    return renderAssistant(byId.get(id), [...kids, ...ran], byId);
  };
  const assistants = nodes.filter((n) => n.kind === 'assistant');
  const roots = assistants.filter((n) => !linkedParent.has(n.id));
  const tree = assistants.length
    ? `<ul class="pa-graph-list" role="tree">${roots.map((a) => subtree(a.id)).join('')}</ul>`
    : msg('empty', 'No assistants yet.');
  const specialists = nodes.filter((n) => n.kind === 'specialist');
  const spec = specialists.map((s) => `<li class="pa-graph-node" data-graph-kind="specialist" data-graph-id="${esc(s.id)}">`
    + `<strong>${esc(text(s.name))}</strong> ${s.bound ? chip('ok', 'ready') : chip('off', 'not set up')}`
    + `${s.role ? ` <span class="pa-muted">${esc(text(s.role))}</span>` : ''}</li>`).join('');
  const hosts = nodes.filter((n) => n.kind === 'host');
  return `<div class="pa-graph"><h3 class="pa-graph-title">You</h3>${tree}`
    + `<h3 class="pa-graph-title">Specialists</h3>${spec ? `<ul class="pa-graph-list">${spec}</ul>` : msg('none', 'No specialists are configured.')}`
    + (hosts.length ? `<h3 class="pa-graph-title">Where agents run</h3><ul class="pa-graph-list">${hosts.map(renderHost).join('')}</ul>` : '')
    + '</div>';
}

// Primary agents as workspace TABS, spawned subagents as PANES, derived ONLY from explicit lineage.
// Pure (DOM-free): the model builder and the renderer return data / escaped HTML and are node-testable.
//
// Rules (docs/contracts/lineage-events.md, "Tabs and panes"):
//  * an `agent:` node with no visible `spawn` parent is a primary agent -> a tab;
//  * an agent with a visible `spawn` parent is a subagent -> a pane under the nearest tab ancestor;
//  * an agent that declares `attrs.presentation = "tab"` (long-lived) or `attrs.external = true` (a remote or
//    coworker agent) gets its OWN tab and keeps its spawn lineage;
//  * nothing is inferred from chat text, names or timing; only `spawn` edges and agent attrs count;
//  * closing a pane is a state of the SURFACE (`tab:`/`pane:` node, `attrs.agent_id`), never of the agent:
//    the agent stays listed with its own reported state;
//  * an external agent has a virtual tab: externally reported status, never a terminal;
//  * the page never offers a terminal from here. `can_terminal` is only what the server said for the workspace;
//  * T10: an external coworker/remote agent is a VIRTUAL tab built from the server's own roster
//    (`GET /rooms/{id}/externals`: invite provenance, presence, revoked/expired state), never from attrs an
//    emitter wrote. Anything the agent reports (activity text, node labels) is shown as untrusted text, escaped,
//    and presence older than the server's stale window is shown as stale, not as live. A revoked agent keeps its tab.
import { escapeHtml as esc } from './herdr-web-components/util.js';

const MAX_DEPTH = 32;
const MAX_AGENTS = 500;
const VIEW_GRANTS = ['tabs', 'panes'];
const STATE_TONE = {
  running: 'var(--status-active, #1f7ae0)', working: 'var(--status-active, #1f7ae0)',
  blocked: 'var(--status-attention, #d89b1d)', waiting: 'var(--status-attention, #d89b1d)',
  done: 'var(--status-ok, #1a9b6c)', failed: 'var(--status-danger, #c0392b)',
  cancelled: 'var(--text-muted, #888)', superseded: 'var(--text-muted, #888)',
  idle: 'var(--text-muted, #888)', stale: 'var(--status-attention, #d89b1d)', revoked: 'var(--status-danger, #c0392b)',
  expired: 'var(--text-muted, #888)', removed: 'var(--text-muted, #888)', 'no heartbeat': 'var(--text-muted, #888)',
};

const attrsOf = (n) => (n && typeof n.attrs === 'object' && n.attrs) || {};
const agentKey = (id) => String(id || '').replace(/^agent:/, '');
const text = (v, fallback = '') => (typeof v === 'string' && v ? v : fallback);

function merge(graphs) {
  const nodes = new Map();
  const edges = new Map();
  for (const g of graphs || []) {
    for (const n of (g && g.nodes) || []) if (n && typeof n.id === 'string' && !nodes.has(n.id)) nodes.set(n.id, n);
    for (const e of (g && g.edges) || []) {
      if (!e || e.removed) continue;
      const k = `${e.edge_type}\u0000${e.from_id}\u0000${e.to_id}`;
      if (!edges.has(k)) edges.set(k, e);
    }
  }
  return { nodes, edges };
}

// Grants per workspace from the room's links (as the server reports them). `null` = links unknown.
function grantsByWorkspace(links) {
  if (!Array.isArray(links)) return null;
  const out = new Map();
  for (const l of links) {
    if (!l || typeof l.workspace_id !== 'string') continue;
    const have = out.get(l.workspace_id) || { tabs: false, panes: false, can_terminal: false };
    for (const g of l.grants || []) if (VIEW_GRANTS.includes(g)) have[g] = true;
    have.can_terminal = have.can_terminal || l.can_terminal === true;
    out.set(l.workspace_id, have);
  }
  return out;
}

const MAX_REPORTED = 20;

// What a virtual tab shows as its state: the server's verdict first (revoked/expired/removed), then live
// presence, and only then "stale" / "no heartbeat". A stale status is never shown as if it were live.
export function externalState(row) {
  if (row.state && row.state !== 'active') return row.state;
  const p = row.presence || {};
  if (p.state === 'online' && typeof p.status === 'string') return p.status;
  if (p.state === 'stale') return 'stale';
  return 'no heartbeat';
}

function externalEntry(row, nodes) {
  const raw = String(row.agent_id || '');
  const reported = [...nodes.values()].filter((n) => {
    if (n.synthetic) return false;       // the placeholder for an agent that has reported nothing
    const rest = String(n.id).slice(String(n.id).indexOf(':') + 1);
    return rest === raw || rest.startsWith(`${raw}.`);
  }).sort((a, b) => (a.id < b.id ? -1 : 1)).slice(0, MAX_REPORTED)
    .map((n) => ({ id: n.id, node_type: text(n.node_type), label: text(n.label, '(no label)'), state: text(n.state, 'unknown') }));
  const p = row.presence || {};
  return {
    agent_id: text(row.node_id, `agent:${raw}`), raw_id: raw, label: text(row.label, 'Remote agent'),
    state: externalState(row), external: true, attested: true, workspace_id: null, surface: null, parent: null,
    can_terminal: false, reported_by: text(row.label, 'the remote agent'),
    ext_state: text(row.state, 'active'), presence: p, invited_by: text(row.invited_by_email, text(row.invited_by)),
    enrolled_at: text(row.enrolled_at), expires_at: row.expires_at, revoked_at: text(row.revoked_at), revoked_by: text(row.revoked_by),
    actions: Array.isArray(row.actions) ? row.actions.filter((a) => typeof a === 'string') : [], lineage_allowed: row.lineage === true,
    declared_harness: text(row.declared_harness), reported, panes: [],
  };
}

export function buildTabsPanes(graphs, links = null, externals = null) {
  const { nodes, edges } = merge(graphs);
  const grants = grantsByWorkspace(links);
  const rows = new Map((Array.isArray(externals) ? externals : [])
    .filter((r) => r && typeof r.agent_id === 'string' && typeof r.node_id === 'string' && r.kind === 'external')
    .map((r) => [r.node_id, r]));
  // an attested external's node is an agent in the graph only if it reported lineage; either way it has a tab
  for (const r of rows.values()) {
    if (!nodes.has(r.node_id)) nodes.set(r.node_id, { id: r.node_id, node_type: 'agent', label: r.label, state: 'unknown', attrs: {}, synthetic: true });
  }
  const allAgents = [...nodes.values()].filter((n) => n.node_type === 'agent')
    .sort((a, b) => (rows.has(a.id) === rows.has(b.id) ? (a.id < b.id ? -1 : 1) : (rows.has(a.id) ? -1 : 1)));   // attested externals are never cut by the limit
  const agents = allAgents.slice(0, MAX_AGENTS);
  const omitted = allAgents.length - agents.length;
  const byId = new Map(agents.map((a) => [a.id, a]));

  // spawn lineage between visible agents only; the smallest parent id wins so the result is stable
  const parent = new Map();
  for (const e of edges.values()) {
    if (e.edge_type !== 'spawn' || !byId.has(e.from_id) || !byId.has(e.to_id) || e.from_id === e.to_id) continue;
    if (!parent.has(e.to_id) || e.from_id < parent.get(e.to_id)) parent.set(e.to_id, e.from_id);
  }
  // break cycles: an edge that would make an agent its own ancestor is ignored (the agent becomes a tab)
  for (const id of [...parent.keys()]) {
    let cur = id;
    for (let i = 0; i < MAX_DEPTH && cur; i += 1) {
      cur = parent.get(cur);
      if (cur === id) { parent.delete(id); break; }
    }
  }

  // surfaces (tab:/pane: nodes) name their agent through attrs.agent_id
  const surface = new Map();
  for (const n of nodes.values()) {
    if (n.node_type !== 'tab' && n.node_type !== 'pane') continue;
    const target = attrsOf(n).agent_id;
    if (typeof target !== 'string') continue;
    const id = byId.has(target) ? target : `agent:${agentKey(target)}`;
    if (byId.has(id)) surface.set(id, { id: n.id, kind: n.node_type, state: text(n.state, 'open'), closed: ['closed', 'gone'].includes(n.state) });
  }

  const isOwnTab = (a) => rows.has(a.id) || !parent.has(a.id) || attrsOf(a).presentation === 'tab' || attrsOf(a).external === true;
  // null when no tab ancestor is reachable within MAX_DEPTH (a very deep chain, or a cycle with no entry)
  const tabOf = (id) => {
    let cur = id;
    for (let i = 0; i < MAX_DEPTH && cur; i += 1) {
      if (isOwnTab(byId.get(cur))) return cur;
      cur = parent.get(cur);
    }
    return null;
  };
  const depthOf = (id, tab) => {
    let d = 0;
    for (let cur = id; cur !== tab && d < MAX_DEPTH; d += 1) cur = parent.get(cur);
    return d;
  };

  let hiddenByGrant = 0;
  const allowed = (a, kind) => {
    const ws = attrsOf(a).workspace_id;
    if (grants === null || typeof ws !== 'string') return true;       // the server already gated the lineage itself
    const g = grants.get(ws);
    const ok = !!g && g[kind];
    if (!ok) hiddenByGrant += 1;
    return ok;
  };
  const entry = (a) => {
    const at = attrsOf(a);
    const ws = typeof at.workspace_id === 'string' ? at.workspace_id : null;
    const external = at.external === true;
    const g = ws && grants ? grants.get(ws) : null;
    if (rows.has(a.id)) return externalEntry(rows.get(a.id), nodes);
    return {
      agent_id: a.id, label: text(a.label, agentKey(a.id)), state: text(a.state, 'unknown'), external,
      workspace_id: ws, surface: surface.get(a.id) || null,
      parent: parent.get(a.id) || null,
      // an external agent is virtual: it never has a terminal, whatever the workspace says
      can_terminal: !external && !!g && g.can_terminal === true,
      reported_by: external ? text(at.reported_by, 'the remote agent') : null,
      attested: false,         // self-declared by whoever wrote the node; only the server roster attests an external
    };
  };

  const tabs = new Map();
  for (const a of agents) if (isOwnTab(a) && (rows.has(a.id) || allowed(a, 'tabs'))) tabs.set(a.id, { ...entry(a), panes: [] });
  const unplaced = [];            // nested too deep (or cyclic) to place under a tab: listed flat, never dropped
  for (const a of agents) {
    if (isOwnTab(a)) continue;
    const root = tabOf(a.id);
    const t = root && tabs.get(root);
    if (!root) { if (allowed(a, 'panes')) unplaced.push(entry(a)); continue; }
    if (!t || !allowed(a, 'panes')) continue;
    t.panes.push({ ...entry(a), depth: depthOf(a.id, t.agent_id) });
  }
  const list = [...tabs.values()];
  for (const t of list) t.panes.sort((x, y) => x.depth - y.depth || (x.agent_id < y.agent_id ? -1 : 1));
  return { tabs: list, hidden_by_grant: hiddenByGrant, unplaced, omitted };
}

// Arrow / Home / End navigation for the tablist (roving tabindex).
export function nextTab(ids, current, key) {
  if (!ids.length) return null;
  const i = Math.max(0, ids.indexOf(current));
  if (key === 'ArrowRight' || key === 'ArrowDown') return ids[(i + 1) % ids.length];
  if (key === 'ArrowLeft' || key === 'ArrowUp') return ids[(i - 1 + ids.length) % ids.length];
  if (key === 'Home') return ids[0];
  if (key === 'End') return ids[ids.length - 1];
  return null;
}

const pill = (label, color) => `<span class="hd-status" style="--sc:${esc(color)}"><span class="hd-status__dot" aria-hidden="true">●</span><span class="hd-status__label">${esc(label)}</span></span>`;
const stateTone = (s) => STATE_TONE[s] || 'var(--text-muted, #888)';

const REMOTE = 'var(--accent, #6f5bd6)';
const remotePill = (t) => (t.external && t.attested === false ? pill('Remote (self-declared)', REMOTE) : (t.external ? pill('Remote', REMOTE) : ''));
const when = (iso) => (typeof iso === 'string' && iso ? iso : 'unknown');
const epoch = (n) => (typeof n === 'number' && Number.isFinite(n) ? new Date(n * 1000).toISOString() : 'unknown');

function presenceLine(t) {
  const p = t.presence || {};
  if (t.ext_state === 'revoked') return `Access revoked ${esc(when(t.revoked_at))}${t.revoked_by ? ` by ${esc(t.revoked_by)}` : ''}. It can no longer report, post or emit lineage; what it already did stays visible.`;
  if (t.ext_state === 'expired') return 'The credential has expired. The agent must be invited again.';
  if (t.ext_state === 'removed') return 'The agent was removed from this room.';
  if (p.state === 'online') return `Online. Last heartbeat ${esc(String(p.age_seconds ?? '?'))} s ago.`;
  if (p.state === 'stale') return `No heartbeat for ${esc(String(p.age_seconds ?? '?'))} s (stale). Its last reported status is not shown as current.`;
  return 'No heartbeat since this service started.';
}

function externalPanel(t, canManage) {
  const can = [...t.actions, ...(t.lineage_allowed ? ['report lineage'] : [])].join(', ') || 'nothing';
  const act = t.presence && t.presence.last_activity
    ? `<div class="cx-msg">Last reported activity (untrusted text from the agent, ${esc(when(t.presence.last_seen_at))}): <q>${esc(t.presence.last_activity)}</q></div>` : '';
  const rep = t.reported.length
    ? `<ul class="cx-list" aria-label="Lineage reported by ${esc(t.label)}">${t.reported.map((n) => `<li class="cx-pane" data-node="${esc(n.id)}"><span class="grow"><b>${esc(n.label)}</b> <span class="cx-msg">${esc(n.node_type)} reported by the agent</span></span>${pill(n.state, stateTone(n.state))}</li>`).join('')}</ul>`
    : `<div class="cx-msg">${t.lineage_allowed ? 'It has not reported any lineage yet.' : 'This invite does not allow it to report lineage.'}</div>`;
  const revoke = canManage && t.ext_state === 'active'
    ? `<div><button type="button" class="hd-btn hd-btn--ghost hd-btn--sm" data-revoke="${esc(t.raw_id)}">Revoke access</button></div>` : '';
  return `<div class="cx-msg" role="note">Remote agent. Everything here is reported by the agent itself and is not verified by Herdr. There is no terminal and no shell access.</div>`
    + `<div class="cx-msg">Invited by ${esc(t.invited_by || 'unknown')} · enrolled ${esc(when(t.enrolled_at))} · credential expires ${esc(epoch(t.expires_at))} · may: ${esc(can)}${t.declared_harness ? ` · declares itself ${esc(t.declared_harness)}` : ''}</div>`
    + `<div class="cx-msg" role="status">${presenceLine(t)}</div>${act}${rep}${revoke}`;
}

function paneRow(p) {
  const closed = p.surface && p.surface.closed;
  const notes = [
    closed ? 'Pane closed. The agent keeps running independently of it.' : '',
    p.external ? `Status reported by ${p.reported_by}${p.attested === false ? ' (self-declared, not verified)' : ''}. No terminal.` : '',
  ].filter(Boolean);
  return `<li class="cx-pane" data-agent="${esc(p.agent_id)}" style="margin-left:${Math.min(p.depth - 1, 6) * 16}px">`
    + `<span class="grow"><b>${esc(p.label)}</b>${notes.length ? `<br><span class="cx-msg">${notes.map(esc).join(' ')}</span>` : ''}</span>`
    + `${remotePill(p)}${pill(p.state, stateTone(p.state))}</li>`;
}

function flatNote(model) {
  const flat = model.unplaced || [];
  const more = model.omitted || 0;
  if (!flat.length && !more) return '';
  const rows = flat.map((p) => `<li class="cx-pane" data-agent="${esc(p.agent_id)}"><span class="grow"><b>${esc(p.label)}</b></span>${pill(p.state, stateTone(p.state))}</li>`).join('');
  return `<div class="cx-msg" role="status">${flat.length ? `${esc(String(flat.length))} more agents are nested too deeply or cyclic to place under a tab; listed flat.` : ''}`
    + `${more ? ` ${esc(String(more))} further agents are not shown (limit).` : ''}</div>${rows ? `<ul class="cx-list" aria-label="Agents not placed under a tab">${rows}</ul>` : ''}`;
}

export function renderTabsPanes(model, selected = null, opts = {}) {
  if (!model || (!model.tabs.length && !(model.unplaced || []).length)) {
    const note = model && model.hidden_by_grant ? ' Some are not shared with your role by their workspace link.' : '';
    return `<div class="hd-empty">No agents are shown for this room yet.${esc(note)} Tabs and panes appear only from recorded spawn lineage.</div>`;
  }
  const extra = flatNote(model);
  if (!model.tabs.length) return extra;
  const tab = model.tabs.find((t) => t.agent_id === selected) || model.tabs[0];
  const tabId = (t) => `hcn-tab-${esc(t.agent_id)}`;
  const strip = model.tabs.map((t) => `<button type="button" role="tab" class="hd-btn hd-btn--ghost hd-btn--sm" id="${tabId(t)}"`
    + ` data-tab="${esc(t.agent_id)}" aria-selected="${t === tab}" aria-controls="hcn-tabpanel" tabindex="${t === tab ? 0 : -1}">`
    + `${esc(t.label)} ${pill(t.state, stateTone(t.state))}${t.external ? ` ${remotePill(t)}` : ''}</button>`).join('');
  const lineage = tab.parent ? `<div class="cx-msg">Spawned by ${esc(agentKey(tab.parent))}; kept as its own tab.</div>` : '';
  const ext = tab.external
    ? (tab.attested ? externalPanel(tab, opts.canManage === true)
      : `<div class="cx-msg">Virtual tab: status reported by ${esc(tab.reported_by)} (self-declared, not verified). There is no terminal for this agent.</div>`) : '';
  const closed = tab.surface && tab.surface.closed ? '<div class="cx-msg">This tab was closed. The agent is not stopped by closing it.</div>' : '';
  const panes = tab.panes.length
    ? `<ul class="cx-list" aria-label="Subagents spawned by ${esc(tab.label)}">${tab.panes.map(paneRow).join('')}</ul>`
    : '<div class="hd-empty">This agent has not spawned any subagents.</div>';
  const hidden = model.hidden_by_grant ? `<div class="cx-msg">${esc(String(model.hidden_by_grant))} more are not shared with your role by their workspace link.</div>` : '';
  return `<div role="tablist" aria-label="Primary agents" class="cx-pills">${strip}</div>`
    + `<div role="tabpanel" id="hcn-tabpanel" aria-labelledby="${tabId(tab)}" tabindex="0" class="cx-stack">`
    + `<h3 class="hd-card__title">${esc(tab.label)} ${pill(tab.state, stateTone(tab.state))}${tab.external ? ` ${remotePill(tab)}` : ''}</h3>${lineage}${ext}${closed}${tab.external && tab.attested && !tab.panes.length ? '' : panes}${hidden}${extra}</div>`;
}

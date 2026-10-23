// Read-only lineage workbench for one room: a lineage graph, a Gantt timeline, a replay scrubber and a node
// inspector. Pure (DOM-free): models and renderers return data / escaped HTML and are node-testable.
//
// Rules (docs/contracts/lineage-events.md, "Lineage views and replay (T09)"):
//  * the server already filtered everything by the reader's CURRENT membership; this file never widens it and
//    never infers an edge, a span or a state: it draws only what the response says;
//  * nothing is encoded by colour alone: node types differ by shape, edge types by dash pattern and a text tag,
//    states by a glyph and a word, Gantt states by a fill pattern and a label; every view also has a table;
//  * every id, label and value is data: it reaches the page only through esc() (text and attributes alike);
//  * there is no terminal, no command and no write here. Fork/steer/comment arrive in later tasks.
import { escapeHtml as esc } from './herdr-web-components/util.js';

export const MAX_RENDER_NODES = 300;       // beyond this the SVG is skipped and the table is the view
export const MAX_TABLE_ROWS = 1000;
const COL_W = 230;
const NODE_W = 188;
const NODE_H = 44;
const ROW_H = 56;
const PAD = 20;

const text = (v, fallback = '') => (typeof v === 'string' && v ? v : fallback);
const clip = (s, n) => (String(s).length > n ? `${String(s).slice(0, n - 1)}…` : String(s));
const idTail = (id) => String(id).replace(/^[a-z]+:/, '');

// ---- how things look: shape / dash / glyph + word, never colour alone -------------------------------------
export const EDGE_STYLE = {
  spawn: { dash: '', tag: 'spawn', word: 'spawned' },
  handoff: { dash: '8 4', tag: 'handoff', word: 'handed off to' },
  delegation: { dash: '2 3', tag: 'delegates', word: 'delegated to' },
  consultation: { dash: '10 3 2 3', tag: 'consults', word: 'consulted' },
  dependency: { dash: '5 5', tag: 'needs', word: 'depends on' },
  issue_link: { dash: '1 4', tag: 'issue', word: 'is linked to issue/PR' },
  fork: { dash: '12 4', tag: 'fork', word: 'forked into' },
  supersession: { dash: '3 2 9 2', tag: 'supersedes', word: 'supersedes' },
  reviewed: { dash: '6 2 2 2', tag: 'reviewed', word: 'reviewed' },
  produced: { dash: '14 3', tag: 'produced', word: 'produced' },
  validated: { dash: '4 2', tag: 'validated', word: 'validated' },
};
export const EDGE_TYPES = Object.keys(EDGE_STYLE);

const SHAPE = {
  agent: 'round', attempt: 'round', session: 'round', tab: 'round', pane: 'round', worktree: 'round',
  task: 'rect', plan: 'rect', mission: 'rect', room: 'rect', workspace: 'rect', approach: 'rect',
  issue: 'tag', pr: 'tag', commit: 'tag', ci: 'tag', artifact: 'tag',
  review: 'diamond', finding: 'diamond', gate: 'diamond', decision: 'diamond',
  handoff: 'hex', thread: 'hex', message: 'hex',
};
export const shapeOf = (type) => SHAPE[type] || 'rect';
export const NODE_TYPES = Object.keys(SHAPE);

const GLYPH = [
  [/^(running|working|active|open|in_progress|started)$/, '●', 'active'],
  [/^(blocked|waiting|pending|paused|queued)$/, '◐', 'waiting'],
  [/^(done|merged|passed|completed|success|approved|closed)$/, '✓', 'finished'],
  [/^(failed|error|rejected|aborted)$/, '✕', 'failed'],
  [/^(cancelled|canceled|superseded|gone)$/, '■', 'stopped'],
];
export function stateLook(state) {
  const s = text(state);
  if (!s) return { glyph: '○', kind: 'unknown', word: 'no state recorded' };
  for (const [re, glyph, kind] of GLYPH) if (re.test(s)) return { glyph, kind, word: s };
  return { glyph: '◇', kind: 'other', word: s };
}

// ---- merge / filter -------------------------------------------------------------------------------------
// Accepts one or several pages of a lineage response; later pages add nodes and edges (deduped by identity).
export function mergePages(pages) {
  const nodes = new Map();
  const edges = new Map();
  for (const p of pages || []) {
    for (const n of (p && p.nodes) || []) if (n && typeof n.id === 'string') nodes.set(n.id, n);
    for (const e of (p && p.edges) || []) {
      if (!e || typeof e.from_id !== 'string' || typeof e.to_id !== 'string' || !EDGE_STYLE[e.edge_type]) continue;
      edges.set(edgeKey(e), e);
    }
  }
  return { nodes: [...nodes.values()], edges: [...edges.values()] };
}
export const edgeKey = (e) => `${e.edge_type}\u0000${e.from_id}\u0000${e.to_id}\u0000${bindingText(e.binding)}`;
export const bindingText = (b) => (b && typeof b === 'object' ? Object.entries(b).map(([k, v]) => `${k}=${v}`).join(',') : '');

export function filterGraph(graph, f = {}) {
  const types = f.types instanceof Set && f.types.size ? f.types : null;
  const etypes = f.edgeTypes instanceof Set && f.edgeTypes.size ? f.edgeTypes : null;
  const q = text(f.q).trim().toLowerCase();
  let nodes = graph.nodes.filter((n) => (!types || types.has(n.node_type)) && (!f.state || text(n.state, 'none') === f.state));
  const edges0 = graph.edges.filter((e) => !etypes || etypes.has(e.edge_type));
  const match = new Set();
  if (q) {
    for (const n of nodes) {
      if ([n.id, n.label, n.state, n.node_type].some((x) => typeof x === 'string' && x.toLowerCase().includes(q))) match.add(n.id);
    }
    const keep = new Set(match);
    for (const e of edges0) {                       // one hop of context around every hit
      if (match.has(e.from_id)) keep.add(e.to_id);
      if (match.has(e.to_id)) keep.add(e.from_id);
    }
    nodes = nodes.filter((n) => keep.has(n.id));
  }
  const have = new Set(nodes.map((n) => n.id));
  const edges = edges0.filter((e) => have.has(e.from_id) && have.has(e.to_id));
  return { nodes, edges, match, query: q };
}

// ---- layered layout (deterministic; cycles are cut, never dropped) ---------------------------------------
export function layoutGraph(graph) {
  const ids = graph.nodes.map((n) => n.id);
  const byId = new Map(graph.nodes.map((n) => [n.id, n]));
  const succ = new Map(ids.map((i) => [i, []]));
  const indeg = new Map(ids.map((i) => [i, 0]));
  for (const e of graph.edges) {
    if (e.from_id === e.to_id || !byId.has(e.from_id) || !byId.has(e.to_id)) continue;
    succ.get(e.from_id).push(e.to_id);
    indeg.set(e.to_id, indeg.get(e.to_id) + 1);
  }
  const level = new Map();
  const ready = ids.filter((i) => indeg.get(i) === 0).sort();
  const left = new Map(indeg);
  const done = new Set();
  const visit = (queue) => {
    while (queue.length) {
      const id = queue.shift();
      if (done.has(id)) continue;
      done.add(id);
      if (!level.has(id)) level.set(id, 0);
      for (const to of succ.get(id)) {
        level.set(to, Math.max(level.get(to) || 0, level.get(id) + 1));
        left.set(to, left.get(to) - 1);
        if (left.get(to) <= 0) queue.push(to);
      }
    }
  };
  visit(ready);
  for (const id of [...ids].sort()) {          // what is left sits on a cycle: cut at the smallest id
    if (done.has(id)) continue;
    level.set(id, level.get(id) || 0);
    visit([id]);
  }
  const cols = new Map();
  for (const n of graph.nodes) {
    const l = level.get(n.id) || 0;
    if (!cols.has(l)) cols.set(l, []);
    cols.get(l).push(n);
  }
  const placed = new Map();
  const columns = [];
  let height = PAD;
  [...cols.keys()].sort((a, b) => a - b).forEach((l, ci) => {
    const list = cols.get(l).sort((a, b) => (a.node_type < b.node_type ? -1 : a.node_type > b.node_type ? 1 : 0)
      || (a.id < b.id ? -1 : 1));
    columns.push(list.map((n) => n.id));
    list.forEach((n, i) => {
      placed.set(n.id, { node: n, level: l, col: ci, row: i, x: PAD + ci * COL_W, y: PAD + i * ROW_H, w: NODE_W, h: NODE_H });
      height = Math.max(height, PAD + (i + 1) * ROW_H);
    });
  });
  const width = PAD * 2 + Math.max(1, columns.length) * COL_W - (COL_W - NODE_W);
  const degree = new Map(ids.map((i) => [i, { in: 0, out: 0 }]));
  for (const e of graph.edges) {
    if (degree.has(e.from_id)) degree.get(e.from_id).out += 1;
    if (degree.has(e.to_id)) degree.get(e.to_id).in += 1;
  }
  return { placed, columns, width, height: height + PAD, degree };
}

// Arrow / Home / End movement between nodes (roving tabindex). Up/Down: same column. Left/Right: neighbouring
// column, nearest row. Home/End: first / last in reading order.
export function nextNode(layout, current, key) {
  const flat = layout.columns.flat();
  if (!flat.length) return null;
  const cur = layout.placed.get(current) || layout.placed.get(flat[0]);
  if (key === 'Home') return flat[0];
  if (key === 'End') return flat[flat.length - 1];
  const col = layout.columns[cur.col];
  if (key === 'ArrowDown') return col[Math.min(col.length - 1, cur.row + 1)];
  if (key === 'ArrowUp') return col[Math.max(0, cur.row - 1)];
  const to = key === 'ArrowRight' ? cur.col + 1 : key === 'ArrowLeft' ? cur.col - 1 : null;
  if (to === null || to < 0 || to >= layout.columns.length) return null;
  const target = layout.columns[to];
  return target[Math.min(target.length - 1, cur.row)];
}

// ---- graph renderer ----------------------------------------------------------------------------------------
function nodeShape(p, cls) {
  const { x, y, w, h } = p;
  const shape = shapeOf(p.node.node_type);
  const c = `class="${cls}"`;
  if (shape === 'round') return `<rect ${c} x="${x}" y="${y}" width="${w}" height="${h}" rx="${h / 2}"/>`;
  if (shape === 'tag') return `<polygon ${c} points="${x},${y} ${x + w - 14},${y} ${x + w},${y + h / 2} ${x + w - 14},${y + h} ${x},${y + h}"/>`;
  if (shape === 'diamond') return `<polygon ${c} points="${x + w / 2},${y - 4} ${x + w + 2},${y + h / 2} ${x + w / 2},${y + h + 4} ${x - 2},${y + h / 2}"/>`;
  if (shape === 'hex') return `<polygon ${c} points="${x + 12},${y} ${x + w - 12},${y} ${x + w},${y + h / 2} ${x + w - 12},${y + h} ${x + 12},${y + h} ${x},${y + h / 2}"/>`;
  return `<rect ${c} x="${x}" y="${y}" width="${w}" height="${h}" rx="3"/>`;
}

export function nodeLabel(n, deg) {
  const look = stateLook(n.state);
  const d = deg || { in: 0, out: 0 };
  return `${n.node_type} ${text(n.label, idTail(n.id))}, ${look.word}, ${d.out} outgoing and ${d.in} incoming links. Press Enter to inspect.`.slice(0, 120);
}

function edgePath(a, b, back) {
  const x1 = a.x + a.w;
  const y1 = a.y + a.h / 2;
  const x2 = b.x;
  const y2 = b.y + b.h / 2;
  if (back || x2 <= x1) {                  // same column or backwards: loop out and around
    const dy = a.y <= b.y ? 1 : -1;
    const bx = Math.max(x1, b.x + b.w) + 26;
    return { d: `M${x1},${y1} C${bx},${y1} ${bx},${y2} ${b.x + b.w},${y2}`, mx: bx - 8, my: (y1 + y2) / 2 + dy };
  }
  const k = (x2 - x1) / 2;
  return { d: `M${x1},${y1} C${x1 + k},${y1} ${x2 - k},${y2} ${x2},${y2}`, mx: (x1 + x2) / 2, my: (y1 + y2) / 2 };
}

export function renderGraphLegend() {
  const edges = EDGE_TYPES.map((t) => `<li><svg width="46" height="10" aria-hidden="true" focusable="false"><line x1="1" y1="5" x2="45" y2="5" class="lin-edge-line" stroke-dasharray="${esc(EDGE_STYLE[t].dash)}"/></svg> ${esc(EDGE_STYLE[t].tag)}</li>`).join('');
  const shapes = [['round', 'agent, attempt, session'], ['rect', 'task, plan, mission'], ['tag', 'issue, PR, commit, CI, artifact'],
    ['diamond', 'review, finding, gate, decision'], ['hex', 'handoff, thread']].map(([s, w]) => `<li>${esc(s)}: ${esc(w)}</li>`).join('');
  const states = GLYPH.map(([, g, k]) => `<li><span aria-hidden="true">${g}</span> ${esc(k)}</li>`).join('') + '<li><span aria-hidden="true">○</span> no state recorded</li>';
  return `<details class="lin-legend"><summary>Legend</summary><div class="lin-legend__cols"><ul aria-label="Link types">${edges}</ul><ul aria-label="Node shapes">${shapes}</ul><ul aria-label="States">${states}</ul></div></details>`;
}

// The graph as SVG. `sel` is the selected node id; `focus` is the roving-tabindex node.
export function renderGraphSvg(graph, layout, { sel = null, focus = null, match = new Set() } = {}) {
  if (graph.nodes.length > MAX_RENDER_NODES) {
    return `<div class="cx-msg" role="status">This view has ${esc(String(graph.nodes.length))} nodes, more than the ${esc(String(MAX_RENDER_NODES))} drawn. Narrow it with the filters or use the table view.</div>`;
  }
  const neighbours = new Set();
  if (sel) for (const e of graph.edges) { if (e.from_id === sel) neighbours.add(e.to_id); if (e.to_id === sel) neighbours.add(e.from_id); }
  const tabId = focus && layout.placed.has(focus) ? focus : layout.columns.flat()[0];
  const edges = graph.edges.map((e) => {
    const a = layout.placed.get(e.from_id);
    const b = layout.placed.get(e.to_id);
    if (!a || !b || e.from_id === e.to_id) return '';
    const p = edgePath(a, b, a.level >= b.level);
    const st = EDGE_STYLE[e.edge_type];
    const dim = sel && e.from_id !== sel && e.to_id !== sel ? ' lin-dim' : '';
    const stale = e.stale ? ' lin-edge--stale' : '';
    const tag = `${st.tag}${e.stale ? ' (stale)' : ''}`;
    return `<g class="lin-edge${stale}${dim}" data-edge-type="${esc(e.edge_type)}"><title>${esc(`${idTail(e.from_id)} ${st.word} ${idTail(e.to_id)}${bindingText(e.binding) ? ` at ${bindingText(e.binding)}` : ''}${e.stale ? '. Stale: a newer revision exists.' : ''}`)}</title>`
      + `<path class="lin-edge-line" d="${p.d}" fill="none" stroke-dasharray="${esc(st.dash)}" marker-end="url(#lin-arrow)"/>`
      + `<text class="lin-edge-tag" x="${p.mx}" y="${p.my - 3}" text-anchor="middle">${esc(clip(tag, 16))}</text></g>`;
  }).join('');
  const nodes = [...layout.placed.values()].map((p) => {
    const n = p.node;
    const look = stateLook(n.state);
    const on = n.id === sel ? ' lin-node--sel' : '';
    const nb = sel && n.id !== sel && !neighbours.has(n.id) ? ' lin-dim' : '';
    const hit = match.has(n.id) ? ' lin-node--hit' : '';
    return `<g class="lin-node lin-node--${esc(look.kind)}${on}${nb}${hit}" role="button" tabindex="${n.id === tabId ? 0 : -1}" data-node="${esc(n.id)}" aria-pressed="${n.id === sel}" aria-label="${esc(nodeLabel(n, layout.degree.get(n.id)))}">`
      + `${nodeShape(p, 'lin-box')}`
      + `<text class="lin-name" x="${p.x + 16}" y="${p.y + 18}"><tspan aria-hidden="true">${look.glyph} </tspan>${esc(clip(text(n.label, idTail(n.id)), 22))}</text>`
      + `<text class="lin-sub" x="${p.x + 16}" y="${p.y + 33}">${esc(clip(`${n.node_type} · ${look.word}`, 30))}</text></g>`;
  }).join('');
  return `<svg class="lin-svg" width="${layout.width}" height="${layout.height}" viewBox="0 0 ${layout.width} ${layout.height}" role="group" aria-label="Lineage graph: ${esc(String(graph.nodes.length))} nodes and ${esc(String(graph.edges.length))} links. Use the arrow keys to move between nodes. A table view lists the same data.">`
    + '<defs><marker id="lin-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" class="lin-arrowhead"/></marker></defs>'
    + `${edges}${nodes}</svg>`;
}

// The same data as plain tables (the text alternative; also the view for very large graphs).
export function renderGraphTable(graph, { sel = null } = {}) {
  const deg = layoutGraph({ nodes: graph.nodes.slice(0, MAX_TABLE_ROWS), edges: graph.edges }).degree;
  const nrows = graph.nodes.slice(0, MAX_TABLE_ROWS).map((n) => {
    const look = stateLook(n.state);
    const d = deg.get(n.id) || { in: 0, out: 0 };
    return `<tr${n.id === sel ? ' aria-current="true"' : ''}><th scope="row"><button type="button" class="hd-btn hd-btn--ghost hd-btn--sm" data-node="${esc(n.id)}">${esc(text(n.label, idTail(n.id)))}</button></th>`
      + `<td>${esc(n.node_type)}</td><td><code>${esc(n.id)}</code></td><td>${look.glyph} ${esc(look.word)}</td><td>${d.out}</td><td>${d.in}</td></tr>`;
  }).join('');
  const erows = graph.edges.slice(0, MAX_TABLE_ROWS).map((e) => {
    const st = EDGE_STYLE[e.edge_type];
    return `<tr><td>${esc(st.tag)}</td><td><code>${esc(e.from_id)}</code></td><td><code>${esc(e.to_id)}</code></td><td>${esc(bindingText(e.binding) || 'none')}</td><td>${e.stale ? 'stale' : e.binding ? 'current' : 'n/a'}</td></tr>`;
  }).join('');
  const cut = graph.nodes.length > MAX_TABLE_ROWS || graph.edges.length > MAX_TABLE_ROWS
    ? `<p class="cx-msg" role="status">Showing the first ${MAX_TABLE_ROWS} rows of each table.</p>` : '';
  return `<div class="lin-tables">${cut}<div class="cx-scroll" role="region" tabindex="0" aria-label="Nodes table"><table class="lin-table"><caption>${graph.nodes.length} nodes</caption>`
    + `<thead><tr><th scope="col">Name</th><th scope="col">Type</th><th scope="col">Id</th><th scope="col">State</th><th scope="col">Links out</th><th scope="col">Links in</th></tr></thead><tbody>${nrows || '<tr><td colspan="6">No nodes.</td></tr>'}</tbody></table></div>`
    + `<div class="cx-scroll" role="region" tabindex="0" aria-label="Links table"><table class="lin-table"><caption>${graph.edges.length} links</caption>`
    + `<thead><tr><th scope="col">Type</th><th scope="col">From</th><th scope="col">To</th><th scope="col">Revision</th><th scope="col">Freshness</th></tr></thead><tbody>${erows || '<tr><td colspan="5">No links.</td></tr>'}</tbody></table></div></div>`;
}

export function renderFilters(graphAll, f = {}) {
  const types = [...new Set(graphAll.nodes.map((n) => n.node_type))].sort();
  const etypes = [...new Set(graphAll.edges.map((e) => e.edge_type))].sort();
  const states = [...new Set(graphAll.nodes.map((n) => text(n.state, 'none')))].sort();
  const chk = (name, vals, set) => vals.map((v) => `<label class="cx-check"><input type="checkbox" data-filter="${name}" value="${esc(v)}"${set instanceof Set && set.has(v) ? ' checked' : ''}> ${esc(v)}</label>`).join('');
  return `<form class="lin-filters cx-stack" role="search" aria-label="Filter the lineage" data-lineage-filters>`
    + `<label class="cx-field">Search ids, names and states<input class="cx-input" type="search" data-filter="q" value="${esc(text(f.q))}" maxlength="120" autocomplete="off"></label>`
    + `<fieldset class="cx-checks"><legend>Node types</legend>${chk('type', types, f.types) || '<span class="cx-msg">none</span>'}</fieldset>`
    + `<fieldset class="cx-checks"><legend>Link types</legend>${chk('etype', etypes, f.edgeTypes) || '<span class="cx-msg">none</span>'}</fieldset>`
    + `<label class="cx-field">State<select class="cx-input" data-filter="state"><option value="">Any state</option>${states.map((s) => `<option value="${esc(s)}"${f.state === s ? ' selected' : ''}>${esc(s)}</option>`).join('')}</select></label></form>`;
}

// ---- replay scrubber -------------------------------------------------------------------------------------
export function replayText(asOf) {
  if (!asOf) return 'No position.';
  if (asOf.head) return `Live: showing all ${asOf.visible_events} events you can see.`;
  const when = asOf.received_at ? ` recorded ${asOf.received_at}` : '';
  return asOf.step === 0
    ? `Replay: before the first of ${asOf.visible_events} events you can see.`
    : `Replay: event ${asOf.step} of ${asOf.visible_events}${when}. Read-only; events after this point are not shown.`;
}

export function renderReplay(asOf, { busy = false } = {}) {
  const n = asOf ? asOf.visible_events : 0;
  const k = asOf ? asOf.step : 0;
  // Busy keeps every control enabled (so focus is never lost to a disabled button); the handler ignores clicks.
  const dis = !n ? ' disabled' : '';
  const trimmed = asOf && asOf.window_trimmed ? '<p class="cx-msg">Only the most recent events of this room are scrubbable; counts are rounded and earlier history is summarised in position 0.</p>' : '';
  return `<div class="lin-replay" role="group" aria-label="Replay"${busy ? ' aria-busy="true"' : ''}>`
    + `<button type="button" class="hd-btn hd-btn--ghost hd-btn--sm" data-replay="first"${dis}>⏮ Start</button>`
    + `<button type="button" class="hd-btn hd-btn--ghost hd-btn--sm" data-replay="prev"${dis}>◀ Back</button>`
    + `<label class="lin-slider">Position<input type="range" data-replay="slider" min="0" max="${n}" step="1" value="${k}" aria-valuetext="${esc(replayText(asOf))}"${dis}></label>`
    + `<button type="button" class="hd-btn hd-btn--ghost hd-btn--sm" data-replay="next"${dis}>Next ▶</button>`
    + `<button type="button" class="hd-btn hd-btn--ghost hd-btn--sm" data-replay="last"${dis}>End ⏭</button>`
    + `<button type="button" class="hd-btn hd-btn--sm" data-replay="live"${asOf && asOf.head ? ' aria-pressed="true"' : ''}>Live</button>`
    + `<output class="lin-replay__status" role="status" aria-live="polite">${esc(replayText(asOf))}</output>${trimmed}</div>`;
}

// Re-rendering replaces the panel's HTML, so the focused control is remembered by a stable key and found again.
const attr = (el, k) => (el && el.getAttribute ? el.getAttribute(k) : null);
export function focusKey(el) {
  if (!el || !el.getAttribute) return null;
  const f = attr(el, 'data-filter');
  if (attr(el, 'data-node') !== null) return `node:${attr(el, 'data-node')}`;
  if (attr(el, 'data-replay') !== null) return `replay:${attr(el, 'data-replay')}`;
  if (attr(el, 'data-view') !== null) return `view:${attr(el, 'data-view')}`;
  if (attr(el, 'data-mode') !== null) return 'mode:';
  if (attr(el, 'data-clock') !== null) return 'clock:';
  if (attr(el, 'data-more') !== null) return 'more:';
  if (attr(el, 'data-retry') !== null) return 'retry:';
  if (f !== null) return (f === 'type' || f === 'etype') ? `filter:${f}:${attr(el, 'value')}` : `filter:${f}:`;
  return null;
}
export function focusSelector(key) {
  const q = (v) => String(v).replace(/["\\]/g, '\\$&');
  const [kind, ...rest] = String(key || '').split(':');
  const v = rest.join(':');
  switch (kind) {
    case 'node': return `[data-node="${q(v)}"]`;
    case 'replay': return `[data-replay="${q(v)}"]`;
    case 'view': return `[data-view="${q(v)}"]`;
    case 'mode': return '[data-mode]';
    case 'clock': return '[data-clock]';
    case 'more': return '[data-more]';
    case 'retry': return '[data-retry]';
    case 'filter': { const [name, ...val] = rest; return val.join(':') ? `[data-filter="${q(name)}"][value="${q(val.join(':'))}"]` : `[data-filter="${q(name)}"]`; }
    default: return null;
  }
}

// Where a replay control moves to (pure; the caller fetches). Returns a step number or 'live'.
export function replayTarget(asOf, action, value) {
  const n = asOf ? asOf.visible_events : 0;
  const k = asOf ? asOf.step : 0;
  switch (action) {
    case 'first': return 0;
    case 'prev': return Math.max(0, k - 1);
    case 'next': return k + 1 >= n ? 'live' : k + 1;
    case 'last': case 'live': return 'live';
    case 'slider': { const v = Math.max(0, Math.min(n, Math.trunc(Number(value)))); return Number.isFinite(v) ? (v >= n ? 'live' : v) : k; }
    default: return k;
  }
}

// ---- Gantt ---------------------------------------------------------------------------------------------------
const WHEN = (t) => { const ms = Date.parse(t); return Number.isFinite(ms) ? ms : null; };
export function fmtDuration(ms) {
  if (!Number.isFinite(ms) || ms < 0) return 'n/a';
  if (ms < 1000) return `${Math.round(ms)} ms`;
  const s = Math.round(ms / 1000);
  if (s < 90) return `${s} s`;
  const m = Math.round(s / 60);
  if (m < 90) return `${m} min`;
  const h = Math.round((m / 60) * 10) / 10;
  return h < 48 ? `${h} h` : `${Math.round(h / 24)} d`;
}
const pick = (rec, recorded, reported, clock) => (clock === 'reported' ? (rec[reported] || rec[recorded]) : rec[recorded]);

export function buildGantt(tl, { clock = 'recorded', types = null } = {}) {
  const lanes = [];
  let min = Infinity;
  let max = -Infinity;
  const see = (ms) => { if (ms !== null) { min = Math.min(min, ms); max = Math.max(max, ms); } };
  const pickT = (r, a, b) => WHEN(pick(r, a, b, clock));
  for (const l of (tl && tl.lanes) || []) {
    if (types && types.size && !types.has(l.node_type)) continue;
    const spans = [];
    for (const s of l.spans || []) {
      const start = pickT(s, 'start', 'claimed_start');
      const end = s.open ? null : pickT(s, 'end', 'claimed_end');
      if (start === null) continue;
      see(start); see(end);
      spans.push({ ...s, startMs: start, endMs: end });
    }
    const first = pickT(l, 'first_at', 'first_at');
    see(first); see(pickT(l, 'last_at', 'last_at'));
    lanes.push({ node_id: l.node_id, node_type: l.node_type, label: text(l.label, idTail(l.node_id)), state: l.state, spans, firstMs: first });
  }
  const markers = [];
  for (const m of (tl && tl.markers) || []) {
    const at = WHEN(pick(m, 'at', 'claimed', clock));
    if (at === null) continue;
    see(at);
    markers.push({ ...m, atMs: at });
  }
  if (!Number.isFinite(min)) { min = 0; max = 0; }
  // an open span runs to the end of the observed range (it is still going, as far as the record shows)
  const end = max > min ? max : min + 1;
  for (const l of lanes) for (const s of l.spans) if (s.open) s.endMs = end;
  return { lanes, markers, min, max: end, clock, truncated: !!(tl && tl.truncated), reason: tl ? tl.truncated_reason : null };
}

const SPAN_KIND = (s) => stateLook(s.state).kind;
const PATTERNS = { active: 'lin-p-active', waiting: 'lin-p-waiting', finished: 'lin-p-finished', failed: 'lin-p-failed', stopped: 'lin-p-stopped', other: 'lin-p-other', unknown: 'lin-p-other' };

export function renderGanttSvg(g, { width = 900 } = {}) {
  if (!g.lanes.length && !g.markers.length) return '<div class="hd-empty">No timeline events yet.</div>';
  const LABEL = 190;
  const PLOT = Math.max(300, width - LABEL - 24);
  const AXIS = 28;
  const ROW = 30;
  const x = (ms) => LABEL + ((ms - g.min) / (g.max - g.min || 1)) * PLOT;
  const mrows = [...new Set(g.markers.map((m) => (m.kind === 'head_change' ? 'head change' : m.edge_type)))].sort();
  const lanesH = g.lanes.length * ROW;
  const height = AXIS + lanesH + (mrows.length ? 16 + mrows.length * ROW : 0) + 12;
  const ticks = [0, 1, 2, 3, 4].map((i) => {
    const ms = g.min + ((g.max - g.min) * i) / 4;
    return `<g><line class="lin-grid" x1="${x(ms)}" y1="${AXIS - 6}" x2="${x(ms)}" y2="${height - 8}"/><text class="lin-sub" x="${x(ms)}" y="14" text-anchor="${i === 0 ? 'start' : i === 4 ? 'end' : 'middle'}">${esc(new Date(ms).toISOString().replace('T', ' ').slice(0, 19))}</text></g>`;
  }).join('');
  const bars = g.lanes.map((l, i) => {
    const y = AXIS + i * ROW;
    const spans = l.spans.map((s) => {
      const x1 = x(s.startMs);
      const w = Math.max(s.terminal ? 8 : 6, x(s.endMs) - x1);
      const dur = fmtDuration(s.endMs - s.startMs);
      const note = `${l.label}: ${s.state}${s.open ? ' (ongoing)' : s.terminal ? ' (final)' : ''}, ${s.open || !s.terminal ? dur : 'instant'}${s.reason ? `. ${s.reason}` : ''}`;
      const word = w > 70 ? `<text class="lin-bar-text" x="${x1 + 4}" y="${y + 15}">${esc(clip(s.state, Math.floor(w / 7)))}${s.open ? ' →' : ''}</text>` : '';
      return `<g class="lin-span lin-span--${SPAN_KIND(s)}"><title>${esc(note)}</title><rect x="${x1}" y="${y + 4}" width="${w}" height="18" rx="3" fill="url(#${PATTERNS[SPAN_KIND(s)]})" class="lin-bar"/>${word}</g>`;
    }).join('');
    const lone = !l.spans.length && l.firstMs !== null ? `<g><title>${esc(`${l.label}: recorded, no state`)}</title><circle class="lin-dot" cx="${x(l.firstMs)}" cy="${y + 13}" r="4"/></g>` : '';
    return `<g data-lane="${esc(l.node_id)}"><text class="lin-name" x="4" y="${y + 17}">${esc(clip(`${l.node_type} ${l.label}`, 28))}</text>${spans}${lone}</g>`;
  }).join('');
  const base = AXIS + lanesH + 16;
  const marks = g.markers.map((m) => {
    const row = mrows.indexOf(m.kind === 'head_change' ? 'head change' : m.edge_type);
    const cx = x(m.atMs);
    const cy = base + row * ROW + 14;
    const tag = m.kind === 'head_change' ? 'H' : m.kind === 'edge_removed' ? '×' : '';
    const desc = `${m.kind === 'head_change' ? 'Head changed' : m.kind === 'edge_removed' ? 'Link removed' : 'Link added'}: ${m.edge_type} ${idTail(m.from_id)} to ${idTail(m.to_id)}${bindingText(m.binding) ? ` at ${bindingText(m.binding)}` : ''}${m.stale === true ? '. Stale.' : m.stale === false ? '. Current.' : ''}`;
    return `<g class="lin-mark${m.stale ? ' lin-mark--stale' : ''}"><title>${esc(desc)}</title><polygon points="${cx},${cy - 8} ${cx + 8},${cy} ${cx},${cy + 8} ${cx - 8},${cy}" class="lin-diamond"/>`
      + `${tag ? `<text class="lin-mark-text" x="${cx}" y="${cy + 4}" text-anchor="middle">${tag}</text>` : ''}${m.stale ? `<text class="lin-sub" x="${cx + 11}" y="${cy + 4}">stale</text>` : ''}</g>`;
  }).join('');
  const mlabels = mrows.map((r, i) => `<text class="lin-name" x="4" y="${base + i * ROW + 18}">${esc(r)}</text>`).join('');
  const defs = '<defs>'
    + '<pattern id="lin-p-active" width="8" height="8" patternUnits="userSpaceOnUse"><rect width="8" height="8" class="lin-fill-active"/></pattern>'
    + '<pattern id="lin-p-waiting" width="8" height="8" patternUnits="userSpaceOnUse" patternTransform="rotate(45)"><rect width="8" height="8" class="lin-fill-waiting"/><line x1="0" y1="0" x2="0" y2="8" class="lin-hatch" stroke-width="3"/></pattern>'
    + '<pattern id="lin-p-finished" width="8" height="8" patternUnits="userSpaceOnUse"><rect width="8" height="8" class="lin-fill-finished"/><circle cx="4" cy="4" r="1.4" class="lin-hatch-fill"/></pattern>'
    + '<pattern id="lin-p-failed" width="8" height="8" patternUnits="userSpaceOnUse"><rect width="8" height="8" class="lin-fill-failed"/><path d="M0,0 L8,8 M8,0 L0,8" class="lin-hatch" stroke-width="1.5"/></pattern>'
    + '<pattern id="lin-p-stopped" width="8" height="8" patternUnits="userSpaceOnUse"><rect width="8" height="8" class="lin-fill-stopped"/><line x1="0" y1="4" x2="8" y2="4" class="lin-hatch" stroke-width="2"/></pattern>'
    + '<pattern id="lin-p-other" width="8" height="8" patternUnits="userSpaceOnUse"><rect width="8" height="8" class="lin-fill-other"/></pattern></defs>';
  return `<svg class="lin-svg" width="${LABEL + PLOT + 24}" height="${height}" viewBox="0 0 ${LABEL + PLOT + 24} ${height}" role="img" aria-label="Gantt timeline of ${g.lanes.length} items and ${g.markers.length} link events, ${esc(g.clock)} time. A table below lists the same data.">`
    + `${defs}${ticks}${mlabels}${bars}${marks}</svg>`;
}

export function renderGanttLegend() {
  return '<details class="lin-legend"><summary>Gantt legend</summary><div class="lin-legend__cols"><ul aria-label="Bar fills">'
    + '<li><span class="lin-sw lin-sw--active"></span> plain: active</li><li><span class="lin-sw lin-sw--waiting"></span> diagonal stripes: waiting or blocked</li>'
    + '<li><span class="lin-sw lin-sw--finished"></span> dots: finished</li><li><span class="lin-sw lin-sw--failed"></span> crosses: failed</li>'
    + '<li><span class="lin-sw lin-sw--stopped"></span> single line: cancelled or superseded</li></ul>'
    + '<ul aria-label="Markers"><li>◆ link added or removed (the tag says which)</li><li>◆ H: head changed, a new revision was reviewed or validated</li><li>stale: an older revision than the latest one</li><li>→ after a state name: still going</li></ul></div></details>';
}

export function renderGanttTable(g) {
  const rows = [];
  for (const l of g.lanes) {
    if (!l.spans.length) { rows.push(`<tr><th scope="row">${esc(l.label)}</th><td>${esc(l.node_type)}</td><td colspan="4">recorded, no state</td></tr>`); continue; }
    for (const s of l.spans) {
      const note = [s.open ? 'ongoing' : '', s.terminal ? 'final' : '', s.reason || ''].filter(Boolean).join('; ');
      rows.push(`<tr><th scope="row">${esc(l.label)}</th><td>${esc(l.node_type)}</td><td>${esc(stateLook(s.state).glyph)} ${esc(s.state)}</td>`
        + `<td><time datetime="${esc(new Date(s.startMs).toISOString())}">${esc(new Date(s.startMs).toISOString().replace('T', ' ').slice(0, 19))}</time></td>`
        + `<td>${esc(s.open ? 'ongoing' : fmtDuration(s.endMs - s.startMs))}</td><td>${esc(note)}</td></tr>`);
    }
  }
  const mrows = g.markers.map((m) => `<tr><td>${esc(m.kind.replace('_', ' '))}</td><td>${esc(m.edge_type)}</td><td><code>${esc(m.from_id)}</code></td><td><code>${esc(m.to_id)}</code></td>`
    + `<td>${esc(bindingText(m.binding) || 'none')}</td><td>${m.stale === true ? 'stale' : m.stale === false ? 'current' : 'n/a'}</td>`
    + `<td>${esc(new Date(m.atMs).toISOString().replace('T', ' ').slice(0, 19))}</td></tr>`);
  const cut = g.truncated ? `<p class="cx-msg" role="status">Some rows are not shown (${esc(String(g.reason || 'limit'))}).</p>` : '';
  return `<div class="lin-tables">${cut}<div class="cx-scroll" role="region" tabindex="0" aria-label="Timeline table"><table class="lin-table"><caption>${g.lanes.length} items, ${esc(g.clock)} time</caption>`
    + `<thead><tr><th scope="col">Item</th><th scope="col">Type</th><th scope="col">State</th><th scope="col">Started</th><th scope="col">Duration</th><th scope="col">Notes</th></tr></thead><tbody>${rows.slice(0, MAX_TABLE_ROWS).join('') || '<tr><td colspan="6">Nothing recorded yet.</td></tr>'}</tbody></table></div>`
    + `<div class="cx-scroll" role="region" tabindex="0" aria-label="Link events table"><table class="lin-table"><caption>${g.markers.length} link events</caption>`
    + `<thead><tr><th scope="col">Kind</th><th scope="col">Link</th><th scope="col">From</th><th scope="col">To</th><th scope="col">Revision</th><th scope="col">Freshness</th><th scope="col">When</th></tr></thead><tbody>${mrows.slice(0, MAX_TABLE_ROWS).join('') || '<tr><td colspan="7">No link events.</td></tr>'}</tbody></table></div></div>`;
}

// ---- inspector ------------------------------------------------------------------------------------------------
const shown = (v) => { try { const s = typeof v === 'string' ? v : JSON.stringify(v); return clip(s === undefined ? '' : s, 240); } catch { return ''; } };

export function renderInspector(graph, id, tl = null) {
  const n = graph.nodes.find((x) => x.id === id);
  if (!n) return '<div class="hd-empty">Select a node to inspect it. Arrow keys move between nodes; Enter selects.</div>';
  const look = stateLook(n.state);
  const attrs = Object.entries(n.attrs || {}).sort(([a], [b]) => (a < b ? -1 : 1)).slice(0, 40);
  const prov = (n.fields && n.fields.label) || null;
  const links = (dir) => graph.edges.filter((e) => (dir === 'out' ? e.from_id === id : e.to_id === id)).slice(0, 60).map((e) => {
    const other = dir === 'out' ? e.to_id : e.from_id;
    const o = graph.nodes.find((x) => x.id === other);
    return `<li><span class="hd-ident">${esc(EDGE_STYLE[e.edge_type].tag)}</span> <button type="button" class="hd-btn hd-btn--ghost hd-btn--sm" data-node="${esc(other)}">${esc(text(o && o.label, idTail(other)))}</button>`
      + `${bindingText(e.binding) ? ` <code>${esc(bindingText(e.binding))}</code>` : ''}${e.stale ? ' <strong>stale</strong>' : ''}${e.writer ? ` <span class="cx-msg">by ${esc(e.writer)}</span>` : ''}</li>`;
  }).join('');
  const lane = tl && (tl.lanes || []).find((l) => l.node_id === id);
  const history = lane && lane.spans.length ? `<h4>State history</h4><ol class="cx-list">${lane.spans.map((s) => `<li><span>${esc(stateLook(s.state).glyph)} ${esc(s.state)}${s.open ? ' (ongoing)' : ''}</span><time datetime="${esc(s.start)}">${esc(s.start)}</time></li>`).join('')}</ol>` : '';
  return `<section class="lin-inspector" aria-label="Inspector for ${esc(text(n.label, idTail(n.id)))}">`
    + `<h3 class="hd-card__title">${esc(text(n.label, idTail(n.id)))}</h3>`
    + `<dl class="lin-dl"><dt>Id</dt><dd><code>${esc(n.id)}</code></dd><dt>Type</dt><dd>${esc(n.node_type)}</dd>`
    + `<dt>State</dt><dd>${look.glyph} ${esc(look.word)}${n.state_reason ? ` (${esc(n.state_reason)})` : ''}</dd>`
    + `<dt>Visibility</dt><dd>${esc(n.visibility_scope || 'unknown')}</dd>`
    + `<dt>First event</dt><dd><code>${esc(n.first_event_id || 'n/a')}</code></dd><dt>Last event</dt><dd><code>${esc(n.last_event_id || 'n/a')}</code></dd>`
    + `${prov ? `<dt>Label written by</dt><dd>${esc(text(prov.writer, 'unknown'))} via ${esc(text(prov.source, 'unknown'))}</dd>` : ''}</dl>`
    + `${attrs.length ? `<h4>Attributes</h4><dl class="lin-dl">${attrs.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(shown(v))}</dd>`).join('')}</dl>` : ''}`
    + `<h4>Links out</h4>${links('out') ? `<ul class="cx-list">${links('out')}</ul>` : '<p class="cx-msg">None.</p>'}`
    + `<h4>Links in</h4>${links('in') ? `<ul class="cx-list">${links('in')}</ul>` : '<p class="cx-msg">None.</p>'}`
    + `${history}<p class="cx-msg">Read-only. This page offers no terminal and no commands.</p></section>`;
}

// ---- whole panel -------------------------------------------------------------------------------------------------
export function renderLineage(m) {
  const a = m.graphAll;
  if (m.error) {
    return `${renderReplay(m.asOf, { busy: m.busy })}<div class="cx-msg" role="alert">${esc(m.error)} `
      + `<button type="button" class="hd-btn hd-btn--sm" data-retry="1">Retry</button></div>`;
  }
  if (!a.nodes.length && !m.timeline) return `${renderReplay(m.asOf, { busy: m.busy })}<div class="hd-empty">No lineage is shown for this room yet. Lineage appears only from recorded events you are allowed to see.</div>`;
  const view = m.filtered;
  const layout = layoutGraph(view);
  const graphPane = m.mode === 'table' ? renderGraphTable(view, { sel: m.sel })
    : `<div class="cx-scroll lin-scroll" role="region" tabindex="0" aria-label="Lineage graph">${renderGraphSvg(view, layout, { sel: m.sel, focus: m.focus, match: view.match })}</div>`;
  const trunc = m.graphTruncated ? `<p class="cx-msg" role="status">Showing ${a.nodes.length} of ${m.total} nodes (${esc(m.graphTruncated)}). <button type="button" class="hd-btn hd-btn--ghost hd-btn--sm" data-more="1">Load more</button></p>` : '';
  const gantt = m.timeline ? buildGantt(m.timeline, { clock: m.clock, types: m.filters.types }) : null;
  const tab = (k, l) => `<button type="button" role="tab" class="cx-stab" data-view="${k}" aria-selected="${m.view === k}" tabindex="${m.view === k ? 0 : -1}">${l}</button>`;
  const body = m.view === 'gantt'
    ? `<div class="cx-row"><label class="cx-field">Time shown<select class="cx-input" data-clock><option value="recorded"${m.clock === 'recorded' ? ' selected' : ''}>When the server recorded it</option><option value="reported"${m.clock === 'reported' ? ' selected' : ''}>When the emitter says it happened</option></select></label>`
      + `<button type="button" class="hd-btn hd-btn--ghost hd-btn--sm" data-mode="${m.mode === 'table' ? 'visual' : 'table'}">${m.mode === 'table' ? 'Show chart' : 'Show as table'}</button></div>`
      + `${m.timelineError ? `<p class="cx-msg" role="alert">${esc(m.timelineError)} <button type="button" class="hd-btn hd-btn--sm" data-retry="1">Retry</button></p>` : ''}`
      + `${renderGanttLegend()}${m.mode === 'table' ? '' : `<div class="cx-scroll lin-scroll" role="region" tabindex="0" aria-label="Gantt timeline">${gantt ? renderGanttSvg(gantt, { width: m.width }) : ''}</div>`}${gantt ? renderGanttTable(gantt) : ''}`
    : `<div class="cx-row"><button type="button" class="hd-btn hd-btn--ghost hd-btn--sm" data-mode="${m.mode === 'table' ? 'visual' : 'table'}">${m.mode === 'table' ? 'Show graph' : 'Show as table'}</button><span class="cx-msg">${view.nodes.length} nodes, ${view.edges.length} links shown</span></div>${renderGraphLegend()}${trunc}${graphPane}`;
  return `${renderReplay(m.asOf, { busy: m.busy })}`
    + `<div class="lin-grid2"><div class="cx-stack">${renderFilters(a, m.filters)}`
    + `<div role="tablist" aria-label="Lineage views" class="cx-stabs">${tab('graph', 'Graph')}${tab('gantt', 'Gantt')}</div>`
    + `<div role="tabpanel" aria-label="${m.view === 'gantt' ? 'Gantt' : 'Graph'}" class="cx-stack">${body}</div></div>`
    + `<aside class="cx-stack" aria-label="Inspector" aria-live="polite">${renderInspector(a, m.sel, m.timeline)}</aside></div>`;
}

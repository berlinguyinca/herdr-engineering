// Read-only lineage workbench for one room: a lineage graph, a Gantt timeline, a replay scrubber and a node
// inspector. Pure (DOM-free): models and renderers return data / escaped HTML and are node-testable.
//
// Rules (docs/contracts/lineage-events.md, "Lineage views and replay (T09)"):
//  * the server already filtered everything by the reader's CURRENT membership; this file never widens it and
//    never infers an edge, a span or a state: it draws only what the response says;
//  * nothing is encoded by colour alone: node types differ by shape, edge types by dash pattern and a text tag,
//    states by a glyph and a word, Gantt states by a fill pattern and a label; every view also has a table;
//  * every id, label and value is data: it reaches the page only through esc() (text and attributes alike);
//  * there is no terminal and no command here. Comments and steering (collab-workbench.js) are drawn beside the
//    inspector; they are written only through the server's checked routes. The one exception is a link to the
//    dashboard's own session page (which owns the authorized Terminal button), offered only when the caller passes it;
//  * the graph is a viewport, not a page: only the nodes inside the visible rectangle (plus the selected and the
//    keyboard-focused one) are put in the DOM, so a thousand-node graph costs the same as a small one. Pan/zoom is
//    plain state ({x, y, k}); nothing here reads the DOM.
import { escapeHtml as esc } from './herdr-web-components/util.js';

export const MAX_DRAWN_NODES = 240;        // the most nodes ever put in the DOM by the graph, however large the graph or far out the zoom
export const MAX_DRAWN_EDGES = 480;
export const MAX_TABLE_ROWS = 1000;
export const ZOOM_MIN = 0.1;
export const ZOOM_MAX = 2.5;
export const VIEW_H = 480;                 // default height of the graph viewport (CSS shrinks it on a phone)
export const COL_W = 230;
const NODE_W = 188;
const NODE_H = 44;
export const ROW_H = 56;
const PAD = 20;

const text = (v, fallback = '') => (typeof v === 'string' && v ? v : fallback);
const clip = (s, n) => (String(s).length > n ? `${String(s).slice(0, n - 1)}…` : String(s));
const idTail = (id) => String(id).replace(/^[a-z]+:/, '');

// ---- how things look: shape / dash / glyph + word, never colour alone -------------------------------------
export const EDGE_STYLE = {
  spawn: { dash: '', tag: 'spawn', word: 'spawned' },
  handoff: { dash: '8 4', tag: 'handoff', word: 'handed off to', label: 'handoff' },
  delegation: { dash: '2 3', tag: 'delegates', word: 'delegated to' },
  consultation: { dash: '10 3 2 3', tag: 'consults', word: 'consulted' },
  dependency: { dash: '5 5', tag: 'needs', word: 'depends on', label: 'dependency' },
  issue_link: { dash: '1 4', tag: 'issue', word: 'is linked to issue/PR' },
  fork: { dash: '12 4', tag: 'fork', word: 'forked into', label: 'branch' },
  supersession: { dash: '3 2 9 2', tag: 'supersedes', word: 'supersedes' },
  reviewed: { dash: '6 2 2 2', tag: 'reviewed', word: 'reviewed', label: 'review' },
  produced: { dash: '14 3', tag: 'produced', word: 'produced' },
  validated: { dash: '4 2', tag: 'validated', word: 'validated' },
  // system-intelligence edge kinds (spec 02): observed topology vs static intent.
  calls: { dash: '1 1', tag: 'calls', word: 'calls' },
  observed_in: { dash: '1 2', tag: 'seen-in', word: 'observed in' },
  belongs_to: { dash: '2 2', tag: 'part-of', word: 'belongs to' },
  blocked_by: { dash: '3 1', tag: 'blocked-by', word: 'blocked by' },
  implements: { dash: '2 6', tag: 'implements', word: 'implements' },
  references: { dash: '2 3 6 3', tag: 'references', word: 'references' },
  runs_on: { dash: '6 1 2 1', tag: 'runs-on', word: 'runs on' },
  serves: { dash: '5 1', tag: 'serves', word: 'serves' },
  deploys_to: { dash: '3 3 1 3', tag: 'deploys-to', word: 'deploys to' },
  assigned_to: { dash: '4 4 1 4', tag: 'assigned-to', word: 'assigned to' },
  precedes: { dash: '1 5', tag: 'precedes', word: 'precedes' },
  imports: { dash: '2 2 2 2', tag: 'imports', word: 'imports' },
  routes_to: { dash: '8 1 2 1', tag: 'routes-to', word: 'routes to' },
  queued_on: { dash: '3 5', tag: 'queued-on', word: 'queued on' },
};
export const EDGE_TYPES = Object.keys(EDGE_STYLE);
// The word drawn on a link in the graph: the four kinds a person asks about (handoff, branch, review, dependency) are spelled out.
export const edgeLabel = (type) => { const st = EDGE_STYLE[type]; return st ? (st.label || st.tag) : String(type); };

// A three-letter chip on a node, so session / attempt / agent (all round) and task / artifact / ci are told apart by a WORD, not by colour.
export const KIND_CHIP = { agent: 'AGT', attempt: 'ATT', session: 'SES', task: 'TSK', artifact: 'ART', ci: 'CI', pr: 'PR', review: 'REV', handoff: 'HOF', gate: 'GATE', mission: 'MIS' };

const SHAPE = {
  agent: 'round', attempt: 'round', session: 'round', tab: 'round', pane: 'round', worktree: 'round',
  task: 'rect', plan: 'rect', mission: 'rect', room: 'rect', workspace: 'rect', approach: 'rect',
  issue: 'tag', pr: 'tag', commit: 'tag', ci: 'tag', artifact: 'tag',
  review: 'diamond', finding: 'diamond', gate: 'diamond', decision: 'diamond',
  handoff: 'hex', thread: 'hex', message: 'hex',
  // system-intelligence node kinds (spec 02): infrastructure/repo/trace shapes.
  repository: 'round', branch: 'round', module: 'round', package: 'tag',
  service: 'round', endpoint: 'round', deployment: 'round', host: 'hex',
  gpu: 'hex', model: 'round', worker: 'round',
  request: 'tag', span: 'tag', trace: 'tag', queue: 'tag', dataset: 'tag',
  memory_reference: 'tag', external_dependency: 'tag', symbol: 'tag',
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
// Why the merged graph is incomplete, across every page loaded so far. One page leaving out links (an edge or byte budget) keeps
// the merged view incomplete even when a later page is whole, so the reasons are OR-ed: a scan budget (more nodes may exist) wins,
// otherwise the most recent reason any page gave; null only when no page was truncated.
export function pagesTruncated(pages) {
  const reasons = (pages || []).map((p) => (p && p.truncated_reason) || null).filter(Boolean);
  if (!reasons.length) return null;
  return reasons.includes('scan_budget') ? 'scan_budget' : reasons[reasons.length - 1];
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

// ---- viewport: pan / zoom / focus / culling (pure) ------------------------------------------------------------
// A view is {x, y, k}: the graph-space point at the top-left corner of the viewport and the scale (screen px per graph unit).
const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
export const clampZoom = (k) => clamp(Number.isFinite(k) ? k : 1, ZOOM_MIN, ZOOM_MAX);

// The whole layout in the viewport; never magnified past 1:1, so a small graph looks as it always did. A graph too wide to read
// at FIT_FLOOR (a chain of 60 sessions is 13,800 units wide) is shown from its start at that scale instead of as unreadable specks;
// the Latest button and the End key take you to the other end.
export const FIT_FLOOR = 0.75;
export function fitView(layout, vw, vh) {
  const k = clampZoom(Math.max(FIT_FLOOR, Math.min(1, vw / Math.max(1, layout.width), vh / Math.max(1, layout.height))));
  return { x: 0, y: 0, k };
}
export const visibleRect = (view, vw, vh) => ({ x0: view.x, y0: view.y, x1: view.x + vw / view.k, y1: view.y + vh / view.k });
// Keep some of the graph in sight: a view may not be dragged entirely off the layout.
export function clampView(view, layout, vw, vh) {
  const k = clampZoom(view.k);
  const w = vw / k;
  const h = vh / k;
  return { k, x: clamp(view.x, -w + COL_W / 2, Math.max(0, layout.width - COL_W / 2)), y: clamp(view.y, -h + ROW_H, Math.max(0, layout.height - ROW_H)) };
}
// Zoom by `factor` keeping the graph point under (cx, cy) (viewport pixels) where it is.
export function zoomAt(view, factor, cx, cy) {
  const k = clampZoom(view.k * factor);
  const gx = view.x + cx / view.k;
  const gy = view.y + cy / view.k;
  return { k, x: gx - cx / k, y: gy - cy / k };
}
export const panBy = (view, dxPx, dyPx) => ({ ...view, x: view.x + dxPx / view.k, y: view.y + dyPx / view.k });
// Centre a placed node in the viewport (the zoom is kept).
export function centerOn(view, p, vw, vh) {
  return { ...view, x: p.x + p.w / 2 - vw / view.k / 2, y: p.y + p.h / 2 - vh / view.k / 2 };
}
export function isVisible(view, p, vw, vh) {
  const r = visibleRect(view, vw, vh);
  return p.x + p.w >= r.x0 && p.x <= r.x1 && p.y + p.h >= r.y0 && p.y <= r.y1;
}
// The viewport's pixel size: as large as the layout up to the room the page has (so a small graph is not given empty space).
export const viewportSize = (layout, maxW = 960, maxH = VIEW_H) => ({
  w: Math.max(1, Math.min(maxW, layout ? layout.width : maxW)), h: Math.max(160, Math.min(maxH, layout ? layout.height : maxH)) });
export const zoomPercent = (view) => `${Math.round(view.k * 100)}%`;

// Which layers a graph needs: the identity of its content, so the layout is not rebuilt when only the viewport moved.
export function graphSignature(graph) {
  let h = 2166136261;
  const mix = (str) => { for (let i = 0; i < str.length; i += 1) { h ^= str.charCodeAt(i); h = Math.imul(h, 16777619); } h ^= 124; };
  for (const n of graph.nodes) mix(n.id);
  for (const e of graph.edges) { mix(e.edge_type); mix(e.from_id); mix(e.to_id); }
  return `${graph.nodes.length}/${graph.edges.length}/${h >>> 0}`;
}
const layoutMemo = { sig: null, layout: null };
export function layoutCached(graph) {
  const sig = graphSignature(graph);
  if (layoutMemo.sig !== sig) { layoutMemo.sig = sig; layoutMemo.layout = layoutGraph(graph); }
  else for (const n of graph.nodes) { const p = layoutMemo.layout.placed.get(n.id); if (p) p.node = n; }   // same shape, fresh state/label
  return layoutMemo.layout;
}
// edge indexes by endpoint, built once per layout (so culling touches only edges at drawn nodes)
function adjacency(layout, graph) {
  if (layout.adj) return layout.adj;
  const adj = new Map();
  graph.edges.forEach((e, i) => {
    if (e.from_id === e.to_id) return;
    for (const id of [e.from_id, e.to_id]) { if (!adj.has(id)) adj.set(id, []); adj.get(id).push(i); }
  });
  layout.adj = adj;
  return adj;
}

/** What the viewport draws: the nodes whose boxes meet `rect` (graph space), found by column and row arithmetic rather than a scan,
 * capped at MAX_DRAWN_NODES (the ones nearest the middle of the rect win; `keep` ids are always drawn), and the edges that touch
 * a drawn node, capped at MAX_DRAWN_EDGES (edges to the selection first). Counts of what was left out are returned so the page can say so. */
export function cullGraph(graph, layout, rect, { keep = [], sel = null } = {}) {
  const margin = COL_W / 2;
  const c0 = Math.max(0, Math.floor((rect.x0 - margin - PAD) / COL_W));
  const c1 = Math.min(layout.columns.length - 1, Math.ceil((rect.x1 + margin - PAD) / COL_W));
  const r0 = Math.max(0, Math.floor((rect.y0 - ROW_H - PAD) / ROW_H));
  const r1 = Math.ceil((rect.y1 + ROW_H - PAD) / ROW_H);
  const cx = (rect.x0 + rect.x1) / 2;
  const cy = (rect.y0 + rect.y1) / 2;
  const inView = [];
  for (let c = c0; c <= c1; c += 1) {
    const col = layout.columns[c] || [];
    for (let r = r0; r <= Math.min(r1, col.length - 1); r += 1) inView.push(layout.placed.get(col[r]));
  }
  const pinned = new Set(keep.filter((id) => layout.placed.has(id)));
  let drawn = inView;
  if (inView.length > MAX_DRAWN_NODES) {
    const dist = (p) => Math.abs(p.x + p.w / 2 - cx) / COL_W + Math.abs(p.y + p.h / 2 - cy) / ROW_H;
    drawn = [...inView].sort((a, b) => dist(a) - dist(b) || (a.node.id < b.node.id ? -1 : 1)).slice(0, MAX_DRAWN_NODES);
  }
  const ids = new Set(drawn.map((p) => p.node.id));
  for (const id of pinned) if (!ids.has(id)) { ids.add(id); drawn = [...drawn, layout.placed.get(id)]; }
  const adj = adjacency(layout, graph);
  const seen = new Set();
  const cand = [];
  for (const id of ids) for (const i of adj.get(id) || []) if (!seen.has(i)) { seen.add(i); cand.push(i); }
  cand.sort((a, b) => {
    const ea = graph.edges[a]; const eb = graph.edges[b];
    const sa = sel && (ea.from_id === sel || ea.to_id === sel) ? 0 : 1;
    const sb = sel && (eb.from_id === sel || eb.to_id === sel) ? 0 : 1;
    return sa - sb || a - b;
  });
  const edges = cand.slice(0, MAX_DRAWN_EDGES).sort((a, b) => a - b).map((i) => graph.edges[i]);
  return { nodes: drawn, edges, inView: inView.length, total: layout.placed.size, hiddenEdges: Math.max(0, cand.length - MAX_DRAWN_EDGES) };
}

/** Focus on one lineage: the node, everything it descends from and everything that descends from it (following links in their
 * stated direction). Nothing is inferred; a node on no link focuses to itself. */
export function focusSubgraph(graph, id) {
  if (!graph.nodes.some((n) => n.id === id)) return graph;
  const out = new Map();
  const inn = new Map();
  for (const e of graph.edges) {
    if (!out.has(e.from_id)) out.set(e.from_id, []);
    if (!inn.has(e.to_id)) inn.set(e.to_id, []);
    out.get(e.from_id).push(e.to_id); inn.get(e.to_id).push(e.from_id);
  }
  const keep = new Set([id]);
  for (const dir of [out, inn]) {
    const stack = [id];
    const seen = new Set([id]);
    while (stack.length) for (const nx of dir.get(stack.pop()) || []) if (!seen.has(nx)) { seen.add(nx); keep.add(nx); stack.push(nx); }
  }
  return { ...graph, nodes: graph.nodes.filter((n) => keep.has(n.id)), edges: graph.edges.filter((e) => keep.has(e.from_id) && keep.has(e.to_id)) };
}

/** Counts for the status strip, over the nodes loaded so far; absent states are "no state recorded", never guessed. */
export function statusSummary(graph) {
  const counts = { active: 0, waiting: 0, finished: 0, failed: 0, stopped: 0, other: 0, unknown: 0 };
  const attention = [];
  const approvals = [];
  for (const n of graph.nodes) {
    const kind = stateLook(n.state).kind;
    counts[kind] += 1;
    if (kind === 'failed') attention.push(n);
    else if (kind === 'waiting' && n.node_type === 'gate') approvals.push(n);
    else if (kind === 'waiting') attention.push(n);
  }
  attention.sort((a, b) => (stateLook(a.state).kind === 'failed' ? 0 : 1) - (stateLook(b.state).kind === 'failed' ? 0 : 1) || (a.id < b.id ? -1 : 1));
  return { counts, attention, approvals, total: graph.nodes.length };
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
  const byShape = new Map();
  for (const t of NODE_TYPES) byShape.set(shapeOf(t), [...(byShape.get(shapeOf(t)) || []), t]);
  const shapeOrder = ['round', 'rect', 'tag', 'diamond', 'hex'];
  const shapes = shapeOrder
    .filter((s) => byShape.has(s))
    .map((s) => `<li>${esc(s)}: ${esc(byShape.get(s).sort().join(', '))}</li>`).join('');
  const states = GLYPH.map(([, g, k]) => `<li><span aria-hidden="true">${g}</span> ${esc(k)}</li>`).join('') + '<li><span aria-hidden="true">○</span> no state recorded</li>';
  const chips = Object.entries(KIND_CHIP).map(([t, c]) => `<li><b>${esc(c)}</b> ${esc(t)}</li>`).join('');
  return `<details class="lin-legend"><summary>Legend</summary><div class="lin-legend__cols"><ul aria-label="Link types">${edges}</ul><ul aria-label="Node shapes">${shapes}</ul><ul aria-label="Node kind chips">${chips}</ul><ul aria-label="States">${states}</ul></div></details>`;
}

// The graph as an SVG viewport. `sel` is the selected node id; `focus` is the roving-tabindex node. `view` is {x, y, k}
// (default: the whole layout, fitted) in a `vw` x `vh` pixel viewport. Only what is in view is drawn (see cullGraph).
export function renderGraphSvg(graph, layout, { sel = null, focus = null, match = new Set(), view = null, vw = null, vh = null } = {}) {
  const W = vw || Math.max(1, layout.width);
  const H = vh || Math.max(1, layout.height);
  const v = view || fitView(layout, W, H);
  const tabId = focus && layout.placed.has(focus) ? focus : layout.columns.flat()[0];
  const cull = cullGraph(graph, layout, visibleRect(v, W, H), { keep: [sel, tabId].filter(Boolean), sel });
  const far = v.k < 0.45;                      // zoomed out: names only, no second line and no link words, to keep the DOM small
  const neighbours = new Set();
  if (sel) for (const e of cull.edges) { if (e.from_id === sel) neighbours.add(e.to_id); if (e.to_id === sel) neighbours.add(e.from_id); }
  const edges = cull.edges.map((e) => {
    const a = layout.placed.get(e.from_id);
    const b = layout.placed.get(e.to_id);
    if (!a || !b || e.from_id === e.to_id) return '';
    const p = edgePath(a, b, a.level >= b.level);
    const st = EDGE_STYLE[e.edge_type];
    const dim = sel && e.from_id !== sel && e.to_id !== sel ? ' lin-dim' : '';
    const stale = e.stale ? ' lin-edge--stale' : '';
    const tag = `${edgeLabel(e.edge_type)}${e.stale ? ' (stale)' : ''}`;
    return `<g class="lin-edge${stale}${dim}" data-edge-type="${esc(e.edge_type)}"><title>${esc(`${idTail(e.from_id)} ${st.word} ${idTail(e.to_id)}${bindingText(e.binding) ? ` at ${bindingText(e.binding)}` : ''}${e.stale ? '. Stale: a newer revision exists.' : ''}`)}</title>`
      + `<path class="lin-edge-line" d="${p.d}" fill="none" stroke-dasharray="${esc(st.dash)}" marker-end="url(#lin-arrow)"/>`
      + `${far ? '' : `<text class="lin-edge-tag" x="${p.mx}" y="${p.my - 3}" text-anchor="middle">${esc(clip(tag, 22))}</text>`}</g>`;
  }).join('');
  const nodes = cull.nodes.map((p) => {
    const n = p.node;
    const look = stateLook(n.state);
    const on = n.id === sel ? ' lin-node--sel' : '';
    const nb = sel && n.id !== sel && !neighbours.has(n.id) ? ' lin-dim' : '';
    const hit = match.has(n.id) ? ' lin-node--hit' : '';
    const chip = KIND_CHIP[n.node_type];
    return `<g class="lin-node lin-node--${esc(look.kind)} lin-k-${esc(n.node_type)}${on}${nb}${hit}" role="button" tabindex="${n.id === tabId ? 0 : -1}" data-node="${esc(n.id)}" aria-pressed="${n.id === sel}" aria-label="${esc(nodeLabel(n, layout.degree.get(n.id)))}">`
      + `${nodeShape(p, 'lin-box')}`
      + `<text class="lin-name" x="${p.x + 16}" y="${p.y + 18}"><tspan aria-hidden="true">${look.glyph} </tspan>${esc(clip(text(n.label, idTail(n.id)), 22))}</text>`
      + `${far ? '' : `<text class="lin-sub" x="${p.x + 16}" y="${p.y + 33}">${esc(clip(`${n.node_type} · ${look.word}`, 30))}</text>`}`
      + `${chip && !far ? `<g aria-hidden="true" class="lin-chip lin-chip--${esc(n.node_type)}"><rect x="${p.x + p.w - 42}" y="${p.y + 3}" width="${chip.length * 6 + 10}" height="13" rx="3"/><text x="${p.x + p.w - 37}" y="${p.y + 13}">${esc(chip)}</text></g>` : ''}</g>`;
  }).join('');
  const note = cull.inView > MAX_DRAWN_NODES
    ? `<p class="cx-msg lin-drawnote" role="status">${cull.inView} nodes are in view; the ${MAX_DRAWN_NODES} nearest the middle are drawn. Zoom in, narrow the filters or use the table for the rest.</p>`
    : (cull.hiddenEdges ? `<p class="cx-msg lin-drawnote" role="status">${cull.hiddenEdges} links are not drawn at this zoom. Zoom in or use the table.</p>` : '');
  return `<svg class="lin-svg" width="${W}" height="${H}" viewBox="${v.x} ${v.y} ${W / v.k} ${H / v.k}" data-vw="${W}" data-vh="${H}" data-drawn="${cull.nodes.length}" role="group" aria-label="Lineage graph: ${esc(String(graph.nodes.length))} nodes and ${esc(String(graph.edges.length))} links. Use the arrow keys to move between nodes, plus and minus to zoom, Shift with the arrow keys to pan. A table view lists the same data.">`
    + '<defs><marker id="lin-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" class="lin-arrowhead"/></marker></defs>'
    + `${edges}${nodes}</svg>${note}`;
}

// The controls over the graph: zoom, fit, focus on one lineage. Plain buttons (keyboard and screen reader operable); the page binds them.
export function renderGraphToolbar({ view, focusId = null, sel = null } = {}) {
  const v = view || { k: 1 };
  const btn = (act, label, text, extra = '') => `<button type="button" class="hd-btn hd-btn--ghost hd-btn--sm lin-tool" data-vp="${act}" aria-label="${esc(label)}"${extra}>${text}</button>`;
  return `<div class="lin-toolbar" role="toolbar" aria-label="Graph view">`
    + btn('out', 'Zoom out', '−') + btn('in', 'Zoom in', '+') + btn('fit', 'Fit the graph, or its start when it is too wide to read', 'Fit') + btn('end', 'Go to the latest end of the graph', 'Latest')
    + `<span class="lin-zoom" role="status" aria-label="Zoom">${esc(zoomPercent(v))}</span>`
    + (focusId
      ? btn('unfocus', 'Show the whole graph again', 'Show all', ' aria-pressed="true"')
      : btn('focus', sel ? 'Focus on the lineage of the selected node: its ancestors and descendants only' : 'Select a node first to focus on its lineage', 'Focus lineage', sel ? ' aria-pressed="false"' : ' disabled'))
    + '</div>';
}

// Live or replay, as a word and a glyph (never colour alone). Live is the head of the ledger; a replay is a past position.
export function renderModeBadge(asOf) {
  if (!asOf) return '<span class="lin-badge" data-mode-badge="unknown"><span aria-hidden="true">○</span> No position</span>';
  if (asOf.head) return '<span class="lin-badge lin-badge--live" data-mode-badge="live"><span aria-hidden="true">●</span> Live</span>';
  return `<span class="lin-badge lin-badge--replay" data-mode-badge="replay"><span aria-hidden="true">⏪</span> Replay${asOf.step === 0 ? ': before the first event' : `: event ${esc(String(asOf.step))} of ${esc(String(asOf.visible_events))}`}</span>`;
}

// The first thing on a small screen: what is the state of the work, what needs a person, what is waiting for approval.
// Measured over the nodes loaded so far; a graph with none of a kind says so rather than showing a zero as if it were known.
export function renderStatusStrip(graph, { truncated = false } = {}) {
  const s = statusSummary(graph);
  if (!s.total) return '';
  const c = s.counts;
  const chips = [['●', 'active', c.active], ['◐', 'waiting', c.waiting], ['✕', 'failed', c.failed], ['✓', 'finished', c.finished]]
    .filter(([, , n]) => n > 0).map(([g, w, n]) => `<li><span aria-hidden="true">${g}</span> ${n} ${w}</li>`).join('');
  const pick = (list) => list.slice(0, 3).map((n) => `<li><button type="button" class="hd-btn hd-btn--ghost hd-btn--sm" data-node="${esc(n.id)}">${esc(stateLook(n.state).glyph)} ${esc(clip(text(n.label, idTail(n.id)), 28))} <span class="cx-msg">${esc(n.node_type)}, ${esc(stateLook(n.state).word)}</span></button></li>`).join('');
  const more = (list) => (list.length > 3 ? `<li class="cx-msg">and ${list.length - 3} more</li>` : '');
  return `<section class="lin-status" aria-label="Status of the work">`
    + `<ul class="lin-status__counts" aria-label="Nodes by state">${chips || '<li>no states recorded</li>'}</ul>`
    + `<div class="lin-status__need"><h3 class="lin-status__h">Needs attention</h3>${s.attention.length ? `<ul class="lin-status__list">${pick(s.attention)}${more(s.attention)}</ul>` : '<p class="cx-msg">Nothing failed or blocked in the nodes loaded.</p>'}</div>`
    + `<div class="lin-status__need"><h3 class="lin-status__h">Approvals waiting</h3>${s.approvals.length ? `<ul class="lin-status__list">${pick(s.approvals)}${more(s.approvals)}</ul>` : '<p class="cx-msg">No gate is waiting.</p>'}</div>`
    + `${truncated ? '<p class="cx-msg">Counts cover the nodes loaded so far, not the whole room.</p>' : ''}</section>`;
}

// The same data as plain tables (the text alternative; also the view for very large graphs).
export function renderGraphTable(graph, { sel = null } = {}) {
  const deg = new Map(graph.nodes.slice(0, MAX_TABLE_ROWS).map((n) => [n.id, { in: 0, out: 0 }]));       // degrees only: no layout needed for a table
  for (const e of graph.edges) {
    if (deg.has(e.from_id)) deg.get(e.from_id).out += 1;
    if (deg.has(e.to_id)) deg.get(e.to_id).in += 1;
  }
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

export function renderReplay(asOf, { busy = false, queued = false } = {}) {
  const n = asOf ? asOf.visible_events : 0;
  const k = asOf ? asOf.step : 0;
  // Busy keeps every control enabled (so focus is never lost to a disabled button); the handler ignores clicks.
  const dis = !n ? ' disabled' : '';
  const trimmed = asOf && asOf.window_trimmed ? '<p class="cx-msg">Only the newest events you can see are scrubbable, and a replay shows those events alone; older history is not part of it.</p>' : '';
  return `<div class="lin-replay" role="group" aria-label="Replay"${busy ? ' aria-busy="true"' : ''}>`
    + `${renderModeBadge(asOf)}<button type="button" class="hd-btn hd-btn--ghost hd-btn--sm" data-replay="first"${dis}>⏮ Start</button>`
    + `<button type="button" class="hd-btn hd-btn--ghost hd-btn--sm" data-replay="prev"${dis}>◀ Back</button>`
    + `<label class="lin-slider">Position<input type="range" data-replay="slider" min="0" max="${n}" step="1" value="${k}" aria-valuetext="${esc(replayText(asOf))}"${dis}></label>`
    + `<button type="button" class="hd-btn hd-btn--ghost hd-btn--sm" data-replay="next"${dis}>Next ▶</button>`
    + `<button type="button" class="hd-btn hd-btn--ghost hd-btn--sm" data-replay="last"${dis}>End ⏭</button>`
    + `<button type="button" class="hd-btn hd-btn--sm" data-replay="live"${asOf && asOf.head ? ' aria-pressed="true"' : ''}>Live</button>`
    + `<output class="lin-replay__status" role="status" aria-live="polite">${busy ? (queued ? 'Loading. Your latest choice will load next.' : 'Loading the position.') : esc(replayText(asOf))}</output>${trimmed}</div>`;
}

// Re-rendering replaces the panel's HTML, so the focused control is remembered by a stable key and found again.
const attr = (el, k) => (el && el.getAttribute ? el.getAttribute(k) : null);
export function focusKey(el) {
  if (!el || !el.getAttribute) return null;
  const f = attr(el, 'data-filter');
  if (attr(el, 'data-node') !== null) return `node:${attr(el, 'data-node')}`;
  if (attr(el, 'data-replay') !== null) return `replay:${attr(el, 'data-replay')}`;
  if (attr(el, 'data-view') !== null) return `view:${attr(el, 'data-view')}`;
  if (attr(el, 'data-vp') !== null) return `vp:${attr(el, 'data-vp')}`;
  if (attr(el, 'data-mode') !== null) return 'mode:';
  if (attr(el, 'data-clock') !== null) return 'clock:';
  if (attr(el, 'data-more') !== null) return 'more:';
  if (attr(el, 'data-retry') !== null) return 'retry:';
  if (f !== null) return (f === 'type' || f === 'etype') ? `filter:${f}:${attr(el, 'value')}` : `filter:${f}:`;
  const wf = attr(el, 'data-wb-field');
  if (wf !== null) return wf === 'action' ? `wbf:action:${attr(el, 'value')}` : `wbf:${wf}:`;
  const wa = attr(el, 'data-wb-act');
  if (wa !== null) return `wba:${wa}:${attr(el, 'data-wb-id') || ''}`;
  // a form's submit button (Post comment, Propose, Save edit) is rebuilt with the form after the request completes
  if (attr(el, 'type') === 'submit' && el.closest) { const form = el.closest('[data-wb-form]'); if (form) return `wbs:${attr(form, 'data-wb-form')}:${attr(form, 'data-wb-id') || ''}`; }
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
    case 'vp': return `[data-vp="${q(v)}"]`;
    case 'mode': return '[data-mode]';
    case 'clock': return '[data-clock]';
    case 'more': return '[data-more]';
    case 'retry': return '[data-retry]';
    case 'filter': { const [name, ...val] = rest; return val.join(':') ? `[data-filter="${q(name)}"][value="${q(val.join(':'))}"]` : `[data-filter="${q(name)}"]`; }
    case 'wbf': { const [name, ...val] = rest; return val.join(':') ? `[data-wb-field="${q(name)}"][value="${q(val.join(':'))}"]` : `[data-wb-field="${q(name)}"]`; }
    case 'wbs': { const [form, ...id] = rest; return `[data-wb-form="${q(form)}"]${id.join(':') ? `[data-wb-id="${q(id.join(':'))}"]` : ''} button[type="submit"]`; }
    case 'wba': { const [act, ...id] = rest; return id.join(':') ? `[data-wb-act="${q(act)}"][data-wb-id="${q(id.join(':'))}"]` : `[data-wb-act="${q(act)}"]`; }
    default: return null;
  }
}

// When the focused control is replaced by a different one (a button that opens a step that has its own buttons), focus goes to the
// logical successor, not to the container: "Review and confirm" opens the confirm step, so focus lands on "Confirm and send";
// "Cancel" closes it, so focus returns to "Review and confirm". Returns selectors to try in order; none means "park on the panel".
export function focusSuccessor(key) {
  const q = (v) => String(v).replace(/["\\]/g, '\\$&');
  const [kind, act, ...id] = String(key || '').split(':');
  const ident = id.join(':');
  if (kind !== 'wba') return [];
  if (act === 'confirm') return [ident ? `[data-wb-act="confirm-go"][data-wb-id="${q(ident)}"]` : '', '[data-wb-act="confirm-go"]'].filter(Boolean);
  if (act === 'confirm-cancel') return ['[data-wb-act="confirm"]'];
  // the request was sent (or declined): stay on that request, on its remaining action if it has one, else on the row itself
  if ((act === 'confirm-go' || act === 'reject') && ident) return [`[data-wb-act="reject"][data-wb-id="${q(ident)}"]`, `[data-wb-steer="${q(ident)}"]`];
  return [];
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

export function renderGanttSvg(g, { width = 900, sel = null } = {}) {
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
    const hl = sel && l.node_id === sel ? `<rect class="lin-lane-hl" x="0" y="${y}" width="${LABEL + PLOT + 24}" height="${ROW}" aria-hidden="true"/>` : '';
    return `<g data-lane="${esc(l.node_id)}"${hl ? ' data-selected="1"' : ''}>${hl}<text class="lin-name" x="4" y="${y + 17}">${esc(clip(`${l.node_type} ${l.label}`, 28))}</text>${spans}${lone}</g>`;
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
export const NOT_REPORTED = 'not reported';
// An artifact's attributes that point at where the bytes live. The inspector never prints them: it shows version and digest only.
// For an artifact only these attributes are printed (an allowlist: a new storage-location key can never leak by being unknown).
const ARTIFACT_ATTRS = new Set(['kind', 'version_number', 'version_id', 'parent_version_id', 'content_digests', 'digest', 'sha256', 'sequence', 'commit', 'checkpoint_sha', 'source', 'mission_id', 'session_id', 'agent_id', 'job_id', 'host_id', 'host']);
const short = (v, n = 12) => (typeof v === 'string' && v ? (v.length > n ? `${v.slice(0, n)}…` : v) : null);
const firstText = (o, ...keys) => { for (const k of keys) { const v = o && o[k]; if (typeof v === 'string' && v.trim()) return v.trim(); if (typeof v === 'number' && Number.isFinite(v)) return String(v); } return null; };
const asList = (v) => (Array.isArray(v) ? v.filter((x) => typeof x === 'string' && x.trim()).map((x) => x.trim()) : (typeof v === 'string' && v.trim() ? [v.trim()] : []));
const withKind = (id, kind) => (typeof id === 'string' && id ? (id.includes(':') ? id : `${kind}:${id}`) : null);

/** The measured facts the inspector shows for one node. Every field is either a value taken from a node, an edge or an attribute of
 * the response, or null ("not reported"); nothing is derived from a label, estimated or defaulted. */
export function inspectorFacts(graph, id) {
  const byId = new Map(graph.nodes.map((n) => [n.id, n]));
  const n = byId.get(id);
  if (!n) return null;
  const a = n.attrs || {};
  const outE = graph.edges.filter((e) => e.from_id === id);
  const inE = graph.edges.filter((e) => e.to_id === id);
  const look = (x) => ({ id: x.id, label: text(x.label, idTail(x.id)), type: x.node_type, state: x.state || null });
  // mission: the node itself, a mission named by attrs.mission_id, else a directly linked mission node
  const mid = n.node_type === 'mission' ? n.id : withKind(a.mission_id, 'mission');
  const mission = byId.get(mid) || [...outE.map((e) => e.to_id), ...inE.map((e) => e.from_id)].map((x) => byId.get(x)).find((x) => x && x.node_type === 'mission') || null;
  const missionStatus = mission ? { ...look(mission), reason: mission.state_reason || null } : (firstText(a, 'mission_status') ? { id: null, label: null, type: 'mission', state: firstText(a, 'mission_status'), reason: null } : null);
  const blockers = [];
  for (const e of outE) if (e.edge_type === 'blocked_by' && byId.has(e.to_id)) blockers.push({ ...look(byId.get(e.to_id)), why: 'blocked by' });
  for (const t of asList(a.blockers)) blockers.push({ id: null, label: clip(t, 160), why: 'reported blocker' });
  const br = firstText(a, 'blocked_reason', 'blocker');
  if (br) blockers.push({ id: null, label: clip(br, 160), why: 'reported blocker' });
  if (stateLook(n.state).kind === 'waiting' && n.state_reason) blockers.push({ id: null, label: clip(n.state_reason, 160), why: `state is ${n.state}` });
  const nextAction = firstText(a, 'next_action', 'next_step');
  // artifacts: produced by this node (or by the attempt it names), or this node itself
  const owners = new Set([id, withKind(a.attempt_id, 'attempt')].filter(Boolean));
  const artifactIds = new Set(n.node_type === 'artifact' ? [id] : []);
  for (const e of graph.edges) if (e.edge_type === 'produced' && owners.has(e.from_id) && byId.get(e.to_id) && byId.get(e.to_id).node_type === 'artifact') artifactIds.add(e.to_id);
  const artifacts = [...artifactIds].sort().map((aid) => {
    const x = byId.get(aid);
    const xa = x.attrs || {};
    const digests = Array.isArray(xa.content_digests) ? xa.content_digests.filter((d) => typeof d === 'string' && d) : [];
    const num = firstText(xa, 'version_number');
    return { ...look(x), kind: firstText(xa, 'kind'), version: num ? `v${num}` : (firstText(xa, 'sequence') ? `#${firstText(xa, 'sequence')}` : firstText(xa, 'version_id', 'version')),
      digest: short(digests[0] || firstText(xa, 'digest', 'sha256', 'commit', 'checkpoint_sha')), digestCount: digests.length };
  });
  // PR / CI / review within two links (the shortest path decides how far; the hop count is shown)
  const adj = new Map();
  for (const e of graph.edges) {
    if (!adj.has(e.from_id)) adj.set(e.from_id, []);
    if (!adj.has(e.to_id)) adj.set(e.to_id, []);
    adj.get(e.from_id).push({ to: e.to_id, e }); adj.get(e.to_id).push({ to: e.from_id, e });
  }
  const hops = new Map([[id, 0]]);
  const queue = [id];
  while (queue.length) {
    const cur = queue.shift();
    if (hops.get(cur) >= 2) continue;
    for (const { to } of adj.get(cur) || []) if (!hops.has(to)) { hops.set(to, hops.get(cur) + 1); queue.push(to); }
  }
  const stale = new Set(graph.edges.filter((e) => e.stale).flatMap((e) => [e.from_id, e.to_id]));
  const delivery = { pr: [], ci: [], review: [] };
  for (const [nid, h] of hops) {
    const x = byId.get(nid);
    if (x && delivery[x.node_type]) delivery[x.node_type].push({ ...look(x), hops: h, stale: stale.has(nid), headSha: short(firstText(x.attrs || {}, 'head', 'head_sha', 'commit')) });
  }
  for (const k of Object.keys(delivery)) delivery[k].sort((p, q) => p.hops - q.hops || (p.id < q.id ? -1 : 1));
  // handoff: this node is a handoff record, or a session that was handed work / handed it on
  const handoffEdges = graph.edges.filter((e) => e.edge_type === 'handoff');
  const hnode = (hid) => byId.get(hid) || null;
  const describe = (e, otherId) => {
    const ea = e.attrs || {};
    const h = hnode(ea.handoff_id);
    const ha = (h && h.attrs) || {};
    return { other: otherId, otherLabel: byId.get(otherId) ? text(byId.get(otherId).label, idTail(otherId)) : idTail(otherId),
      checkpoint: short(firstText(ha, 'checkpoint_sha') || firstText(ea, 'checkpoint_sha'), 12), cause: firstText(ea, 'cause', 'handoff_kind') || firstText(ha, 'handoff_kind', 'cause'),
      handoffId: firstText(ea, 'handoff_id') };
  };
  let handoff = null;
  if (n.node_type === 'handoff') {
    const e = handoffEdges.find((x) => (x.attrs || {}).handoff_id === id);
    handoff = { record: true, from: firstText(a, 'from_session_id'), to: firstText(a, 'to_session_id'), checkpoint: short(firstText(a, 'checkpoint_sha'), 12),
      cause: (e && firstText(e.attrs || {}, 'cause')) || firstText(a, 'handoff_kind', 'cause'), state: n.state || null };
  } else if (n.node_type === 'session' || n.node_type === 'attempt' || n.node_type === 'agent') {
    const from = handoffEdges.filter((e) => e.to_id === id).map((e) => describe(e, e.from_id));
    const to = handoffEdges.filter((e) => e.from_id === id).map((e) => describe(e, e.to_id));
    handoff = { record: false, from, to };
  }
  const upstream = [...new Set(inE.map((e) => e.from_id))].length;
  const downstream = [...new Set(outE.map((e) => e.to_id))].length;
  return { node: look(n), missionStatus, blockers, nextAction, artifacts, delivery, handoff, upstream, downstream };
}

const nr = '<span class="lin-nr">not reported</span>';
const nodeBtn = (x, extra = '') => `<button type="button" class="hd-btn hd-btn--ghost hd-btn--sm" data-node="${esc(x.id)}">${esc(clip(x.label, 40))}</button>${extra}`;
const stateTxt = (st) => { const l = stateLook(st); return `${esc(l.glyph)} ${esc(l.word)}`; };

function renderFacts(f) {
  const row = (k, v) => `<dt>${esc(k)}</dt><dd>${v}</dd>`;
  const ms = f.missionStatus;
  const mission = ms ? `${ms.label ? `${esc(clip(ms.label, 40))}: ` : ''}${stateTxt(ms.state)}${ms.reason ? ` (${esc(clip(ms.reason, 120))})` : ''}` : nr;
  const blockers = f.blockers.length ? `<ul class="lin-facts__list">${f.blockers.slice(0, 6).map((b) => `<li>${b.id ? nodeBtn(b) : esc(b.label)} <span class="cx-msg">${esc(b.why)}${b.state ? `, ${esc(stateLook(b.state).word)}` : ''}</span></li>`).join('')}</ul>` : nr;
  const arts = f.artifacts.length ? `<ul class="lin-facts__list">${f.artifacts.slice(0, 6).map((x) => `<li>${nodeBtn(x)} <span class="cx-msg">${esc(x.kind || 'artifact')} · version ${x.version ? esc(x.version) : nr} · digest ${x.digest ? `<code>${esc(x.digest)}</code>${x.digestCount > 1 ? ` +${x.digestCount - 1}` : ''}` : nr}</span></li>`).join('')}${f.artifacts.length > 6 ? `<li class="cx-msg">and ${f.artifacts.length - 6} more</li>` : ''}</ul>` : nr;
  const dl = (list, label) => (list.length ? `<ul class="lin-facts__list">${list.slice(0, 3).map((x) => `<li>${nodeBtn(x)} <span class="cx-msg">${stateTxt(x.state)}${x.headSha ? ` · head <code>${esc(x.headSha)}</code>` : ''}${x.stale ? ' · <strong>stale</strong>' : ''}${x.hops > 1 ? ` · ${x.hops} links away` : ''}</span></li>`).join('')}${list.length > 3 ? `<li class="cx-msg">and ${list.length - 3} more ${esc(label)}</li>` : ''}</ul>` : nr);
  let ho = '';
  const h = f.handoff;
  if (h && h.record) {
    ho = `<h4>Handoff</h4><dl class="lin-dl lin-facts">${row('From session', h.from ? `<code>${esc(h.from)}</code>` : nr)}${row('To session', h.to ? `<code>${esc(h.to)}</code>` : nr)}${row('Checkpoint', h.checkpoint ? `<code>${esc(h.checkpoint)}</code>` : nr)}${row('Cause', h.cause ? esc(h.cause) : nr)}${row('State', h.state ? stateTxt(h.state) : nr)}</dl>`;
  } else if (h) {
    const one = (x) => `<li>${nodeBtn({ id: x.other, label: x.otherLabel })} <span class="cx-msg">checkpoint ${x.checkpoint ? `<code>${esc(x.checkpoint)}</code>` : nr} · cause ${x.cause ? esc(x.cause) : nr}</span></li>`;
    ho = `<h4>Handoff</h4><dl class="lin-dl lin-facts">${row('Received from', h.from.length ? `<ul class="lin-facts__list">${h.from.slice(0, 3).map(one).join('')}</ul>` : 'none recorded')}${row('Handed on to', h.to.length ? `<ul class="lin-facts__list">${h.to.slice(0, 3).map(one).join('')}</ul>` : 'none recorded')}</dl>`;
  }
  return `<h4>Summary</h4><dl class="lin-dl lin-facts" data-facts="1">${row('Mission', mission)}${row('Blockers', blockers)}${row('Next action', f.nextAction ? esc(clip(f.nextAction, 200)) : nr)}`
    + `${row('Artifacts', arts)}${row('Pull request', dl(f.delivery.pr, 'pull requests'))}${row('CI', dl(f.delivery.ci, 'CI runs'))}${row('Review', dl(f.delivery.review, 'reviews'))}`
    + `${row('Upstream', `${f.upstream} node${f.upstream === 1 ? '' : 's'}`)}${row('Downstream', `${f.downstream} node${f.downstream === 1 ? '' : 's'}`)}</dl>${ho}`;
}

// `opts.terminal` is {href} when the caller (the dashboard) has already established that the viewer is the operator and the session
// is one the dashboard knows; the link goes to the dashboard's own session page, which owns the authorized Terminal button.
// Without it the explicit "not offered" text stays: this module never builds a terminal command and never widens authorization.
export function renderInspector(graph, id, tl = null, opts = {}) {
  const n = graph.nodes.find((x) => x.id === id);
  if (!n) return '<div class="hd-empty">Select a node to inspect it. Arrow keys move between nodes; Enter selects.</div>';
  const look = stateLook(n.state);
  const hideBlob = n.node_type === 'artifact';
  const attrs = Object.entries(n.attrs || {}).filter(([k]) => !(hideBlob && !ARTIFACT_ATTRS.has(k))).sort(([a], [b]) => (a < b ? -1 : 1)).slice(0, 40);
  const hiddenAttrs = hideBlob ? Object.keys(n.attrs || {}).length - attrs.length : 0;
  const prov = (n.fields && n.fields.label) || null;
  const links = (dir) => graph.edges.filter((e) => (dir === 'out' ? e.from_id === id : e.to_id === id)).slice(0, 60).map((e) => {
    const other = dir === 'out' ? e.to_id : e.from_id;
    const o = graph.nodes.find((x) => x.id === other);
    return `<li><span class="hd-ident">${esc(EDGE_STYLE[e.edge_type].tag)}</span> <button type="button" class="hd-btn hd-btn--ghost hd-btn--sm" data-node="${esc(other)}">${esc(text(o && o.label, idTail(other)))}</button>`
      + `${bindingText(e.binding) ? ` <code>${esc(bindingText(e.binding))}</code>` : ''}${e.stale ? ' <strong>stale</strong>' : ''}${e.writer ? ` <span class="cx-msg">by ${esc(e.writer)}</span>` : ''}`
      + ` <button type="button" class="hd-btn hd-btn--ghost hd-btn--sm" data-wb-act="edge" data-wb-edge-type="${esc(e.edge_type)}" data-wb-from="${esc(e.from_id)}" data-wb-to="${esc(e.to_id)}" aria-label="Discuss the ${esc(EDGE_STYLE[e.edge_type].tag)} link between ${esc(e.from_id)} and ${esc(e.to_id)}">Discuss</button></li>`;
  }).join('');
  const lane = tl && (tl.lanes || []).find((l) => l.node_id === id);
  const history = lane && lane.spans.length ? `<h4>State history</h4><ol class="cx-list">${lane.spans.map((s) => `<li><span>${esc(stateLook(s.state).glyph)} ${esc(s.state)}${s.open ? ' (ongoing)' : ''}</span><time datetime="${esc(s.start)}">${esc(s.start)}</time></li>`).join('')}</ol>` : '';
  const facts = inspectorFacts(graph, id);
  const term = opts && opts.terminal && typeof opts.terminal.href === 'string' && opts.terminal.href.startsWith('/#/herd/session/')
    ? `<p class="lin-terminal"><a class="hd-btn hd-btn--sm lin-tool" href="${esc(opts.terminal.href)}">Open this session on the dashboard</a> <span class="cx-msg">The Terminal button on that page shows the attach command. It is limited to operators and to live sessions.</span></p>`
    : '<p class="cx-msg">This page offers no terminal and no commands. Discussion and steering requests are below.</p>';
  return `<section class="lin-inspector" aria-label="Inspector for ${esc(text(n.label, idTail(n.id)))}">`
    + `<h3 class="hd-card__title">${esc(text(n.label, idTail(n.id)))}</h3>`
    + `<dl class="lin-dl"><dt>Id</dt><dd><code>${esc(n.id)}</code></dd><dt>Type</dt><dd>${esc(n.node_type)}</dd>`
    + `<dt>State</dt><dd>${look.glyph} ${esc(look.word)}${n.state_reason ? ` (${esc(n.state_reason)})` : ''}</dd>`
    + `<dt>Visibility</dt><dd>${esc(n.visibility_scope || 'unknown')}</dd>`
    + `<dt>First event</dt><dd><code>${esc(n.first_event_id || 'n/a')}</code></dd><dt>Last event</dt><dd><code>${esc(n.last_event_id || 'n/a')}</code></dd>`
    + `${prov ? `<dt>Label written by</dt><dd>${esc(text(prov.writer, 'unknown'))} via ${esc(text(prov.source, 'unknown'))}</dd>` : ''}</dl>`
    + `${facts ? renderFacts(facts) : ''}`
    + `${attrs.length ? `<h4>Attributes</h4><dl class="lin-dl">${attrs.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(shown(v))}</dd>`).join('')}</dl>${hiddenAttrs ? '<p class="cx-msg">Storage locations are not shown.</p>' : ''}` : ''}`
    + `<h4>Downstream: links out</h4>${links('out') ? `<ul class="cx-list">${links('out')}</ul>` : '<p class="cx-msg">None.</p>'}`
    + `<h4>Upstream: links in</h4>${links('in') ? `<ul class="cx-list">${links('in')}</ul>` : '<p class="cx-msg">None.</p>'}`
    + `${history}${term}</section>`;
}

// ---- whole panel -------------------------------------------------------------------------------------------------
const notice = (m) => (m.notice ? `<p class="cx-msg" role="status">${esc(m.notice)}</p>` : '');
export function renderLineage(m) {
  const a = m.graphAll;
  if (m.error) {
    return `${renderReplay(m.asOf, { busy: m.busy, queued: m.queued })}<div class="cx-msg" role="alert">${esc(m.error)} `
      + `<button type="button" class="hd-btn hd-btn--sm" data-retry="1">Retry</button></div>`;
  }
  if (!a.nodes.length && !m.timeline) return `${renderReplay(m.asOf, { busy: m.busy, queued: m.queued })}${notice(m)}<div class="hd-empty">No lineage is shown for this room yet. Lineage appears only from recorded events you are allowed to see.</div>`;
  const view = m.filtered;
  // The layout is cached by content, so panning, zooming and selecting never rebuild it; the SVG is a bounded viewport onto it
  // (cullGraph), so a graph of any size costs the same DOM as a small one. The table below stays the full, accessible alternative.
  const layout = m.mode === 'table' ? null : layoutCached(view);
  const { w: vpW, h: vpH } = m.vpW ? { w: m.vpW, h: m.vpH } : viewportSize(layout);
  const graphPane = m.mode === 'table' ? renderGraphTable(view, { sel: m.sel })
    : `${renderGraphToolbar({ view: m.vp || fitView(layout, vpW, vpH), focusId: m.focusId, sel: m.sel })}`
      + `<div class="lin-viewport lin-scroll" role="region" tabindex="0" data-viewport aria-label="Lineage graph, drag to pan">${renderGraphSvg(view, layout, { sel: m.sel, focus: m.focus, match: view.match, view: m.vp, vw: vpW, vh: vpH })}</div>`
      + '<p class="cx-msg lin-hint">Drag to pan. Hold Ctrl or Alt and turn the wheel, or press + and −, to zoom. Shift with the arrow keys pans; 0 fits the graph.</p>';
  // `graphMore === false` is the last page the server will give. What the server says it ran out of decides what is missing:
  // `scan_budget` means it stopped reading NODES (more may exist); any other reason with no next page (`edge_budget`,
  // `limit` on edges, `response_bytes`) means every node was returned but some LINKS were left out. Silence made a cut-off
  // graph look complete (T16 large-graph gate); a wrong claim ("more nodes") for an edges-only cut would mislead as well.
  const trunc = m.graphTruncated
    ? (m.graphMore === false
      ? (m.graphTruncated === 'scan_budget'
        ? `<p class="cx-msg" role="status">Showing ${a.nodes.length} nodes (${esc(m.graphTruncated)}). The server stopped reading here, so more nodes may exist that this view cannot load.</p>`
        : `<p class="cx-msg" role="status">Showing all ${a.nodes.length} nodes, but the server left out some links (${esc(m.graphTruncated)}), so the links drawn may be incomplete.</p>`)
      : `<p class="cx-msg" role="status">Showing ${a.nodes.length} of ${m.total} nodes (${esc(m.graphTruncated)}). <button type="button" class="hd-btn hd-btn--ghost hd-btn--sm" data-more="1">Load more</button></p>`)
    : '';
  const gantt = m.timeline ? buildGantt(m.timeline, { clock: m.clock, types: m.filters.types }) : null;
  const tab = (k, l) => `<button type="button" role="tab" class="cx-stab" data-view="${k}" aria-selected="${m.view === k}" tabindex="${m.view === k ? 0 : -1}">${l}</button>`;
  const body = m.view === 'gantt'
    ? `<div class="cx-row"><label class="cx-field">Time shown<select class="cx-input" data-clock><option value="recorded"${m.clock === 'recorded' ? ' selected' : ''}>When the server recorded it</option><option value="reported"${m.clock === 'reported' ? ' selected' : ''}>When the emitter says it happened</option></select></label>`
      + `<button type="button" class="hd-btn hd-btn--ghost hd-btn--sm" data-mode="${m.mode === 'table' ? 'visual' : 'table'}">${m.mode === 'table' ? 'Show chart' : 'Show as table'}</button></div>`
      + `${m.timelineError ? `<p class="cx-msg" role="alert">${esc(m.timelineError)} <button type="button" class="hd-btn hd-btn--sm" data-retry="1">Retry</button></p>` : ''}`
      + `${renderGanttLegend()}${m.mode === 'table' ? '' : `<div class="cx-scroll lin-scroll" role="region" tabindex="0" aria-label="Gantt timeline">${gantt ? renderGanttSvg(gantt, { width: m.width, sel: m.sel }) : ''}</div>`}${gantt ? renderGanttTable(gantt) : ''}`
    : `<div class="cx-row"><button type="button" class="hd-btn hd-btn--ghost hd-btn--sm" data-mode="${m.mode === 'table' ? 'visual' : 'table'}">${m.mode === 'table' ? 'Show graph' : 'Show as table'}</button><span class="cx-msg">${view.nodes.length} nodes, ${view.edges.length} links shown</span></div>${renderGraphLegend()}${trunc}${graphPane}`;
  return `${renderStatusStrip(a, { truncated: Boolean(m.graphTruncated) })}${renderReplay(m.asOf, { busy: m.busy, queued: m.queued })}${notice(m)}`
    + `<div class="lin-grid2"><div class="cx-stack">${renderFilters(a, m.filters)}`
    + `<div role="tablist" aria-label="Lineage views" class="cx-stabs">${tab('graph', 'Graph')}${tab('gantt', 'Gantt')}</div>`
    + `<div role="tabpanel" aria-label="${m.view === 'gantt' ? 'Gantt' : 'Graph'}" class="cx-stack">${body}</div></div>`
    + `<aside class="cx-stack" aria-label="Inspector" aria-live="polite">${renderInspector(a, m.sel, m.timeline, { terminal: m.terminal })}${m.workbench || ''}</aside></div>`;
}

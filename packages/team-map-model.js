// The Team Map's logic (unified workspace P06), with no DOM: what the Work Graph says, grouped the way a person reads it
// (project -> issue -> task -> deliverables), what each line between two things means, which things stay folded away, a text
// description of all of it for people who cannot use the picture, and where things sit. Fed by GET /api/v1/graph/work/snapshot.

/** The five meanings a line can have, in the words the map's legend uses. Anything unlisted is "coordination". */
export const EDGE_MEANINGS = {
  coordination: 'works together with',
  delegation: 'handed work to',
  dependency: 'waits for',
  produced: 'produced',
  reviewed: 'was reviewed by',
};
const MEANING_OF = {
  delegation: 'delegation', delegated_to: 'delegation', assigned_to: 'delegation', spawn: 'delegation', spawned: 'delegation', handoff: 'delegation',
  dependency: 'dependency', depends_on: 'dependency', blocked_by: 'dependency', precedes: 'dependency',
  produced: 'produced', derived_from: 'produced',
  reviewed: 'reviewed', reviews: 'reviewed', validated: 'reviewed', approved_by: 'reviewed',
};
export const edgeMeaning = (type) => MEANING_OF[type] || 'coordination';

const STRUCTURE = ['project', 'issue', 'task'];
const DELIVERABLE = new Set(['artifact', 'artifact_version', 'review', 'decision']);
/** Execution detail that is noise until asked for: folded into counts by default. */
export const NOISY_KINDS = new Set(['agent_session', 'host', 'external_action']);
const DONE = new Set(['done', 'closed', 'merged', 'completed', 'succeeded', 'resolved']);
const BLOCKED = new Set(['blocked', 'failed', 'error']);

const text = (v) => (typeof v === 'string' ? v : '');
const list = (v) => (Array.isArray(v) ? v : []);
const byId = (a, b) => (a.id < b.id ? -1 : a.id > b.id ? 1 : 0);
export const nodeName = (n) => text(n.display_name) || text(n.native_id) || n.id;

/** Milestone counts and a plain label. A percentage is never invented: it is "3 of 5 tasks done" or a word. */
export function progressOf(tasks) {
  const total = tasks.length;
  const done = tasks.filter((t) => DONE.has(text(t.status).toLowerCase())).length;
  const blocked = tasks.filter((t) => BLOCKED.has(text(t.status).toLowerCase())).length;
  const label = !total ? 'No tasks yet' : done === total ? 'All tasks done' : blocked ? `${blocked} blocked` : done ? 'In progress' : 'Not started';
  return { total, done, blocked, label, milestones: total ? `${done} of ${total} tasks done` : '' };
}

/**
 * Group a snapshot. `contains` edges give the hierarchy; a node with none falls back to its project_id, then to "Unplaced".
 * Deliverables hang off whatever produced them. Noisy kinds are counted, not listed, unless `showExecution`.
 */
export function buildTeamMap(snapshot, { kinds = null, status = null, query = '', showExecution = false } = {}) {
  const nodes = list(snapshot?.nodes).filter((n) => n && n.id && !n.tombstone);
  const edges = list(snapshot?.edges).filter((e) => e && e.from_id && e.to_id);
  const index = new Map(nodes.map((n) => [n.id, n]));
  const q = query.trim().toLowerCase();
  const visible = (n) => (!kinds || kinds.has(n.kind)) && (!status || text(n.status) === status)
    && (!q || nodeName(n).toLowerCase().includes(q) || n.id.toLowerCase().includes(q));
  const parentOf = new Map();
  for (const e of edges) if (e.type === 'contains' && index.has(e.from_id) && index.has(e.to_id) && !parentOf.has(e.to_id)) parentOf.set(e.to_id, e.from_id);
  const producedBy = new Map();
  for (const e of edges) if (edgeMeaning(e.type) === 'produced' && DELIVERABLE.has(index.get(e.to_id)?.kind) && !producedBy.has(e.to_id)) producedBy.set(e.to_id, e.from_id);
  const hidden = { execution: nodes.filter((n) => NOISY_KINDS.has(n.kind)).length };
  const shown = nodes.filter((n) => visible(n) && (showExecution || !NOISY_KINDS.has(n.kind)));
  const shownIds = new Set(shown.map((n) => n.id));
  const children = new Map();
  const add = (parent, child) => { if (!children.has(parent)) children.set(parent, []); children.get(parent).push(child); };
  const roots = [];
  for (const n of shown.slice().sort(byId)) {
    const parent = DELIVERABLE.has(n.kind) ? (producedBy.get(n.id) || parentOf.get(n.id)) : parentOf.get(n.id);
    if (parent && shownIds.has(parent)) add(parent, n);
    else roots.push(n);
  }
  const tree = (n) => {
    const kids = (children.get(n.id) || []).map(tree);
    const tasks = kids.filter((k) => k.node.kind === 'task').map((k) => k.node);
    return { node: n, children: kids, progress: n.kind === 'task' ? null : progressOf(tasks), deliverables: kids.filter((k) => DELIVERABLE.has(k.node.kind)).length };
  };
  const rank = (n) => { const i = STRUCTURE.indexOf(n.kind); return i < 0 ? STRUCTURE.length : i; };
  roots.sort((a, b) => rank(a) - rank(b) || byId(a, b));
  const unplaced = roots.filter((n) => !STRUCTURE.includes(n.kind));
  return {
    groups: roots.filter((n) => STRUCTURE.includes(n.kind)).map(tree),
    unplaced: unplaced.map(tree),
    hidden,
    edges: edges.filter((e) => shownIds.has(e.from_id) && shownIds.has(e.to_id)).map((e) => ({ ...e, meaning: edgeMeaning(e.type) })),
    truncated: Boolean(snapshot?.has_more),
  };
}

/** The same map as sentences, for the text view and for screen readers. Live state is named apart from the history. */
export function describeTeamMap(map) {
  const index = new Map();
  const walk = (t, out, depth) => {
    index.set(t.node.id, t.node);
    const p = t.progress && t.progress.total ? ` — ${t.progress.milestones}` : '';
    const d = t.deliverables ? `, ${t.deliverables} deliverable${t.deliverables === 1 ? '' : 's'}` : '';
    out.push({ depth, id: t.node.id, text: `${t.node.kind} ${nodeName(t.node)}${t.node.status ? ` (${t.node.status})` : ''}${p}${d}` });
    for (const c of t.children) walk(c, out, depth + 1);
  };
  const lines = [];
  for (const g of [...map.groups, ...map.unplaced]) walk(g, lines, 0);
  const relations = map.edges.filter((e) => index.has(e.from_id) && index.has(e.to_id)).map((e) => ({
    id: e.id, live: e.live !== false,
    text: `${nodeName(index.get(e.from_id))} ${EDGE_MEANINGS[e.meaning]} ${nodeName(index.get(e.to_id))}${e.live === false ? ' (earlier, no longer current)' : ''}`,
  }));
  return { lines, relations };
}

// ---- layout: kept apart from the graph data, deterministic, and never moving what is already placed
const COLUMN = { project: 0, team: 0, issue: 1, task: 2, agent: 3, artifact: 4, artifact_version: 4, review: 4, decision: 4 };
export const CELL = { w: 220, h: 72 };

/** Positions for every node. `placed` (saved or personal) is kept exactly; new nodes go in the next free row of their column. */
export function layoutNodes(nodes, placed = {}) {
  const out = {};
  const used = new Map();       // column -> rows taken
  for (const n of nodes) if (placed[n.id]) { out[n.id] = { x: placed[n.id].x, y: placed[n.id].y }; }
  const rowOf = (y) => Math.round(y / CELL.h);
  for (const n of nodes) if (out[n.id]) { const c = Math.round(out[n.id].x / CELL.w); if (!used.has(c)) used.set(c, new Set()); used.get(c).add(rowOf(out[n.id].y)); }
  for (const n of nodes.slice().sort(byId)) {
    if (out[n.id]) continue;
    const c = COLUMN[n.kind] ?? 5;
    if (!used.has(c)) used.set(c, new Set());
    let r = 0;
    while (used.get(c).has(r)) r += 1;
    used.get(c).add(r);
    out[n.id] = { x: c * CELL.w, y: r * CELL.h };
  }
  return out;
}

/** personal > shared > automatic, per node: a person's own move never changes what anyone else sees. */
export const effectiveLayout = (auto, shared = {}, personal = {}) => {
  const out = {};
  for (const id of Object.keys(auto)) out[id] = personal[id] || shared[id] || auto[id];
  return out;
};

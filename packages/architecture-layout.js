// Pure, deterministic layered layout for the architecture overview. No DOM.
// Cycles are broken by ignoring back edges for layering only; every edge is still drawn.
export const NODE_W = 188, NODE_H = 44, COL_GAP = 96, ROW_GAP = 16, PAD = 16;

export function layoutGraph(nodes, edges, {limit = 80} = {}) {
  const degree = new Map(nodes.map(n => [n.id, 0]));
  for (const e of edges) for (const id of [e.from, e.to]) if (degree.has(id)) degree.set(id, degree.get(id) + 1);
  const ranked = [...nodes].sort((a, b) => degree.get(b.id) - degree.get(a.id) || a.id.localeCompare(b.id));
  const shown = ranked.slice(0, limit).sort((a, b) => a.id.localeCompare(b.id));
  const ids = new Set(shown.map(n => n.id));
  const es = edges.filter(e => ids.has(e.from) && ids.has(e.to) && e.from !== e.to)
    .sort((a, b) => a.from.localeCompare(b.from) || a.to.localeCompare(b.to));
  const out = new Map(shown.map(n => [n.id, []]));
  for (const e of es) out.get(e.from).push(e.to);
  // DFS back-edge removal (iterative, ordered)
  const state = new Map(), back = new Set();
  for (const n of shown) {
    if (state.has(n.id)) continue;
    const stack = [[n.id, 0]]; state.set(n.id, 1);
    while (stack.length) {
      const top = stack[stack.length - 1], [id, i] = top, next = out.get(id)[i];
      if (next === undefined) { state.set(id, 2); stack.pop(); continue; }
      top[1]++;
      if (state.get(next) === 1) back.add(id + '\0' + next);
      else if (!state.has(next)) { state.set(next, 1); stack.push([next, 0]); }
    }
  }
  const preds = new Map(shown.map(n => [n.id, []]));
  for (const e of es) if (!back.has(e.from + '\0' + e.to)) preds.get(e.to).push(e.from);
  const layer = new Map();
  const depth = id => {
    if (layer.has(id)) return layer.get(id);
    layer.set(id, 0); // guards recursion; DAG after back-edge removal
    const d = preds.get(id).reduce((m, p) => Math.max(m, depth(p) + 1), 0);
    layer.set(id, d); return d;
  };
  shown.forEach(n => depth(n.id));
  const cols = [];
  for (const n of shown) (cols[layer.get(n.id)] ||= []).push(n.id);
  const idx = new Map();
  cols.forEach(c => c.forEach((id, i) => idx.set(id, i)));
  for (let sweep = 0; sweep < 2; sweep++) {
    cols.forEach((c, ci) => {
      if (ci === 0 && sweep === 0) return;
      const bary = id => { const ps = preds.get(id); return ps.length ? ps.reduce((s, p) => s + idx.get(p), 0) / ps.length : idx.get(id); };
      c.sort((a, b) => bary(a) - bary(b) || a.localeCompare(b));
      c.forEach((id, i) => idx.set(id, i));
    });
  }
  const pos = new Map();
  cols.forEach((c, ci) => c.forEach((id, ri) => pos.set(id, {x: PAD + ci * (NODE_W + COL_GAP), y: PAD + ri * (NODE_H + ROW_GAP)})));
  const placed = shown.map(n => ({...n, ...pos.get(n.id), w: NODE_W, h: NODE_H}));
  const drawn = es.map(e => {
    const a = pos.get(e.from), b = pos.get(e.to), rev = b.x <= a.x;
    const x1 = a.x + (rev ? 0 : NODE_W), y1 = a.y + NODE_H / 2, x2 = b.x + (rev ? NODE_W : 0), y2 = b.y + NODE_H / 2;
    const dx = Math.max(40, Math.abs(x2 - x1) / 2) * (rev ? -1 : 1);
    return {...e, back: back.has(e.from + '\0' + e.to), d: `M${x1},${y1} C${x1 + dx},${y1} ${x2 - dx},${y2} ${x2},${y2}`};
  });
  const width = PAD * 2 + Math.max(1, cols.length) * (NODE_W + COL_GAP) - COL_GAP;
  const height = PAD * 2 + Math.max(1, ...cols.map(c => c.length)) * (NODE_H + ROW_GAP) - ROW_GAP;
  return {nodes: placed, edges: drawn, width, height, hidden: nodes.length - shown.length};
}

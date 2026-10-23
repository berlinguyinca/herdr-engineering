// Live flow of work between people and agents in a room: an SVG node-link diagram, no libraries.
// Pure layout (testable) + a renderer. Every string from the room is escaped; nothing is parsed as HTML.
import { escapeHtml as esc } from './herdr-web-components/util.js';

const COL_W = 210;
const NODE_W = 168;
const NODE_H = 34;
const ROW_H = 46;
const PAD = 16;
const MAX_LEVEL = 4;
const IDLE_ROWS = 8;                  // idle agents wrap into columns this tall instead of one long stack
const STATUS_COLOR = { spawn: 'var(--text-muted, #888)', pending: 'var(--status-attention, #d89b1d)', accepted: 'var(--accent, #1f7ae0)', done: 'var(--status-ok, #1a9b6c)', failed: 'var(--status-danger, #c0392b)', cancelled: 'var(--text-muted, #888)', expired: 'var(--text-muted, #888)' };
const FAMILY_COLOR = { 'claude-code': '#c96442', codex: '#10a37f', pi: '#6f5bd6' };
const clip = (s, n) => (String(s).length > n ? `${String(s).slice(0, n - 1)}…` : String(s));

export const edgeState = (counts) => {
  const total = Object.values(counts).reduce((a, b) => a + b, 0);
  if ((counts.pending || 0) + (counts.accepted || 0) > 0) return { state: counts.accepted ? 'accepted' : 'pending', live: true, total };
  if (counts.failed) return { state: 'failed', live: false, total };
  return { state: 'done', live: false, total };
};

// People on the left; an agent sits one column right of the nearest source that feeds it, so a
// delegation chain reads left to right. Agents with no work yet wait in a compact "idle" grid
// to the right, so dozens of them don't stretch the diagram into one tall column.
export function layoutFlow(flow) {
  const level = new Map();
  for (const n of flow.nodes) if (n.kind === 'person') level.set(n.id, 0);
  const incoming = new Map();
  const spawns = (flow.spawns || []).map((e) => ({ ...e, counts: {}, spawn: true }));   // helper started under a parent agent
  for (const e of [...flow.edges, ...spawns]) { if (!incoming.has(e.to)) incoming.set(e.to, []); incoming.get(e.to).push(e.from); }
  for (let pass = 0; pass < MAX_LEVEL + 1; pass += 1) {
    for (const n of flow.nodes) {
      if (n.kind !== 'agent') continue;
      const srcs = (incoming.get(n.id) || []).filter((s) => level.has(s) && s !== n.id);
      const want = srcs.length ? Math.min(MAX_LEVEL, 1 + Math.min(...srcs.map((s) => level.get(s)))) : 1;
      if (!level.has(n.id) || want < level.get(n.id)) level.set(n.id, want);
    }
  }
  const touched = new Set([...flow.edges, ...spawns].flatMap((e) => [e.from, e.to]));
  const idle = flow.nodes.filter((n) => n.kind === 'agent' && !touched.has(n.id));
  const idleIds = new Set(idle.map((n) => n.id));
  const cols = new Map();
  for (const n of flow.nodes) {
    if (idleIds.has(n.id)) continue;
    const l = level.get(n.id) ?? 1;
    if (!cols.has(l)) cols.set(l, []);
    cols.get(l).push(n);
  }
  const placed = new Map();
  let height = 0;
  const used = [...cols.keys()].sort((a, b) => a - b);
  used.forEach((l, ci) => {
    const list = cols.get(l).sort((a, b) => String(a.family || '').localeCompare(String(b.family || '')) || String(a.name).localeCompare(String(b.name)));
    list.forEach((n, i) => {
      placed.set(n.id, { ...n, level: l, x: PAD + ci * COL_W, y: PAD + i * ROW_H, w: NODE_W, h: NODE_H });
      height = Math.max(height, PAD + (i + 1) * ROW_H);
    });
  });
  let idleLabel = null;
  let totalCols = used.length;
  if (idle.length) {
    idle.sort((a, b) => String(a.family || '').localeCompare(String(b.family || '')) || String(a.name).localeCompare(String(b.name)));
    const rows = Math.min(IDLE_ROWS, idle.length);
    const x0 = PAD + used.length * COL_W;
    idleLabel = { x: x0, y: PAD + 8, text: `Idle · ${idle.length}` };
    idle.forEach((n, i) => {
      const col = Math.floor(i / rows); const row = i % rows;
      placed.set(n.id, { ...n, level: -1, x: x0 + col * COL_W, y: PAD + 20 + row * ROW_H, w: NODE_W, h: NODE_H });
      height = Math.max(height, PAD + 20 + (row + 1) * ROW_H);
    });
    totalCols += Math.ceil(idle.length / rows);
  }
  const width = PAD * 2 + Math.max(1, totalCols - 1) * COL_W + NODE_W;
  const edges = [...flow.edges, ...spawns].filter((e) => placed.has(e.from) && placed.has(e.to)).map((e) => {
    const a = placed.get(e.from); const b = placed.get(e.to);
    const self = a.id === b.id;
    const x1 = a.x + a.w; const y1 = a.y + a.h / 2; const x2 = b.x; const y2 = b.y + b.h / 2;
    const back = x2 <= x1;                          // same column or backwards: loop around the right edge
    const d = self ? `M${x1},${y1} c40,-24 40,24 0,0`
      : back ? `M${x1},${y1} C${x1 + 60},${y1} ${x2 - 60},${y2} ${x2},${y2}`
        : `M${x1},${y1} C${x1 + (x2 - x1) / 2},${y1} ${x1 + (x2 - x1) / 2},${y2} ${x2},${y2}`;
    if (e.spawn) return { ...e, d, state: 'spawn', live: false, total: 0, width: 1.5, key: `${e.from}>${e.to}#spawn` };
    const st = edgeState(e.counts);
    return { ...e, d, ...st, width: 1.5 + Math.min(6, Math.log2(st.total + 1) * 1.5), key: `${e.from}>${e.to}` };
  });
  return { nodes: [...placed.values()], edges, width: Math.max(width, 360), height: Math.max(height + PAD, 120), idleLabel };
}

const sig = (e) => `${e.state}:${e.total}:${e.last}`;

// Draws into `box`; `prev` is the signature map returned by the last call so changed edges can pulse.
export function renderFlow(box, flow, { onNode, onEdge, prev = new Map() } = {}) {
  const lay = layoutFlow(flow);
  const next = new Map(lay.edges.map((e) => [e.key, sig(e)]));
  const edgeSvg = lay.edges.map((e) => `<path class="cx-flow-edge${e.spawn ? ' cx-flow-edge--spawn' : ''}${e.live ? ' cx-flow-edge--live' : ''}${prev.size && prev.get(e.key) !== next.get(e.key) ? ' cx-flow-pulse' : ''}" data-edge="${esc(e.key)}" d="${e.d}" fill="none" stroke="${STATUS_COLOR[e.state]}" stroke-width="${e.width.toFixed(1)}" stroke-linecap="round"><title>${esc(e.spawn ? 'started under this agent (same herdr tab)' : Object.entries(e.counts).map(([k, v]) => `${v} ${k}`).join(', '))}</title></path>`).join('');
  const nodeSvg = lay.nodes.map((n) => {
    const fam = n.kind === 'agent' ? (FAMILY_COLOR[n.family] || 'var(--text-muted, #888)') : 'var(--accent, #1f7ae0)';
    const live = n.fabric && n.fabric.status === 'active';
    const gone = n.status === 'removed';
    const sub = n.kind === 'agent' ? `${n.family || ''}${n.group ? ` · ${n.group}` : ''}${n.fabric ? ` · ${n.fabric.host || 'fabric'}` : ''}` : (n.role || '');
    return `<g class="cx-flow-node${gone ? ' cx-flow-node--gone' : ''}" data-node="${esc(n.id)}" transform="translate(${n.x},${n.y})" tabindex="0" role="button" aria-label="${esc(n.fabric?.display_name || n.name)}">
      <rect width="${n.w}" height="${n.h}" rx="8" class="cx-flow-box"/>
      <circle cx="12" cy="${n.h / 2}" r="5" fill="${fam}"/>
      ${n.kind === 'agent' && n.fabric ? `<circle cx="${n.w - 12}" cy="10" r="4" fill="${live ? 'var(--status-ok, #1a9b6c)' : 'var(--text-muted, #888)'}"><title>fabric agent ${esc(n.fabric.status || '')}</title></circle>` : ''}
      <text x="24" y="14" class="cx-flow-name">${esc(clip(n.fabric?.display_name || (n.kind === 'agent' ? `@${n.name}` : n.name), 20))}</text>
      <text x="24" y="27" class="cx-flow-sub">${esc(clip(sub, 26))}</text>
      <title>${esc(n.fabric?.display_name || n.name)}</title></g>`;
  }).join('');
  box.innerHTML = `<div class="cx-flow-scroll"><svg class="cx-flow" viewBox="0 0 ${lay.width} ${lay.height}" width="${lay.width}" height="${lay.height}" role="img" aria-label="Flow of tasks between people and agents">${lay.idleLabel ? `<text x="${lay.idleLabel.x}" y="${lay.idleLabel.y}" class="cx-flow-sub">${esc(lay.idleLabel.text)}</text>` : ''}${edgeSvg}${nodeSvg}</svg></div>`;
  box.querySelectorAll('[data-node]').forEach((el) => {
    const act = () => onNode && onNode(lay.nodes.find((n) => n.id === el.dataset.node));
    el.onclick = act;
    el.onkeydown = (ev) => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); act(); } };
  });
  box.querySelectorAll('[data-edge]').forEach((el) => { el.onclick = () => onEdge && onEdge(lay.edges.find((e) => e.key === el.dataset.edge)); });
  return next;
}

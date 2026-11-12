// Agent Operations Activity Map: pure model. Snapshot (GET /api/v1/agent-ops/snapshot) -> scene (positions, rings, edge geometry),
// plus the replay helpers the scrubber uses. No DOM, no clock, no randomness: the same snapshot always gives the same scene.
//
// Visual semantics (docs/specs/2026-11-12-agent-operations-map/.../01-product-ux.md):
//   node = a real agent/session; label inside = name + model; outer ring = measured context fraction ("ctx unknown" when not measured,
//   never 0%); halo = activity state; edge colour/dash = kind + planned/accepted. Every signal also has a text form (colour is never alone).

const text = (v) => (typeof v === 'string' ? v : v == null ? '' : String(v));
const list = (v) => (Array.isArray(v) ? v : []);

export const CAPABILITY = 'agent_ops_v1';
export const agentOpsEnabled = (capabilities) => Array.isArray(capabilities) && capabilities.includes(CAPABILITY);

// ---- geometry constants (scene units; the SVG scales the viewBox) ----------------------------------------------------------
export const NODE_R = 44;
export const RING_GAP = 8;
export const RING_W = 7;
export const RING_R = NODE_R + RING_GAP;
export const HALO_R = RING_R + 12;
export const CLUSTER_PAD = 54;
export const NO_ROOM = '';

// ---- context fraction (ADR: amber >= 70, red >= 90) -------------------------------------------------------------------------
export const CTX_WARN = 0.7;
export const CTX_CRITICAL = 0.9;

/** Context utilisation of a snapshot agent. `fraction` is null unless both a measured numerator and a positive limit exist. */
export function contextInfo(context) {
  const used = context && Number.isFinite(context.used) ? context.used : null;
  const limit = context && Number.isFinite(context.limit) && context.limit > 0 ? context.limit : null;
  if (used == null) return { known: false, fraction: null, percent: null, level: 'unknown', label: 'ctx unknown', words: 'context not reported', used: null, limit: null };
  if (limit == null) return { known: true, fraction: null, percent: null, level: 'unknown', label: `ctx ${used} used`, words: `${used} tokens used, limit not reported`, used, limit: null };
  const raw = used / limit;
  const fraction = Math.max(0, Math.min(1, raw));
  const percent = Math.round(raw * 100);
  const level = raw >= CTX_CRITICAL ? 'critical' : raw >= CTX_WARN ? 'warn' : 'ok';
  const tag = level === 'critical' ? ' critical' : level === 'warn' ? ' high' : '';
  return { known: true, fraction, percent, level, label: `${percent}% ctx${tag}`, words: `${percent}% of context used (${used} of ${limit})${tag ? `, ${tag.trim()}` : ''}`, used, limit };
}

/** SVG stroke arithmetic for the ring: dash = filled arc, gap = the rest. An unknown fraction draws a dotted track only. */
export function ringGeometry(fraction, r = RING_R) {
  const circumference = Math.round(2 * Math.PI * r * 100) / 100;
  if (fraction == null) return { r, circumference, dash: 0, gap: circumference, unknown: true };
  const dash = Math.round(circumference * fraction * 100) / 100;
  return { r, circumference, dash, gap: Math.round((circumference - dash) * 100) / 100, unknown: false };
}

// ---- state halo ---------------------------------------------------------------------------------------------------------------
export const STATES = {
  working: { label: 'Working', tone: 'active' },
  idle: { label: 'Idle', tone: 'ok' },
  attention: { label: 'Needs attention', tone: 'attention' },
  blocked: { label: 'Blocked', tone: 'blocked' },
  disconnected: { label: 'Disconnected', tone: 'muted' },
  ended: { label: 'Ended', tone: 'muted' },
  unknown: { label: 'State unknown', tone: 'unknown' },
};
export function stateInfo(state) {
  const key = text(state).toLowerCase();
  if (STATES[key]) return { key, ...STATES[key] };
  return { key: 'unknown', ...STATES.unknown };
}

// ---- edge styling ---------------------------------------------------------------------------------------------------------------
export const EDGE_KINDS = {
  communication: { label: 'Communication', tone: 'comm', meaning: 'a message between two agents or people' },
  delegation: { label: 'Delegation', tone: 'delegation', meaning: 'work handed to another agent; ownership stays where it was' },
  handoff: { label: 'Handoff', tone: 'handoff', meaning: 'a session handed to another harness or agent' },
  review: { label: 'Review', tone: 'review', meaning: 'a review asked of or done by another agent' },
  ownership: { label: 'Ownership', tone: 'ownership', meaning: 'accountability for a task moved to another owner' },
};
export const KIND_ORDER = Object.keys(EDGE_KINDS);
const PLANNED = new Set(['planned', 'proposed', 'queued', 'claimed', 'pending', 'requested']);
const REJECTED = new Set(['rejected', 'declined', 'denied', 'cancelled', 'failed', 'expired']);

export const DASH_PLANNED = '9 7';
export const DASH_REJECTED = '2 7';

/** Solid = accepted (or done), dashed = planned, dotted = rejected. The state word is always part of the label. */
export function edgeStyle(edge) {
  const kind = Object.hasOwn(EDGE_KINDS, edge?.kind) ? edge.kind : 'communication';
  const meta = EDGE_KINDS[kind];
  const state = text(edge?.state).toLowerCase() || 'unknown';
  const planned = PLANNED.has(state), rejected = REJECTED.has(state);
  const dash = planned ? DASH_PLANNED : rejected ? DASH_REJECTED : null;
  return {
    kind, tone: meta.tone, kindLabel: meta.label, state, planned, rejected, dashed: dash != null, dash,
    line: planned ? 'dashed' : rejected ? 'dotted' : 'solid',
    // Flowing particles only on edges that are live: accepted or in-flight, never a refused one.
    flowing: !rejected, label: `${meta.label} · ${state}`,
  };
}

// ---- modes and filters ------------------------------------------------------------------------------------------------------------
export const MODES = [
  { key: 'activity', label: 'Activity', kinds: null },
  { key: 'communication', label: 'Communication', kinds: ['communication'] },
  { key: 'handoffs', label: 'Handoffs', kinds: ['handoff', 'delegation'] },
  { key: 'ownership', label: 'Ownership', kinds: ['ownership', 'review'] },
  { key: 'replay', label: 'Replay', kinds: null },
];
export const modeKinds = (mode) => MODES.find((m) => m.key === mode)?.kinds ?? null;

export const FILTER_KEYS = ['room', 'task', 'host', 'provider', 'model', 'kind'];
export const emptyFilters = () => ({ room: '', task: '', host: '', provider: '', model: '', kind: '' });

const uniqSorted = (values) => [...new Set(values.map(text).filter(Boolean))].sort();

/** The distinct values a person can filter on, from the unfiltered snapshot. */
export function filterOptions(snapshot) {
  const agents = list(snapshot?.agents);
  return {
    room: uniqSorted(list(snapshot?.rooms).map((r) => r.room_id)),
    task: uniqSorted(list(snapshot?.tasks).map((t) => t.task_id)),
    host: uniqSorted(agents.map((a) => a.host_id)),
    provider: uniqSorted(agents.map((a) => a.provider)),
    model: uniqSorted(agents.map((a) => a.model)),
    kind: KIND_ORDER.filter((k) => list(snapshot?.edges).some((e) => e.kind === k)),
  };
}

const isPerson = (id) => text(id).startsWith('user:');

/** Narrow a snapshot. Edges survive only while both ends do (a person end survives with the edge). Pure; the input is untouched. */
export function filterSnapshot(snapshot, filters = {}) {
  const f = { ...emptyFilters(), ...filters };
  const agents0 = list(snapshot?.agents), edges0 = list(snapshot?.edges), tasks0 = list(snapshot?.tasks);
  const task = f.task ? tasks0.find((t) => t.task_id === f.task) : null;
  const taskNodes = f.task ? new Set([task?.accountable_owner, task?.reviewer, ...list(task?.contributors),
    ...edges0.filter((e) => e.task_id === f.task).flatMap((e) => [e.from, e.to])].filter(Boolean)) : null;
  const agents = agents0.filter((a) => (!f.room || list(a.room_ids).includes(f.room)) && (!f.host || a.host_id === f.host)
    && (!f.provider || a.provider === f.provider) && (!f.model || a.model === f.model) && (!taskNodes || taskNodes.has(a.node_id)));
  const keep = new Set(agents.map((a) => a.node_id));
  const edges = edges0.filter((e) => (!f.kind || e.kind === f.kind) && (!f.task || e.task_id === f.task)
    && (keep.has(e.from) || isPerson(e.from)) && (keep.has(e.to) || isPerson(e.to)) && (keep.has(e.from) || keep.has(e.to)));
  const rooms = list(snapshot?.rooms).filter((r) => !f.room || r.room_id === f.room)
    .map((r) => ({ ...r, agent_ids: list(r.agent_ids).filter((id) => keep.has(id)) }));
  return { ...snapshot, agents, edges, rooms, tasks: f.task ? tasks0.filter((t) => t.task_id === f.task) : tasks0 };
}

// ---- naming ---------------------------------------------------------------------------------------------------------------------
const clip = (s, n) => { const t = text(s); return t.length > n ? `${t.slice(0, n - 1)}…` : t; };
export const shortId = (id) => { const t = text(id).replace(/^user:/, ''); return t.length > 14 ? `${t.slice(0, 6)}…${t.slice(-5)}` : t; };

/** Name shown inside a node: the sanitized label when the agent reported one, else its agent/session id. */
export function nodeName(agent) {
  if (isPerson(agent?.node_id)) return text(agent.node_id).slice(5) || 'person';
  return text(agent?.label) || text(agent?.agent_id) || text(agent?.node_id) || 'agent';
}
/** The model label shown inside a node; absent means the model is not known, which is said out loud. */
export const modelLabel = (agent) => text(agent?.model) || 'model unknown';
export const roomLabel = (id) => (id === NO_ROOM ? 'No room' : `Room ${shortId(id)}`);

// ---- layout ---------------------------------------------------------------------------------------------------------------------
const r1 = (n) => Math.round(n * 10) / 10;

function personStub(id) { return { node_id: id, agent_id: id, session_id: null, host_id: null, provider: null, model: null, model_changes: [], state: 'unknown', context: null, room_ids: [], label: null, provenance: 'unknown', kind: 'person' }; }

/** Rooms are clusters (membership, not ownership); an agent sits in the first of its rooms (sorted), else in the "No room" cluster. */
function groupByRoom(nodes, rooms) {
  const known = new Set(list(rooms).map((r) => r.room_id));
  const groups = new Map();
  for (const n of nodes) {
    const own = list(n.room_ids).filter((id) => known.size === 0 || known.has(id)).sort();
    const room = own[0] ?? NO_ROOM;
    if (!groups.has(room)) groups.set(room, []);
    groups.get(room).push(n);
  }
  const keys = [...groups.keys()].sort((a, b) => (a === NO_ROOM ? 1 : b === NO_ROOM ? -1 : a < b ? -1 : a > b ? 1 : 0));
  return keys.map((id) => ({ id, members: groups.get(id).sort((a, b) => (a.node_id < b.node_id ? -1 : a.node_id > b.node_id ? 1 : 0)) }));
}

function clusterRadius(n) {
  if (n <= 1) return HALO_R + CLUSTER_PAD;
  const ring = Math.max(HALO_R * 1.25, (n * (2 * HALO_R + 18)) / (2 * Math.PI));
  return ring + HALO_R + CLUSTER_PAD;
}

export const edgeId = (e) => [e.kind, e.from, e.to, e.room_id || '', e.task_id || e.handoff_id || ''].join('|');

function curve(a, b, bend) {
  const dx = b.x - a.x, dy = b.y - a.y, len = Math.hypot(dx, dy) || 1;
  const ux = dx / len, uy = dy / len, nx = -uy, ny = ux;
  const cut = RING_R + RING_W / 2 + 6;
  // Start/end sit on the ring of each node, aimed at the curve's control point so a bent edge still leaves the circle cleanly.
  const cx = (a.x + b.x) / 2 + nx * bend, cy = (a.y + b.y) / 2 + ny * bend;
  const sa = Math.hypot(cx - a.x, cy - a.y) || 1, sb = Math.hypot(cx - b.x, cy - b.y) || 1;
  const x1 = a.x + ((cx - a.x) / sa) * cut, y1 = a.y + ((cy - a.y) / sa) * cut;
  const x2 = b.x + ((cx - b.x) / sb) * (cut + 4), y2 = b.y + ((cy - b.y) / sb) * (cut + 4);
  const mid = { x: r1(0.25 * x1 + 0.5 * cx + 0.25 * x2), y: r1(0.25 * y1 + 0.5 * cy + 0.25 * y2) };
  return { d: `M${r1(x1)} ${r1(y1)} Q${r1(cx)} ${r1(cy)} ${r1(x2)} ${r1(y2)}`, mid, length: Math.round(len), ux, uy };
}

/**
 * snapshot -> scene. Deterministic: clusters sorted by room id, members by node id, parallel edges fanned by index.
 * `mode` only decides which edges are dimmed; `filters` (see filterSnapshot) decide what is drawn at all.
 */
export function layoutScene(snapshot, { mode = 'activity', filters = {} } = {}) {
  const view = filterSnapshot(snapshot || {}, filters);
  const agents = view.agents.map((a) => ({ ...a }));
  const byId = new Map(agents.map((a) => [a.node_id, a]));
  for (const e of view.edges) for (const id of [e.from, e.to]) if (id && !byId.has(id)) { const stub = personStub(id); byId.set(id, stub); agents.push(stub); }
  const roles = new Map();
  const role = (id, r, task) => { if (!id) return; if (!roles.has(id)) roles.set(id, []); roles.get(id).push({ role: r, task }); };
  for (const t of view.tasks) { role(t.accountable_owner, 'owner', t.task_id); role(t.reviewer, 'reviewer', t.task_id); for (const c of list(t.contributors)) role(c, 'contributor', t.task_id); }

  const groups = groupByRoom(agents, view.rooms);
  const radii = groups.map((g) => clusterRadius(g.members.length));
  const k = groups.length, maxR = Math.max(0, ...radii);
  const orbit = k <= 1 ? 0 : k === 2 ? maxR + 24 : maxR / Math.sin(Math.PI / k) + 24;
  const rooms = [], nodes = [];
  groups.forEach((g, gi) => {
    const angle = k <= 1 ? 0 : -Math.PI / 2 + (2 * Math.PI * gi) / k;
    const cx = k <= 1 ? 0 : orbit * Math.cos(angle) * 1.25, cy = k <= 1 ? 0 : orbit * Math.sin(angle);
    const n = g.members.length, ringR = n <= 1 ? 0 : Math.max(HALO_R * 1.25, (n * (2 * HALO_R + 18)) / (2 * Math.PI));
    g.members.forEach((m, i) => {
      const a = n <= 1 ? 0 : -Math.PI / 2 + (2 * Math.PI * i) / n;
      const ctx = contextInfo(m.context), st = stateInfo(m.state);
      nodes.push({
        id: m.node_id, x: r1(cx + ringR * Math.cos(a)), y: r1(cy + ringR * Math.sin(a)), r: NODE_R, room: g.id, person: m.kind === 'person' || isPerson(m.node_id),
        name: nodeName(m), model: modelLabel(m), modelKnown: Boolean(m.model), provider: m.provider || null, host: m.host_id || null,
        ctx, ring: ringGeometry(ctx.fraction), state: st, provenance: m.provenance || 'unknown', parent: m.parent_session_id || null,
        modelChanges: list(m.model_changes).length, compactions: m.compactions || 0, drift: m.drift_warnings || 0, lastEventAt: m.last_event_at || null,
        roles: roles.get(m.node_id) || [], agent: m,
      });
    });
    rooms.push({ id: g.id, label: roomLabel(g.id), cx: r1(cx), cy: r1(cy), r: r1(radii[gi]), members: g.members.map((m) => m.node_id) });
  });

  const at = new Map(nodes.map((n) => [n.id, n]));
  const pairs = new Map();
  const live = view.edges.filter((e) => at.has(e.from) && at.has(e.to) && e.from !== e.to);
  for (const e of live) { const key = [e.from, e.to].sort().join('>'); pairs.set(key, (pairs.get(key) || 0) + 1); }
  const seen = new Map();
  const kinds = modeKinds(mode);
  const edges = live.map((e) => {
    const key = [e.from, e.to].sort().join('>'), i = seen.get(key) || 0, total = pairs.get(key);
    seen.set(key, i + 1);
    const forward = e.from < e.to ? 1 : -1;
    // Bend: a slight arc always (so lines read as flows), parallel edges fan out to either side of the chord.
    const bend = forward * (30 + (i - (total - 1) / 2) * 44);
    const g = curve(at.get(e.from), at.get(e.to), bend);
    const style = edgeStyle(e);
    return {
      id: edgeId(e), from: e.from, to: e.to, kind: style.kind, state: style.state, style, d: g.d, mid: g.mid, length: g.length,
      label: style.label, task: e.task_id || null, handoff: e.handoff_id || null, room: e.room_id || null, eventId: e.event_id || null,
      provenance: e.provenance || 'unknown', updatedAt: e.updated_at || null, dimmed: kinds != null && !kinds.includes(style.kind), raw: e,
    };
  });

  const pad = CLUSTER_PAD;
  const xs = rooms.flatMap((r) => [r.cx - r.r, r.cx + r.r]), ys = rooms.flatMap((r) => [r.cy - r.r, r.cy + r.r]);
  const minX = xs.length ? Math.min(...xs) - pad / 2 : -200, maxX = xs.length ? Math.max(...xs) + pad / 2 : 200;
  const minY = ys.length ? Math.min(...ys) - pad / 2 : -140, maxY = ys.length ? Math.max(...ys) + pad / 2 : 140;
  return { mode, nodes, edges, rooms, tasks: view.tasks, viewBox: { x: r1(minX), y: r1(minY), w: r1(maxX - minX), h: r1(maxY - minY) },
    counts: { nodes: nodes.length, edges: edges.length, rooms: rooms.length, tasks: view.tasks.length }, cursor: snapshot?.cursor ?? null, truncated: Boolean(snapshot?.truncated) };
}

// ---- change detection: what animates ----------------------------------------------------------------------------------------------
/**
 * What changed between two snapshots. A model change on an existing node is a ring pulse on that node; a session that was not there
 * before is a new node. Nothing here invents identity: nodes are matched by node_id only.
 */
export function diffSnapshots(prev, next) {
  const before = new Map(list(prev?.agents).map((a) => [a.node_id, a]));
  const out = { added: [], removed: [], modelChanged: [], stateChanged: [], edgesAdded: [], edgesChanged: [] };
  for (const a of list(next?.agents)) {
    const p = before.get(a.node_id);
    if (!p) { out.added.push(a.node_id); continue; }
    if (p.model !== a.model || list(a.model_changes).length > list(p.model_changes).length) out.modelChanged.push(a.node_id);
    if (p.state !== a.state) out.stateChanged.push(a.node_id);
  }
  const now = new Set(list(next?.agents).map((a) => a.node_id));
  for (const id of before.keys()) if (!now.has(id)) out.removed.push(id);
  const old = new Map(list(prev?.edges).map((e) => [edgeId(e), e]));
  for (const e of list(next?.edges)) {
    const p = old.get(edgeId(e));
    if (!p) out.edgesAdded.push(edgeId(e)); else if (p.state !== e.state) out.edgesChanged.push(edgeId(e));
  }
  return out;
}

// ---- text alternatives ------------------------------------------------------------------------------------------------------------
export function describeNode(n) {
  const who = n.person ? `Person ${n.name}` : `Agent ${n.name}`;
  const parts = [who, n.person ? '' : `model ${n.modelKnown ? n.model : 'unknown'}`, n.person ? '' : n.ctx.known ? n.ctx.words : 'context unknown',
    `state ${n.state.label.toLowerCase()}`, n.host ? `host ${n.host}` : '', `${roomLabel(n.room).toLowerCase()}`,
    ...n.roles.map((r) => `${r.role} of task ${r.task}`)];
  return parts.filter(Boolean).join(', ');
}
export function describeEdge(e, scene) {
  const name = (id) => scene.nodes.find((n) => n.id === id)?.name || id;
  return `${e.style.kindLabel} from ${name(e.from)} to ${name(e.to)}, ${e.style.state}${e.style.dashed ? (e.style.planned ? ' (planned, dashed)' : ' (rejected, dotted)') : ' (solid)'}${e.task ? `, task ${e.task}` : ''}`;
}
/** Rows for the visually hidden table that carries everything the picture says. */
export function altTable(scene) {
  return {
    nodes: scene.nodes.map((n) => ({ id: n.id, name: n.name, model: n.person ? '' : n.model, context: n.person ? '' : n.ctx.label, state: n.state.label, room: roomLabel(n.room), host: n.host || '' })),
    edges: scene.edges.map((e) => ({ id: e.id, kind: e.style.kindLabel, from: scene.nodes.find((n) => n.id === e.from)?.name || e.from, to: scene.nodes.find((n) => n.id === e.to)?.name || e.to, state: e.style.state, line: e.style.line, task: e.task || '' })),
  };
}

// ---- keyboard order -----------------------------------------------------------------------------------------------------------------
/** Visual reading order: cluster by cluster, left to right; arrows move to the nearest node in that direction. */
export function neighbour(scene, id, dir) {
  const from = scene.nodes.find((n) => n.id === id);
  if (!from) return scene.nodes[0]?.id ?? null;
  const v = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] }[dir];
  if (!v) return null;
  let best = null, score = Infinity;
  for (const n of scene.nodes) {
    if (n.id === id) continue;
    const dx = n.x - from.x, dy = n.y - from.y, along = dx * v[0] + dy * v[1];
    if (along <= 1) continue;
    const across = Math.abs(dx * v[1] - dy * v[0]);
    const s = along + across * 2;
    if (s < score) { score = s; best = n.id; }
  }
  return best;
}

// ---- replay -----------------------------------------------------------------------------------------------------------------------
export const SPEEDS = [0.5, 1, 2, 4, 8];

/** `2026-11-12T10:00:02.000000Z~e2` -> epoch ms of the ingest time (NaN when malformed). Cursors stay opaque otherwise. */
export const cursorTime = (cursor) => Date.parse(text(cursor).split('~')[0]);

const cursorOf = (e) => `${e.ingested_at}~${e.event_id}`;

export function eventLabel(ev) {
  const what = text(ev?.event_type).replace(/\./g, ' ');
  const who = text(ev?.agent_id) || text(ev?.session_id);
  return `${what}${who ? ` · ${shortId(who)}` : ''}`;
}

/** Normalized /events rows -> ordered, de-duplicated timeline entries. Cursor order = server order. */
export function buildTimeline(events, previous = []) {
  const byCursor = new Map(previous.map((t) => [t.cursor, t]));
  for (const ev of list(events)) {
    if (!ev || !ev.event_id || !ev.ingested_at) continue;
    const cursor = cursorOf(ev);
    byCursor.set(cursor, { cursor, id: ev.event_id, type: text(ev.event_type), at: cursorTime(cursor), agent: ev.agent_id || null, session: ev.session_id || null,
      room: ev.room_id || null, task: ev.task_id || null, provenance: ev.provenance || 'unknown', label: eventLabel(ev) });
  }
  return [...byCursor.values()].sort((a, b) => (a.cursor < b.cursor ? -1 : a.cursor > b.cursor ? 1 : 0));
}

/** Where the scrubber stands. `index` null = Live. */
export function replayFrame(timeline, index) {
  const count = timeline.length;
  if (index == null || count === 0) return { live: true, index: null, count, cursor: null, at: count ? timeline[count - 1].at : null, fraction: 1, entry: count ? timeline[count - 1] : null };
  const i = Math.max(0, Math.min(count - 1, Math.round(index)));
  return { live: false, index: i, count, cursor: timeline[i].cursor, at: timeline[i].at, fraction: count > 1 ? i / (count - 1) : 1, entry: timeline[i] };
}
/** Slider fraction (0..1) -> timeline index. */
export const indexAtFraction = (timeline, fraction) => (timeline.length ? Math.max(0, Math.min(timeline.length - 1, Math.round(fraction * (timeline.length - 1)))) : null);
/** Next index while playing; null when it runs past the end (the player then returns to Live). */
export const nextIndex = (timeline, index, step = 1) => { const n = (index ?? -1) + step; return n >= timeline.length ? null : Math.max(0, n); };
/** Milliseconds to wait before showing the event after `index`: real gap scaled by speed, bounded so a quiet hour does not stall. */
export function stepDelay(timeline, index, speed = 1) {
  const s = SPEEDS.includes(speed) ? speed : 1;
  const a = timeline[index], b = timeline[index + 1];
  const gap = a && b && Number.isFinite(a.at) && Number.isFinite(b.at) ? Math.max(0, b.at - a.at) : 600;
  return Math.round(Math.max(120, Math.min(1500, gap)) / s);
}
export const formatClock = (ms) => (Number.isFinite(ms) ? new Date(ms).toISOString().slice(11, 19) : '--:--:--');

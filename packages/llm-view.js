// LLM page (#/llm): requests, latency, speed, tokens and problems from /api/v1/llm/*.
// DOM-free so it is node-testable. Every dynamic string goes through `esc`; a value the
// record did not carry renders as an explicit placeholder, never "0" or "null", and a
// percentile with fewer than 5 samples says "insufficient data" instead of a number.
import { escapeHtml as esc } from './herdr-web-components/util.js';

export const WINDOWS = ['1h', '24h', '7d', '30d'];
export const STEP_FOR = { '1h': '5m', '24h': '1h', '7d': '1h', '30d': '1d' };
const GAP = '—';
const PROXY = 'llm-proxy';

const num = (v) => (typeof v === 'number' && Number.isFinite(v) ? v : null);
const list = (v) => (Array.isArray(v) ? v : []);
const nf = new Intl.NumberFormat('en-US');
const count = (v) => (num(v) == null ? GAP : nf.format(v));
/** Requests for a proxy row, or the usage-event count of a transcript row (which has no requests). */
const reqs = (g) => (num(g.requests) != null ? count(g.requests) : (num(g.events) != null ? `${count(g.events)} events` : GAP));
const sourceLabel = (g) => (g.cross_check ? `${g.source} (cross-check)` : g.source);
const round1 = (v) => Math.round(v * 10) / 10;

export function compact(v) {
  const n = num(v);
  if (n == null) return GAP;
  if (n >= 1e9) return `${round1(n / 1e9)} B`;
  if (n >= 1e6) return `${round1(n / 1e6)} M`;
  if (n >= 1e4) return `${round1(n / 1e3)} k`;
  return nf.format(n);
}

export function ms(v) {
  const n = num(v);
  if (n == null) return GAP;
  return n >= 1000 ? `${round1(n / 1000)} s` : `${Math.round(n)} ms`;
}

export function rate(v) {
  const n = num(v);
  return n == null ? GAP : `${n >= 100 ? Math.round(n) : round1(n)} t/s`;
}

export function percent(v) {
  const n = num(v);
  return n == null ? GAP : `${round1(n * 100)}%`;
}

/** "p50 / p95" for a distribution summary, or the reason there is none. */
export function pair(dist, fmt) {
  const d = dist && typeof dist === 'object' ? dist : {};
  if (!num(d.n)) return 'not reported';
  if (d.insufficient) return `insufficient data (n=${d.n})`;
  return `${fmt(d.p50)} / ${fmt(d.p95)}`;
}

export function single(dist, fmt, field = 'p50') {
  const d = dist && typeof dist === 'object' ? dist : {};
  if (!num(d.n)) return 'not reported';
  if (d.insufficient) return `insufficient data (n=${d.n})`;
  return fmt(d[field]);
}

/** A time such as 2026-10-24T12:03:00Z as "2026-10-24 12:03 UTC". */
export function when(iso) {
  return typeof iso === 'string' && /^\d{4}-\d\d-\d\dT\d\d:\d\d/.test(iso)
    ? `${iso.slice(0, 10)} ${iso.slice(11, 16)} UTC` : GAP;
}

/** A hash query without the empty values, so "All" leaves no `project=&`. */
export const llmQuery = (obj) => new URLSearchParams(Object.entries(obj).filter(([, v]) => v)).toString();

const sessionHref = (id) => `#/herd/session/${encodeURIComponent(id)}`;
export const llmSessionHref = (id) => `#/llm/session/${encodeURIComponent(id)}`;

function link(key, names, href = sessionHref) {
  if (typeof key !== 'string') return GAP;
  if (key.startsWith('unmatched:')) {
    return `<span title="A usage record whose session id is not a known dashboard session">${esc(key.slice(10))} <em>(unmatched)</em></span>`;
  }
  const name = names && Object.hasOwn(names, key) ? names[key] : null;
  return `<a href="${esc(href(key))}">${esc(typeof name === 'string' && name ? name : key)}</a>`;
}

/** The proxy figures when the window has any, else the first source reported. */
export function pickTotal(all) {
  const groups = list(all?.groups);
  return groups.find((g) => g.source === PROXY) || groups[0] || null;
}

/** "↑ 18% vs previous 24h" for a count, or '' when either window has no figure to compare. */
function delta(now, before, win) {
  if (num(now) == null || num(before) == null || before <= 0) return '';
  const pct = Math.round(((now - before) / before) * 100);
  return `${pct > 0 ? '↑' : pct < 0 ? '↓' : '→'} ${Math.abs(pct)}% vs previous ${win}`;
}

function tiles(total, prev, win) {
  const t = total || {};
  const before = prev || {};
  const withDelta = (note, d) => [note, d].filter(Boolean).join(' · ');
  const tokens = t.tokens || {};
  const bound = tokens.lower_bound ? '≥ ' : '';
  const cards = [
    ['Requests', total ? reqs(t) : GAP, withDelta(!total ? 'no records in this window' : t.source === 'transcript' ? 'tokens only, from the agent\'s transcript' : '', delta(t.requests, before.requests, win))],
    ['Output tokens', total ? `${bound}${compact(tokens.output)}` : GAP,
      withDelta(tokens.lower_bound ? 'some replies carried no usage figure' : '', delta(tokens.output, before.tokens?.output, win))],
    ['TTFT p50 / p95', pair(t.ttft_ms, ms), withDelta('streams only', /^(not reported|insufficient data|—)/.test(pair(t.queue_ms, ms)) ? '' : `queue ${pair(t.queue_ms, ms)} waiting in the proxy`)],
    ['Speed p50', single(t.speed_tps, rate), 'streams with a usage figure'],
    ['Errors', total ? `${percent(t.error_rate)} (${count(t.errors)})` : GAP, delta(t.errors, before.errors, win)],
    // appended after the original five so their order never changes
    ['Input tokens', num(tokens.input) != null ? compact(tokens.input) : GAP, delta(tokens.input, before.tokens?.input, win)],
    ['Cache hit', num(tokens.cache_read) > 0 ? percent(tokens.cache_read / (tokens.cache_read + (num(tokens.input) || 0))) : GAP, 'of input tokens served from cache'],
  ];
  const reported = (value) => !/^(not reported|insufficient data|—)/.test(value);
  const shown = cards.filter(([, value]) => reported(value));
  const quiet = cards.filter(([, value]) => !reported(value));
  return `${shown.length ? `<div class="llm-tiles">${shown.map(([label, value, note]) => `
    <div class="llm-tile"><div class="llm-tile__label">${esc(label)}</div>
    <div class="llm-tile__value">${esc(value)}</div>${note ? `<div class="llm-tile__note">${esc(note)}</div>` : ''}</div>`).join('')}</div>` : ''}${
    quiet.length ? `<p class="llm-quiet" data-llm-quiet>${quiet.map(([label, value, note]) => `<span title="${esc(note || '')}">${esc(label)}: ${esc(value === GAP && total ? 'not reported' : value)}</span>`).join('')}</p>` : ''}`;
}


/** A tab switcher with no script: radio inputs choose which panel shows. `tabs`: [[label, badge|'', html, hasData]]; the first
 *  tab with data is the one shown, else the first. Every panel stays in the DOM, so the exact numbers are always there to read. */
function switcher(id, tabs) {
  const first = Math.max(0, tabs.findIndex((t) => t[3] !== false));
  return `<section class="llm-card llm-switch" data-llm-switch="${id}">${tabs.map((t, i) => `<input type="radio" class="llm-sr" name="${id}" id="${id}-${i}"${i === first ? ' checked' : ''}>`).join('')}
    <div class="llm-tabs" role="presentation">${tabs.map((t, i) => `<label for="${id}-${i}"${t[3] === false ? ' class="llm-tab--empty"' : ''}>${esc(t[0])}${t[1] ? ` <span class="llm-tab__badge">${esc(t[1])}</span>` : ''}</label>`).join('')}</div>
    <div class="llm-panels">${tabs.map((t, i) => `<div class="llm-panel" data-panel="${id}-${i}">${t[2]}</div>`).join('')}</div></section>`;
}
const switchCss = (id, n) => Array.from({ length: n }, (_, i) => `[data-llm-switch="${id}"] #${id}-${i}:checked ~ .llm-panels [data-panel="${id}-${i}"]{display:block}[data-llm-switch="${id}"] #${id}-${i}:checked ~ .llm-tabs label[for="${id}-${i}"]{background:var(--surface-2);color:var(--text);border-color:var(--border)}[data-llm-switch="${id}"] #${id}-${i}:focus-visible ~ .llm-tabs label[for="${id}-${i}"]{outline:2px solid var(--accent)}`).join('');

const TABLE_ROWS = 12;       // a long table shows its first rows; the rest sit behind one disclosure instead of a screen of ids
const tableOf = (headers, rows, empty, head = true) => `<div class="llm-table"><table class="llm-fixed"><thead${head ? '' : ' class="llm-sr"'}><tr>${headers.map((h) => `<th scope="col">${esc(h)}</th>`).join('')}</tr></thead><tbody>${
  rows.length ? rows.map((r) => `<tr>${r.map((c) => `<td>${c}</td>`).join('')}</tr>`).join('')
    : `<tr><td colspan="${headers.length}">${esc(empty)}</td></tr>`}</tbody></table></div>`;
const table = (headers, rows, empty) => rows.length <= TABLE_ROWS + 3 ? tableOf(headers, rows, empty)
  : `${tableOf(headers, rows.slice(0, TABLE_ROWS), empty)}<details class="llm-more"><summary>Show ${rows.length - TABLE_ROWS} more</summary>${tableOf(headers, rows.slice(TABLE_ROWS), empty, false)}</details>`;

function modelsTable(models) {
  return table(['Model', 'Source', 'Requests', 'Output tokens', 'TTFT p95', 'Speed p50', 'Errors'],
    list(models?.groups).map((g) => [esc(g.key), esc(g.source), reqs(g),
      `${g.tokens?.lower_bound ? '≥ ' : ''}${compact(g.tokens?.output)}`,
      single(g.ttft_ms, ms, 'p95'), single(g.speed_tps, rate), percent(g.error_rate)].map((c, i) => (i === 0 ? c : esc(c)))),
    'No requests recorded in this window.');
}

function missionsTable(missions) {
  return table(['Mission', 'Source', 'Requests', 'Output tokens', 'Errors', 'Last seen'],
    list(missions?.groups).map((g) => [`<a href="${esc(`#/missions/${encodeURIComponent(g.key)}`)}">${esc(g.key)}</a>`, esc(sourceLabel(g)), esc(reqs(g)),
      esc(`${g.tokens?.lower_bound ? '≥ ' : ''}${compact(g.tokens?.output)}`), esc(count(g.errors)), esc(when(g.last_seen))]),
    'No request in this window belonged to a mission.');
}

/** Agents joined by recorded handoffs form a chain; its usage is the sum over the chain's agents that have usage. Only recorded
 *  handoffs join anything: an agent no handoff names is not in a chain, and a chain with no usage in the window is left out. */
export function handoffChains(handoffs, agents) {
  const rows = list(handoffs).filter((h) => h && h.source?.session_id && h.target?.session_id)
    .sort((a, b) => (num(a.created_at) ?? 0) - (num(b.created_at) ?? 0));
  const parent = new Map();
  const find = (x) => { while (parent.get(x) !== x) { parent.set(x, parent.get(parent.get(x))); x = parent.get(x); } return x; };
  const add = (x) => { if (!parent.has(x)) parent.set(x, x); };
  for (const h of rows) { add(h.source.session_id); add(h.target.session_id); parent.set(find(h.source.session_id), find(h.target.session_id)); }
  const usage = new Map(list(agents?.groups).filter((g) => g.source === PROXY || g.source === 'transcript').map((g) => [g.key, g]));
  const chains = new Map();
  for (const h of rows) {
    const root = find(h.source.session_id);
    if (!chains.has(root)) chains.set(root, { ids: [] });
    for (const id of [h.source.session_id, h.target.session_id]) if (!chains.get(root).ids.includes(id)) chains.get(root).ids.push(id);
  }
  return [...chains.values()].map((c) => {
    const used = c.ids.map((id) => usage.get(id)).filter(Boolean);
    const sum = (f) => used.reduce((t, g) => t + (num(f(g)) || 0), 0);
    return { ids: c.ids, agents: used.length, requests: sum((g) => g.requests ?? g.events), output: sum((g) => g.tokens?.output), errors: sum((g) => g.errors) };
  }).filter((c) => c.agents > 0).sort((a, b) => b.output - a.output);
}

function chainsTable(chains, names) {
  return table(['Handoff chain', 'Agents with usage', 'Requests', 'Output tokens', 'Errors'],
    chains.map((c) => [c.ids.map((id) => link(id, names, llmSessionHref)).join(' → '), esc(String(c.agents)), esc(count(c.requests)), esc(compact(c.output)), esc(count(c.errors))]),
    'No recorded handoff joined agents that have usage in this window.');
}

const AGENTS_SHOWN = 10;
function agentsTable(agents, names) {
  const rows = list(agents?.groups).map((g) => [`${link(g.key, names, llmSessionHref)}${typeof g.key === 'string' && !g.key.startsWith('unmatched:') ? ` <a class="llm-ext" href="${esc(sessionHref(g.key))}" title="Open the agent" aria-label="Open the agent">↗</a>` : ''}`, esc(sourceLabel(g)), esc(reqs(g)),
    esc(`${g.tokens?.lower_bound ? '≥ ' : ''}${compact(g.tokens?.output)}`), esc(single(g.speed_tps, rate)),
    esc(count(g.errors)), esc(when(g.last_seen))]);
  const head = ['Agent', 'Source', 'Requests', 'Output tokens', 'Speed p50', 'Errors', 'Last seen'];
  const none = 'No request carried a session id in this window.';
  if (rows.length <= AGENTS_SHOWN + 2) return table(head, rows, none);
  // Long lists hide behind the busiest few: the rest is one click away, in the same order.
  return `${table(head, rows.slice(0, AGENTS_SHOWN), none)}<details class="llm-more"><summary>Show all ${rows.length} agents (${rows.length - AGENTS_SHOWN} more)</summary>${table(head, rows.slice(AGENTS_SHOWN), none)}</details>`;
}

function problemsTable(problems, names) {
  return table(['Class', 'Count', 'Model', 'Last seen', 'Sessions'],
    list(problems?.problems).map((p) => [esc(p.class), esc(count(p.count)), esc(p.model), esc(when(p.last_seen)),
      list(p.sessions).slice(0, 5).map((s) => link(s, names, llmSessionHref)).join(', ')
        + (p.session_count > 5 ? ` +${p.session_count - 5} more` : '') || GAP]),
    'No problems in this window.');
}

// --- charts: SVG with <title>/<desc>, a table of the same numbers below, tokens only ---
const W = 780; const H = 64; const X0 = 42; const Y1 = 84;     // wide and short: the plot spans the card instead of a small square in its middle
const fx = (i, n) => (n <= 1 ? X0 + W / 2 : X0 + (i / (n - 1)) * W);
const r2 = (v) => Math.round(v * 100) / 100;

/** When the data sits in the last part of the window, the empty lead is cut (one empty bucket is kept) and said so, so a lone
 *  bar is not drawn at the edge of a mostly blank axis. The table of every point stays complete. */
function trimLead(points, isEmpty) {
  const lead = points.findIndex((p) => !isEmpty(p));
  if (lead < 0 || lead <= points.length / 2) return { from: 0, note: '' };
  const from = lead - 1;
  return { from, note: `<p class="llm-note" data-llm-trim>Showing from ${esc(when(points[from].t))}: no requests earlier in this window.</p>` };
}

function barPlot(id, title, desc, ok0, bad0) {
  if (!ok0.length || ok0.every((p) => !p.v)) return `<p class="llm-note">No requests in this window.</p>`;
  const { from, note } = trimLead(ok0, (p) => !p.v);
  const ok = ok0.slice(from); const bad = bad0.slice(from);
  const n = ok.length;
  const top = Math.max(...ok.map((p) => p.v));
  const bw = Math.min(22, (W / n) * 0.7);
  const bars = ok.map((p, i) => {
    const failed = Math.min(num(bad[i]?.v) || 0, p.v);
    const good = p.v - failed;
    const x = r2(fx(i, n) - bw / 2);
    const hg = (good / top) * H; const hb = (failed / top) * H;
    return `${good ? `<rect x="${x}" y="${r2(Y1 - hg)}" width="${r2(bw)}" height="${r2(hg)}" fill="var(--status-ok)"><title>${esc(when(p.t))}: ${good} ok</title></rect>` : ''}${
      failed ? `<rect x="${x}" y="${r2(Y1 - hg - hb)}" width="${r2(bw)}" height="${r2(hb)}" fill="var(--status-attention)"><title>${esc(when(p.t))}: ${failed} failed</title></rect>` : ''}`;
  }).join('');
  return `<svg viewBox="0 0 ${X0 + W + 18} ${Y1 + 28}" role="img" aria-labelledby="${id}-t ${id}-d"><title id="${id}-t">${esc(title)}</title><desc id="${id}-d">${esc(desc)}</desc><line x1="${X0}" y1="${Y1}" x2="${X0 + W}" y2="${Y1}"/><text x="34" y="${Y1 - H + 4}" text-anchor="end">${top}</text><text x="34" y="${Y1 + 4}" text-anchor="end">0</text>${bars}<text x="${X0}" y="${Y1 + 20}">${esc(when(ok[0].t).slice(5, 16))}</text><text x="${X0 + W}" y="${Y1 + 20}" text-anchor="end">${esc(when(ok[n - 1].t).slice(5, 16))}</text></svg>${note}`;
}

function linePlot(id, title, desc, series0, fmt) {
  const lead = series0[0] ? trimLead(series0[0].points, (p) => num(p.v) == null) : { from: 0, note: '' };
  const series = lead.from ? series0.map((s) => ({ ...s, points: s.points.slice(lead.from) })) : series0;
  const all = series.flatMap((s) => s.points.map((p) => num(p.v)).filter((v) => v != null));
  if (!all.length) return `<p class="llm-note">Not reported in this window, or fewer than 5 samples per bucket.</p>`;
  const top = Math.max(...all);
  const lines = series.map((s) => {
    const n = s.points.length;
    const segs = []; let cur = [];
    s.points.forEach((p, i) => {
      if (num(p.v) == null) { if (cur.length) segs.push(cur); cur = []; return; }
      cur.push([r2(fx(i, n)), r2(Y1 - (p.v / top) * H), p]);
    });
    if (cur.length) segs.push(cur);
    return segs.map((seg) => (seg.length === 1
      ? `<circle cx="${seg[0][0]}" cy="${seg[0][1]}" r="3" fill="${s.color}"><title>${esc(when(seg[0][2].t))}: ${esc(fmt(seg[0][2].v))}</title></circle>`
      : `<polyline points="${seg.map((q) => `${q[0]},${q[1]}`).join(' ')}" fill="none" stroke="${s.color}" stroke-width="2"/>`)).join('');
  }).join('');
  const first = series[0].points[0]; const last = series[0].points[series[0].points.length - 1];
  return `<svg viewBox="0 0 ${X0 + W + 18} ${Y1 + 28}" role="img" aria-labelledby="${id}-t ${id}-d"><title id="${id}-t">${esc(title)}</title><desc id="${id}-d">${esc(desc)}</desc><line x1="${X0}" y1="${Y1}" x2="${X0 + W}" y2="${Y1}"/><text x="34" y="${Y1 - H + 4}" text-anchor="end">${esc(fmt(top))}</text><text x="34" y="${Y1 + 4}" text-anchor="end">0</text>${lines}<text x="${X0}" y="${Y1 + 20}">${esc(when(first.t).slice(5, 16))}</text><text x="${X0 + W}" y="${Y1 + 20}" text-anchor="end">${esc(when(last.t).slice(5, 16))}</text></svg>${lead.note}`;
}

function seriesTable(headers, rows) {
  return table(headers, rows, 'No points.');
}

function charts(series) {
  const queue = list(series?.queue?.points);
  const req = list(series?.requests?.points); const err = list(series?.errors?.points);
  const ttft = list(series?.ttft?.points); const spd = list(series?.speed?.points);
  const step = series?.requests?.step || '';
  const data = seriesTable(['Time (UTC)', 'Requests', 'Errors', 'TTFT p95', 'Speed p50'],
    req.map((p, i) => [esc(when(p.t)), esc(count(p.v)), esc(count(err[i]?.v)),
      esc(num(ttft[i]?.v) == null ? GAP : ms(ttft[i].v)), esc(num(spd[i]?.v) == null ? GAP : rate(spd[i].v))]));
  const hasReq = req.some((p) => num(p.v) > 0);
  const hasLat = ttft.some((p) => num(p.v) != null);
  const hasSpd = spd.some((p) => num(p.v) != null);
  const hasQueue = queue.some((p) => num(p.v) != null);
  const sw = switcher('llm-chart', [
    [`Requests per ${step}`, '', `${barPlot('llm-req', 'Requests over time', `${req.length} buckets of ${step}; failed requests are stacked on top. Exact counts follow in Chart data.`, req, err)}
      <div class="llm-legend"><span style="--c:var(--status-ok)">OK</span><span style="--c:var(--status-attention)">Failed</span></div>`, hasReq],
    ['TTFT p95', '', `${linePlot('llm-lat', 'Latency over time', `${ttft.length} buckets of ${step}. Buckets with fewer than 5 samples are left out. Exact values follow in Chart data.`,
      [{ points: ttft, color: 'var(--accent)' }], ms)}<div class="llm-legend"><span style="--c:var(--accent)">TTFT p95 (axis in time)</span></div>`, hasLat],
    ['Speed p50', '', `${linePlot('llm-spd', 'Generation speed over time', `${spd.length} buckets of ${step}.`, [{ points: spd, color: 'var(--status-ok)' }], rate)}
      <div class="llm-legend"><span style="--c:var(--status-ok)">Speed p50 (axis in tokens/s)</span></div>`, hasSpd],
    ['Queue p95', '', `${linePlot('llm-q', 'Time waiting in the proxy', `${queue.length} buckets of ${step}. Buckets with fewer than 5 samples are left out.`, [{ points: queue, color: 'var(--status-attention)' }], ms)}
      <div class="llm-legend"><span style="--c:var(--status-attention)">Queue p95 (axis in time)</span></div>`, hasQueue],
  ]);
  return `<div class="llm-charts">${sw}</div><details class="llm-data"><summary>Chart data</summary>${data}</details>`;
}

export function disclosure(meta) {
  const m = meta || {};
  if (!m.retained_since) return 'No usage has been recorded yet.';
  return m.partial
    ? `Partial: records start ${when(m.retained_since)}, after the start of this window (${when(m.since)}).`
    : `Complete since ${when(m.since)}; records are kept from ${when(m.retained_since)}.`;
}

function controls(state, base = '#/llm') {
  const link2 = (w) => `<a class="llm-window" href="${esc(`${base}?${llmQuery({ ...state.filters, window: w })}`)}"${w === state.window ? ' aria-current="page"' : ''}>${w}</a>`;
  const select = (name, label, values) => `<select data-llm-filter="${name}" aria-label="${label}" title="${label}"${state.filters[name] ? ' class="llm-set"' : ''}><option value="">All ${label.toLowerCase()}s</option>${
    values.map((v) => `<option value="${esc(v)}" ${v === state.filters[name] ? 'selected' : ''}>${esc(v)}</option>`).join('')}</select>`;
  const filters = base === '#/llm'
    ? `${select('project', 'Project', state.projects || [])}${select('model', 'Model', state.modelNames || [])}${(state.missionNames || []).length || state.filters.mission ? select('mission', 'Mission', state.missionNames || []) : ''}` : '';
  return `<div class="llm-controls"><nav class="llm-windows" aria-label="Time window">${WINDOWS.map(link2).join('')}</nav>${filters}</div>`;
}

export const llmStyles = `<style>
.llm-controls{display:flex;flex-wrap:wrap;gap:12px 16px;align-items:center;margin:16px 0}.llm-controls select{padding:4px 12px;min-height:32px;border:1px solid var(--border);border-radius:var(--radius-sm);font:inherit;font-size:13px;color:var(--text-secondary);background:var(--surface);max-width:100%}.llm-controls select.llm-set{border-color:var(--accent);color:var(--text)}
.llm-windows{display:inline-flex;border:1px solid var(--border);border-radius:var(--radius-sm);overflow:hidden}.llm-window{padding:4px 12px;min-height:32px;display:inline-flex;align-items:center;font-size:13px;text-decoration:none;color:var(--text-secondary);background:var(--surface);border-left:1px solid var(--border)}.llm-window:first-child{border-left:0}.llm-window[aria-current=page]{background:var(--surface-2);color:var(--text);font-weight:600}
.llm-tiles{display:flex;flex-wrap:wrap;gap:12px;margin:12px 0}.llm-tile{flex:1 1 170px;border:1px solid var(--border);border-radius:var(--radius);background:var(--surface);padding:12px 14px;min-width:0}.llm-tile__label{font-size:12px;color:var(--text-secondary)}.llm-tile__value{font-size:20px;font-weight:600;margin-top:4px;overflow-wrap:anywhere}.llm-tile__note{font-size:11px;color:var(--text-secondary);margin-top:4px}
.llm-charts{display:grid;grid-template-columns:minmax(0,1fr);gap:16px;margin:16px 0}.llm-quiet{display:flex;flex-wrap:wrap;gap:4px 24px;margin:12px 0;font-size:12px;color:var(--text-muted)}.llm-card{min-width:0;border:1px solid var(--border);border-radius:var(--radius);background:var(--surface);padding:6px 14px 12px}.llm-card h2{font-size:14px;margin:10px 0 4px}.llm-card svg{display:block;width:100%;height:auto}.llm-card svg text{font-size:11px;fill:var(--text-secondary);font-family:inherit}.llm-card svg line{stroke:var(--border)}
.llm-legend{display:flex;flex-wrap:wrap;gap:12px;font-size:11px;color:var(--text-secondary)}.llm-legend span::before{content:'';display:inline-block;width:9px;height:9px;margin-right:5px;background:var(--c);border-radius:2px}
.llm-note{font-size:12px;line-height:1.6;color:var(--text-secondary);margin:6px 0}.llm-table{overflow:auto}.llm-table table{border-collapse:collapse;width:100%;font-size:13px}.llm-table th,.llm-table td{padding:7px 8px;text-align:left;border-bottom:1px solid var(--border-subtle);overflow-wrap:anywhere}.llm-table th{color:var(--text-secondary);font-weight:500}.llm-fixed{table-layout:fixed}.llm-fixed td:first-child,.llm-fixed th:first-child{width:34%}.llm-sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0)}
.llm-badge{font-size:11px;font-weight:500;padding:2px 8px;border:1px solid var(--border);border-radius:99px;color:var(--text-secondary);vertical-align:middle}.llm-back{font-size:13px}.llm-data{font-size:12px;margin:8px 0 16px}.llm-data>summary{cursor:pointer}.llm-more>summary{cursor:pointer;padding:8px 0;color:var(--accent);font-size:13px}.llm-section h2{font-size:15px;margin:22px 0 6px}.llm-switch{position:relative;margin:16px 0;padding:12px 16px 16px}.llm-tabs{display:flex;flex-wrap:wrap;gap:8px;margin:0 0 12px}.llm-tabs label{padding:4px 12px;min-height:32px;display:inline-flex;align-items:center;gap:4px;border:1px solid transparent;border-radius:var(--radius-sm);font-size:13px;color:var(--text-secondary);cursor:pointer}.llm-tabs label:hover{color:var(--text)}.llm-tab--empty{opacity:.6}.llm-tab__badge{font-size:11px;padding:0 8px;border:1px solid var(--border);border-radius:99px}.llm-panel{display:none}.llm-ext{margin-left:4px;color:var(--text-muted);text-decoration:none}.llm-ext:hover{color:var(--accent)}.llm-switch .llm-section>.llm-table:first-child{margin-top:0}
@media(max-width:650px){.llm-tile{flex-basis:140px}.llm-charts{grid-template-columns:minmax(0,1fr)}}
${switchCss('llm-chart', 4)}${switchCss('llm-tab', 5)}</style>`;

/** Models, agents and problems share one card; a tab with nothing to show says so in its label and is not the one opened first. */
function tables(id, defs) {
  return switcher(id, defs.map(([label, n, html]) => [label, n ? String(n) : '', `<div class="llm-section">${html}</div>`, n > 0]));
}

/** The whole page. `state`: {window, filters:{project,model}, projects, modelNames, names}.
 *  `data`: {total, models, agents, problems, series:{requests,errors,ttft,speed}} from the API. */
export function renderLlm(state, data) {
  const d = data || {};
  const chains = handoffChains(d.handoffs, d.agents);
  const extra = list(d.total?.groups).filter((g) => g !== pickTotal(d.total)).map((g) => g.source);
  return `${llmStyles}<h1>Usage</h1>${controls(state)}
    <p class="llm-note" data-llm-disclosure>${esc(disclosure(d.total))}${
      extra.length ? ` Other sources (${esc(extra.join(', '))}) are listed in the tables and never added to these figures.` : ''}${
      list(d.agents?.groups).some((g) => g.partial_source) ? ' Transcript rows cover only the events still retained for each agent, and carry tokens only.' : ''}</p>
    ${tiles(pickTotal(d.total), pickTotal(d.previous), state.window)}${charts(d.series)}
    ${tables('llm-tab', [['Models', list(d.models?.groups).length, modelsTable(d.models)], ['Agents', list(d.agents?.groups).length, agentsTable(d.agents, state.names)],
      ['Missions', list(d.missions?.groups).length, missionsTable(d.missions)],
      ['Handoffs', chains.length, chainsTable(chains, state.names)],
      ['Problems', list(d.problems?.problems).length, problemsTable(d.problems, state.names)]])}`;
}

/** One agent's usage (#/llm/session/<id>). `state`: {window, sessionId, name, known}.
 *  `data`: {total, models, problems, series}, all scoped to the session by the API. */
/** Where this agent sits in recorded handoffs: who handed work to it, who it handed to, and the chain's total. */
export function handoffNote(id, handoffs, agents, names) {
  const rows = list(handoffs).filter((h) => h?.source?.session_id && h?.target?.session_id);
  const from = [...new Set(rows.filter((h) => h.target.session_id === id).map((h) => h.source.session_id))];
  const to = [...new Set(rows.filter((h) => h.source.session_id === id).map((h) => h.target.session_id))];
  if (!from.length && !to.length) return '';
  const names_ = (ids) => ids.map((x) => link(x, names, llmSessionHref)).join(', ');
  const chain = handoffChains(rows, agents).find((c) => c.ids.includes(id));
  return `<p class="llm-note" data-llm-handoff>${from.length ? `← from ${names_(from)}` : ''}${from.length && to.length ? ' · ' : ''}${to.length ? `→ to ${names_(to)}` : ''}${
    chain && chain.agents > 1 ? ` · chain total: ${esc(compact(chain.output))} output tokens in ${esc(count(chain.requests))} requests` : ''}</p>`;
}

export function renderLlmSession(state, data) {
  const d = data || {};
  const id = state.sessionId;
  const title = typeof state.name === 'string' && state.name ? state.name : id;
  const proxy = pickTotal(d.total);
  const sources = list(d.total?.groups).map((g) => g.source);
  const badge = sources.map((x) => `<span class="llm-badge">${esc(x)}</span>`).join(' ');
  const opens = state.known === false
    ? '<span class="llm-note">Not a known dashboard session.</span>'
    : `<a class="llm-back" href="${esc(sessionHref(id))}">Open agent ↗</a>`;
  const notReported = proxy && !num(proxy.ttft_ms?.n) && !num(proxy.speed_tps?.n)
    ? '<p class="llm-note" data-llm-latency-note>Latency and speed are not reported by this agent\'s path.</p>' : '';
  return `${llmStyles}<p class="llm-note"><a href="#/llm">← Usage</a></p><h1>${esc(title)} ${badge}</h1>
    <p>${opens}</p>${handoffNote(id, d.handoffs, d.agents, state.names)}${controls({ window: state.window, filters: {} }, llmSessionHref(id))}
    <p class="llm-note" data-llm-disclosure>${esc(disclosure(d.total))}</p>${notReported}
    ${tiles(proxy, pickTotal(d.previous), state.window)}${charts(d.series)}
    ${tables('llm-tab', [['Models', list(d.models?.groups).length, modelsTable(d.models) + (list(d.models?.groups).some((g) => g.source !== PROXY) ? '<p class="llm-note">Rows from another source (such as the transcript) come from the agent\'s own log and are not added to the figures above.</p>' : '')], ['Problems', list(d.problems?.problems).length, problemsTable(d.problems, {})]])}`;
}

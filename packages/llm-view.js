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

function tiles(total) {
  const t = total || {};
  const tokens = t.tokens || {};
  const bound = tokens.lower_bound ? '≥ ' : '';
  const cards = [
    ['Requests', count(t.requests), t.requests == null ? 'no records in this window' : ''],
    ['Output tokens', total ? `${bound}${compact(tokens.output)}` : GAP,
      tokens.lower_bound ? 'some replies carried no usage figure' : ''],
    ['TTFT p50 / p95', pair(t.ttft_ms, ms), 'streams only'],
    ['Speed p50', single(t.speed_tps, rate), 'streams with a usage figure'],
    ['Errors', total ? `${percent(t.error_rate)} (${count(t.errors)})` : GAP, ''],
  ];
  return `<div class="llm-tiles">${cards.map(([label, value, note]) => `
    <div class="llm-tile"><div class="llm-tile__label">${esc(label)}</div>
    <div class="llm-tile__value">${esc(value)}</div>${note ? `<div class="llm-tile__note">${esc(note)}</div>` : ''}</div>`).join('')}</div>`;
}

const table = (headers, rows, empty) => `<div class="llm-table"><table><thead><tr>${headers.map((h) => `<th scope="col">${esc(h)}</th>`).join('')}</tr></thead><tbody>${
  rows.length ? rows.map((r) => `<tr>${r.map((c) => `<td>${c}</td>`).join('')}</tr>`).join('')
    : `<tr><td colspan="${headers.length}">${esc(empty)}</td></tr>`}</tbody></table></div>`;

function modelsTable(models) {
  return table(['Model', 'Source', 'Requests', 'Output tokens', 'TTFT p95', 'Speed p50', 'Errors'],
    list(models?.groups).map((g) => [esc(g.key), esc(g.source), count(g.requests),
      `${g.tokens?.lower_bound ? '≥ ' : ''}${compact(g.tokens?.output)}`,
      single(g.ttft_ms, ms, 'p95'), single(g.speed_tps, rate), percent(g.error_rate)].map((c, i) => (i === 0 ? c : esc(c)))),
    'No requests recorded in this window.');
}

function agentsTable(agents, names) {
  return table(['Agent', 'Source', 'Requests', 'Output tokens', 'Speed p50', 'Errors', 'Last seen'],
    list(agents?.groups).map((g) => [link(g.key, names, llmSessionHref), esc(g.source), esc(count(g.requests)),
      esc(`${g.tokens?.lower_bound ? '≥ ' : ''}${compact(g.tokens?.output)}`), esc(single(g.speed_tps, rate)),
      esc(count(g.errors)), esc(when(g.last_seen))]),
    'No request carried a session id in this window.');
}

function problemsTable(problems, names) {
  return table(['Class', 'Count', 'Model', 'Last seen', 'Sessions'],
    list(problems?.problems).map((p) => [esc(p.class), esc(count(p.count)), esc(p.model), esc(when(p.last_seen)),
      list(p.sessions).slice(0, 5).map((s) => link(s, names, llmSessionHref)).join(', ')
        + (p.session_count > 5 ? ` +${p.session_count - 5} more` : '') || GAP]),
    'No problems in this window.');
}

// --- charts: SVG with <title>/<desc>, a table of the same numbers below, tokens only ---
const W = 300; const H = 100; const X0 = 42; const Y1 = 130;
const fx = (i, n) => (n <= 1 ? X0 + W / 2 : X0 + (i / (n - 1)) * W);
const r2 = (v) => Math.round(v * 100) / 100;

function barPlot(id, title, desc, ok, bad) {
  const n = ok.length;
  if (!n || ok.every((p) => !p.v)) return `<p class="llm-note">No requests in this window.</p>`;
  const top = Math.max(...ok.map((p) => p.v));
  const bw = Math.min(14, (W / n) * 0.7);
  const bars = ok.map((p, i) => {
    const failed = Math.min(num(bad[i]?.v) || 0, p.v);
    const good = p.v - failed;
    const x = r2(fx(i, n) - bw / 2);
    const hg = (good / top) * H; const hb = (failed / top) * H;
    return `${good ? `<rect x="${x}" y="${r2(Y1 - hg)}" width="${r2(bw)}" height="${r2(hg)}" fill="var(--status-ok)"><title>${esc(when(p.t))}: ${good} ok</title></rect>` : ''}${
      failed ? `<rect x="${x}" y="${r2(Y1 - hg - hb)}" width="${r2(bw)}" height="${r2(hb)}" fill="var(--status-attention)"><title>${esc(when(p.t))}: ${failed} failed</title></rect>` : ''}`;
  }).join('');
  return `<svg viewBox="0 0 360 165" role="img" aria-labelledby="${id}-t ${id}-d"><title id="${id}-t">${esc(title)}</title><desc id="${id}-d">${esc(desc)}</desc><line x1="${X0}" y1="${Y1}" x2="${X0 + W}" y2="${Y1}"/><text x="34" y="34" text-anchor="end">${top}</text><text x="34" y="134" text-anchor="end">0</text>${bars}<text x="${X0}" y="150">${esc(when(ok[0].t).slice(5, 16))}</text><text x="${X0 + W}" y="150" text-anchor="end">${esc(when(ok[n - 1].t).slice(5, 16))}</text></svg>`;
}

function linePlot(id, title, desc, series, fmt) {
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
  return `<svg viewBox="0 0 360 165" role="img" aria-labelledby="${id}-t ${id}-d"><title id="${id}-t">${esc(title)}</title><desc id="${id}-d">${esc(desc)}</desc><line x1="${X0}" y1="${Y1}" x2="${X0 + W}" y2="${Y1}"/><text x="34" y="20" text-anchor="end">${esc(fmt(top))}</text><text x="34" y="134" text-anchor="end">0</text>${lines}<text x="${X0}" y="150">${esc(when(first.t).slice(5, 16))}</text><text x="${X0 + W}" y="150" text-anchor="end">${esc(when(last.t).slice(5, 16))}</text></svg>`;
}

function seriesTable(headers, rows) {
  return table(headers, rows, 'No points.');
}

function charts(series) {
  const req = list(series?.requests?.points); const err = list(series?.errors?.points);
  const ttft = list(series?.ttft?.points); const spd = list(series?.speed?.points);
  const step = series?.requests?.step || '';
  const data = seriesTable(['Time (UTC)', 'Requests', 'Errors', 'TTFT p95', 'Speed p50'],
    req.map((p, i) => [esc(when(p.t)), esc(count(p.v)), esc(count(err[i]?.v)),
      esc(num(ttft[i]?.v) == null ? GAP : ms(ttft[i].v)), esc(num(spd[i]?.v) == null ? GAP : rate(spd[i].v))]));
  return `<div class="llm-charts"><section class="llm-card"><h2>Requests per ${esc(step)}</h2>${
    barPlot('llm-req', 'Requests over time', `${req.length} buckets of ${step}; failed requests are stacked on top. Exact counts follow in Chart data.`, req, err)}
    <div class="llm-legend"><span style="--c:var(--status-ok)">OK</span><span style="--c:var(--status-attention)">Failed</span></div></section>
    <section class="llm-card"><h2>TTFT p95 and speed p50</h2>${
      linePlot('llm-lat', 'Latency and speed over time', `${ttft.length} buckets of ${step}. Buckets with fewer than 5 samples are left out. Exact values follow in Chart data.`,
        [{ points: ttft, color: 'var(--accent)' }], ms)}
    <div class="llm-legend"><span style="--c:var(--accent)">TTFT p95 (axis in time)</span></div>${
      linePlot('llm-spd', 'Generation speed over time', `${spd.length} buckets of ${step}.`, [{ points: spd, color: 'var(--status-ok)' }], rate)}
    <div class="llm-legend"><span style="--c:var(--status-ok)">Speed p50 (axis in tokens/s)</span></div></section></div>
    <details class="llm-data"><summary>Chart data</summary>${data}</details>`;
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
  const select = (name, label, values) => `<label>${label}<select data-llm-filter="${name}"><option value="">All</option>${
    values.map((v) => `<option value="${esc(v)}" ${v === state.filters[name] ? 'selected' : ''}>${esc(v)}</option>`).join('')}</select></label>`;
  const filters = base === '#/llm'
    ? `${select('project', 'Project', state.projects || [])}${select('model', 'Model', state.modelNames || [])}` : '';
  return `<div class="llm-controls"><nav class="llm-windows" aria-label="Time window">${WINDOWS.map(link2).join('')}</nav>${filters}</div>`;
}

export const llmStyles = `<style>
.llm-controls{display:flex;flex-wrap:wrap;gap:12px;align-items:flex-end;margin:10px 0}.llm-controls label{display:flex;flex-direction:column;gap:4px;font-size:12px}.llm-controls select{padding:7px 9px;border:1px solid var(--border);border-radius:var(--radius-sm);font:inherit;color:var(--text);background:var(--surface);max-width:100%}
.llm-windows{display:flex;gap:4px}.llm-window{padding:7px 12px;border:1px solid var(--border);border-radius:var(--radius-sm);font-size:13px;text-decoration:none;color:var(--text);background:var(--surface)}.llm-window[aria-current=page]{border-color:var(--accent);font-weight:600}
.llm-tiles{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:12px;margin:12px 0}.llm-tile{border:1px solid var(--border);border-radius:var(--radius);background:var(--surface);padding:12px 14px;min-width:0}.llm-tile__label{font-size:12px;color:var(--text-secondary)}.llm-tile__value{font-size:20px;font-weight:600;margin-top:4px;overflow-wrap:anywhere}.llm-tile__note{font-size:11px;color:var(--text-secondary);margin-top:4px}
.llm-charts{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:18px;margin:16px 0}.llm-card{min-width:0;border:1px solid var(--border);border-radius:var(--radius);background:var(--surface);padding:6px 14px 12px}.llm-card h2{font-size:14px;margin:10px 0 4px}.llm-card svg{display:block;width:100%;max-height:210px}.llm-card svg text{font-size:11px;fill:var(--text-secondary);font-family:inherit}.llm-card svg line{stroke:var(--border)}
.llm-legend{display:flex;flex-wrap:wrap;gap:12px;font-size:11px;color:var(--text-secondary)}.llm-legend span::before{content:'';display:inline-block;width:9px;height:9px;margin-right:5px;background:var(--c);border-radius:2px}
.llm-note{font-size:12px;line-height:1.6;color:var(--text-secondary);margin:6px 0}.llm-table{overflow:auto}.llm-table table{border-collapse:collapse;width:100%;font-size:13px}.llm-table th,.llm-table td{padding:7px 8px;text-align:left;border-bottom:1px solid var(--border-subtle);overflow-wrap:anywhere}.llm-table th{color:var(--text-secondary);font-weight:500}
.llm-badge{font-size:11px;font-weight:500;padding:2px 8px;border:1px solid var(--border);border-radius:99px;color:var(--text-secondary);vertical-align:middle}.llm-back{font-size:13px}.llm-data{font-size:12px;margin:8px 0 16px}.llm-data>summary{cursor:pointer}.llm-section h2{font-size:15px;margin:22px 0 6px}
@media(max-width:650px){.llm-tiles{grid-template-columns:repeat(2,minmax(0,1fr))}.llm-charts{grid-template-columns:minmax(0,1fr)}}
</style>`;

/** The whole page. `state`: {window, filters:{project,model}, projects, modelNames, names}.
 *  `data`: {total, models, agents, problems, series:{requests,errors,ttft,speed}} from the API. */
export function renderLlm(state, data) {
  const d = data || {};
  const extra = list(d.total?.groups).filter((g) => g !== pickTotal(d.total)).map((g) => g.source);
  return `${llmStyles}<h1>LLM</h1>${controls(state)}
    <p class="llm-note" data-llm-disclosure>${esc(disclosure(d.total))}${
      extra.length ? ` Other sources (${esc(extra.join(', '))}) are listed in the tables and never added to these figures.` : ''}</p>
    ${tiles(pickTotal(d.total))}${charts(d.series)}
    <section class="llm-section"><h2>Models</h2>${modelsTable(d.models)}</section>
    <section class="llm-section"><h2>Agents</h2>${agentsTable(d.agents, state.names)}</section>
    <section class="llm-section"><h2>Problems</h2>${problemsTable(d.problems, state.names)}</section>`;
}

/** One agent's usage (#/llm/session/<id>). `state`: {window, sessionId, name, known}.
 *  `data`: {total, models, problems, series}, all scoped to the session by the API. */
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
  return `${llmStyles}<p class="llm-note"><a href="#/llm">← LLM</a></p><h1>${esc(title)} ${badge}</h1>
    <p>${opens}</p>${controls({ window: state.window, filters: {} }, llmSessionHref(id))}
    <p class="llm-note" data-llm-disclosure>${esc(disclosure(d.total))}</p>${notReported}
    ${tiles(proxy)}${charts(d.series)}
    <section class="llm-section"><h2>Models</h2>${modelsTable(d.models)}</section>
    <section class="llm-section"><h2>Problems</h2>${problemsTable(d.problems, {})}</section>`;
}

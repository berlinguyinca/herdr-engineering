// History of report snapshots as small SVG sparklines. Pure: numbers in, SVG text out, so it runs under `node --test`.
// Every value is a code-computed count from the server (never model text); the caller never passes markup in.
export const PLOT_ROWS = [
  ['prompts', 'Prompts'],
  ['events', 'Events'],
  ['tool_failures', 'Failed calls'],
  ['worked_s', 'Time worked'],
  ['fresh', 'Tokens fresh'],
];
const W = 240, H = 22, PAD = 2;

export function formatCount(n) {
  if (!Number.isFinite(n)) return '–';
  if (n >= 1e6) return `${+(n / 1e6).toFixed(1)}M`;
  if (n >= 1e3) return `${+(n / 1e3).toFixed(1)}k`;
  return String(n);
}
export function formatSeconds(s) {
  if (!Number.isFinite(s)) return '–';
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  return m < 60 ? `${m}m` : `${Math.floor(m / 60)}h${String(m % 60).padStart(2, '0')}m`;
}

// snapshots: oldest first. Returns null when there is nothing worth a plot (fewer than 2 snapshots).
export function plotModel(snapshots) {
  const snaps = Array.isArray(snapshots) ? snapshots.filter(s => s && typeof s === 'object') : [];
  if (snaps.length < 2) return null;
  const step = (W - 2 * PAD) / (snaps.length - 1);
  const rows = [];
  for (const [key, label] of PLOT_ROWS) {
    const vals = snaps.map(s => (Number.isFinite(s[key]) ? s[key] : null));
    const known = vals.filter(v => v !== null);
    if (!known.length) continue;
    const lo = Math.min(...known), hi = Math.max(...known);
    const y = v => (hi === lo ? H / 2 : H - PAD - ((v - lo) / (hi - lo)) * (H - 2 * PAD));
    const points = vals.map((v, i) => (v === null ? null : [+(PAD + i * step).toFixed(1), +y(v).toFixed(1)]));
    const fmt = key === 'worked_s' ? formatSeconds : formatCount;
    const firstV = known[0], lastV = known[known.length - 1];
    rows.push({key, label, points, first: fmt(firstV), last: fmt(lastV), text: `${fmt(firstV)} → ${fmt(lastV)}`});
  }
  const drift = snaps.map((s, i) => (s.drift === true ? i : -1)).filter(i => i >= 0);
  return {count: snaps.length, rows, drift, width: W, height: H, step};
}

// Break the line where a value is missing instead of drawing across it.
function path(points) {
  let d = '', pen = false;
  for (const p of points) {
    if (!p) { pen = false; continue; }
    d += `${pen ? 'L' : 'M'}${p[0]} ${p[1]}`;
    pen = true;
  }
  return d;
}

export function plotHtml(snapshots, esc = s => String(s)) {
  const m = plotModel(snapshots);
  if (!m) return '';
  const rows = m.rows.map(r => {
    const dots = r.points.filter(Boolean);
    const last = dots[dots.length - 1];
    const marks = m.drift.map(i => (r.points[i] ? `<circle class="sw-plot-drift" cx="${r.points[i][0]}" cy="${r.points[i][1]}" r="3"/>` : '')).join('');
    return `<div class="sw-plot-row" data-plot-row="${esc(r.key)}"><span class="sw-plot-label">${esc(r.label)}</span>`
      + `<svg viewBox="0 0 ${m.width} ${m.height}" width="${m.width}" height="${m.height}" role="img" aria-label="${esc(`${r.label}: ${r.text}`)}">`
      + `<path d="${path(r.points)}" fill="none" stroke="currentColor" stroke-width="1.5"/>${marks}`
      + (last ? `<circle cx="${last[0]}" cy="${last[1]}" r="2" fill="currentColor"/>` : '')
      + `</svg><span class="sw-plot-value">${esc(r.text)}</span></div>`;
  }).join('');
  const note = m.drift.length ? `<p class="sw-plot-note">Highlighted: snapshots where the latest segment's tokens exceeded the earlier average.</p>` : '';
  return `<section class="sw-plot" data-snapshot-plot><h4>Snapshot history (${m.count} snapshots)</h4>${rows}${note}</section>`;
}

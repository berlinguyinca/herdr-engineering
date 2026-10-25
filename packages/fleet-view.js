// Fleet version matrix: one row per tool/plugin, one column per host. The
// reference is the fleet's majority version, so drift reads at a glance.
import { escapeHtml as esc } from './herdr-web-components/util.js';
import { formatTimestamp } from './herdr-web-components/format.js';
import { reportFreshness } from './record-state.js';

const STATE_LABEL = { ok: 'in sync', drifted: 'differs', missing: 'not installed' };
const GROUP_LABEL = { tools: 'Tools', plugins: 'Plugins and packages' };

export function selectComponents(matrix, { driftedOnly = false } = {}) {
  const all = matrix?.components ?? [];
  return driftedOnly ? all.filter(c => c.drifted || c.missing) : all;
}

function cell(entry, host) {
  const state = entry?.state ?? 'missing';
  const text = entry?.version ? esc(entry.version) : '—';
  // Text carries the state too (never colour alone).
  return `<td class="hd-fleet__cell hd-fleet__cell--${state}" data-host-id="${esc(host)}" title="${STATE_LABEL[state]}">${text}${state === 'drifted' ? ' <span aria-label="differs from fleet">≠</span>' : ''}</td>`;
}

export function renderFleetMatrix(matrix, opts = {}, now = Date.now()) {
  const hosts = matrix?.hosts ?? [];
  if (!hosts.length) {
    return '<herdr-empty message="No host has reported its versions yet. The snapshot service on each host reports every 10 minutes."></herdr-empty>';
  }
  const components = selectComponents(matrix, opts);
  const summary = matrix.summary ?? {};
  const head = hosts.map(h => {
    const stale = reportFreshness(h.collected_at, now, 60 * 60 * 1000) !== 'current';
    return `<th scope="col" data-host-id="${esc(h.host)}">${esc(h.host)}<div class="hd-card__sub">${stale ? 'stale · ' : ''}${esc(formatTimestamp(h.collected_at))}</div></th>`;
  }).join('');
  let body = '';
  for (const group of ['tools', 'plugins']) {
    const rows = components.filter(c => c.group === group);
    if (!rows.length) continue;
    body += `<tr><th class="hd-fleet__group" colspan="${hosts.length + 2}" scope="colgroup">${GROUP_LABEL[group]}</th></tr>`;
    body += rows.map(c => `<tr><th scope="row">${esc(c.name.replace(/^(claude|pi|codex):/, ''))}${/^(claude|pi|codex):/.test(c.name) ? ` <span class="hd-card__sub">${esc(c.name.split(':')[0])}</span>` : ''}</th>
      <td class="hd-fleet__ref">${esc(c.reference ?? '—')}</td>${hosts.map(h => cell(c.hosts[h.host], h.host)).join('')}</tr>`).join('');
  }
  if (!body) body = `<tr><td colspan="${hosts.length + 2}">Every host matches the fleet reference.</td></tr>`;
  return `<div class="hd-fleet" role="region" aria-label="Fleet versions" tabindex="0">
    <p class="hd-card__sub" style="margin:0 0 8px">${summary.drifted ?? 0} differing · ${summary.missing ?? 0} not installed · reference is the most common version across ${hosts.length} host(s)</p>
    <table class="hd-ins__table"><thead><tr><th scope="col">Component</th><th scope="col">Reference</th>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
}

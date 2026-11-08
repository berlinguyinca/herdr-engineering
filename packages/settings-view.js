import { escapeHtml as esc } from './herdr-web-components/util.js';
import { describeServices } from './service-catalog.js';

// The one place disabled services appear. Rows show a service's name and on/off state; an off service also shows the config
// key that enables it. Names and booleans only: no configuration value is read or shown, and no off service is linked.
export function renderSettings(capabilities) {
  if (capabilities === null) {
    return '<p class="hd-note" role="status">Could not read which services are enabled. Try again in a moment.</p>';
  }
  const groups = new Map();
  for (const row of describeServices(capabilities)) {
    if (!groups.has(row.group)) groups.set(row.group, []);
    groups.get(row.group).push(row);
  }
  const item = (row) => `<li data-service="${esc(row.capability)}" data-state="${row.enabled ? 'on' : 'off'}">`
    + `<strong>${esc(row.label)}</strong> <span class="hd-chip">${row.enabled ? 'On' : 'Off'}</span>`
    + `<span class="hd-note"> ${esc(row.description)}</span>`
    + (row.howToEnable ? `<span class="hd-note"> To enable, set <code>${esc(row.howToEnable)}</code> in the fleet config.</span>` : '')
    + '</li>';
  return '<p class="hd-note">Services that are off are hidden from the rest of the dashboard and listed here only.</p>'
    + [...groups].map(([group, rows]) => `<section class="hd-card"><h2>${esc(group)}</h2><ul class="hd-list">${rows.map(item).join('')}</ul></section>`).join('');
}

export async function mountSettings(container, client, { signal, freshness = () => null } = {}) {
  container.innerHTML = '<herdr-spinner label="Reading services"></herdr-spinner>';
  let capabilities = null;
  try {
    const health = await client.health({ probe: true, signal });
    capabilities = Array.isArray(health?.capabilities) ? health.capabilities : null;
  } catch { capabilities = null; }
  if (signal?.aborted) return { dispose() {} };
  container.innerHTML = renderSettings(capabilities);
  freshness()?.touch(capabilities !== null, capabilities === null ? 'could not read services' : undefined);
  return { dispose() {} };
}

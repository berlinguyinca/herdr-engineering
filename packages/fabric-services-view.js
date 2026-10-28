// Fabric Services: every service the gateway currently exposes, with its fabric address, owner, source host and
// health. Data comes from GET /api/v1/fabric/services; nothing here builds a link from free text.
import { escapeHtml as esc } from './herdr-web-components/util.js';
import { parseWebUrl } from './conversation-markdown.js';

export function leaseRow(lease) {
  const t = lease?.target || {}, o = lease?.owner || {};
  const url = parseWebUrl(lease?.url || '');
  const state = lease?.state !== 'active' ? lease?.state || 'unknown' : lease?.health_last_error ? 'unhealthy' : 'healthy';
  return { id: lease?.id || '', name: lease?.label || `${t.host || '?'}:${t.port || '?'}`, url: url?.href || null, state,
    host: t.machine_id || '', target: `${t.host || '?'}:${t.port || '?'}`, owner: o.herdr_session_id || o.repository || '',
    error: lease?.health_last_error || '', renewed: lease?.renewed_at || '' };
}

export function filterLeases(leases, search = '') {
  const q = String(search).trim().toLowerCase();
  const rows = (leases || []).map(leaseRow);
  return q ? rows.filter((r) => [r.name, r.url, r.host, r.target, r.owner, r.state].some((v) => String(v || '').toLowerCase().includes(q))) : rows;
}

export function renderFabricServices(data, search = '') {
  if (data && data.available === false) return '<p class="empty">The fabric gateway is not reachable from the control plane, so fabric services cannot be listed.</p>';
  const rows = filterLeases(data?.leases, search);
  if (!rows.length) return `<p class="empty">${search ? 'No fabric service matches that search.' : 'No services are currently exposed on the fabric.'}</p>`;
  return `<table class="fabric-services"><thead><tr><th>Service</th><th>Fabric address</th><th>Host</th><th>Target</th><th>Owner</th><th>Status</th></tr></thead><tbody>${rows.map((r) => `<tr data-lease="${esc(r.id)}"><td>${esc(r.name)}</td><td>${r.url ? `<a href="${esc(r.url)}" target="_blank" rel="noopener noreferrer nofollow">${esc(r.url)}</a>` : '—'}</td><td>${esc(r.host)}</td><td>${esc(r.target)}</td><td>${esc(r.owner || '—')}</td><td><span class="fabric-state fabric-state--${esc(r.state)}"${r.error ? ` title="${esc(r.error)}"` : ''}>${esc(r.state)}</span></td></tr>`).join('')}</tbody></table>`;
}

export function mountFabricServices(root, client, data, { search = '' } = {}) {
  root.innerHTML = `<section class="fabric-services-page"><h2>Fabric services</h2><p>Services exposed through the private fabric gateway. Only registered, authorised destinations appear here.</p><label>Search <input type="search" data-fabric-search value="${esc(search)}" placeholder="name, host, address, owner"></label><div data-fabric-list></div></section>`;
  const list = root.querySelector('[data-fabric-list]'), input = root.querySelector('[data-fabric-search]');
  const paint = () => { list.innerHTML = renderFabricServices(data, input.value); };
  input.addEventListener('input', paint);
  paint();
  const timer = setInterval(async () => { try { data = await client.listFabricServices(); paint(); } catch { /* keep the last list */ } }, 15000);
  return () => clearInterval(timer);
}

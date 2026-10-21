// Hosts page: central fleet config revision badge. The status API carries only
// revisions and host ids (no values), and we render nothing else from it.
import { escapeHtml as esc } from './herdr-web-components/util.js';

const short = (rev) => (typeof rev === 'string' && rev ? rev.slice(0, 7) : '');
const key = (v) => (typeof v === 'string' ? v.toLowerCase() : '');

// null status (server has no fleet config) yields an index that renders nothing.
export function indexConfigStatus(status) {
  if (!status || typeof status !== 'object' || !Array.isArray(status.hosts)) return null;
  const byHost = new Map();
  for (const h of status.hosts) {
    if (h && typeof h.id === 'string') byHost.set(key(h.id), h.applied_revision ?? null);
  }
  return { revision: status.revision, hasError: Boolean(status.last_error), byHost };
}

export function configNotice(idx) {
  if (!idx || !idx.hasError) return '';
  return '<p class="hd-card__sub">Fleet config file has an error; hosts keep the last good revision</p>';
}

export function configBadge(idx, host) {
  if (!idx) return '';
  const k = [key(host?.host_id), key(host?.host_name)].find((c) => c && idx.byHost.has(c));
  if (k === undefined) return '';
  const applied = idx.byHost.get(k);
  if (applied && applied === idx.revision) {
    return '<span class="hd-ins__badge hd-ins__badge--live">Config current</span>';
  }
  const title = applied ? `applied ${esc(short(applied))} of ${esc(short(idx.revision))}` : 'No config reported';
  return `<span class="hd-ins__badge hd-ins__badge--warn" title="${title}">Config behind${applied ? '' : ' · No config reported'}</span>`;
}

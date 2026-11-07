// Fabric-trust display helpers for the collaboration dashboard: a "via fabric" badge on events and the fabric state of an agent.
// Pure string builders; every value is escaped here, so callers can drop the result into innerHTML.
import { escapeHtml as esc } from './herdr-web-components/util.js';

const str = (v) => (typeof v === 'string' ? v : '');

/** Small badge for an event or audit row whose actor was identified by its tailnet machine; '' for everything else. */
export function fabricBadge(ev) {
  if (!ev || ev.via !== 'fabric') return '';
  const m = str(ev.machine);
  return `<span class="hd-ident" data-fabric-via title="Identified by its tailnet machine, not a secret">via fabric${m ? ` · ${esc(m)}` : ''}</span>`;
}

/** 'off' | 'active' | 'expired' for an agent listing row (expiry in epoch seconds; none recorded counts as expired). */
export function fabricState(a, nowSec = Date.now() / 1000) {
  if (!a || !a.fabric) return 'off';
  return typeof a.fabric_expires_at === 'number' && a.fabric_expires_at > nowSec ? 'active' : 'expired';
}

/** One line for an agent card: machine, fabric bit and its expiry. '' when the agent has neither. */
export function fabricAgentLine(a, nowSec = Date.now() / 1000) {
  if (!a) return '';
  const machine = str(a.machine);
  const state = fabricState(a, nowSec);
  if (!machine && state === 'off') return '';
  const parts = [];
  if (machine) parts.push(`machine ${esc(machine)}`);
  if (state === 'active') parts.push(`fabric until ${esc(new Date(a.fabric_expires_at * 1000).toLocaleDateString())}`);
  else if (state === 'expired') parts.push('<b data-fabric-expired>fabric expired</b> (the owner can renew it)');
  else parts.push('fabric off');
  return `<span class="hd-card__sub" data-fabric-line>${parts.join(' · ')}</span>`;
}

/** "coordinator: <team> · roles a, b" for a grant's coordinator view ({team_id, roles}); '' for a plain agent. */
export function coordinatorLine(c) {
  if (!c || !c.team_id) return '';
  const roles = Array.isArray(c.roles) ? c.roles.map(str).filter(Boolean) : [];
  return `<span class="hd-card__sub" data-coordinator-line>coordinator: ${esc(c.team_id)} · roles ${roles.length ? esc(roles.join(', ')) : 'none'}</span>`;
}

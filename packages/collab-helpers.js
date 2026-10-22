// Agent helpers on the collab page: room profile editor (owner), host-access requests and the
// external-hosts table (operator), the helper tree and the panic switch. Pure string renderers plus
// one action dispatcher, so they run under `node --test`. Every string is escaped. The profile holds
// the NAMES of secrets only; values live in files on the host and are never requested or shown here.
import { escapeHtml as esc } from './herdr-web-components/util.js';

const DAY = 86400;
const lines = (v) => String(v ?? '').split(/[\n,]/).map((x) => x.trim()).filter(Boolean);

export function renderPanic(paused, { operator }) {
  if (!operator) return '';
  const label = paused ? 'Helpers paused' : 'Helpers running';
  return `<div class="cx-row cx-panic${paused ? ' cx-panic--on' : ''}"><span class="hd-status" style="--sc:${paused ? 'var(--status-failed)' : 'var(--status-ok)'}"><span class="hd-status__dot" aria-hidden="true">●</span><span class="hd-status__label">${label}</span></span>`
    + `<button type="button" class="hd-btn hd-btn--sm${paused ? '' : ' hd-btn--danger'}" data-panic="${paused ? 'resume' : 'pause'}">${paused ? 'Resume helpers' : 'Pause all helpers'}</button></div>`;
}

const when = (epoch) => (epoch ? new Date(epoch * 1000).toLocaleString() : '');

export function renderEgressRequests(requests, { operator }) {
  if (!operator) return '';
  const open = (requests || []).filter((r) => r.state === 'open');
  if (!open.length) return '<div class="hd-empty">No host access requests.</div>';
  const b = (act, label, cls = '') => `<button type="button" class="hd-btn hd-btn--sm${cls}" data-egress-act="${act}">${label}</button>`;
  return `<ul class="cx-list">${open.map((r) => `<li data-id="${esc(r.id)}" data-room="${esc(r.room_id)}" data-hp="${esc(r.host_port)}"><span class="grow"><b>${esc(r.host_port)}</b><span class="cx-sub1">asked by agent ${esc(r.agent_id)}</span></span>`
    + `<span class="cx-row">${b('approve-1d', 'Approve 1 day')}${b('approve-1w', 'Approve 1 week')}${b('always', 'Always', ' hd-btn--ghost')}${b('deny', 'Deny', ' hd-btn--danger')}</span></li>`).join('')}</ul>`;
}

// Permanent hosts from the profile plus approvals that have not expired.
export function renderHostsTable(permanent, requests, { operator, room, now = Date.now() / 1000 }) {
  const rows = [...new Set(permanent || [])].map((h) => ({ hp: h, kind: 'always', until: null }));
  for (const r of requests || []) {
    if (r.state === 'approved' && r.expires_at > now && !rows.some((x) => x.hp === r.host_port)) rows.push({ hp: r.host_port, kind: 'until', until: r.expires_at });
  }
  if (!rows.length) return '<div class="hd-empty">No external hosts.</div>';
  return `<table class="cx-table"><thead><tr><th>Host</th><th>Access</th>${operator ? '<th></th>' : ''}</tr></thead><tbody>${rows.map((r) => `<tr data-room="${esc(room || '')}" data-hp="${esc(r.hp)}"><td>${esc(r.hp)}</td><td>${r.kind === 'always' ? 'Always' : `Until ${esc(when(r.until))}`}</td>`
    + `${operator ? '<td><button type="button" class="hd-btn hd-btn--danger hd-btn--sm" data-egress-act="remove">Remove</button></td>' : ''}</tr>`).join('')}</tbody></table>`;
}

export function renderHelperTree(parentAgentId, flow, helpers, { operator } = {}) {
  const byAgent = new Map((helpers || []).map((h) => [h.helper_agent_id, h]));
  const seen = new Set();
  const kids = (id) => (flow.nodes || []).filter((n) => n.kind === 'agent' && n.parent_agent_id === id);
  const level = (id) => {
    if (seen.has(id)) return '';
    seen.add(id);
    const list = kids(id);
    if (!list.length) return '';
    return `<ul class="cx-tree">${list.map((n) => {
      const h = byAgent.get(n.agent_id);
      return `<li><b>@${esc(n.name)}</b>${h ? ` <span class="cx-sub1">${esc(h.status)}${operator && h.task ? ` · ${esc(h.task)}` : ''}</span>` : ''}${level(n.agent_id)}</li>`;
    }).join('')}</ul>`;
  };
  const inner = level(parentAgentId);
  return inner ? `<div class="cx-helpers"><b>Helpers</b>${inner}</div>` : '';
}

const csv = (a) => (a || []).join(', ');
const field = (label, html) => `<label class="cx-field">${label}${html}</label>`;

export function renderProfileEditor(profile, { owner }) {
  if (!owner) return '';
  const p = profile || {};
  const svc = (p.services || []).map((s) => `${s.name} | ${s.upstream_url} | ${s.header} | ${s.secret_name}`).join('\n');
  return `<form id="hcn-profile" class="cx-stack">
    <p class="cx-msg">Secrets are named here, never typed here. Their values live in files on the host and are set with the host CLI; this page cannot read or change them.</p>
    ${field('API keys: one per line as name | https upstream URL | header | secret name', `<textarea class="cx-input" name="services" rows="3" maxlength="4000">${esc(svc)}</textarea>`)}
    ${field('SSH secret names (one per line)', `<textarea class="cx-input" name="ssh_secret_names" rows="2">${esc((p.ssh_secret_names || []).join('\n'))}</textarea>`)}
    ${field('Allowed models (comma separated; empty = any)', `<input class="cx-input" name="allowed_models" value="${esc(p.allowed_models ? csv(p.allowed_models) : '')}">`)}
    <label class="cx-check"><input type="checkbox" name="tools_allowed"${p.tools_allowed ? ' checked' : ''}> Helpers may use tools</label>
    ${field('Always-allowed hosts (host:port, one per line)', `<textarea class="cx-input" name="permanent_hosts" rows="3">${esc((p.permanent_hosts || []).join('\n'))}</textarea>`)}
    <button class="hd-btn" type="submit">Save profile</button><span id="hcn-profile-msg" class="cx-msg" role="alert"></span></form>`;
}

// Form values -> profile body. Fields the form does not edit keep their loaded values.
export function parseProfileForm(v, base = {}) {
  const services = String(v.services ?? '').split('\n').map((l) => l.trim()).filter(Boolean).map((l) => {
    const [name, upstream_url, header, secret_name, ...rest] = l.split('|').map((x) => x.trim());
    if (!name || !upstream_url || !header || !secret_name || rest.length) throw new Error('each service needs: name | https URL | header | secret name');
    return { name, upstream_url, header, secret_name };
  });
  const models = lines(v.allowed_models);
  return {
    allowed_families: base.allowed_families || [], allowed_models: models.length ? models : null,
    tools_allowed: Boolean(v.tools_allowed), permanent_hosts: lines(v.permanent_hosts), services,
    ssh_secret_names: lines(v.ssh_secret_names), spend_tokens_per_day: base.spend_tokens_per_day ?? null,
    limits: base.limits || {},
  };
}

// One click handler for every button above (dataset of the clicked button, merged with its row's).
export async function helperAction(api, ds, extra = {}) {
  if (ds.panic) return api.pauseHelpers(ds.panic === 'pause');
  if (ds.profile === 'save') return api.setRoomProfile(ds.room, extra.profileBody);
  switch (ds.egressAct) {
    case 'approve-1d': return api.decideEgress(ds.id, 'approve', DAY);
    case 'approve-1w': return api.decideEgress(ds.id, 'approve', 7 * DAY);
    case 'deny': return api.decideEgress(ds.id, 'deny');
    case 'always': {                                  // permanent for the room, and close the open request
      await api.promoteEgress(ds.room, ds.hp);
      return api.decideEgress(ds.id, 'approve', DAY);
    }
    case 'remove': return api.removeEgress(ds.room, ds.hp);
    default: return undefined;
  }
}

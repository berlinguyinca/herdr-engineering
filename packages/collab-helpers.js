// Agent helpers on the collab page: room profile editor (owner), host-access requests and the
// external-hosts table (operator), the helper tree and the panic switch. Pure string renderers plus
// one action dispatcher, so they run under `node --test`. Every string is escaped. The profile holds
// the NAMES of secrets only; values live in files on the host and are never requested or shown here.
import { escapeHtml as esc } from './herdr-web-components/util.js';

const DAY = 86400;
export const FAMILIES = ['claude-code', 'codex', 'pi', 'agent'];   // collab.py SPAWN_FAMILIES
const MODEL_ENDPOINTS = new Set(['api.anthropic.com:443', 'api.openai.com:443', 'llm.metabolomics.us:443']);   // FAMILY_ENDPOINTS: always allowed
const lines = (v) => String(v ?? '').split(/[\n,]/).map((x) => x.trim()).filter(Boolean);

export function renderPanic(paused, { operator }) {
  if (!operator) return '';
  const label = paused ? 'Helpers paused' : 'Helpers running';
  return `<div class="cx-row cx-panic${paused ? ' cx-panic--on' : ''}"><span class="hd-status" style="--sc:${paused ? 'var(--status-failed)' : 'var(--status-ok)'}"><span class="hd-status__dot" aria-hidden="true">●</span><span class="hd-status__label">${label}</span></span>`
    + `<button type="button" class="hd-btn hd-btn--sm${paused ? '' : ' hd-btn--danger'}" data-panic="${paused ? 'resume' : 'pause'}">${paused ? 'Resume helpers' : 'Pause all helpers'}</button></div>`;
}

const when = (epoch) => (epoch ? new Date(epoch * 1000).toLocaleString() : '');

// A room as a link when the viewer can open it (it is in their room list), else plain escaped text.
export function renderRoomRef(roomId, rooms) {
  const r = (rooms || []).find((x) => x.room_id === roomId);
  return r ? `<a href="#/room/${encodeURIComponent(roomId)}" data-go="/room/${esc(encodeURIComponent(roomId))}">${esc(r.name || roomId)}</a>` : esc(roomId);
}

export function renderEgressRequests(requests, { operator, rooms = null }) {
  if (!operator) return '';
  const open = (requests || []).filter((r) => r.state === 'open');
  if (!open.length) return '<div class="hd-empty">No host access requests.</div>';
  const b = (act, label, cls = '') => `<button type="button" class="hd-btn hd-btn--sm${cls}" data-egress-act="${act}">${label}</button>`;
  return `<ul class="cx-list">${open.map((r) => `<li data-id="${esc(r.id)}" data-room="${esc(r.room_id)}" data-hp="${esc(r.host_port)}"><span class="grow"><b>${esc(r.host_port)}</b><span class="cx-sub1">asked by agent ${esc(r.agent_id)}${rooms ? ` in ${renderRoomRef(r.room_id, rooms)}` : ''}</span></span>`
    + `<span class="cx-row">${b('approve-1d', 'Approve 1 day')}${b('approve-1w', 'Approve 1 week')}${b('always', 'Always', ' hd-btn--ghost')}${b('deny', 'Deny', ' hd-btn--danger')}</span></li>`).join('')}</ul>`;
}

// Permanent hosts from the profile plus approvals that have not expired.
export function renderHostsTable(permanent, requests, opts) {
  const { operator, room, now = Date.now() / 1000 } = opts;
  // `permanent` is the room's allowlist (permanent + live + model endpoints) or just its permanent hosts
  const live = new Map((requests || []).filter((r) => r.state === 'approved' && r.expires_at > now).map((r) => [r.host_port, r.expires_at]));
  const rows = [...new Set(permanent || [])].map((h) => (MODEL_ENDPOINTS.has(h) ? { hp: h, kind: 'model' } : live.has(h) && !(opts.permanent || []).includes(h) ? { hp: h, kind: 'until', until: live.get(h) } : { hp: h, kind: 'always' }));
  for (const [hp, until] of live) {
    if (!rows.some((x) => x.hp === hp)) rows.push({ hp, kind: 'until', until });
  }
  if (!rows.length) return '<div class="hd-empty">No external hosts.</div>';
  return `<table class="cx-table"><thead><tr><th>Host</th><th>Access</th>${operator ? '<th></th>' : ''}</tr></thead><tbody>${rows.map((r) => `<tr data-room="${esc(room || '')}" data-hp="${esc(r.hp)}"><td>${esc(r.hp)}</td><td>${r.kind === 'always' ? 'Always' : r.kind === 'model' ? 'Model endpoint' : `Until ${esc(when(r.until))}`}</td>`
    + `${operator && r.kind !== 'model' ? '<td><button type="button" class="hd-btn hd-btn--danger hd-btn--sm" data-egress-act="remove">Remove</button></td>' : ''}</tr>`).join('')}</tbody></table>`;
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
    <fieldset class="cx-checks"><legend>Agent families helpers may use</legend>${FAMILIES.map((f) => `<label class="cx-check"><input type="checkbox" name="allowed_families" value="${esc(f)}"${(p.allowed_families || []).includes(f) ? ' checked' : ''}> ${esc(f)}</label>`).join('')}</fieldset>
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
    allowed_families: Array.isArray(v.allowed_families) ? v.allowed_families : (base.allowed_families || []), allowed_models: models.length ? models : null,
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
      try { return await api.decideEgress(ds.id, 'approve', DAY); } catch { return undefined; }   // promoted already: a 409 here is fine
    }
    case 'remove': return api.removeEgress(ds.room, ds.hp);
    default: return undefined;
  }
}

// Click and submit handling for one open room. Returns an unbind function the room's teardown must
// call, so a room visited earlier can never act on a click made in a later one.
export function wireHelperHandlers(container, o) {
  const onClick = (ev) => {
    const b = ev.target.closest?.('[data-panic],[data-egress-act]');
    if (!b || !o.operator) return;
    const row = b.closest('[data-id],[data-hp]');
    const rd = row ? row.dataset : {};
    // the top-level panel has no current room: each row carries the room of its own request / host
    o.act(async () => { await helperAction(o.api, { ...rd, ...b.dataset, room: o.perRow ? rd.room : o.roomId }); await o.refresh(); });
  };
  const onSubmit = (ev) => {
    if (ev.target.id !== 'hcn-profile' || !o.owner) return;
    ev.preventDefault();
    let body;
    try { body = parseProfileForm(o.readForm(ev.target), o.getProfile() || {}); } catch (e) { o.onError(e.message); return; }
    o.act(async () => { o.setProfile(await helperAction(o.api, { profile: 'save', room: o.roomId }, { profileBody: body })); });
  };
  container.addEventListener('click', onClick);
  container.addEventListener('submit', onSubmit);
  return () => { container.removeEventListener('click', onClick); container.removeEventListener('submit', onSubmit); };
}

// ---- the operator's top-level Helpers panel: every room, not just the open one
const ago = (s) => (s < 90 ? `${Math.max(0, Math.round(s))}s` : s < 5400 ? `${Math.round(s / 60)}m` : `${Math.round(s / 3600)}h`);

export function renderHelperList(helpers, rooms, now) {
  if (!(helpers || []).length) return '<div class="hd-empty">No helpers are running.</div>';
  return `<table class="cx-table"><thead><tr><th>Role</th><th>Status</th><th>Requester</th><th>Room</th><th>Age</th><th>Expires</th></tr></thead><tbody>${helpers.map((h) => `<tr data-helper="${esc(h.helper_id)}">`
    + `<td>${esc(h.role)}${h.task ? `<span class="cx-sub1">${esc(String(h.task).slice(0, 160))}</span>` : ''}</td><td>${esc(h.status)}</td><td>${esc(h.requester_agent_id)}</td><td>${renderRoomRef(h.room_id, rooms)}</td>`
    + `<td>${h.created_epoch ? esc(ago(now - h.created_epoch)) : ''}</td><td>${esc(when(h.expires_epoch))}</td></tr>`).join('')}</tbody></table>`;
}

// state: { paused, requests, helpers, allow: {roomId: [host...]}, rooms, now, error }
export function renderHelpersPanel(st, { operator }) {
  if (!operator) return '';
  const rooms = st.rooms || [];
  const reqs = st.requests || [];
  const hostRooms = Object.keys(st.allow || {}).filter((r) => (st.allow[r] || []).length);
  return `<section class="cx-section cx-helpers-panel"><h2>Helpers</h2>${st.error ? `<div class="hd-error" role="alert">${esc(st.error)}</div>` : ''}`
    + `${renderPanic(Boolean(st.paused), { operator })}`
    + `<div class="hd-card cx-stack"><div class="hd-card__title">Host access requests (all rooms)</div>${renderEgressRequests(reqs, { operator, rooms })}`
    + `<div class="hd-card__title">Helpers across rooms</div>${renderHelperList(st.helpers, rooms, st.now ?? Date.now() / 1000)}`
    + `<div class="hd-card__title">External hosts</div>${hostRooms.length ? hostRooms.map((r) => `<div class="cx-sub1">${renderRoomRef(r, rooms)}</div>${renderHostsTable(st.allow[r], reqs.filter((q) => q.room_id === r), { operator, room: r, permanent: [], now: st.now })}`).join('') : '<div class="hd-empty">No external hosts.</div>'}</div></section>`;
}

// Mounts the panel into its own container. All state and listeners belong to this instance, and
// destroy() unbinds them, so remounting (or leaving the page) can never leave a stale handler behind.
export function createHelpersPanel({ container, api, operator, getRooms, go, schedule = setTimeout, clear = clearTimeout, interval = 6000, hidden = () => false }) {
  if (!operator) return { refresh: async () => {}, start() {}, destroy() {} };
  let alive = true; let timer = null; let seq = 0;
  const st = { paused: false, requests: [], helpers: [], allow: {}, rooms: [], error: '' };
  const draw = () => { if (alive) container.innerHTML = renderHelpersPanel(st, { operator }); };
  const refresh = async () => {
    if (!alive) return;
    const mine = ++seq;
    const errs = [];
    const [rq, hp, rm] = await Promise.all([
      api.egressRequests().catch((e) => { errs.push(e.message); return null; }),
      api.helpers().catch((e) => { errs.push(e.message); return null; }),
      Promise.resolve().then(getRooms).catch(() => null)]);
    if (!alive || mine !== seq) return;
    if (rq) st.requests = (rq.requests || []).filter((r) => r.state === 'open' || r.state === 'approved');
    if (hp) { st.helpers = hp.helpers || []; st.paused = Boolean(hp.paused); st.now = hp.now; }
    if (rm) st.rooms = rm;
    const ids = [...new Set([...st.requests.map((r) => r.room_id), ...st.rooms.map((r) => r.room_id)])].slice(0, 40);
    const lists = await Promise.all(ids.map((id) => api.egressAllowlist(id).then((x) => [id, x.allowlist || []]).catch(() => null)));
    if (!alive || mine !== seq) return;
    st.allow = Object.fromEntries(lists.filter(Boolean));
    st.error = errs[0] || '';
    draw();
  };
  const unbind = wireHelperHandlers(container, {
    api, roomId: null, perRow: true, operator, owner: false,
    act: async (fn) => { try { await fn(); } catch (e) { st.error = e.message; draw(); } },
    refresh, getProfile: () => null, setProfile() {}, onError() {}, readForm: () => ({}),
  });
  const onGo = (ev) => {
    const a = ev.target.closest?.('a[data-go]');
    if (!a || !go) return;
    ev.preventDefault?.(); go(a.dataset.go);
  };
  container.addEventListener('click', onGo);
  const loop = async () => { await refresh(); if (alive) timer = schedule(loop, hidden() ? interval * 3 : interval); };
  return {
    refresh, start() { loop(); },
    destroy() { alive = false; clear(timer); unbind(); container.removeEventListener('click', onGo); },
  };
}

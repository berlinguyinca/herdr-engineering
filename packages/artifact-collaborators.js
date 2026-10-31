// Collaborators tab of the artifact share panel: which agents may read or commit to one artifact.
//
// Same three layers as artifact-share-panel.js: pure renderers (HTML built only from escaped values, no inline
// handlers), a DOM-free controller that owns the calls and the in-flight guard, and a thin delegated-event mount.
// The invite code exists only in controller.state.invite and in the value of one readonly input; it never appears in
// a rendered HTML string, and Close (or dispose) drops it. Restrict all access runs only after an explicit confirm.
import { escapeHtml as esc } from './herdr-web-components/util.js';

export const DEFAULT_DAYS = 30;
const MIN_DAYS = 1;
const MAX_DAYS = 90;
const INVITE_TTL_SECONDS = 3600;
const NOT_ENABLED = 'Collaboration is not enabled';

// ISO strings are trimmed to seconds; numbers are epoch seconds (the collab service's convention).
export function formatWhen(value) {
  if (value === null || value === undefined || value === '') return '';
  const text = typeof value === 'number' ? new Date(value * 1000).toISOString() : String(value);
  return text.replace('T', ' ').slice(0, 19);
}

// Missing fields show an em dash instead of the text "undefined".
export const show = value => (value === null || value === undefined || value === '' ? '—' : esc(value));
const agentLabel = agent => agent.name || agent.display_name || agent.agent_id;
const disabledAttr = on => (on ? ' disabled' : '');

export function renderCollaboratorRow(c, { busy = false } = {}) {
  return `<tr data-collab-row><td>${show(c.display_name || c.agent_id)}<br><code>${show(c.agent_id)}</code></td>`
    + `<td>${show(c.permission)}</td><td><time>${show(formatWhen(c.expires_at))}</time></td>`
    + `<td><time>${esc(formatWhen(c.last_used_at) || 'never')}</time></td><td>${esc(c.commits ?? 0)}</td><td>${esc(c.conflicts ?? 0)}</td>`
    + `<td><button type="button" class="hd-btn" data-collab-action="revoke" data-agent-id="${esc(c.agent_id ?? '')}"${disabledAttr(busy)}>Revoke</button></td></tr>`;
}

const renderPending = invite => `<li data-collab-pending><code>${show(invite.invite_id)}</code> · ${show(invite.permission)} · expires <time>${show(formatWhen(invite.expires_at))}</time></li>`;

export function renderCollaboratorList(state) {
  const rows = state.collaborators || [];
  const pending = state.pending || [];
  const table = rows.length
    ? `<table class="hd-table"><thead><tr><th>Agent</th><th>Permission</th><th>Expires</th><th>Last used</th><th>Commits</th><th>Conflicts</th><th></th></tr></thead><tbody>${rows.map(c => renderCollaboratorRow(c, { busy: state.busy })).join('')}</tbody></table>`
    : '<p class="hd-note">No agent has access to this artifact.</p>';
  const waiting = pending.length ? `<h4>Pending invites</h4><ul>${pending.map(renderPending).join('')}</ul>` : '';
  return table + waiting;
}

const daysInput = (field, value) => `<input class="hd-input" type="number" min="${MIN_DAYS}" max="${MAX_DAYS}" step="1" required data-collab-field="${field}" value="${esc(value ?? DEFAULT_DAYS)}" aria-label="Days until access expires (${MIN_DAYS}-${MAX_DAYS})">`;

const permissionSelect = (field, value, { writable, writableReason }) => `<select class="hd-input" data-collab-field="${field}" aria-label="Permission"><option value="read"${value !== 'write' ? ' selected' : ''}>Read</option><option value="write"${value === 'write' ? ' selected' : ''}${disabledAttr(!writable)}>Read+write</option></select>${writable ? '' : `<p class="hd-note" data-collab-writable-reason>Read+write is unavailable: ${esc(writableReason || 'this artifact cannot take collaborator commits')}</p>`}`;

export function renderGrantForm(state) {
  const form = state.form || {};
  const picker = Array.isArray(state.agents)
    ? `<select class="hd-input" data-collab-field="agent_id" aria-label="Agent"><option value="">Select an agent</option>${state.agents.map(a => `<option value="${esc(a.agent_id ?? '')}"${form.agent_id === a.agent_id ? ' selected' : ''}>${show(agentLabel(a))}</option>`).join('')}</select>`
    : `<input class="hd-input" type="text" data-collab-field="agent_id" value="${esc(form.agent_id ?? '')}" placeholder="Agent id" aria-label="Agent id" autocomplete="off">`;
  return `<fieldset class="hd-card" data-collab-grant><legend>Give an agent access</legend>${picker}`
    + `${permissionSelect('permission', form.permission, state)}${daysInput('days', form.days)}`
    + `<button type="button" class="hd-btn" data-collab-action="grant"${disabledAttr(state.busy)}>Grant access</button></fieldset>`;
}

export function renderInviteForm(state) {
  const form = state.form || {};
  const room = Array.isArray(state.rooms)
    ? `<select class="hd-input" data-collab-field="room" aria-label="Room"><option value="">Select a room</option>${state.rooms.map(r => `<option value="${esc(r.room_id ?? '')}"${form.room === r.room_id ? ' selected' : ''}>${show(r.name || r.room_id)}</option>`).join('')}</select>`
    : `<input class="hd-input" type="text" data-collab-field="room" value="${esc(form.room ?? '')}" placeholder="Room id" aria-label="Room id" autocomplete="off">`;
  return `<fieldset class="hd-card" data-collab-invite-form><legend>Invite a remote agent</legend>`
    + `<p class="hd-note">An invite is bound to one room. The remote agent redeems the code once.</p>${room}`
    + `<input class="hd-input" type="text" data-collab-field="label" maxlength="40" value="${esc(form.label ?? '')}" placeholder="Label" aria-label="Label" autocomplete="off">`
    + `${permissionSelect('invite_permission', form.permission, state)}${daysInput('invite_days', form.days)}`
    + `<button type="button" class="hd-btn" data-collab-action="invite"${disabledAttr(state.busy)}>Create invite</button></fieldset>`;
}

// The input is empty here: the mount sets its value so the code never enters an HTML string.
export function renderInviteResult(invite) {
  return `<div role="dialog" aria-label="New invite code" data-collab-invite class="hd-card"><p><strong>Invite created.</strong> This code is shown only once: copy it now and give it to the remote agent.`
    + `${invite?.expires_at ? ` It expires <time>${esc(formatWhen(invite.expires_at))}</time>.` : ''}</p>`
    + `<input class="hd-input" type="text" readonly data-invite-code aria-label="Invite code" style="width:100%;box-sizing:border-box">`
    + `<button type="button" class="hd-btn" data-collab-action="close-invite">Close</button></div>`;
}

export function renderRestrictAll(state = {}) {
  const offered = state.offered ? ' data-restrict-offered' : '';
  const note = state.offered ? '<p role="alert">The retraction could not revoke every collaborator write grant. Restrict all access to remove them.</p>' : '';
  if (!state.confirming) {
    return `<section class="hd-card"${offered}>${note}<button type="button" class="hd-btn${state.offered ? ' hd-btn--danger' : ''}" data-collab-action="restrict"${disabledAttr(state.busy)}>Restrict all access</button></section>`;
  }
  return `<section class="hd-card"${offered}>${note}<p><strong>Revoke every collaborator grant and pending invite for this artifact?</strong></p>`
    + `<button type="button" class="hd-btn hd-btn--danger" data-collab-action="restrict-confirm"${disabledAttr(state.busy)}>Yes, restrict all access</button> `
    + `<button type="button" class="hd-btn" data-collab-action="restrict-cancel"${disabledAttr(state.busy)}>Cancel</button></section>`;
}

export function renderCollaboratorsPanel(state) {
  const status = state.error ? `<p role="alert">${esc(state.error)}</p>` : state.notice ? `<p>${esc(state.notice)}</p>` : '';
  const retry = state.error ? `<button type="button" class="hd-btn" data-collab-action="retry"${disabledAttr(state.busy)}>Retry</button>` : '';
  if (state.status === 'disabled') return `<section data-collab-panel><p role="status">${NOT_ENABLED}.</p></section>`;
  if (state.status === 'loading') return '<section data-collab-panel><p role="status">Loading collaborators…</p></section>';
  if (state.status === 'error') return `<section data-collab-panel>${status}${retry}</section>`;
  const common = { busy: state.busy, writable: state.writable, writableReason: state.writableReason };
  return `<section data-collab-panel><div role="status">${status}</div>${retry}`
    + `<h3>Collaborators</h3>${renderCollaboratorList(state)}`
    + renderGrantForm({ ...common, agents: state.agents, form: state.grantForm })
    + (state.invite ? renderInviteResult(state.invite) : '')
    + renderInviteForm({ ...common, rooms: state.rooms, form: state.inviteForm })
    + renderRestrictAll({ confirming: state.restrictConfirm, offered: state.restrictOffered, busy: state.busy })
    + '</section>';
}

// ------------------------------------------------------------------------------------------------ controller

const dayCount = value => {
  const n = Number(value);
  return Number.isInteger(n) && n >= MIN_DAYS && n <= MAX_DAYS ? n : null;
};
const failure = error => String(error?.message || 'The request could not be completed.');

export function createCollaboratorsController(client, collabApi, artifactId, { signal, onChange = () => {} } = {}) {
  const aborter = new AbortController();
  const state = {
    status: 'loading', error: '', notice: '', busy: false, writable: false, writableReason: null,
    collaborators: [], pending: [], agents: null, rooms: null,
    grantForm: { agent_id: '', permission: 'read', days: DEFAULT_DAYS },
    inviteForm: { room: '', label: '', permission: 'read', days: DEFAULT_DAYS },
    invite: null, restrictConfirm: false, restrictOffered: false,
  };
  const ctrl = { state, disposed: false };
  const changed = () => { if (!ctrl.disposed) onChange(); };
  const call = { signal: aborter.signal };

  const loadHelpers = async () => {
    const [agents, rooms] = await Promise.allSettled([collabApi.agents(), collabApi.rooms()]);
    state.agents = agents.status === 'fulfilled' && Array.isArray(agents.value?.agents) ? agents.value.agents : null;
    state.rooms = rooms.status === 'fulfilled' && Array.isArray(rooms.value?.rooms) ? rooms.value.rooms : null;
  };
  const read = async () => {
    const data = await client.listArtifactCollaborators(artifactId, call);
    if (ctrl.disposed) return;
    state.status = 'ready'; state.error = '';
    state.writable = data?.writable === true; state.writableReason = data?.writable_reason ?? null;
    state.collaborators = Array.isArray(data?.collaborators) ? data.collaborators : [];
    state.pending = Array.isArray(data?.pending_invites) ? data.pending_invites : [];
  };
  const loadFailed = error => {
    if (ctrl.disposed || aborter.signal.aborted) return;
    if (error?.status === 503) { state.status = 'disabled'; state.error = ''; return; }
    if (state.status === 'loading') state.status = 'error';
    state.error = failure(error);
  };

  ctrl.refresh = async () => {
    if (ctrl.disposed) return;
    try { await read(); if (state.agents === null && state.rooms === null) { await loadHelpers(); } }
    catch (error) { loadFailed(error); }
    changed();
  };

  // One call at a time: every action button is disabled while busy, and a stray second click is ignored here.
  const act = async (notice, fn) => {
    if (ctrl.disposed || state.busy || state.status !== 'ready') return;
    state.busy = true; state.error = ''; state.notice = ''; changed();
    try {
      await fn();
      if (ctrl.disposed) return;
      state.notice = notice;
      await read().catch(loadFailed);
    } catch (error) {
      if (!ctrl.disposed) state.error = failure(error);
    } finally {
      state.busy = false;
      changed();
    }
  };

  ctrl.setField = (name, value) => {
    const group = { agent_id: 'grantForm', permission: 'grantForm', days: 'grantForm', room: 'inviteForm', label: 'inviteForm', invite_permission: 'inviteForm', invite_days: 'inviteForm' }[name];
    if (!group) return;
    const key = name.replace(/^invite_/, '');
    state[group][key] = value;
  };

  const checkedPermission = form => {
    if (form.permission === 'write' && !state.writable) return `Read+write is unavailable: ${state.writableReason || 'this artifact cannot take collaborator commits'}`;
    if (dayCount(form.days) === null) return `Days must be a whole number from ${MIN_DAYS} to ${MAX_DAYS}.`;
    return '';
  };
  const refuse = message => { state.error = message; state.notice = ''; changed(); };

  ctrl.grant = async () => {
    const form = state.grantForm;
    const bad = !String(form.agent_id).trim() ? 'Choose an agent.' : checkedPermission(form);
    if (bad) return refuse(bad);
    await act('Access granted.', () => client.grantArtifactCollaborator(artifactId,
      { agent_id: String(form.agent_id).trim(), permission: form.permission, expires_in_days: dayCount(form.days) }, call));
  };
  ctrl.revoke = agentId => act('Access revoked.', () => client.revokeArtifactCollaborator(artifactId, agentId, call));

  ctrl.invite = async () => {
    const form = state.inviteForm;
    const bad = !String(form.room).trim() ? 'Choose a room.' : checkedPermission(form);
    if (bad) return refuse(bad);
    await act('Invite created.', async () => {
      const made = await collabApi.inviteExternal(form.room, { label: form.label || 'artifact collaborator', actions: ['read'], ttl_seconds: INVITE_TTL_SECONDS });
      if (ctrl.disposed) return;
      // The code is shown even if the grant below fails: it was created and cannot be fetched again.
      state.invite = { code: made.code, expires_at: made.expires_at };
      try {
        await client.grantArtifactInvite(artifactId, { invite_id: made.invite_id, permission: form.permission, expires_in_days: dayCount(form.days) }, call);
      } catch (error) {
        throw new Error(`The invite exists but the artifact grant failed: ${failure(error)} Revoke the invite in its room.`);
      }
    });
  };
  ctrl.closeInvite = () => { state.invite = null; changed(); };

  ctrl.offerRestrictAll = () => { state.restrictOffered = true; changed(); };
  ctrl.askRestrict = () => { if (state.busy || state.status !== 'ready') return; state.restrictConfirm = true; changed(); };
  ctrl.cancelRestrict = () => { state.restrictConfirm = false; changed(); };
  ctrl.confirmRestrict = async () => {
    if (!state.restrictConfirm || state.busy) return;
    state.restrictConfirm = false;                       // one confirm authorises one call
    let counts = null;
    await act('', async () => { counts = await client.restrictArtifactCollab(artifactId, call); });
    if (counts && !ctrl.disposed) {
      state.restrictOffered = false;
      state.notice = `Restricted: ${counts.revoked ?? 0} grants revoked, ${counts.pending_invites_revoked ?? 0} pending invites revoked.`;
      changed();
    }
  };

  ctrl.dispose = () => {
    if (ctrl.disposed) return;
    ctrl.disposed = true; state.invite = null; aborter.abort();
    signal?.removeEventListener?.('abort', ctrl.dispose);
  };
  if (signal?.aborted) ctrl.dispose(); else signal?.addEventListener?.('abort', ctrl.dispose, { once: true });
  return ctrl;
}

// ------------------------------------------------------------------------------------------------ mount

export function mountCollaborators(container, client, collabApi, artifactId, { signal } = {}) {
  const render = () => {
    if (ctrl.disposed || !container.isConnected) return;
    container.innerHTML = renderCollaboratorsPanel(ctrl.state);
    if (ctrl.state.invite) {
      const input = container.querySelector('[data-invite-code]');
      if (input) input.value = ctrl.state.invite.code;
    }
  };
  const ctrl = createCollaboratorsController(client, collabApi, artifactId, { signal, onChange: render });

  const onClick = event => {
    const button = event.target.closest?.('[data-collab-action]');
    if (!button || button.disabled) return;
    switch (button.dataset.collabAction) {
      case 'grant': void ctrl.grant(); break;
      case 'revoke': void ctrl.revoke(button.dataset.agentId); break;
      case 'invite': void ctrl.invite(); break;
      case 'close-invite': ctrl.closeInvite(); break;
      case 'restrict': ctrl.askRestrict(); break;
      case 'restrict-cancel': ctrl.cancelRestrict(); break;
      case 'restrict-confirm': void ctrl.confirmRestrict(); break;
      case 'retry': void ctrl.refresh(); break;
      default: break;
    }
  };
  // Typing only updates state: re-rendering here would drop focus; rendered values come from the same state.
  const onField = event => {
    const field = event.target.closest?.('[data-collab-field]');
    if (field) ctrl.setField(field.dataset.collabField, field.value);
  };
  container.addEventListener('click', onClick);
  container.addEventListener('input', onField);
  container.addEventListener('change', onField);

  const dispose = () => {
    ctrl.dispose();
    container.removeEventListener('click', onClick);
    container.removeEventListener('input', onField);
    container.removeEventListener('change', onField);
    container.replaceChildren();                         // removing the panel removes the one-time code with it
  };
  if (signal?.aborted) { dispose(); return { dispose, controller: ctrl, offerRestrictAll: ctrl.offerRestrictAll }; }
  signal?.addEventListener?.('abort', dispose, { once: true });
  render();
  void ctrl.refresh();
  return { dispose, controller: ctrl, offerRestrictAll: ctrl.offerRestrictAll };
}

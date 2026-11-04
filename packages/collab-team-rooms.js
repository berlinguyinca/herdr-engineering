// Team-room screens for the collaboration page (unified workspace, slices 1-3 of docs/specs/herdr-unified-workspace-20261028/
// design/TEAM_COLLABORATION.md): attach a team, the guest level, the Suggest queue, role holders, coordinators, and a person's
// own tasks. Three views:
//   owner    the room owner's controls (teams, guest level, queue, roles, coordinators)
//   mine     any person's tasks assigned to them (accept / decline / done, with the overdue state)
//   requests a guest's own requests (file, withdraw)
//
// Plain functions returning escaped HTML, and a controller that talks to the server through the real CollabApi, so the whole
// thing runs under node. Nothing here touches `window` at load; mountTeamRoom (bottom) wires the DOM.
//
// The rules the screens keep:
//  * every flag is a separate server switch. A 404 on a route the caller may use means "not turned on" and is shown as a plain
//    sentence, never as an error. Nothing is requested until the person opens a section (so a server with the flags off sees
//    no traffic from a room nobody opened this in).
//  * a guest's words and a task's text are hostile data: escaped, in a labelled quote block, never rendered as Markdown or links,
//    and never copied into a confirm dialog.
//  * approve and reject send the digest of the text the owner was shown (the row rendered), not a fresh one.
//  * owner controls exist only in the owner view; the guest and member views carry no owner hooks.
import { escapeHtml as esc } from './herdr-web-components/util.js';
import { AuthError, randomString } from './herdr-web-client/collab.js';

export const TEAM_ID = /^team-[0-9a-f]{20}$/;
export const TEAM_ROLES = [['issue_writer', 'Issue writer'], ['implementer', 'Implementer'], ['tester', 'Tester'], ['reviewer', 'Reviewer']];
export const GUEST_LEVELS = [
  ['read_only', 'Read only', 'Guests can read the room and nothing else.'],
  ['suggest', 'Suggest', 'A guest can file a request. It waits for you; nothing runs until you approve it.'],
  ['direct', 'Direct (acts as Suggest for now)', 'Stored and checked, but not built yet: until it is, a guest still cannot assign work to anything.'],
];
export const POLICIES = [
  ['escalate', 'Escalate', 'Nobody is chosen for them: the task is marked blocked and you are told.'],
  ['reassign', 'Reassign once', 'The task moves to another eligible holder once, with a new deadline, then escalates.'],
  ['wait', 'Wait', 'The deadline is extended a few times, never past the hard limit, then the task escalates.'],
];
const POLICY_SENTENCE = {
  escalate: 'If nobody answers in time, the room owner is told and the task is blocked.',
  reassign: 'If nobody answers in time, the task moves to another holder once, then the room owner is told.',
  wait: 'If nobody answers in time, the deadline is extended a few times, then the room owner is told.',
};
const SUGGESTION_STATE = {
  pending: ['Waiting for the room owner', 'var(--status-attention)'], approved: ['Approved', 'var(--status-active)'],
  materialized: ['Approved, task created', 'var(--status-ok)'], rejected: ['Not approved', 'var(--text-muted)'],
  withdrawn: ['Withdrawn', 'var(--text-muted)'], expired: ['Expired', 'var(--text-muted)'],
};
const MAX_TEXT = 8000;
const EXTERNAL_LIMITS = [
  'They can only create tasks for the roles you tick here, for this team only, and only under a task you give them.',
  'They are held to the hourly and open-task caps below (always lower than your own agents’ caps).',
  'They see the status of what they created, not the results, the task list, or other people’s tasks.',
  'They can never approve an outside action, grant a role or permission, change the guest level, change the coordinator or this opt-in, or attach or detach a team.',
  'Turning this off, or removing them from the room, ends it at once and puts their open work back with you.',
];

const str = (v, d = '') => (typeof v === 'string' ? v : d);
const list = (v) => (Array.isArray(v) ? v : []);
const short = (id) => (str(id).length > 12 ? `…${str(id).slice(-8)}` : str(id));
const pill = (label, color) => `<span class="hd-status" style="--sc:${esc(color)}"><span class="hd-status__dot" aria-hidden="true">●</span><span class="hd-status__label">${esc(label)}</span></span>`;

export function when(iso) {
  const s = str(iso);
  if (!s) return 'unknown';
  return `${s.replace('T', ' ').slice(0, 16)}${/Z$|[+-]00:?00$/.test(s) ? ' UTC' : ''}`;
}

// ------------------------------------------------------------------ loading

/** Maps a failed call to one phase. 404 on a route this caller may use = the feature is off. */
export function phaseOf(err) {
  if (err instanceof AuthError) return 'signin';
  const status = err && err.status;
  if (status === 404) return 'off';
  if (status === 403) return 'forbidden';
  if (status === 503) return 'unavailable';
  return 'error';
}
const settle = async (fn) => {
  try { return { status: 'ok', data: await fn() }; } catch (e) { return { status: phaseOf(e), message: str(e && e.message, 'request failed') }; }
};
const fallback = (r, key) => (r.status === 'ok' ? list(r.data && r.data[key]) : []);

/** Everything the owner view shows. Teams decide the first flag; roles and coordinators only load when the roles flag answers. */
export async function loadOwner(api, roomId) {
  const teams = await settle(() => api.roomTeams(roomId));
  if (teams.status !== 'ok') return { status: teams.status, message: teams.message };
  const [level, queue, visibility, room, mine, inRoom] = await Promise.all([
    settle(() => api.guestLevel(roomId)), settle(() => api.suggestions(roomId)), settle(() => api.memberVisibility(roomId)),
    settle(() => api.room(roomId)), settle(() => api.agents()), settle(() => api.roomAgents(roomId))]);
  const rolesOn = visibility.status === 'ok';
  const rows = await Promise.all(list(teams.data.teams).map(async (team) => {
    const [roles, coord] = rolesOn
      ? await Promise.all([settle(() => api.teamRoles(roomId, team.team_id)), settle(() => api.teamCoordinator(roomId, team.team_id))])
      : [{ status: visibility.status }, { status: visibility.status }];
    return { ...team, roles, coord };
  }));
  return {
    status: 'ok', teams: rows, level, queue, visibility,
    members: fallback(room, 'members'), myAgents: fallback(mine, 'agents').filter((a) => a && a.status === 'active' && !a.external), roomAgents: fallback(inRoom, 'agents'),
  };
}
export async function loadMine(api, roomId) {
  const r = await settle(() => api.myTasks(roomId));
  return r.status === 'ok' ? { status: 'ok', tasks: list(r.data.tasks) } : { status: r.status, message: r.message };
}
export async function loadRequests(api, roomId) {
  const level = await settle(() => api.guestLevel(roomId));
  if (level.status !== 'ok') return { status: level.status, message: level.message };
  const rows = await settle(() => api.suggestions(roomId));
  if (rows.status !== 'ok') return { status: rows.status, message: rows.message };
  return { status: 'ok', level: level.data, requests: list(rows.data.suggestions) };
}

// ------------------------------------------------------------------ pure helpers the tests pin

/** waiting | overdue | accepted | escalated for one of a person's tasks. `nowMs` is injected so the state is testable. */
export function taskState(task, nowMs) {
  const status = str(task && task.status);
  if (status === 'blocked') return 'escalated';
  if (status === 'accepted') return 'accepted';
  const due = Date.parse(str(task && task.lease && task.lease.expires_at));
  return Number.isFinite(due) && due <= nowMs ? 'overdue' : 'waiting';
}
export const memberName = (members, id) => {
  const m = list(members).find((x) => x && x.user_id === id);
  return m ? str(m.email) || short(id) : short(id);
};
const holderName = (model, h) => (h.type === 'agent' ? `@${str(h.name) || short(h.holder)}` : memberName(model.members, str(h.holder).replace(/^user:/, '')));

// ------------------------------------------------------------------ rendering

const live = (n) => `<div class="cx-msg" role="status" aria-live="polite" data-trr-live>${n && n.kind !== 'error' ? esc(n.text) : ''}</div>`
  + `<div class="cx-msg" role="alert" data-trr-alert>${n && n.kind === 'error' ? esc(n.text) : ''}</div>`;

const OFF_TEXT = {
  owner: 'Team rooms are not turned on for this control plane. Nothing is wrong with your account or this room.',
  mine: 'Tasks for people are not turned on for this control plane. Nothing is wrong with your account or this room.',
  requests: 'Requests are not turned on for this room. Nothing is wrong with your account.',
  roles: 'Roles are not turned on for this control plane.',
  coord: 'Coordinators are not turned on for this control plane.',
};
const SIDE_TEXT = {
  forbidden: 'You do not have access to this.', unavailable: 'This is not available right now. Try again shortly.',
  signin: 'Your session expired. Sign in again.',
};
function stateLine(kind, st, retry) {
  if (st.status === 'loading') return `<p class="cx-msg" role="status" data-trr-state="loading">Loading…</p>`;
  if (st.status === 'off') return `<p class="cx-msg" role="status" data-trr-state="off">${esc(OFF_TEXT[kind])}</p>`;
  const text = SIDE_TEXT[st.status] || `This could not be loaded: ${str(st.message, 'request failed')}`;
  return `<p class="cx-msg" role="status" data-trr-state="${esc(st.status)}">${esc(text)}</p>${retry ? `<button type="button" class="hd-btn hd-btn--ghost hd-btn--sm" data-trr-act="reload" data-trr-view="${esc(retry)}">Try again</button>` : ''}`;
}

const quote = (label, text) => `<blockquote class="trr-quote" data-untrusted="true"><p class="cx-msg">${esc(label)}</p><div class="trr-text">${esc(text)}</div></blockquote>`;

function agentChoices(model) {
  const roleOf = new Map();
  for (const t of model.teams) for (const a of list(t.agents)) roleOf.set(a.agent_id, TEAM_ROLES.find(([k]) => k === a.role)?.[1] || str(a.role));
  return model.roomAgents.filter((a) => list(a.actions).includes('execute')).map((a) => ({
    id: a.agent_id, label: `@${str(a.name) || short(a.agent_id)}${roleOf.has(a.agent_id) ? ` (${roleOf.get(a.agent_id)})` : ''}`,
  }));
}

function levelSection(model) {
  const st = model.level;
  if (st.status !== 'ok') return `<section class="cx-stack" aria-labelledby="trr-h-level"><h3 class="hd-card__title" id="trr-h-level">Guests</h3>${stateLine('owner', st)}</section>`;
  const cur = str(st.data.level);
  const limits = st.data.direct_limits || {};
  const roomAgents = model.roomAgents.filter((a) => list(a.actions).includes('execute'));
  const radios = GUEST_LEVELS.map(([v, label, help]) => `<label class="cx-check trr-choice"><input type="radio" name="level" value="${v}"${v === cur ? ' checked' : ''}> <span><b>${esc(label)}</b><br><span class="cx-msg">${esc(help)}</span></span></label>`).join('');
  const direct = `<fieldset class="cx-stack trr-direct"><legend class="cx-msg">Direct only: who a guest may address (never “any”)</legend>
    <div class="cx-checks">${roomAgents.map((a) => `<label class="cx-check"><input type="checkbox" name="agents" value="${esc(a.agent_id)}"${list(limits.agents).includes(a.agent_id) ? ' checked' : ''}> @${esc(a.name)}</label>`).join('') || '<span class="cx-msg">No agent in this room can receive tasks.</span>'}</div>
    <div class="cx-checks">${TEAM_ROLES.map(([v, l]) => `<label class="cx-check"><input type="checkbox" name="roles" value="${v}"${list(limits.roles).includes(v) ? ' checked' : ''}> ${esc(l)}</label>`).join('')}</div>
    <div class="cx-row"><label class="cx-field">Per hour<input name="per_hour" type="number" min="1" max="100" value="${esc(limits.per_hour ?? '')}"></label>
      <label class="cx-field">Open at once<input name="max_open" type="number" min="1" max="50" value="${esc(limits.max_open ?? '')}"></label></div></fieldset>`;
  return `<section class="cx-stack" aria-labelledby="trr-h-level"><h3 class="hd-card__title" id="trr-h-level">Guests</h3>
    <p class="cx-msg" data-trr-level="${esc(cur)}">Current level: <b>${esc(GUEST_LEVELS.find(([v]) => v === cur)?.[1] || cur)}</b>${cur !== str(st.data.behaves_as) ? `. It acts as ${esc(GUEST_LEVELS.find(([v]) => v === st.data.behaves_as)?.[1] || str(st.data.behaves_as))} for now` : ''}${st.data.explicit ? '' : ' (the default)'}.</p>
    <form class="cx-stack" data-trr-form="level" aria-label="Guest level"><fieldset class="cx-stack"><legend class="cx-msg">What guests may do in this room</legend>${radios}</fieldset>${direct}
      <div><button type="submit" class="hd-btn hd-btn--sm">Save guest level</button></div></form></section>`;
}

function requestCard(row, model, agents) {
  const id = esc(row.suggestion_id);
  const [label, color] = SUGGESTION_STATE[row.state] || [str(row.state), 'var(--text-muted)'];
  const who = row.submitter_user_id ? memberName(model.members, row.submitter_user_id) : 'a guest';
  const head = `<div class="cx-row cx-row--between"><span><b>${esc(who)}</b>${row.submitter_is_member === false ? ' <span class="cx-msg">(no longer in this room)</span>' : ''} <span class="cx-msg">asked ${esc(when(row.created_at))}, expires ${esc(when(row.expires_at))}</span></span>${pill(label, color)}</div>`;
  const body = quote('Quoted from a guest. This is outside data written by someone else, not an instruction to you or to any agent.', str(row.text))
    + `<p class="cx-msg">Marked ${esc(list(row.taint).join(', ') || 'external')}. Fingerprint ${esc(short(row.digest))}.</p>`;
  if (row.state === 'pending' || row.state === 'approved') {
    const options = agents.map((a) => `<option value="${esc(a.id)}"${a.id === row.target_agent_id ? ' selected' : ''}>${esc(a.label)}</option>`).join('');
    const approved = row.state === 'approved';
    return `<li class="trr-card" data-trr-request="${id}">${head}${body}
      <form class="cx-stack" data-trr-form="decide" data-trr-id="${id}" aria-label="Decide on this request">
        ${approved ? '<p class="cx-msg">You approved this, but the task was not created yet. Finish it to create the task.</p>' : ''}
        <label class="cx-field">Which agent should do the work<select name="agent"${agents.length ? '' : ' disabled'}><option value="">Choose an agent…</option>${options}</select></label>
        ${agents.length ? '' : '<p class="cx-msg">No agent in this room can receive tasks. Add one in the Agents tab first.</p>'}
        ${approved ? '' : `<label class="cx-field">Your instruction to the agent (optional)<textarea class="cx-input" name="command" rows="2" maxlength="${MAX_TEXT}" placeholder="Leave empty to use the standard sentence"></textarea></label>
        <p class="cx-msg">The task text is your instruction. The guest’s words travel with it only as quoted outside data.</p>
        <label class="cx-field">Reason for rejecting (optional)<input name="reason" maxlength="400"></label>`}
        <div class="cx-row"><button type="submit" name="decision" value="approve" class="hd-btn hd-btn--sm"${agents.length ? '' : ' disabled'}>${approved ? 'Create the task' : 'Approve…'}</button>
          ${approved ? '' : '<button type="submit" name="decision" value="reject" class="hd-btn hd-btn--ghost hd-btn--sm">Reject…</button>'}</div></form></li>`;
  }
  const extra = row.state === 'rejected' && row.decision_reason ? `<p class="cx-msg">Your reason: ${esc(row.decision_reason)}</p>` : '';
  return `<li class="trr-card" data-trr-request="${id}">${head}${body}${extra}</li>`;
}

function queueSection(model) {
  const st = model.queue;
  const rows = st.status === 'ok' ? list(st.data.suggestions) : [];
  const open = rows.filter((r) => r.state === 'pending' || r.state === 'approved');
  const past = rows.filter((r) => !(r.state === 'pending' || r.state === 'approved'));
  const agents = agentChoices(model);
  const levelOff = model.level.status === 'ok' && model.level.data.behaves_as === 'read_only';
  return `<section class="cx-stack" aria-labelledby="trr-h-queue"><h3 class="hd-card__title" id="trr-h-queue">Requests from guests${open.length ? ` (${open.length} waiting)` : ''}</h3>
    ${st.status !== 'ok' ? stateLine('owner', st)
    : `${levelOff ? '<p class="cx-msg">This room is read only for guests, so no new requests can come in.</p>' : ''}
    ${open.length ? `<ul class="cx-list trr-cards" aria-label="Requests waiting for you">${open.map((r) => requestCard(r, model, agents)).join('')}</ul>` : '<p class="hd-empty">No request is waiting.</p>'}
    ${past.length ? `<details class="trr-past"><summary>Earlier requests (${past.length})</summary><ul class="cx-list trr-cards" aria-label="Earlier requests">${past.map((r) => requestCard(r, model, agents)).join('')}</ul></details>` : ''}`}</section>`;
}

function rolesBlock(team, model) {
  const st = team.roles;
  if (st.status !== 'ok') return `<div class="cx-stack"><h5 class="cx-msg"><b>Role holders</b></h5>${stateLine('roles', st)}</div>`;
  const d = st.data;
  const roleRows = list(d.roles).map((r) => {
    const holders = list(r.holders).map((h) => `<li><span class="grow">${esc(holderName(model, h))} <span class="cx-msg">${h.type === 'agent' ? 'agent' : 'person'}${h.can_take_tasks ? '' : ', cannot take tasks right now'}</span></span>${h.type === 'person' ? `<button type="button" class="hd-btn hd-btn--ghost hd-btn--sm" data-trr-act="clear-role" data-trr-team="${esc(team.team_id)}" data-trr-role="${esc(r.role)}" data-trr-holder="${esc(h.holder)}" aria-label="Remove ${esc(holderName(model, h))} from ${esc(r.role)}">Remove</button>` : ''}</li>`).join('');
    return `<li><b>${esc(TEAM_ROLES.find(([k]) => k === r.role)?.[1] || r.role)}</b> <span class="cx-msg">${esc(r.kind)}</span>${holders ? `<ul class="cx-list" aria-label="Holders of ${esc(r.role)}">${holders}</ul>` : '<span class="cx-msg"> nobody yet</span>'}</li>`;
  }).join('');
  const people = list(model.members).map((m) => `<option value="${esc(m.user_id)}">${esc(m.email || short(m.user_id))} (${esc(m.role)})</option>`).join('');
  return `<div class="cx-stack"><h5 class="cx-msg"><b>Role holders</b></h5><ul class="cx-list" aria-label="Roles of this team">${roleRows}</ul>
    <form class="cx-row" data-trr-form="assign-role" data-trr-team="${esc(team.team_id)}" aria-label="Put a person in a role">
      <label class="cx-field">Role<select name="role" required>${TEAM_ROLES.map(([v, l]) => `<option value="${v}">${esc(l)}</option>`).join('')}</select></label>
      <label class="cx-field">Person in this room<select name="person" required><option value="">Choose…</option>${people}</select></label>
      <button type="submit" class="hd-btn hd-btn--sm">Put in role</button></form>
    <p class="cx-msg">Agents hold a role by being attached to the team. A person who holds a role is asked for their answer and has a deadline.</p>
    <form class="cx-row" data-trr-form="policy" data-trr-team="${esc(team.team_id)}" aria-label="When nobody answers">
      <label class="cx-field">When nobody answers in time<select name="policy">${POLICIES.map(([v, l]) => `<option value="${v}"${v === d.no_response_policy ? ' selected' : ''}>${esc(l)}</option>`).join('')}</select></label>
      <button type="submit" class="hd-btn hd-btn--ghost hd-btn--sm">Save</button></form>
    <p class="cx-msg" data-trr-policy="${esc(d.no_response_policy)}">${esc(POLICY_SENTENCE[d.no_response_policy] || '')}</p></div>`;
}

function limitsLine(c) {
  const l = c.limits || {};
  const u = c.usage || {};
  return `Up to ${esc(String(l.tasks_per_hour))} tasks an hour (${esc(String(u.created_last_hour ?? 0))} in the last hour) and ${esc(String(l.max_open))} open at once (${esc(String(u.open ?? 0))} now)${list(l.roles).length ? `, for ${esc(list(l.roles).join(', '))}` : ''}.`;
}

function coordBlock(team, model) {
  const st = team.coord;
  if (st.status !== 'ok') return `<div class="cx-stack"><h5 class="cx-msg"><b>Coordinator</b></h5>${stateLine('coord', st)}</div>`;
  const c = st.data;
  const ext = c.external || {};
  const tid = esc(team.team_id);
  const holder = c.holder
    ? `<p data-trr-coordinator="${esc(c.holder)}"><b>${esc(c.holder.startsWith('agent:') ? `@${(model.myAgents.concat(model.roomAgents).find((a) => a.agent_id === c.holder.slice(6)) || {}).name || short(c.holder)}` : memberName(model.members, c.holder.slice(5)))}</b> ${pill(c.kind === 'external' ? 'External person' : 'Your agent', c.kind === 'external' ? 'var(--accent)' : 'var(--status-active)')}${c.active ? '' : ' <span class="cx-msg">(not active right now)</span>'}</p><p class="cx-msg">${limitsLine(c)}</p>
      <div><button type="button" class="hd-btn hd-btn--ghost hd-btn--sm" data-trr-act="coord-clear" data-trr-team="${tid}">Remove the coordinator…</button></div>`
    : '<p class="cx-msg">No coordinator. A coordinator splits a task you gave it into tasks for this team’s roles; it cannot approve outside actions or change anyone’s access.</p>';
  const roomIds = new Set(model.roomAgents.map((a) => a.agent_id));
  const candidates = model.myAgents.filter((a) => !roomIds.has(a.agent_id)).map((a) => `<option value="agent:${esc(a.agent_id)}">@${esc(a.name)} (your agent)</option>`);
  const guests = ext.enabled ? model.members.filter((m) => m.role === 'guest').map((m) => `<option value="user:${esc(m.user_id)}">${esc(m.email || short(m.user_id))} (guest)</option>`) : [];
  const designate = c.holder ? '' : `<form class="cx-row" data-trr-form="coord-set" data-trr-team="${tid}" aria-label="Designate a coordinator">
      <label class="cx-field">Coordinator<select name="holder" required><option value="">Choose…</option>${candidates.length ? `<optgroup label="Your agents not yet in this room">${candidates.join('')}</optgroup>` : ''}${guests.length ? `<optgroup label="Guests (opted in)">${guests.join('')}</optgroup>` : ''}</select></label>
      <button type="submit" class="hd-btn hd-btn--sm">Designate</button></form>${candidates.length || guests.length ? '' : '<p class="cx-msg">No agent of yours outside this room is available, and no guest is opted in.</p>'}`;
  const limitsList = `<ul class="trr-limits">${EXTERNAL_LIMITS.map((t) => `<li>${esc(t)}</li>`).join('')}</ul>`;
  const extBlock = !ext.service_enabled
    ? '<p class="cx-msg" data-trr-ext="service-off">Letting an outside person coordinate is switched off for this whole control plane, so it cannot be turned on here.</p>'
    : ext.enabled
      ? `<p data-trr-ext="on"><b>On.</b> A guest of this room can coordinate this team${ext.limits ? ` for ${esc(list(ext.limits.roles).join(', '))}` : ''}.</p>${limitsList}
        <div><button type="button" class="hd-btn hd-btn--ghost hd-btn--sm" data-trr-act="ext-off" data-trr-team="${tid}">Turn off…</button></div>`
      : `<details data-trr-ext="off"><summary>Let a guest coordinate this team (off by default)</summary>
          <form class="cx-stack" data-trr-form="ext-on" data-trr-team="${tid}" aria-label="Let a guest coordinate this team">
            <p class="cx-msg"><b>Read the limits first.</b></p>${limitsList}
            <fieldset class="cx-stack"><legend class="cx-msg">The roles they may assign to (tick at least one)</legend><div class="cx-checks">${TEAM_ROLES.map(([v, l]) => `<label class="cx-check"><input type="checkbox" name="roles" value="${v}"> ${esc(l)}</label>`).join('')}</div></fieldset>
            <div class="cx-row"><label class="cx-field">Tasks per hour (optional, lower)<input name="per_hour" type="number" min="1" max="200"></label><label class="cx-field">Open at once (optional, lower)<input name="max_open" type="number" min="1" max="100"></label></div>
            <div><button type="submit" class="hd-btn hd-btn--sm">Allow…</button></div></form></details>`;
  return `<div class="cx-stack"><h5 class="cx-msg"><b>Coordinator</b></h5>${holder}${designate}<h5 class="cx-msg"><b>Outside coordination for this team</b></h5>${extBlock}</div>`;
}

function teamsSection(model) {
  const cards = model.teams.map((t) => {
    const agents = list(t.agents).map((a) => `<li><span class="grow">@${esc(str(a.name) || short(a.agent_id))} <span class="cx-msg">${esc(TEAM_ROLES.find(([k]) => k === a.role)?.[1] || str(a.role))}${a.in_room ? '' : ', no longer in the room'}</span></span></li>`).join('');
    return `<li class="trr-card" data-trr-team-card="${esc(t.team_id)}"><div class="cx-row cx-row--between"><b>Team <span class="hd-ident">${esc(short(t.team_id))}</span></b>
        <button type="button" class="hd-btn hd-btn--danger hd-btn--sm" data-trr-act="detach" data-trr-team="${esc(t.team_id)}" aria-label="Detach team ${esc(short(t.team_id))}">Detach…</button></div>
      ${t.verified === false ? '<p class="cx-msg">The team id was only checked for its shape, not against the work graph.</p>' : ''}
      <ul class="cx-list" aria-label="Agents of this team">${agents || '<li class="cx-msg">No agents.</li>'}</ul>
      ${rolesBlock(t, model)}${coordBlock(t, model)}</li>`;
  }).join('');
  const mine = model.myAgents.filter((a) => !model.roomAgents.some((r) => r.agent_id === a.agent_id));   // one that already has access here is refused (remove it first)
  const picker = mine.map((a) => `<li class="cx-row"><span class="grow">@${esc(a.name)}</span><label class="cx-field">Role<select name="agent:${esc(a.agent_id)}" aria-label="Role of @${esc(a.name)}"><option value="">Not in this team</option>${TEAM_ROLES.map(([v, l]) => `<option value="${v}">${esc(l)}</option>`).join('')}</select></label></li>`).join('');
  return `<section class="cx-stack" aria-labelledby="trr-h-teams"><h3 class="hd-card__title" id="trr-h-teams">Teams in this room</h3>
    ${model.teams.length ? `<ul class="cx-list trr-cards" aria-label="Attached teams">${cards}</ul>` : '<p class="hd-empty">No team is attached to this room.</p>'}
    <details><summary>Attach a team</summary><form class="cx-stack" data-trr-form="attach" aria-label="Attach a team">
      <label class="cx-field">Team id (from <code>herdr-eng teams list</code>)<input name="team" required pattern="team-[0-9a-f]{20}" placeholder="team-" autocomplete="off" spellcheck="false"></label>
      <p class="cx-msg">Name which of your own agents does which role. Only your own active agents can be attached; they join the room with the role’s fixed rights. Attaching a team does not make anyone in the room a member of it.</p>
      ${mine.length ? `<ul class="cx-list" aria-label="Your agents">${picker}</ul>` : '<p class="cx-msg">You have no agents to attach.</p>'}
      <div><button type="submit" class="hd-btn hd-btn--sm">Attach team</button></div></form></details></section>`;
}

function visibilitySection(model) {
  const st = model.visibility;
  if (st.status !== 'ok') return `<section class="cx-stack" aria-labelledby="trr-h-vis"><h3 class="hd-card__title" id="trr-h-vis">Who sees the task list</h3>${stateLine('roles', st)}</section>`;
  return `<section class="cx-stack" aria-labelledby="trr-h-vis"><h3 class="hd-card__title" id="trr-h-vis">Who sees the task list</h3>
    <form class="cx-stack" data-trr-form="visibility" aria-label="Member visibility"><label class="cx-check"><input type="checkbox" name="enabled"${st.data.enabled ? ' checked' : ''}> Members can see the room’s tasks for agents</label>
      <p class="cx-msg">Off by default. Guests never see the list, and a task for a person stays visible only to that person, you, and whoever created it.</p>
      <div><button type="submit" class="hd-btn hd-btn--ghost hd-btn--sm">Save</button></div></form></section>`;
}

/** The owner's whole view. `model` comes from loadOwner (or {status:'loading'}). */
export function renderOwner(model, notice = null) {
  if (!model || model.status !== 'ok') return `${live(notice)}${stateLine('owner', model || { status: 'loading' }, 'owner')}`;
  return `${live(notice)}<div class="cx-stack" data-trr-owner>${queueSection(model)}${levelSection(model)}${visibilitySection(model)}${teamsSection(model)}</div>`;
}

function taskCard(t, nowMs) {
  const state = taskState(t, nowMs);
  const id = esc(t.task_id);
  const pillFor = { waiting: ['Waiting for your answer', 'var(--status-attention)'], overdue: ['Overdue', 'var(--status-failed)'], accepted: ['You accepted it', 'var(--status-active)'], escalated: ['Escalated', 'var(--status-blocked)'] }[state];
  const lease = t.lease || {};
  const timing = state === 'accepted' ? `Finish by ${esc(when(t.hard_expiry))}.`
    : state === 'escalated' ? 'The time to answer ran out and the room owner has been told. You can no longer answer it here.'
      : `${state === 'overdue' ? 'Your answer was due' : 'Answer by'} ${esc(when(lease.expires_at))}. ${esc(POLICY_SENTENCE[str(t.no_response_policy)] || '')}`;
  const buttons = state === 'waiting' || state === 'overdue'
    ? `<button type="button" class="hd-btn hd-btn--sm" data-trr-act="respond" data-trr-id="${id}" data-trr-status="accepted">Accept</button>
       <button type="button" class="hd-btn hd-btn--ghost hd-btn--sm" data-trr-act="respond" data-trr-id="${id}" data-trr-status="declined">Decline…</button>`
    : state === 'accepted'
      ? `<button type="submit" class="hd-btn hd-btn--sm">Mark done</button>
         <button type="button" class="hd-btn hd-btn--ghost hd-btn--sm" data-trr-act="respond" data-trr-id="${id}" data-trr-status="declined">Decline…</button>` : '';
  const body = `<div class="cx-row cx-row--between"><span><b>${t.role ? esc(TEAM_ROLES.find(([k]) => k === t.role)?.[1] || t.role) : 'Task for you'}</b> <span class="cx-msg">given ${esc(when(t.created_at))}</span></span>${pill(...pillFor)}</div>
    ${quote('Task text. Plain text from the person who gave you this task; it is never run, opened or turned into a link.', str(t.text))}<p class="cx-msg" data-trr-timing>${timing}</p>`;
  if (state === 'accepted') {
    return `<li class="trr-card" data-trr-task="${id}" data-trr-task-state="${state}">${body}<form class="cx-stack" data-trr-form="done" data-trr-id="${id}" aria-label="Finish this task"><label class="cx-field">Note for the room (optional)<textarea class="cx-input" name="result" rows="2" maxlength="${MAX_TEXT}"></textarea></label><div class="cx-row">${buttons}</div></form></li>`;
  }
  return `<li class="trr-card" data-trr-task="${id}" data-trr-task-state="${state}">${body}${buttons ? `<div class="cx-row">${buttons}</div>` : ''}</li>`;
}

/** A person's own tasks. Anyone with a task sees this; it carries no owner controls. */
export function renderMine(model, notice = null, nowMs = Date.now()) {
  if (!model || model.status !== 'ok') return `${live(notice)}${stateLine('mine', model || { status: 'loading' }, 'mine')}`;
  const overdue = model.tasks.filter((t) => taskState(t, nowMs) === 'overdue').length;
  return `${live(notice)}${overdue ? `<p class="cx-msg" data-trr-overdue>${overdue} of your tasks ${overdue === 1 ? 'is' : 'are'} overdue. Answer now, or it will be escalated.</p>` : ''}
    ${model.tasks.length ? `<ul class="cx-list trr-cards" aria-label="Your tasks">${model.tasks.map((t) => taskCard(t, nowMs)).join('')}</ul>` : '<p class="hd-empty">Nothing is assigned to you in this room.</p>'}`;
}

/** A guest's own requests, and the form to file one. Never shows the owner's reason, command, target or task. */
export function renderRequests(model, notice = null) {
  if (!model || model.status !== 'ok') return `${live(notice)}${stateLine('requests', model || { status: 'loading' }, 'requests')}`;
  const readOnly = model.level.behaves_as === 'read_only';
  const form = readOnly
    ? '<p class="cx-msg" data-trr-readonly>The room owner has made this room read only for guests, so you cannot file requests here.</p>'
    : `<form class="cx-stack" data-trr-form="suggest" aria-label="File a request"><label class="cx-field">What would you like the team to look at?<textarea class="cx-input" name="text" required rows="3" maxlength="${MAX_TEXT}"></textarea></label>
        <p class="cx-msg">Your request waits for the room owner. It does not run anything by itself, and it is shown to them as your words, not as an instruction.</p>
        <div><button type="submit" class="hd-btn hd-btn--sm">File request</button></div></form>`;
  const rows = model.requests.map((r) => {
    const [label, color] = SUGGESTION_STATE[r.state] || [str(r.state), 'var(--text-muted)'];
    return `<li class="trr-card" data-trr-request="${esc(r.suggestion_id)}"><div class="cx-row cx-row--between"><span class="cx-msg">Filed ${esc(when(r.created_at))}${r.state === 'pending' ? `, expires ${esc(when(r.expires_at))}` : ''}</span>${pill(label, color)}</div>
      ${quote('Your request, as the owner sees it.', str(r.text))}
      ${r.state === 'pending' ? `<div><button type="button" class="hd-btn hd-btn--ghost hd-btn--sm" data-trr-act="withdraw" data-trr-id="${esc(r.suggestion_id)}">Withdraw…</button></div>` : ''}</li>`;
  }).join('');
  return `${live(notice)}${form}${model.requests.length ? `<ul class="cx-list trr-cards" aria-label="Your requests">${rows}</ul>` : '<p class="hd-empty">You have not filed a request yet.</p>'}`;
}

// ------------------------------------------------------------------ actions

/** Turns a submitted form (anything with get/getAll) into an action; null when it is not valid yet. */
export function formAction(name, form, data = {}) {
  const get = (k) => str(form.get(k)).trim();
  switch (name) {
    case 'attach': {
      const roles = {};
      for (const key of form.keys ? [...new Set(form.keys())] : []) {
        const v = get(key);
        if (key.startsWith('agent:') && v) (roles[v] ||= []).push(key.slice(6));
      }
      return { type: 'attach', team: get('team'), roles };
    }
    case 'level': {
      const level = get('level');
      return { type: 'level', level, ...(level === 'direct' ? { limits: directLimits(form) } : {}) };
    }
    case 'decide': return { type: get('decision') === 'reject' ? 'reject' : 'approve', id: data.id, agent: get('agent'), command: get('command'), reason: get('reason') };
    case 'assign-role': return { type: 'assign-role', team: data.team, role: get('role'), person: get('person') };
    case 'policy': return { type: 'policy', team: data.team, policy: get('policy') };
    case 'visibility': return { type: 'visibility', enabled: form.get('enabled') !== null };
    case 'coord-set': return { type: 'coord-set', team: data.team, holder: get('holder') };
    case 'ext-on': return { type: 'ext-on', team: data.team, roles: form.getAll('roles').map(str), per_hour: get('per_hour'), max_open: get('max_open') };
    case 'done': return { type: 'respond', id: data.id, status: 'done', result: get('result') };
    case 'suggest': return { type: 'suggest', text: str(form.get('text')).trim() };
    default: return null;
  }
}
function directLimits(form) {
  const out = {};
  const agents = form.getAll('agents').map(str);
  const roles = form.getAll('roles').map(str);
  if (agents.length) out.agents = agents;
  if (roles.length) out.roles = roles;
  for (const k of ['per_hour', 'max_open']) { const n = Number(str(form.get(k)).trim()); if (str(form.get(k)).trim() && Number.isInteger(n)) out[k] = n; }
  return out;
}

const SIGNED_OUT = 'Your session expired. Sign in again.';
function explain(e) {
  if (e instanceof AuthError) return SIGNED_OUT;
  const s = e && e.status;
  if (e && e.code === 'digest_mismatch') return 'This request changed after you opened it. The list was reloaded. Read it again before you decide.';
  if (e && e.code === 'guest_level_read_only') return 'The room owner has made this room read only for guests, so requests are not accepted.';
  if (s === 404) return 'That is not available any more: the feature may have been turned off, or it no longer exists.';
  if (s === 429) return 'Too many changes just now. Wait a minute and try again.';
  return str(e && e.message, 'The request failed.');
}

/** Confirm texts. They never contain a guest’s or a task’s words. */
export const CONFIRM = {
  approve: (agent) => `Approve this request and create a task for ${agent}? The task is made in your name. The guest’s words are attached as outside data, never as the instruction.`,
  reject: () => 'Reject this request? The guest is told it was not approved. This cannot be undone.',
  detach: () => 'Detach this team from the room? Its agents lose their access to this room and the people in its roles are taken out of them.',
  'ext-on': (roles) => `Let a guest of this room coordinate this team for ${roles.join(', ')}? They can create tasks for those roles only, within the caps, and see status only. They can never approve outside actions, change access, or change this setting. You can turn it off at any time.`,
  'ext-off': () => 'Turn off outside coordination for this team? The coordinator is removed at once and their open work comes back to you.',
  'coord-clear': () => 'Remove this coordinator? Their open tasks are cancelled, or put back with you if they are an outside person.',
  decline: () => 'Decline this task? A declined task is not offered to anyone else. This cannot be undone.',
  withdraw: () => 'Withdraw this request? The room owner will not see it any more.',
};

/**
 * One controller per open room. `open(view)` loads a view the first time; `dispatch(action)` runs one action (asking `confirm`
 * first where the screen says so) and reloads what it changed. `onChange` fires after every state change so the DOM can repaint.
 */
export function createTeamRoom({ api, roomId, now = Date.now, confirm = () => true, onChange = () => {} }) {
  const state = { owner: { status: 'loading' }, mine: { status: 'loading' }, requests: { status: 'loading' } };
  const notice = { owner: null, mine: null, requests: null };
  const loaded = { owner: 0, mine: 0, requests: 0 };
  const loaders = { owner: loadOwner, mine: loadMine, requests: loadRequests };
  let suggestKey = null;
  const say = (view, kind, text) => { notice[view] = text ? { kind, text } : null; onChange(view); };

  async function load(view) {
    state[view] = await loaders[view](api, roomId);
    loaded[view] = now();
    onChange(view);
  }
  async function run(view, call, done) {
    try { await call(); say(view, 'ok', done); } catch (e) {
      say(view, 'error', explain(e));
      if (e instanceof AuthError) return false;
    }
    await load(view);
    return true;
  }
  const row = (id) => list(state.owner.queue && state.owner.queue.data && state.owner.queue.data.suggestions).find((r) => r.suggestion_id === id);

  async function dispatch(a) {
    if (!a) return { ignored: true };
    switch (a.type) {
      case 'reload': return load(a.view);
      case 'attach': {
        if (!TEAM_ID.test(a.team || '')) { say('owner', 'error', 'A team id looks like team- followed by 20 letters and digits (herdr-eng teams list shows it).'); return { invalid: true }; }
        if (!Object.keys(a.roles || {}).length) { say('owner', 'error', 'Choose at least one of your agents and a role for it.'); return { invalid: true }; }
        return run('owner', () => api.attachTeam(roomId, a.team, a.roles), 'Team attached.');
      }
      case 'detach':
        if (!await confirm(CONFIRM.detach())) return { cancelled: true };
        return run('owner', () => api.detachTeam(roomId, a.team), 'Team detached.');
      case 'level': {
        if (!GUEST_LEVELS.some(([v]) => v === a.level)) { say('owner', 'error', 'Choose a guest level.'); return { invalid: true }; }
        if (a.level === 'direct' && !(a.limits && (list(a.limits.agents).length || list(a.limits.roles).length))) {
          say('owner', 'error', 'Direct needs the agents or roles a guest may address. Tick at least one (never “any”).');
          return { invalid: true };
        }
        return run('owner', () => api.setGuestLevel(roomId, a.level, a.level === 'direct' ? a.limits : undefined),
          a.level === 'direct' ? 'Saved. Direct is stored, but it acts as Suggest for now.' : 'Guest level saved.');
      }
      case 'approve': {
        const r = row(a.id);
        if (!r) return { ignored: true };
        if (!a.agent) { say('owner', 'error', 'Choose the agent that should do the work.'); return { invalid: true }; }
        const agent = agentChoices(state.owner).find((x) => x.id === a.agent);
        if (!await confirm(CONFIRM.approve(agent ? agent.label : 'the chosen agent'))) return { cancelled: true };
        const body = { decision: 'approve', expected_digest: r.digest, target_agent_id: a.agent, ...(a.command ? { command: a.command } : {}) };
        return run('owner', () => api.decideSuggestion(roomId, a.id, body), 'Approved. A new task was created for the agent.');
      }
      case 'reject': {
        const r = row(a.id);
        if (!r) return { ignored: true };
        if (!await confirm(CONFIRM.reject())) return { cancelled: true };
        return run('owner', () => api.decideSuggestion(roomId, a.id, { decision: 'reject', expected_digest: r.digest, ...(a.reason ? { reason: a.reason } : {}) }), 'Rejected.');
      }
      case 'assign-role':
        if (!a.person) { say('owner', 'error', 'Choose a person in this room.'); return { invalid: true }; }
        return run('owner', () => api.assignRole(roomId, a.team, a.role, `user:${a.person}`), 'Role assigned.');
      case 'clear-role': return run('owner', () => api.clearRole(roomId, a.team, a.role, a.holder), 'Removed from the role.');
      case 'policy': return run('owner', () => api.setRolePolicy(roomId, a.team, a.policy), 'Saved.');
      case 'visibility': return run('owner', () => api.setMemberVisibility(roomId, a.enabled), a.enabled ? 'Members can now see the agents’ tasks.' : 'Members no longer see the task list.');
      case 'coord-set':
        if (!a.holder) { say('owner', 'error', 'Choose who coordinates this team.'); return { invalid: true }; }
        return run('owner', () => api.setTeamCoordinator(roomId, a.team, a.holder), 'Coordinator designated.');
      case 'coord-clear':
        if (!await confirm(CONFIRM['coord-clear']())) return { cancelled: true };
        return run('owner', () => api.clearTeamCoordinator(roomId, a.team), 'Coordinator removed.');
      case 'ext-on': {
        const roles = list(a.roles);
        if (!roles.length) { say('owner', 'error', 'Tick at least one role they may assign to.'); return { invalid: true }; }
        if (!await confirm(CONFIRM['ext-on'](roles))) return { cancelled: true };
        const limits = { roles };
        if (a.per_hour) limits.tasks_per_hour = Number(a.per_hour);
        if (a.max_open) limits.max_open = Number(a.max_open);
        return run('owner', () => api.setExternalCoordination(roomId, a.team, true, limits), 'Outside coordination is on for this team.');
      }
      case 'ext-off':
        if (!await confirm(CONFIRM['ext-off']())) return { cancelled: true };
        return run('owner', () => api.setExternalCoordination(roomId, a.team, false), 'Outside coordination is off.');
      case 'respond': {
        if (a.status === 'declined' && !await confirm(CONFIRM.decline())) return { cancelled: true };
        const done = { accepted: 'Accepted.', done: 'Marked done.', declined: 'Declined.' }[a.status];
        return run('mine', () => api.respondTask(roomId, a.id, a.status, a.result), done);
      }
      case 'suggest': {
        if (!a.text) { say('requests', 'error', 'Write what you would like the team to look at.'); return { invalid: true }; }
        suggestKey ||= `web-sg-${Date.now().toString(36)}-${randomString(9)}`;     // one key per draft: a double click files one request
        const ok = await run('requests', () => api.suggest(roomId, a.text, suggestKey), 'Filed. It waits for the room owner.');
        if (notice.requests && notice.requests.kind === 'ok') suggestKey = null;
        return ok;
      }
      case 'withdraw':
        if (!await confirm(CONFIRM.withdraw())) return { cancelled: true };
        return run('requests', () => api.withdrawSuggestion(roomId, a.id), 'Withdrawn.');
      default: return { ignored: true };
    }
  }

  const render = (view) => (view === 'owner' ? renderOwner(state.owner, notice.owner)
    : view === 'mine' ? renderMine(state.mine, notice.mine, now()) : renderRequests(state.requests, notice.requests));
  return { state, notice, render, load, dispatch, stale: (view, ms) => now() - loaded[view] > ms, loaded: (view) => loaded[view] > 0 };
}

// ------------------------------------------------------------------ DOM

/**
 * Wires the three bodies of a room page. Nothing is requested until the person opens a section: `sections` maps a view to
 * { details, body } element ids. `put(id, html)` is the page's own repaint (it will not overwrite a field being typed in).
 * Returns { dispose }.
 */
export function mountTeamRoom({ root, api, roomId, put, sections, confirm = (m) => window.confirm(m), refreshMs = 30000, hidden = () => document.hidden }) {
  const tr = createTeamRoom({ api, roomId, confirm, onChange: (view) => paint(view) });
  const pending = new Set();
  const paint = (view) => {
    const s = sections[view];
    if (!s) return;
    if (!put(s.body, tr.render(view)) && root.querySelector(`#${s.body}`)?.contains(document.activeElement)) pending.add(view);     // typing in it: redraw on blur
    // the result of an action is announced even while a field in the section has focus (the page's repaint waits for blur)
    const body = root.querySelector(`#${s.body}`);
    const n = tr.notice[view];
    const say = body && body.querySelector('[data-trr-live]');
    const warn = body && body.querySelector('[data-trr-alert]');
    if (say) say.textContent = n && n.kind !== 'error' ? n.text : '';
    if (warn) warn.textContent = n && n.kind === 'error' ? n.text : '';
  };
  const isOpen = (view) => { const d = sections[view] && root.querySelector(`#${sections[view].details}`); return Boolean(d && d.open); };
  const open = (view) => { if (!tr.loaded(view)) { paint(view); tr.load(view); } };
  const handlers = [];
  for (const [view, s] of Object.entries(sections)) {
    const d = root.querySelector(`#${s.details}`);
    if (!d) continue;
    const on = () => { if (d.open) open(view); };
    d.addEventListener('toggle', on);
    handlers.push(() => d.removeEventListener('toggle', on));
    const body = root.querySelector(`#${s.body}`);
    if (!body) continue;
    const click = (ev) => {
      const b = ev.target.closest && ev.target.closest('[data-trr-act]');
      if (!b || !body.contains(b)) return;
      const d2 = b.dataset;
      const map = {
        reload: { type: 'reload', view: d2.trrView }, detach: { type: 'detach', team: d2.trrTeam }, 'clear-role': { type: 'clear-role', team: d2.trrTeam, role: d2.trrRole, holder: d2.trrHolder },
        'coord-clear': { type: 'coord-clear', team: d2.trrTeam }, 'ext-off': { type: 'ext-off', team: d2.trrTeam },
        respond: { type: 'respond', id: d2.trrId, status: d2.trrStatus }, withdraw: { type: 'withdraw', id: d2.trrId },
      };
      ev.preventDefault();
      tr.dispatch(map[d2.trrAct]);
    };
    const submit = (ev) => {
      const f = ev.target.closest && ev.target.closest('[data-trr-form]');
      if (!f || !body.contains(f)) return;
      ev.preventDefault();
      const fd = new FormData(f, ev.submitter || undefined);
      tr.dispatch(formAction(f.dataset.trrForm, fd, { id: f.dataset.trrId, team: f.dataset.trrTeam }));
    };
    const blur = () => setTimeout(() => { for (const v of [...pending]) { pending.delete(v); paint(v); } }, 0);
    body.addEventListener('focusout', blur);
    body.addEventListener('click', click);
    body.addEventListener('submit', submit);
    handlers.push(() => { body.removeEventListener('click', click); body.removeEventListener('submit', submit); body.removeEventListener('focusout', blur); });
  }
  // keep open sections fresh (deadlines pass, requests arrive) while the page is visible
  const timer = setInterval(() => { if (hidden()) return; for (const view of Object.keys(sections)) if (isOpen(view) && tr.stale(view, refreshMs)) tr.load(view); }, Math.max(5000, refreshMs / 2));
  return { controller: tr, dispose() { clearInterval(timer); handlers.forEach((h) => h()); } };
}
